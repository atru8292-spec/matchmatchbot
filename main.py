"""FastAPI-сервер бота Anna.

Входящая труба (без AI): вебхук Wazzup → normalize → insert в БД → debounce → on_flush.
on_flush пока просто читает склеенный залп из БД и логирует (AI встанет сюда в блоке 6).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

import actions
import ai
import ai_conversation
import booking
import db
import escalation
import filters
import funnel
import manager_bot
import mini_api
import scheduler
import sender
import vision
import voice
from config import settings
from debounce import Debouncer
from normalize import normalize_wazzup_message, parse_echo
from phones import canonical_phone

# Логи в stdout → journald (systemd). Помечаем время/уровень/модуль.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx на INFO пишет полный URL запроса — у Telegram в нём токен бота. В журнал не пускаем.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("matchmatch")

# Подтверждение при opt-out (лид попросил не писать). Одно тёплое сообщение, дальше тишина.
_OPTOUT_CONFIRM = "Listo, ya no te voy a escribir más. Te deseo lo mejor 🤍"


def _parse_dob(s: str):
    """Строка даты рождения от AI → date. ISO (AAAA-MM-DD) + пара частых форматов. None если не смог."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# Debouncer создаётся в lifespan (склейка серии быстрых сообщений одного лида).
debouncer: Debouncer | None = None
# Таск планировщика (блок 13): фоллоу-апы + напоминания об ивенте. Отменяется в shutdown.
scheduler_task = None


# Ожидание перед ответом (решение владелицы 2026-10-23): скользящее окно тишины после
# последнего сообщения лида и потолок от первого сообщения серии. Последнее — фото: ждём дольше
# (вдруг пришлёт ещё), потом все фото пачки разбираются вместе и ответ один.
REPLY_DEBOUNCE_KEY = "reply_debounce_seconds"
PHOTO_DEBOUNCE_KEY = "photo_debounce_seconds"
REPLY_DEBOUNCE_DEFAULT = 60.0
PHOTO_DEBOUNCE_DEFAULT = 90.0
REPLY_MAX_WAIT = 180.0
PHOTO_MAX_WAIT = 240.0

# Одна обработка на лида одновременно: вторая ждёт (кроме debounce-сериализации — на случай
# других путей запуска разбора).
_burst_locks: dict[str, asyncio.Lock] = {}


async def _debounce_window(content_type: str | None) -> tuple[float, float]:
    """(окно тишины, потолок) для входящего: фото — photo_debounce_seconds/240,
    остальное — reply_debounce_seconds/180. Нет ключа, мусор или сбой — по умолчанию."""
    photo = content_type == "photo"
    key = PHOTO_DEBOUNCE_KEY if photo else REPLY_DEBOUNCE_KEY
    default = PHOTO_DEBOUNCE_DEFAULT if photo else REPLY_DEBOUNCE_DEFAULT
    cap = PHOTO_MAX_WAIT if photo else REPLY_MAX_WAIT
    try:
        raw = (await db.get_settings([key])).get(key)
        value = float(raw) if raw not in (None, "") else default
    except Exception:
        value = default
    if value <= 0:
        value = default
    return min(value, cap), cap


async def _process_burst(phone: str) -> None:
    """on_flush для debounce. Обёртка: любое непойманное исключение → лог + алерт,
    чтобы сбой не ушёл в debounce молча (debounce намеренно не зависит от escalation).
    Пока идёт разбор, новое сообщение лида прерывает отправку устаревшего ответа (sender
    проверяет перед каждым пузырём); его разберёт следующий проход вместе с историей."""
    lock = _burst_locks.setdefault(phone, asyncio.Lock())
    async with lock:
        sender.set_interrupt_check(
            (lambda: debouncer.has_new(phone)) if debouncer is not None else None)
        try:
            await _process_burst_impl(phone)
        except Exception as e:
            logger.exception("_process_burst упал [%s]", phone)
            await escalation.notify_error("main._process_burst", repr(e), phone)


# Защита от старых входящих: после простоя канала Wazzup может дослать сообщения
# недельной давности — отвечать на них «сейчас» нельзя (Аня уже ответила лично или
# человек давно ушёл). Порог в app_settings, 0 = выключено. Bypass-номера не трогаем.
STALE_INBOUND_KEY = "stale_inbound_hours"
STALE_INBOUND_DEFAULT_HOURS = 12


def _parse_dt(value) -> datetime | None:
    """ISO-время из Wazzup ('2026-10-15T12:00:00.000Z') или datetime из БД → aware UTC.
    Нераспознанное → None (вызывающий берёт запасное время)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _message_time(m: dict) -> datetime | None:
    """Время сообщения: dateTime от Wazzup (meta.received_at), иначе момент записи в БД."""
    return _parse_dt((m.get("meta") or {}).get("received_at")) or _parse_dt(m.get("created_at"))


async def _stale_inbound_hours() -> float:
    """Порог «старого» входящего в часах. Нет ключа/мусор → 12, сбой чтения → 12."""
    try:
        raw = (await db.get_settings([STALE_INBOUND_KEY])).get(STALE_INBOUND_KEY)
    except Exception:
        logger.exception("не прочитала %s — беру %s ч", STALE_INBOUND_KEY, STALE_INBOUND_DEFAULT_HOURS)
        return float(STALE_INBOUND_DEFAULT_HOURS)
    try:
        hours = float(raw) if raw not in (None, "") else float(STALE_INBOUND_DEFAULT_HOURS)
    except ValueError:
        return float(STALE_INBOUND_DEFAULT_HOURS)
    # Отрицательное — опечатка, а не «выключить» (выключает только явный 0).
    return hours if hours >= 0 else float(STALE_INBOUND_DEFAULT_HOURS)


def _split_stale(msgs: list[dict], hours: float, now: datetime) -> tuple[list[dict], list[dict]]:
    """(свежие, старые). hours<=0 → всё свежее. Без времени — считаем свежим (не теряем)."""
    if hours <= 0:
        return msgs, []
    border = now - timedelta(hours=hours)
    fresh, stale = [], []
    for m in msgs:
        t = _message_time(m)
        (stale if t is not None and t < border else fresh).append(m)
    return fresh, stale


async def _process_burst_impl(phone: str) -> None:
    """Собрать залп непроцессенных inbound, принять решение (filters.decide) и исполнить.

    Читаем склеенный залп из БД (источник истины — не память), помечаем processed,
    затем filters.decide → _apply_decision (silent/blocked/payment/AI) или фото-ветка.
    """
    msgs = await db.get_unprocessed_inbound(phone)
    if not msgs:
        # уже обработано параллельным флашем — нечего делать
        return

    # bypass = ENV (silent_bypass_phones, требует деплоя) ∪ тестовые номера из мини-CRM
    # (app_settings, правятся мгновенно без деплоя), всё по канону номера.
    try:
        bypass_phones = await db.bypass_phone_set()
    except Exception:
        bypass_phones = settings.silent_bypass_set

    # Старые входящие (дослал Wazzup после простоя) — сохраняем, но НЕ отвечаем.
    # Тестовые bypass-номера правило обходят (иначе мешало бы тестам с подменой времени).
    if canonical_phone(phone) not in bypass_phones:
        msgs, stale = _split_stale(msgs, await _stale_inbound_hours(), datetime.now(timezone.utc))
        if stale:
            await db.mark_messages_processed([m["id"] for m in stale])
            # Просьбу «не пиши» чтим и в старом сообщении: молча ставим do_not_contact
            # (без ответа лиду), иначе после запуска его догонят/ответят.
            stale_text = "\n".join((m.get("text") or "") for m in stale)
            # Просьба позвонить приоритетнее opt-out и в старом: не баним, ручной режим +
            # алерт Ане (лиду не пишем). Whitelist и do_not_contact — молча, как в decide.
            # Сбой здесь не должен ронять залп: свежие сообщения ниже ещё не обработаны.
            try:
                if filters.is_call_request(stale_text):
                    stale_lead = await db.get_lead_by_phone(phone) or {}
                    if not stale_lead.get("do_not_contact") and not await db.is_whitelisted(phone):
                        await _hand_off_call_request(phone, stale_lead, stale_text)
                        logger.warning("старые входящие [%s]: просит позвонить → manual + алерт", phone)
                elif filters.is_optout(stale_text):
                    await db.block_lead(phone, "opt-out (старое входящее после простоя)")
                    logger.warning("старые входящие [%s]: opt-out → do_not_contact без ответа", phone)
            except Exception as e:
                logger.exception("старые входящие [%s]: сбой обработки opt-out/звонка", phone)
                await escalation.notify_error("старые входящие: opt-out/звонок", repr(e), phone)
            oldest = min(t for t in (_message_time(m) for m in stale) if t)
            logger.warning("старые входящие [%s]: %d сообщ. (самое старое %s) — без ответа бота",
                           phone, len(stale), oldest.isoformat())
        if not msgs:
            return
    content_types = [(m.get("meta") or {}).get("content_type") for m in msgs]
    lead = await db.get_lead_by_phone(phone) or {}
    whitelisted = await db.is_whitelisted(phone)
    # personal_contact (личная база Anna) — молчим без алерта; VIP-клиент — с алертом.
    wl_no_alert = await db.whitelist_no_alert(phone) if whitelisted else False

    # Голосовые: реальная транскрибация Whisper заменяет плейсхолдер '[voice message]'
    # на распознанный текст (кроме whitelist — там бот молчит, транскрибация не нужна).
    if not whitelisted and "voice" in content_types:
        await _transcribe_voices(phone, msgs)

    combined = "\n".join((m.get("text") or "") for m in msgs)
    logger.info(
        "склеенный залп от %s (%d сообщ., типы: %s): %r",
        phone, len(msgs), content_types, combined,
    )
    await db.mark_messages_processed([m["id"] for m in msgs])

    # Глобальная пауза (тумблер в CRM): бот молчит всем, кроме bypass-номеров (тех. режим).
    # Сбой чтения настройки → НЕ паузим (безопасный дефолт: бот продолжает работать).
    try:
        bot_paused = (await db.get_settings(["bot_paused"])).get("bot_paused") == "1"
    except Exception:
        bot_paused = False

    decision = filters.decide(lead, whitelisted, combined, phone,
                              bypass_phones, whitelist_no_alert=wl_no_alert,
                              bot_paused=bot_paused)

    # 1) Любое детерминированное решение приоритетнее фото: whitelist/silent (молчим),
    #    blocked (escort/агрессия), а также opt-out, английский, отказ по фильтрам,
    #    заявление об оплате (фото = чек, не анкетное фото) и Instagram. ИСПРАВЛЕНО
    #    2026-10-15 (аудит): раньше фото в залпе уводило opt-out/английский/оплату в Vision+AI.
    if decision.action != "needs_ai":
        await _apply_decision(phone, decision, lead, combined)
        return

    # 2) фото в залпе → фото-ветка (Vision). ИСПРАВЛЕНО (2026-08-12): раньше брали
    #    ТОЛЬКО ПОСЛЕДНЕЕ фото пачки — если лид присылал 2-3 фото разом, остальные
    #    молча терялись без анализа (реальный кейс: 3 фото за секунду → в БД попало
    #    только 1). Теперь анализируем КАЖДОЕ фото с content_uri.
    photo_msgs = [m for m in msgs if (m.get("meta") or {}).get("content_type") == "photo"]
    if photo_msgs:
        content_uris = [m["meta"]["content_uri"] for m in photo_msgs
                        if (m.get("meta") or {}).get("content_uri")]
        if not content_uris:
            logger.warning("фото без content_uri [%s]", phone)
            await escalation.notify_error("photo", "нет content_uri в meta", phone)
            return
        # Текст, сопровождающий фото в этом же залпе — ИСПРАВЛЕНО (2026-09-01, живой
        # тест): подпись к фото ("soy soltero, 35 años" вместе с картинкой) раньше
        # терялась без следа — combined (выше) её тоже не спасает, потому что фото-ветка
        # уходит в return до использования combined. Берём caption из meta (photo) +
        # реальный текст остальных сообщений пачки (не placeholder).
        accompanying_text = "\n".join(
            (m.get("meta") or {}).get("caption") or ""
            if (m.get("meta") or {}).get("content_type") == "photo"
            else (m.get("text") or "")
            for m in msgs
        ).strip()
        # Подпись к фото проходит те же детерминированные фильтры, что и текст (спутник,
        # opt-out, звонок, escort, агрессия), и темы «молча Ане» (оплата, скидка, возврат)
        # — до Vision и AI, в обоих режимах (ревью 2026-10-22).
        if accompanying_text:
            cap = filters.decide(lead, whitelisted, accompanying_text, phone, bypass_phones,
                                 whitelist_no_alert=wl_no_alert, bot_paused=bot_paused)
            if cap.action != "needs_ai":
                logger.info("подпись к фото [%s]: %s — фото не обрабатываю", phone, cap.action)
                await _apply_decision(phone, cap, lead, accompanying_text)
                return
            if ai_conversation._SILENT_TOPIC_RE.search(accompanying_text):
                logger.info("подпись к фото [%s]: оплата/скидка/возврат — Аня", phone)
                try:
                    await db.update_lead_fields(phone, mode="manual")
                except Exception:
                    logger.exception("не смог поставить manual (подпись к фото) [%s]", phone)
                await escalation.notify_escalation(
                    lead, "Фото с вопросом про оплату, скидку или возврат — ответь сама",
                    accompanying_text)
                return
        # Обёртка: сообщения уже processed — сбой фото-обработки не должен быть тихим.
        try:
            await _process_photos(phone, lead, content_uris, accompanying_text or None)
        except Exception as e:
            logger.exception("_process_photos упал [%s] — фото уже processed", phone)
            await escalation.notify_error("main._process_photo", repr(e), phone)
        return

    # 3) текст → детерминированное решение (needs_ai/rejected).
    await _apply_decision(phone, decision, lead, combined)


async def _transcribe_voices(phone: str, msgs: list[dict]) -> None:
    """Заменить плейсхолдер голосовых на реальный транскрипт Whisper (мутирует msgs).

    На сбой скачивания/транскрибации: лог + алерт, плейсхолдер '[voice message]'
    остаётся — downstream уйдёт в сценарий №35 («me lo escribes?»), лид без ответа
    не остаётся. Пустой транскрипт (тишина/шум) тоже оставляем как №35.
    """
    for m in msgs:
        meta = m.get("meta") or {}
        if meta.get("content_type") != "voice":
            continue
        content_uri = meta.get("content_uri")
        if not content_uri:
            logger.warning("голосовое без content_uri [%s] — плейсхолдер (№35)", phone)
            continue
        try:
            audio = await vision.download_media(content_uri)
            text = await voice.transcribe(audio)
        except Exception as e:
            logger.exception("транскрибация голосового упала [%s] — плейсхолдер (№35)", phone)
            await escalation.notify_error("main._transcribe_voices", repr(e), phone)
            continue
        if not text:
            logger.info("пустой транскрипт [%s] — плейсхолдер (№35)", phone)
            continue
        m["text"] = text
        await db.update_message_text(m["id"], text)  # чинит историю (не критично)
        logger.info("голосовое [%s] транскрибировано: %r", phone, text[:80])


_VERDICT_PRIORITY = ("reject", "ok", "manual", "retry")


async def _process_photos(phone: str, lead: dict, content_uris: list[str],
                          accompanying_text: str | None = None) -> None:
    """Фото-ветка: флуд → скачать+Vision КАЖДОЕ фото пачки → сохранить каждое →
    ОДНО итоговое действие по приоритету вердиктов.

    accompanying_text — подпись к фото / текст, отправленный вместе с ним в этом же
    залпе (напр. "soy soltero, 35 años"). При verdict=='ok' идёт ПЕРЕД маркером
    "[фото одобрено]" в user_text для _run_ai — так AI реально видит и извлекает
    анкетные данные, а не только синтетический маркер (регресс найден 2026-09-01).

    ИСПРАВЛЕНО (2026-08-12): раньше обрабатывалось только последнее фото пачки —
    если лид присылал 2-3 фото разом, остальные молча терялись без анализа/сохранения
    (реальный кейс: 3 фото за секунду → в lead_photos попало только 1). Теперь каждое
    фото реально скачивается, идёт в Vision и сохраняется (db.save_photo).

    Приоритет итогового действия: reject (любое неприемлемое — блок сразу, не важно
    что в остальных) > ok (хотя бы одно нормальное — этого достаточно, лид
    квалифицирован) > manual (есть спорное, но нет явного reject/ok) > retry
    (все плохого качества — просим переслать)."""
    # Флуд-защита один раз на всю пачку (не за каждое фото) — >5 фото/час → ручная
    # проверка, без Vision (экономия токенов).
    try:
        if await db.count_recent_photos(phone) > 5:
            logger.warning("photo flood [%s] — mode=manual, без Vision", phone)
            await db.update_lead_fields(phone, mode="manual")
            await escalation.notify_escalation(lead, "Много фото подряд — проверь вручную", "[фото]")
            return
    except Exception:
        logger.exception("count_recent_photos упал [%s], продолжаю", phone)

    results: list[dict] = []
    for content_uri in content_uris:
        # Скачивание медиа. Сбой ОДНОГО фото → technical-алерт, но остальные фото
        # пачки всё равно обрабатываем (не роняем всю пачку из-за одного сбоя).
        try:
            img = await vision.download_media(content_uri)
        except Exception as e:
            logger.exception("download_media упал [%s]", phone)
            await escalation.notify_error("vision.download_media", repr(e), phone)
            continue

        res = await vision.analyze_photo(img)         # ошибка внутри → manual-фолбэк
        verdict = res["verdict"]
        url, path = await vision.upload_to_storage(phone, img)  # сбой → (None, None)
        await db.save_photo(phone, url, path, verdict, analysis=res,
                            reasons=[res.get("reason", "")] if res.get("reason") else [])
        logger.info("фото [%s]: verdict=%s (%s)", phone, verdict, res.get("reason", ""))
        results.append(res)

    if not results:
        # Скачивание упало у всех фото пачки — алерты уже ушли по каждому, лид без
        # ответа (как и раньше при одиночном сбое download).
        return

    by_verdict = {r["verdict"]: r for r in results}
    verdict = next(v for v in _VERDICT_PRIORITY if v in by_verdict)
    res = by_verdict[verdict]

    if verdict == "ok":
        # Хотя бы одно фото одобрено → лид квалифицирован, AI переходит к питчу.
        await db.mark_photo_received(phone, True)
        await db.set_funnel_stage(phone, "qualified", meta={"photo": "ok"})
        # lead прочитан из БД до проверки фото — флаг в словаре тоже ставим, иначе режим rag
        # считает фото не присланным и не даёт ссылку на ивент (code-review 2026-10-22)
        lead = {**lead, "photo_received": True}
        user_text = (f"{accompanying_text}\n\n[фото одобрено]" if accompanying_text
                     else "[фото одобрено]")
        await _run_ai(phone, lead, user_text)
    elif verdict == "retry":
        # ИСПРАВЛЕНО (2026-08-10, реальный баг на живых тестах — Аня/Мила репортили
        # "опять просит фото"): было #5 — это НЕ ретрай, а generic "mándame tu foto"
        # (сценарий, до которого AI доходит после квалификации). Лид, чьё фото не
        # прошло Vision, получал ДОСЛОВНО тот же текст, что и до отправки фото —
        # выглядело так, будто бот вообще не заметил присланное фото. Правильный
        # сценарий для retry — #11 ("Me mandas otra foto donde estés tú solito...").
        await _send_scenario(phone, 11)
    elif verdict == "reject":
        # Неприемлемое (обнажёнка) → блок навсегда + прощание (сценарий 12) + алерт.
        title = await db.get_scenario_title(12)
        await db.block_lead(phone, f"Vision: {title or 'фото неприемлемо'}")
        await _send_scenario(phone, 12)
        await escalation.notify_block(lead, "неприемлемое фото")
    else:  # manual
        # Пограничное → бот молчит, Аня решает по фото кнопками (блок 11).
        await db.update_lead_fields(phone, mode="manual")
        await escalation.notify_photo_review(lead, res.get("reason", "[фото]"))


async def _send_scenario(phone: str, scenario_id: int) -> None:
    """Отправить лиду текст сценария (детерминированно, бабблами по \\n\\n)."""
    tmpl = await db.get_scenario_template(scenario_id)
    if not tmpl:
        logger.warning("нет template сценария %s для %s", scenario_id, phone)
        return
    bubbles = [p.strip() for p in tmpl.split("\n\n") if p.strip()]
    await sender.send(phone, bubbles)


async def _hand_off_call_request(phone: str, lead: dict, text: str) -> None:
    """Лид просит позвонить: ручной режим + бизнес-алерт Ане. Лиду ничего не отправляем.
    Сбой set_manual → техалерт, а в алерте Ане предупреждение (иначе бот ответит лиду)."""
    manual_ok = True
    try:
        await db.set_manual(phone)
    except Exception as e:
        manual_ok = False
        logger.exception("call_request: не смог поставить manual для %s", phone)
        await escalation.notify_error("call_request: set_manual", repr(e), phone)
    await escalation.notify_call_request({**(lead or {}), "phone": (lead or {}).get("phone") or phone},
                                         text, manual_ok=manual_ok)


async def _hand_off_companion_question(phone: str, lead: dict, text: str) -> None:
    """Вопрос про спутника: ручной режим + алерт Ане, лиду ничего (как просьба позвонить)."""
    # set_manual возвращает False, если строки лида нет: тогда ручной режим не включён.
    try:
        manual_ok = bool(await db.set_manual(phone))
    except Exception as e:
        manual_ok = False
        logger.exception("companion_question: не смог поставить manual для %s", phone)
        await escalation.notify_error("companion_question: set_manual", repr(e), phone)
    await escalation.notify_companion_question(
        {**(lead or {}), "phone": (lead or {}).get("phone") or phone}, text, manual_ok=manual_ok)


async def _apply_decision(phone: str, decision: "filters.Decision", lead: dict,
                          combined: str) -> None:
    """Выполнить решение filters.decide. needs_ai → реальный вызов AI (ai.py)."""
    name = lead.get("whatsapp_name") or lead.get("name") or phone

    if decision.action == "silent_whitelist":
        # Бот молчит; сообщение уже в истории. Алерт «написал клиент/VIP» — только если
        # decision.alert_manager (VIP). personal_contact (личная база Anna) — без алерта.
        logger.info("РЕШЕНИЕ silent_whitelist [%s]: %s (alert=%s)",
                    phone, decision.reason, decision.alert_manager)
        if decision.alert_manager:
            await escalation.notify_vip(lead)
        return

    if decision.action == "silent":
        # Не целевой регион/язык. Молчим, НЕ блокируем, стадию не трогаем.
        # Сообщение уже сохранено в messages (история). AI не вызываем — экономия токенов.
        logger.info("РЕШЕНИЕ silent [%s]: %s", phone, decision.reason)
        return

    if decision.action == "silent_language":
        # Лид пишет по-английски — бот молчит (не отвечает на английском), сразу алертит
        # Аню/Милу, чтобы ответили лично (решение владелицы 2026-09-15). AI не вызываем.
        logger.info("РЕШЕНИЕ silent_language [%s]: %s", phone, decision.reason)
        await escalation.notify_english_lead(lead, combined)
        return

    if decision.action == "call_request":
        # Лид просит позвонить вместо переписки («no me escribas, mejor llámame»). Это НЕ
        # opt-out: do_not_contact не ставим. Ручной режим (как takeover/эхо Ани) — бот ему
        # больше не отвечает и догоны не уходят (due_followups берёт только mode='auto');
        # Аня получает алерт и звонит. Лиду бот ничего не пишет.
        logger.info("РЕШЕНИЕ call_request [%s]: ручной режим + алерт Ане", phone)
        await _hand_off_call_request(phone, lead, combined)
        return

    if decision.action == "companion_question":
        # Друг, девушка, пара, группа, цена на двоих (решение владелицы 2026-10-21): бот это
        # не обсуждает и 5,500 не называет. Ручной режим + алерт Ане, лиду ничего, AI не зовём.
        logger.info("РЕШЕНИЕ companion_question [%s]: ручной режим + алерт Ане", phone)
        await _hand_off_companion_question(phone, lead, combined)
        return

    if decision.action == "optout":
        # Лид явно попросил не писать. Одно тёплое подтверждение + do_not_contact навсегда
        # (block_lead: флаг + стадия lost + гасит next_followup_at). Дальше — полная тишина
        # на всех путях (follow-up/scheduled/ручная отправка уважают do_not_contact).
        logger.info("РЕШЕНИЕ optout [%s]%s", phone, " (тихо: пауза/ручной режим)" if decision.quiet else "")
        if not decision.quiet:
            await sender.send(phone, [_OPTOUT_CONFIRM])
        await db.block_lead(phone, "opt-out: лид попросил не писать")
        await escalation.notify_optout(lead)
        return

    if decision.action == "payment_claim":
        # Лид сообщил об оплате (блок 13). Бот НЕ меняет стадию сам — короткий ack +
        # эскалация Ане с кнопкой «Подтвердить оплату». Реальное действие — confirm_payment.
        logger.info("РЕШЕНИЕ payment_claim [%s]", phone)
        await sender.send(phone, ["¡Genial! 🤍 Ahora lo confirmo y te escribo"])
        await escalation.notify_payment(lead)
        return

    if decision.action == "instagram_handoff":
        # Лид дал Instagram вместо фото: бот не валидирует профиль как фото (Vision).
        # Короткий бридж-ответ, ставим manual (дальше ведёт Аня), алерт на ручную проверку.
        logger.info("РЕШЕНИЕ instagram_handoff [%s]", phone)
        await sender.send(phone, ["Déjame revisarlo y te confirmo en un momento 🤍"])
        try:
            await db.update_lead_fields(phone, mode="manual")
        except Exception:
            logger.exception("не смог поставить manual для %s", phone)
        await escalation.notify_escalation(
            lead, "Прислал Instagram вместо фото — проверь профиль вручную", combined)
        return

    if decision.action == "blocked":
        # Блок навсегда + стадия lost — атомарно внутри block_lead. is_escort из
        # Decision (не парсим текст reason). Алерт Ане о блокировке.
        await db.block_lead(phone, decision.reason, escort=decision.is_escort)
        logger.info("РЕШЕНИЕ blocked [%s]: %s", phone, decision.reason)
        await escalation.notify_block(lead, decision.reason)
        return

    # rejected и needs_ai → AI-ядро. Оборачиваем в try/except: сообщения залпа уже
    # помечены processed до этой точки, поэтому падение AI/БД здесь = тихая потеря
    # ответа (повтора не будет). Ловим, логируем; алерт Ане навесит блок 8.
    if decision.action in ("rejected", "needs_ai"):
        try:
            if decision.action == "rejected":
                # не прошёл жёсткий фильтр по известным полям; текст отказа сформирует AI
                await db.set_funnel_stage(phone, "rejected", meta={"reason": decision.reason})
                logger.info("РЕШЕНИЕ rejected [%s]: %s", phone, decision.reason)
            await _run_ai(phone, lead, combined)
        except Exception as e:
            logger.exception(
                "обработка AI упала [%s] — лид без ответа, сообщения уже processed", phone,
            )
            await escalation.notify_error("main._run_ai", repr(e), phone)
        return

    logger.warning("неизвестное решение %s [%s]", decision.action, phone)


async def _run_ai(phone: str, lead: dict, combined: str) -> None:
    """Сгенерировать и применить ответ AI: extracted → стадия → action → отправка."""
    # 20 сообщений — контекст с запасом (диалоги короткие: debounce склеивает залпы),
    # экономит токены/запрос против 30 без потери качества.
    history = await db.get_conversation_history(phone, 20)
    result = await ai.generate_reply(lead, history, combined)
    try:
        await _apply_ai_result(phone, lead, combined, result)
    except Exception:
        # Режим rag (и тестовый лид в rag_test): сбой после генерации — лид не должен остаться
        # без человека: ручной режим и алерт Ане, техалерт — выше по стеку (ревью 2026-10-22).
        if result.get("rag_meta") is not None:
            try:
                await db.update_lead_fields(phone, mode="manual")
            except Exception:
                logger.exception("rag: не смог поставить manual после сбоя [%s]", phone)
            try:
                await escalation.notify_bot_silent(
                    lead, combined, "Не получилось отправить ответ, проверь переписку и ответь сам")
            except Exception:
                logger.exception("rag: алерт Ане после сбоя не ушёл [%s]", phone)
        raise


async def _apply_ai_result(phone: str, lead: dict, combined: str, result: dict) -> None:
    """Применить ответ AI: extracted → стадия → action → отправка."""
    # Режим rag: состояние разговора (уход от темы подряд, возвраты). Колонка из миграции 008;
    # без неё — только лог, ответ лиду не страдает.
    if result.get("rag_estado") is not None:
        try:
            await db.update_rag_state(phone, result["rag_estado"])
        except Exception:
            logger.warning("rag_estado не сохранён [%s] (миграция 008 применена?)", phone)

    # 1. Извлечённые поля лида — уже провалидированы в ai (whitelist LEAD_COLUMNS).
    #    date_of_birth AI отдаёт строкой ISO → конвертируем в date для типизированной колонки.
    if result["extracted"]:
        extracted = dict(result["extracted"])
        if isinstance(extracted.get("date_of_birth"), str):
            dob = _parse_dob(extracted["date_of_birth"])
            if dob is not None:
                extracted["date_of_birth"] = dob
            else:
                extracted.pop("date_of_birth", None)  # не распарсили — пропускаем, AI переспросит
        # ИСПРАВЛЕНО (2026-08-06, аудит): selected_service нигде не писался — AI кладёт
        # интерес в extracted['interest'] (event/agency/both), а event_recipients/
        # event_lead_candidates (напоминания об ивенте) фильтруют строго по
        # selected_service='event'. Без этой синхронизации автонапоминания (T-1, day-of)
        # находили НОЛЬ получателей всегда, даже после фикса дедупа в event_recipients.
        # 'agency'-интерес НЕ трогаем: подтверждение оплаты там и так спрашивает Аню
        # кнопками при неоднозначном selected_service (см. actions.stage_for_service).
        if extracted.get("interest") in ("event", "both"):
            extracted["selected_service"] = "event"
        try:
            await db.update_lead_fields(phone, **extracted)
        except Exception:
            logger.exception("не смог обновить extracted для %s: %s", phone, extracted)
        # Анкета собрана целиком → пишем в Google Sheet (дедуп внутри; сбой не роняет ответ).
        try:
            await actions.save_anketa_if_complete(phone)
        except Exception:
            logger.exception("save_anketa_if_complete упал [%s]", phone)

    # #53 автозапись видеозвонка: AI распарсил конкретный день+час → бронируем сами
    # (Google-событие + Meet + подтверждение лиду), без участия Ани. Обрабатываем ДО
    # обычной ветки action, т.к. сообщение лиду формируется детерминированно по исходу.
    #
    # action == "respond": защита в глубину (найдено 2026-09-23, аудит логики) — si
    # algún guardrail de ai.py alguna vez pone action='block'/'escalate' (ej. edad
    # fuera de 28-76) SIN limpiar proposed_videocall_at (bug real encontrado en
    # _enforce_age_block, corregido aparte), esta condición extra evita reservar una
    # llamada real de todos modos. Nunca debería agendarse una llamada si el lead
    # no va a recibir un "respond" normal.
    if result.get("proposed_videocall_at") and result.get("action") == "respond":
        await _handle_videocall_booking(phone, lead, combined, result["proposed_videocall_at"])
        return

    action = result["action"]
    messages = result["messages"]

    if action == "block":
        # AI-блок через fixed-сценарий. reason из сценария (за что), без escort-инкремента
        # (escort-счётчик ведёт filters). block_lead сам ставит стадию 'lost' в транзакции.
        # Порядок: сначала block (гарантирован), потом прощальное сообщение.
        title = await db.get_scenario_title(result["used_scenario_id"])
        reason = f"AI: {title}" if title else "AI-блок по сценарию"
        await db.block_lead(phone, reason)
        await sender.send(phone, messages,  # прощальное сообщение лиду
                          allow_repeat_links=sender._is_link_request(combined))
        logger.info("AI block [%s]: %s (scenario=%s)", phone, reason, result["used_scenario_id"])
        await escalation.notify_block(lead, title or "заблокирован по сценарию")
        return

    if action == "silent":
        # to_anna_silent (похоже на клиента агентства/известный контакт): бот НЕ пишет
        # лиду НИЧЕГО (messages уже гарантированно [] — ai._validate_output/_fixed_reply).
        # mode=manual — бот больше не отвечает сам, Аня ведёт лично; escalation — чтобы
        # она узнала и подтвердила/поправила при ошибке классификации.
        try:
            await db.update_lead_fields(phone, mode="manual")
        except Exception:
            logger.exception("не смог поставить manual (silent) для %s", phone)
        title = await db.get_scenario_title(result["used_scenario_id"])
        logger.info("AI silent [%s] (scenario=%s) — без ответа лиду", phone, result["used_scenario_id"])
        if result.get("rag_meta") is not None:
            # причина в журнал — чтобы разбирать молчание без Telegram
            logger.info("rag silent [%s]: %s | %s", phone, result.get("silent_reason"),
                        (result.get("silent_detail") or "")[:200])
            # Новый режим: понятный алерт «бот промолчал» (причина одной фразой).
            await escalation.notify_bot_silent(
                lead, combined, ai_conversation.silent_alert(result.get("silent_reason")),
                result.get("silent_detail"))
            return
        await escalation.notify_escalation(
            lead, result.get("silent_reason") or title
            or "Похоже на клиента агентства — бот промолчал", combined)
        return

    # Режим rag думает дольше (2–4 вызова OpenAI): если за это время Аня взяла лида вручную
    # (эхо её ответа поставило manual), ответ бота поверх её сообщения не шлём (code-review
    # 2026-10-22). Сбой чтения — шлём как обычно.
    if result.get("rag_meta") is not None and lead.get("mode") != "manual":
        try:
            fresh = await db.get_lead_by_phone(phone)
        except Exception:
            logger.exception("rag: не перечитала лида перед отправкой [%s]", phone)
            fresh = None
        if fresh and fresh.get("mode") == "manual":
            logger.info("rag [%s]: пока бот думал, лид ушёл в ручной режим — ответ не шлю", phone)
            return

    # respond / escalate — стадию ставит AI (если вернул валидную).
    if result["funnel_stage"]:
        await db.set_funnel_stage(phone, result["funnel_stage"],
                                  meta={"scenario_id": result["used_scenario_id"]})

    sent = await sender.send(phone, messages,  # messages лиду
                             allow_repeat_links=sender._is_link_request(combined))
    if sender.interrupted():
        # Лид написал ещё раз, пока бот отвечал: остаток ответа и медиа не шлём — новое
        # сообщение разберёт следующий проход с полной историей (уже ушедшее в ней есть).
        logger.info("ответ прерван новым сообщением [%s]: ушло %d из %d, медиа не шлю",
                    phone, sent, len(messages))
        return
    if sent == 0:
        # ИСПРАВЛЕНО (2026-08-06, аудит): если render_bubbles дропнул ВСЕ бабблы (напр.
        # AI вписал [course_link]/[event_link] внутрь фразы вместо отдельного сообщения,
        # а ссылка пуста/уже отправлена — весь баббл дропается целиком, sender.py:154),
        # лид получал полную тишину на своё сообщение — молча, без единого алерта.
        logger.warning("sender.send отправил 0/%d лиду %s — тишина без алерта была багом",
                       len(messages), phone)
        await escalation.notify_error(
            "main._run_ai", f"0 бабблов ушло лиду (были: {messages!r})", phone)
        if result.get("rag_meta") is not None:
            # Режим rag: лид не должен остаться без ответа и без человека (инвариант молчания).
            try:
                await db.update_lead_fields(phone, mode="manual")
            except Exception:
                logger.exception("не смог поставить manual (rag, 0 бабблов) [%s]", phone)
            await escalation.notify_bot_silent(lead, combined, ai_conversation.FAILURE)
    # Антибан-догон: СБРАСЫВАЕМ таймер тишины на любом ответе лида (он активен → не нудим),
    # а не только когда таймер пуст. Так догон уходит лишь после реального молчания N дней
    # (пробел (а): раньше активный лид в той же стадии всё равно получал догон).
    stage_now = result["funnel_stage"] or lead.get("funnel_stage")
    hours = funnel.FOLLOWUP_FIRST_DELAY_HOURS.get(stage_now)
    if hours:
        await db.reset_followup_timer(phone, hours)
    # Блок 13: лид спросил детали/локацию ивента (AI выставил send_invitation) →
    # шлём картинку-приглашение, если Аня отметила её готовой (иначе тихо пропустим).
    # Обёрнуто: сбой отправки картинки не должен ронять уже успешный ответ лиду.
    if result.get("send_invitation"):
        try:
            await actions.maybe_send_invitation(phone)
        except Exception:
            logger.exception("maybe_send_invitation упал [%s] (ответ лиду уже отправлен)", phone)
    # Режим rag: видео раньше фото, а вопрос (after_media) — последним сообщением после медиа
    # (решение владелицы 2026-10-22, ход после одобренного фото).
    if result.get("rag_meta") is not None:
        await _send_rag_media_and_tail(phone, result)
        result = {**result, "send_event_photo": False, "send_event_video": False}
    # Медиа прошлых ивентов — два независимых инструмента AI (фото / видео).
    # Отдельными сообщениями ПОСЛЕ текста; пер-типовой дедуп внутри; сбой не роняет ответ.
    if result.get("send_event_photo"):
        try:
            # photo_caption (ai.py _maybe_announce_event_photo) — подпись К фото;
            # sender.py шлёт её отдельным сообщением перед медиа (Wazzup не поддерживает
            # text+contentUri в одном запросе, см. sender._send_content_uri).
            await actions.send_event_photos(phone, caption=result.get("photo_caption"))
        except Exception:
            logger.exception("send_event_photos упал [%s] (ответ лиду уже отправлен)", phone)
    if result.get("send_event_video"):
        try:
            # video_caption (ai.py _maybe_announce_event_video) — подпись К видео;
            # sender.py шлёт её отдельным сообщением перед медиа (Wazzup не поддерживает
            # text+contentUri в одном запросе, см. sender._send_content_uri).
            await actions.send_event_video(phone, caption=result.get("video_caption"))
        except Exception:
            logger.exception("send_event_video упал [%s] (ответ лиду уже отправлен)", phone)
    # needs_escalation=True — ИСПРАВЛЕНО (2026-08-06): раньше эскалация проверялась
    # только по action=='escalate', а промпт в нескольких местах (реагендар видеозвонка,
    # Instagram вместо фото вне photo_pending) явно велит модели ответить с action:
    # 'respond' И ОТДЕЛЬНО needs_escalation=true — это поле нигде не читалось, алерт
    # Ане не уходил, никто не переносил звонок/не проверял профиль, лид зависал.
    if action == "escalate" or result.get("needs_escalation"):
        # НЕ молчаливо: лид получил messages, плюс алерт Ане (продолжить лично).
        # Причина — название сценария; фолбэк если сценарий не определён.
        title = await db.get_scenario_title(result["used_scenario_id"])
        logger.info("AI escalate [%s] (scenario=%s, action=%s)",
                    phone, result["used_scenario_id"], action)
        try:
            await db.update_lead_fields(phone, mode="manual")
        except Exception:
            logger.exception("не смог поставить manual после эскалации [%s]", phone)
        await escalation.notify_escalation(lead, title or "Нужна твоя помощь", combined)
    else:
        logger.info("AI respond [%s] (scenario=%s, funnel=%s)",
                    phone, result["used_scenario_id"], result["funnel_stage"])


async def _send_rag_media_and_tail(phone: str, result: dict) -> None:
    """Режим rag: видео ивента, фото прошлых ивентов, потом after_media (вопрос последним).
    Сбой медиа не мешает вопросу; сбой вопроса не роняет уже отправленный ответ."""
    if result.get("send_event_video") and not sender.interrupted():
        try:
            await actions.send_event_video(phone, caption=result.get("video_caption"))
        except Exception:
            logger.exception("send_event_video упал [%s] (ответ лиду уже отправлен)", phone)
    if result.get("send_event_photo") and not sender.interrupted():
        try:
            await actions.send_event_photos(phone, caption=result.get("photo_caption"))
        except Exception:
            logger.exception("send_event_photos упал [%s] (ответ лиду уже отправлен)", phone)
    tail = [m for m in (result.get("after_media") or []) if m]
    if sender.interrupted():
        tail = []
    if tail:
        try:
            await sender.send(phone, tail)
        except Exception:
            logger.exception("вопрос после медиа не ушёл [%s]", phone)
            await escalation.notify_error("main._send_rag_media_and_tail", "вопрос после медиа не ушёл", phone)


async def _handle_videocall_booking(phone: str, lead: dict, combined: str,
                                    proposed_iso: str) -> None:
    """#53 автозапись: забронировать/перенести звонок и подтвердить лиду.

    Полностью автомат. Каждый исход (booked/reschedule/vague/past/out_of_hours/busy) —
    детерминированное сообщение лиду. ERROR (сбой Google/не настроено) → фолбэк: тёплый
    ответ + эскалация Ане (лид не зависает).
    """
    now = datetime.now(booking.CDMX)
    res = await booking.resolve_and_book(lead, proposed_iso, now)
    reply = [booking.message_for(res)]
    if res.outcome in (booking.Outcome.BOOKED, booking.Outcome.RESCHEDULED):
        # Ciudad/país se movieron a DESPUÉS de agendar (2026-08-15, menos preguntas antes
        # de la cita) — se preguntan aquí mismo, determinístico, apenas se confirma la hora.
        # NO REPETIR: si YA tenemos AMBAS (reagendado), no se pregunta de nuevo — pero si
        # falta CUALQUIERA de las dos (encontrado en code-review: antes era AND, dejaba un
        # hueco si el lead ya había dado solo una de las dos en la conversación), sí se
        # pregunta (a costo de repetir la que ya sabíamos, mejor que perder la que falta).
        if not lead.get("city") or not lead.get("country"):
            reply.append("Y ya que estamos, para ir preparando todo antes de la llamada: "
                         "¿en qué ciudad vives y de dónde eres originalmente? 🤍")
    await sender.send(phone, reply)
    logger.info("booking [%s]: %s → %s", phone, proposed_iso, res.outcome.value)

    if res.outcome in (booking.Outcome.BOOKED, booking.Outcome.RESCHEDULED):
        await db.set_funnel_stage(phone, "videocall_set",
                                  meta={"videocall_at": res.when.isoformat()})
        # Алерт назначенной (Аня/Мила/Рита): бот забронировал звонок → она отправляет
        # лиду ссылку вручную.
        await escalation.notify_videocall_booked(lead, booking.fmt_es(res.when), res.assignee)
    elif res.outcome == booking.Outcome.ERROR:
        # Фолбэк как у старого #53: назначает Аня вручную.
        try:
            await db.update_lead_fields(phone, mode="manual")
        except Exception:
            logger.exception("booking ERROR: не смог manual [%s]", phone)
        await escalation.notify_escalation(
            lead, "Автозапись звонка не удалась — назначь время вручную", combined)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Поднять пул БД и debouncer при старте, аккуратно закрыть при остановке."""
    global debouncer, scheduler_task
    if settings.supabase_db_dsn:
        await db.init_pool()
    else:
        logger.warning("SUPABASE_DB_DSN не задан — БД не подключена")
    # Прогрев кэшей промптов (синхронное чтение файлов) — чтобы не блокировать event
    # loop на первом запросе под нагрузкой.
    ai.load_system_prompt()
    vision.load_vision_prompt()
    # Менеджер-бот (блок 11): без admin_ids команды/кнопки никому не доступны. Частый
    # случай — TG_MANAGER_CHAT_ID это группа (отрицательный id, не совпадёт с user_id).
    admin_ids = settings.manager_admin_ids
    if not admin_ids:
        logger.warning("TG_MANAGER_ADMIN_IDS пуст — менеджер-бот никому не доступен")
    elif all(i < 0 for i in admin_ids):
        # Отрицательный id = группа/канал; user_id пользователей положительные — не совпадёт.
        logger.warning("TG_MANAGER_ADMIN_IDS только group-id (%s) — команды не пройдут; "
                       "задай user_id Ани/разработки", sorted(admin_ids))
    # ИСПРАВЛЕНО (2026-08-11, найдено по реальным логам): delay=4.0 слишком узкий
    # относительно того, сколько бот реально "печатает" (2-4 баббла × sender.compute_delay
    # 2-8с+рандом = 10-20+ реальных секунд антибан-задержки). Раздражённый/быстро печатающий
    # лид успевает прислать 2-3 отдельных сообщения (одна и та же мысль по кусочкам, обычно
    # 5-12с друг от друга) ДО того, как первое закроется по debounce — каждый кусок уходит
    # в СВОЙ отдельный залп к AI, который не видит продолжения мысли лида. Результат — каша
    # из несвязных/повторяющихся ответов подряд (см. реальный кейс wa_79150981866, 18:18-18:20).
    # ИЗМЕНЕНО (2026-08-15, прямая просьба владелицы): 7.0/20.0 → 90.0/120.0 — сознательно
    # даём лиду до полутора минут тишины дописать мысль ПОЛНОСТЬЮ (несколько сообщений подряд,
    # не только короткая раздражённая серия), прежде чем AI вообще увидит текст и ответит.
    # 120.0 max_wait — потолок для непрерывно печатающего лида (не ждём бесконечно).
    debouncer = Debouncer(_process_burst, delay=REPLY_DEBOUNCE_DEFAULT, max_wait=REPLY_MAX_WAIT)
    # Startup-sweep: debounce-таймеры теряются при рестарте — прогоняем лидов с
    # непроцессенными inbound заново, чтобы их сообщения не зависли (blocks 12).
    if db.is_ready():
        try:
            phones = await db.phones_with_unprocessed_inbound()
            for p in phones:
                await debouncer.trigger(p)
            if phones:
                logger.info("startup-sweep: %d лид(ов) с непроцессенными inbound → в debounce", len(phones))
        except Exception as e:
            # Не роняем старт, но алертим: иначе залипшие inbound молча ждут след. рестарта.
            logger.exception("startup-sweep упал (не критично для старта)")
            await escalation.notify_error("startup_sweep", repr(e))
    # Планировщик (блок 13): фоллоу-апы + напоминания об ивенте, почасовой тик.
    # scheduler_enabled=false → выключен (локальный прогон против прод-БД, чтобы вторая
    # копия не слала сообщения; планировщик тикает сразу на старте).
    if db.is_ready() and settings.scheduler_enabled:
        scheduler_task = asyncio.create_task(scheduler.run_loop())
    elif not settings.scheduler_enabled:
        logger.warning("Планировщик выключен (SCHEDULER_ENABLED=false)")
    yield
    if scheduler_task is not None:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    if debouncer is not None:
        await debouncer.shutdown()
    await db.close_pool()


app = FastAPI(title="MatchMatch Anna Bot", lifespan=lifespan)

# Мини-CRM (Telegram Mini App): роуты /api/mini/* поверх готовых db-функций.
# Авторизация — внутри роутера (mini_auth.require_admin по Telegram initData).
app.include_router(mini_api.router)

# Статика мини-аппа (SPA из miniapp/dist) отдаётся по пути /app — тот же origin, что и
# /api/mini (без CORS). Монтируем ТОЛЬКО если билд существует: иначе StaticFiles уронил
# бы старт бота на сервере без собранного dist. html=True → /app и /app/ отдают index.html.
_MINIAPP_DIST = os.path.join(os.path.dirname(__file__), "miniapp", "dist")
if os.path.isdir(_MINIAPP_DIST):
    app.mount("/app", StaticFiles(directory=_MINIAPP_DIST, html=True), name="miniapp")
    logger.info("мини-апп смонтирован: /app → %s", _MINIAPP_DIST)
else:
    logger.warning("miniapp/dist не найден (%s) — /app не смонтирован", _MINIAPP_DIST)


@app.get("/health")
async def health():
    """Проверка живости для systemd/мониторинга."""
    return {"status": "ok"}


@app.post("/webhook/wazzup/{secret}")
async def wazzup_webhook(secret: str, request: Request):
    """Приём вебхука Wazzup24.

    - Сверяем секрет в пути (у Wazzup нет подписи — это наша защита).
    - Отвечаем 200 на тестовый пинг {test: true}.
    - Каждое сообщение: normalize → insert (persist) → debounce.trigger.
    - ВСЕГДА возвращаем 200 (кроме неверного секрета), ошибки логируем перед 200.

    ВНИМАНИЕ (деплой): секрет — часть URL, попадёт в access-log uvicorn.
    На проде запускать с `--no-access-log` (см. systemd-юнит, блок 12).
    """
    if secret != settings.wazzup_webhook_secret:
        logger.warning("Webhook: неверный секрет в пути")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        logger.error("Webhook: не смог распарсить JSON, тело=%r", raw[:500])
        return Response(status_code=200)

    try:
        # Тестовый пинг при подключении вебхука в кабинете Wazzup.
        if isinstance(body, dict) and body.get("test") is True:
            logger.info("Webhook: тестовый пинг {test:true} — OK")
            return Response(status_code=200)

        messages = body.get("messages") if isinstance(body, dict) else None
        statuses = body.get("statuses") if isinstance(body, dict) else None

        if messages:
            for msg in messages:
                await _handle_incoming(msg)

        if statuses:
            logger.info("Webhook: статусов доставки: %d (пока игнор)", len(statuses))

        if not messages and not statuses:
            logger.info("Webhook: пейлоад без messages/statuses: %r", str(body)[:300])

    except Exception:
        logger.exception("Webhook: ошибка обработки пейлоада")

    return Response(status_code=200)


@app.post("/webhook/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Приём вебхука менеджер-бота (Telegram, бот «Лиды»).

    - Секрет в пути (как у Wazzup). Пустой секрет в конфиге → 403 (fail-safe).
    - Тело — Telegram Update; авторизация и логика в manager_bot.handle_update.
    - ВСЕГДА 200 (кроме неверного секрета) — Telegram иначе будет ретраить.
    """
    if not settings.tg_webhook_secret or secret != settings.tg_webhook_secret:
        logger.warning("Telegram webhook: неверный/пустой секрет в пути")
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = await request.json()
        await manager_bot.handle_update(update)
    except Exception:
        logger.exception("Telegram webhook: ошибка обработки update")
    return Response(status_code=200)


ECHO_MAX_AGE_MIN = 30  # эхо старше — не судим по нему о ручном ответе Ани


async def _handle_echo(echo) -> None:
    """Эхо исходящего с нашего номера. Если это НЕ наше сообщение (бот/CRM через API), а
    Аня ответила лиду с телефона/из Wazzup — переводим чат в ручной режим: бот молчит,
    пока в CRM не нажмут «Вернуть боту». Сообщение Ани сохраняем в историю.

    Отличаем по тексту: эхо, совпавшее с нашим исходящим этому номеру за 30 мин, — наше.
    Медиа без подписи отличить нельзя (Wazzup перезаливает файл) — их не трогаем.
    """
    if not echo.text:
        logger.info("эхо [%s] %s без текста — пропуск (своё/чужое не отличить)",
                    echo.phone, echo.content_type)
        return
    # Старое эхо (Wazzup дослал после простоя/ретрая): память и 30-мин окно БД его уже не
    # узнают — без этой проверки наше же давнее сообщение перевело бы живого лида в manual.
    sent_at = _parse_dt(echo.received_at)
    if sent_at is not None and sent_at < datetime.now(timezone.utc) - timedelta(minutes=ECHO_MAX_AGE_MIN):
        logger.info("эхо [%s] старше %d мин (%s) — пропуск", echo.phone, ECHO_MAX_AGE_MIN,
                    sent_at.isoformat())
        return
    phone = await db.resolve_lead_phone(echo.phone)
    if sender.was_sent_recently(phone, echo.text) or await db.outbound_text_recent(phone, echo.text):
        logger.debug("эхо [%s]: наше исходящее — пропуск", phone)
        return
    # Автоприветствие WhatsApp Business — не ответ Ани: режим не меняем, сохраняем как
    # служебное (бот видит, что приветствие ушло, но фактов из него не берёт).
    try:
        configured = filters.parse_auto_greeting_texts(
            (await db.get_settings(["auto_greeting_texts"])).get("auto_greeting_texts"))
    except Exception:
        logger.exception("эхо [%s]: не прочитала auto_greeting_texts — проверяю по началу", phone)
        configured = []
    if filters.is_auto_greeting(echo.text, configured):
        await db.upsert_lead(phone)
        await db.insert_message(phone, "outbound", "anna", echo.text,
                                external_message_id=echo.external_message_id,
                                meta={"source": "auto_greeting", "received_at": echo.received_at},
                                processed=True)
        logger.info("эхо [%s]: автоприветствие WhatsApp — режим не меняю", phone)
        return
    # Ответ человека. Лид мог ещё не существовать (Аня написала первой) — создаём,
    # иначе его ответ бот принял бы за нового лида.
    await db.upsert_lead(phone)
    lead = await db.get_lead_by_phone(phone) or {}
    if lead.get("mode") != "manual":
        await db.set_manual(phone)
        logger.warning("эхо [%s]: Аня ответила лично → ручной режим, бот молчит", phone)
    await db.insert_message(phone, "outbound", "anna", echo.text,
                            external_message_id=echo.external_message_id,
                            meta={"manual": True, "source": "echo",
                                  "received_at": echo.received_at},
                            processed=True)


async def _handle_incoming(msg) -> None:
    """Обработать одно входящее: normalize → persist → debounce.

    Ошибка одного сообщения не должна рушить остальной батч и ответ 200.
    Порядок: insert ДО trigger (сообщение persist'ится раньше ack и раньше on_flush).
    trigger только если сообщение реально вставлено (не дубль Wazzup-ретрая).
    """
    try:
        echo = parse_echo(msg)
        if echo is not None:
            if db.is_ready():
                await _handle_echo(echo)
            return
        nm = normalize_wazzup_message(msg)
        if nm is None:
            return  # дроп (статус/не-whatsapp/пустой/неизвестный тип)

        if not db.is_ready():
            logger.warning("Webhook: БД не готова, сообщение %s не сохранено", nm.external_message_id)
            return

        # Тот же номер мог уже храниться в другой форме (wa_52… vs wa_521…) — пишем в
        # существующего лида, не плодим дубль. Новый лид — в формате Wazzup как есть.
        phone = await db.resolve_lead_phone(nm.phone)
        if phone != nm.phone:
            logger.info("inbound: номер %s найден у лида как %s", nm.phone, phone)
            nm = dataclasses.replace(nm, phone=phone)

        # Лид должен существовать ДО вставки сообщения: messages.lead_phone имеет
        # FK на leads.phone. upsert создаёт лида (или обновляет имя); status/mode/
        # funnel_stage/source проставятся дефолтами схемы при INSERT.
        await db.upsert_lead(nm.phone, whatsapp_name=nm.user_name)
        # Метка времени входящего — для планировщика фоллоу-апов (блок позже).
        await db.touch_last_inbound(nm.phone)

        # content_uri в meta — чтобы фото-ветка на флаше могла скачать медиа.
        meta = {"content_type": nm.content_type}
        # Время сообщения по Wazzup — для защиты от старых входящих после простоя канала.
        if nm.received_at:
            meta["received_at"] = nm.received_at
        if nm.media_info and nm.media_info.get("content_uri"):
            meta["content_uri"] = nm.media_info["content_uri"]
        if nm.caption:
            meta["caption"] = nm.caption

        inserted = await db.insert_message(
            nm.phone,
            "inbound",
            "lead",
            nm.user_text,
            external_message_id=nm.external_message_id,
            meta=meta,
        )
        logger.info(
            "inbound %s: phone=%s type=%s inserted=%s",
            nm.external_message_id, nm.phone, nm.content_type, inserted,
        )

        if inserted and debouncer is not None:
            delay, cap = await _debounce_window(nm.content_type)
            await debouncer.trigger(nm.phone, delay, cap)

    except Exception:
        logger.exception("Webhook: ошибка обработки сообщения")
