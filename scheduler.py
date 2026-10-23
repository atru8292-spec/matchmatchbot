"""Планировщик (блок 13): почасовой тик.

A) Follow-up молчунов — по next_followup_at, лестница funnel.FOLLOWUP_LADDER (2→5→10 дней,
   макс 3 попытки), тексты из сценариев 36/33/38. Не трогает whitelist/do_not_contact/
   финальные стадии (фильтрует db.due_followups).
B) Напоминания об ивенте — за день (сценарий 50), в день (сценарий 47) и check-in на
   следующее утро (сценарий 23). Адрес/время из app_settings. Идемпотентно (маркер в
   events по дате+типу) — не дублируем при рестарте.
C) Напоминание о видеозвонке за ~2 часа (сценарий 49) — по leads.videocall_at, с окном
   допуска ±30 мин на дискретность почасового тика. Идемпотентно (leads.videocall_reminded_at).

Один in-process asyncio-таск (run_loop) стартует в lifespan, спит TICK_INTERVAL_SEC.
Каждый тик и каждый лид обёрнуты — сбой одного не рушит остальных; сбой тика → notify_error.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import actions
import db
import escalation
import funnel
import sender
from config import settings
from phones import canonical_phone

logger = logging.getLogger("matchmatch.scheduler")

TICK_INTERVAL_SEC = 3600  # раз в час
FOLLOWUP_BATCH = 30       # холодных догонов за тик (≤ суточному лимиту; остаток — след. тик)
EVENT_REMINDER_BATCH = 30 # получателей напоминаний за тик (антибан порциями)

# Антибан: пауза между ЛИДАМИ внутри тика (не путать с compute_delay между бабблами одного
# лида) — размазать всплеск рассылки. Холодные догоны — дольше, тёплые напоминания — короче.
COLD_LEAD_PAUSE = (30.0, 90.0)   # сек между холодными догонами (#33/#36/#38)
WARM_LEAD_PAUSE = (15.0, 40.0)   # сек между тёплыми напоминаниями (ивент/звонок/check-in)
COLD_COUNTER = "cold_followups"  # ключ суточного счётчика холодных догонов (app_settings)
SCHEDULER_ENABLED_KEY = "scheduler_enabled"  # тумблер авто-рассылок в CRM (app_settings)


async def _lead_pause(bounds: tuple[float, float]) -> None:
    """Антибан-пауза между лидами внутри тика (рандом в диапазоне). Тесты мокают в ноль."""
    await asyncio.sleep(random.uniform(*bounds))

# Ключи app_settings ивента.
EVENT_KEYS = ["event_active", "event_date", "event_time", "event_address"]
REMIND_1D_SCENARIO = 50       # «завтра ивент»
REMIND_DAY_SCENARIO = 47      # «сегодня ивент» — Шаблон A (оплатившим, без ссылки)
REMIND_DAY_UNPAID_SCENARIO = 54  # «сегодня ивент» — Шаблон B (неоплатившим, со ссылкой на билет)
CHECKIN_MORNING_SCENARIO = 23  # утренний check-in на следующий день после ивента

# Стадии «уже оплатил/клиент» → в день ивента шлём Шаблон A без призыва купить билет.
# Клиенты агентства (whitelist) исключены из выборки отдельно (event_recipients).
PAID_STAGES = {"event_attended", "client_agency"}

# Напоминание о видеозвонке (сценарий 49).
VIDEOCALL_SCENARIO = 49
VIDEOCALL_REMIND_BEFORE_MIN = 120  # напоминаем за 2 часа до звонка
VIDEOCALL_TOLERANCE_MIN = 30       # окно допуска ±30 мин (тик почасовой — см. run_videocall_reminders)
VIDEOCALL_BATCH = 30
# Часовой пояс Мехико: время звонка лиду и «сегодня/завтра» для напоминаний об ивенте.
CDMX_TZ = ZoneInfo("America/Mexico_City")
# Окно отправки напоминаний об ивенте по Мехико: [10:00, 20:00). Тик почасовой — без окна
# «завтра ивент» ушло бы в первый тик после полуночи (решение владелицы 2026-10-21).
EVENT_SEND_HOUR_FROM = 10
EVENT_SEND_HOUR_TO = 20


def _bubbles(template: str) -> list[str]:
    return [p.strip() for p in (template or "").split("\n\n") if p.strip()]


def _personalize(text: str, name: str | None) -> str:
    """Подстановка имени в follow-up: [имя] → имя, если известно; если нет — убираем
    чисто ('Hola [имя]!' → 'Hola!'). НИКОГДА 'guapo' ни другого обращения-заменителя —
    та же линия, что и в основном промпте (anna_prompt_v5.md: NUNCA 'guapo')."""
    display = (name or "").strip()
    if display:
        return text.replace("[имя]", display)
    return text.replace("Hola [имя]!", "Hola!")


def _fill_event(template: str, settings_map: dict) -> str:
    """Подставить время/адрес из app_settings в плейсхолдеры сценария."""
    addr = settings_map.get("event_address") or ""
    hora = settings_map.get("event_time") or ""
    return (template
            .replace("[hora]", hora)
            .replace("[dirección, lugar, parking]", addr)
            .replace("[dirección]", addr))


# ===== A) Follow-up =====

async def run_followups() -> int:
    """Разослать очередные ХОЛОДНЫЕ фоллоу-апы (#33/#36/#38). Вернуть число отправленных.

    Антибан: суточный лимит cold_followup_daily_cap (персистентный счётчик по номеру) +
    пауза между лидами. Тихое окно (не слать активному лиду) — в db.due_followups."""
    cap = settings.cold_followup_daily_cap
    sent_today = await db.get_daily_counter(COLD_COUNTER)
    remaining = cap - sent_today
    if remaining <= 0:
        logger.info("followups: суточный лимит %d исчерпан (сегодня %d) — стоп", cap, sent_today)
        return 0
    leads = await db.due_followups(list(funnel.NO_FOLLOWUP_STAGES), funnel.MAX_FOLLOWUPS,
                                   settings.followup_quiet_hours,
                                   limit=min(FOLLOWUP_BATCH, remaining),
                                   max_silence_days=settings.followup_max_silence_days)
    done = 0
    for ld in leads:
        if done >= remaining:
            break  # добили дневной лимит на этот тик
        phone = ld["phone"]
        try:
            count = ld["followup_sent_count"]
            if count >= funnel.MAX_FOLLOWUPS:
                continue  # подстраховка (query и так фильтрует < MAX)
            # Стадийный выбор: какой догон слать зависит от состояния лида (анкета/звонок/
            # холодный), а не от позиции в лестнице. None → не догоняем (звонок назначен).
            scenario_id = funnel.followup_scenario_for(ld)
            if scenario_id is None:
                continue
            next_days = funnel.FOLLOWUP_INTERVALS[count]
            tmpl = await db.get_scenario_template(scenario_id)
            if not tmpl:
                logger.error("followup: нет template сценария %s", scenario_id)
                await escalation.notify_error("scheduler.followup", f"нет сценария {scenario_id}")
                continue
            name = ld.get("whatsapp_name") or ld.get("name")
            bubbles = [_personalize(b, name) for b in _bubbles(tmpl)]
            sent = await sender.send(phone, bubbles)
            if sent == 0:
                # Wazzup не принял (send не бросает, вернул 0) — НЕ жжём ступень лестницы,
                # не считаем в лимит, повторим на следующем тике.
                logger.warning("followup: send=0 для %s — не помечаю, повтор позже", phone)
                continue
            next_at = (None if next_days is None
                       else datetime.now(timezone.utc) + timedelta(days=next_days))
            await db.mark_followup_sent(phone, next_at)
            await db.incr_daily_counter(COLD_COUNTER)  # засчитываем в суточный лимит
            done += 1
            await _lead_pause(COLD_LEAD_PAUSE)  # антибан-пауза между лидами
        except Exception as e:
            logger.exception("followup упал для %s", phone)
            await escalation.notify_error("scheduler.followup", repr(e), phone)
    if done:
        logger.info("followups: отправлено %d (сегодня %d/%d)", done, sent_today + done, cap)
    return done


# ===== B) Напоминания об ивенте =====

async def run_event_reminders(now: datetime | None = None) -> int:
    """Разослать напоминания об ивенте, если сегодня T-1 или день ивента. Вернуть число.

    «Сегодня» и окно отправки считаем по Мехико, не по UTC: в 18:00 17.11 по Мехико
    в UTC уже 18.11, и раньше «сегодня ивент» ушло бы на день раньше."""
    local = (now or datetime.now(timezone.utc)).astimezone(CDMX_TZ)
    if not (EVENT_SEND_HOUR_FROM <= local.hour < EVENT_SEND_HOUR_TO):
        logger.info("event reminders: %s по Мехико вне окна %02d:00-%02d:00 — пропуск",
                    local.strftime("%H:%M"), EVENT_SEND_HOUR_FROM, EVENT_SEND_HOUR_TO)
        return 0
    today = local.date()
    s = await db.get_settings(EVENT_KEYS)
    if s.get("event_active") != "1":
        return 0
    date_str = s.get("event_date")
    if not date_str:
        return 0
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        logger.warning("event_date некорректен: %r — пропуск", date_str)
        return 0

    # Прошедший ивент (T+2 и дальше) — ни напоминаний, ни FOMO-медиа (ниже ни одна ветка
    # не срабатывает; явная проверка — чтобы правка веток случайно это не сломала).
    if today > event_date + timedelta(days=1):
        return 0
    if today == event_date - timedelta(days=1):
        # T-1: разогрев + FOMO-фото атмосферы прошлых ивентов.
        return await _send_event_batch("remind_1d", REMIND_1D_SCENARIO, s, date_str,
                                       attach_photos=True)
    if today == event_date:
        return await _send_event_daytime(s, date_str)
    if today == event_date + timedelta(days=1):
        # Утренний check-in на следующий день после ивента (сценарий 23) — без медиа (ивент прошёл).
        # Только оплатившим/пришедшим: «cómo te fue ayer?» тому, кто не был, звучит нелепо.
        return await _send_event_batch("checkin_morning", CHECKIN_MORNING_SCENARIO, s, date_str,
                                       only_stages=sorted(PAID_STAGES))
    return 0


async def _send_event_batch(kind: str, scenario_id: int, settings_map: dict,
                            date_str: str, attach_photos: bool = False,
                            only_stages: list[str] | None = None) -> int:
    tmpl = await db.get_scenario_template(scenario_id)
    if not tmpl:
        logger.error("event reminder: нет template сценария %s", scenario_id)
        await escalation.notify_error("scheduler.event", f"нет сценария {scenario_id}")
        return 0
    text = _fill_event(tmpl, settings_map)
    bubbles = _bubbles(text)
    # LIMIT + батч (антибан: не всё разом). Остаток уйдёт на следующих тиках — дедуп
    # уже в самом SQL-запросе (event_recipients), LIMIT реально означает «следующие».
    # exclude_phones: тестовые/bypass-номера не должны попадать в реальную рассылку.
    recipients = await db.event_recipients(list(funnel.EVENT_REMINDER_EXCLUDE_STAGES),
                                           kind, date_str, limit=EVENT_REMINDER_BATCH,
                                           exclude_phones=await db.bypass_phone_set(),
                                           only_stages=only_stages)
    sent = 0
    for ld in recipients:
        phone = ld["phone"]
        try:
            if await db.event_reminder_sent(phone, kind, date_str):
                continue  # уже слали это напоминание на эту дату — идемпотентность
            ok = await sender.send(phone, bubbles)
            if ok == 0:
                continue  # Wazzup не принял — не логируем маркер, повторим на след. тике
            await db.log_event_reminder(phone, kind, date_str)
            sent += 1
            if attach_photos:
                # FOMO: фото атмосферы после текста. Дедуп по дате ивента (вар. B) — не
                # повторяем этому лиду в рамках этого ивента. Сбой не роняет напоминание.
                try:
                    await actions.send_event_photos(phone, event_date=date_str)
                except Exception:
                    logger.exception("FOMO-фото к напоминанию %s упало для %s", kind, phone)
            await _lead_pause(WARM_LEAD_PAUSE)  # антибан-пауза между лидами (без суточного лимита)
        except Exception as e:
            logger.exception("event reminder %s упал для %s", kind, phone)
            await escalation.notify_error("scheduler.event", repr(e), phone)
    if sent:
        logger.info("event reminder %s: отправлено %d (дата %s)", kind, sent, date_str)
    return sent


async def _send_event_daytime(settings_map: dict, date_str: str) -> int:
    """День ивента: оплатившим/членам — Шаблон A (#47, без ссылки), остальным —
    Шаблон B (#54, призыв + ссылка на билет). Выбор по funnel_stage получателя.

    «Оплатил» = funnel_stage в PAID_STAGES (event_attended/client_agency). Клиенты агентства
    (whitelist) в выборку не попадают вовсе (event_recipients фильтрует w.phone IS NULL).
    Неоплатившим ссылку шлём ВСЕГДА (allow_repeat_links=True): факт оплаты важнее дедупа
    по тексту — «видел ссылку» ≠ «оплатил». Один day-of на лида (идемпотентность по kind).
    """
    kind = "remind_day"
    tmpl_paid = await db.get_scenario_template(REMIND_DAY_SCENARIO)      # A: без ссылки
    tmpl_unpaid = await db.get_scenario_template(REMIND_DAY_UNPAID_SCENARIO)  # B: со ссылкой
    if not tmpl_paid or not tmpl_unpaid:
        logger.error("day-of: нет шаблона %s/%s", REMIND_DAY_SCENARIO, REMIND_DAY_UNPAID_SCENARIO)
        await escalation.notify_error("scheduler.event",
                                      f"нет сценария {REMIND_DAY_SCENARIO}/{REMIND_DAY_UNPAID_SCENARIO}")
        return 0
    recipients = await db.event_recipients(list(funnel.EVENT_REMINDER_EXCLUDE_STAGES),
                                           kind, date_str, limit=EVENT_REMINDER_BATCH,
                                           exclude_phones=await db.bypass_phone_set())
    sent = 0
    for ld in recipients:
        phone = ld["phone"]
        try:
            if await db.event_reminder_sent(phone, kind, date_str):
                continue  # один day-of на лида на эту дату (идемпотентность), любой шаблон
            paid = ld.get("funnel_stage") in PAID_STAGES
            bubbles = _bubbles(_fill_event(tmpl_paid if paid else tmpl_unpaid, settings_map))
            ok = await sender.send(phone, bubbles, allow_repeat_links=not paid)
            if ok == 0:
                continue  # Wazzup не принял — маркер не логируем, повторим на след. тике
            await db.log_event_reminder(phone, kind, date_str)
            sent += 1
            if not paid:
                # FOMO неоплатившим: до 3 фото атмосферы (EVENT_PHOTO_COUNT), без видео —
                # второе медиа-место в лестнице, бережём антибан-объём. Дедуп по дате (вар. B).
                try:
                    await actions.send_event_photos(phone, event_date=date_str)
                except Exception:
                    logger.exception("FOMO-фото день-ивента упало для %s", phone)
            await _lead_pause(WARM_LEAD_PAUSE)  # антибан-пауза между лидами (без суточного лимита)
        except Exception as e:
            logger.exception("day-of reminder упал для %s", phone)
            await escalation.notify_error("scheduler.event", repr(e), phone)
    if sent:
        logger.info("day-of reminder: отправлено %d (дата %s)", sent, date_str)
    return sent


# ===== C) Напоминание о видеозвонке за ~2 часа =====

async def run_videocall_reminders(now=None) -> int:
    """Напомнить о видеозвонке за ~2 часа (сценарий 49). Вернуть число отправленных.

    Окно [now+1.5ч, now+2.5ч]: попадаем в «за 2 часа» с допуском ±30 мин на дискретность
    почасового тика. Окно шириной 60 мин при часовом тике → каждый звонок покрыт ровно
    одним тиком (плюс идемпотентность videocall_reminded_at страхует от дублей).
    """
    now = now or datetime.now(timezone.utc)
    start = now + timedelta(minutes=VIDEOCALL_REMIND_BEFORE_MIN - VIDEOCALL_TOLERANCE_MIN)
    end = now + timedelta(minutes=VIDEOCALL_REMIND_BEFORE_MIN + VIDEOCALL_TOLERANCE_MIN)
    leads = await db.due_videocall_reminders(start, end, limit=VIDEOCALL_BATCH)
    if not leads:
        return 0
    tmpl = await db.get_scenario_template(VIDEOCALL_SCENARIO)
    if not tmpl:
        logger.error("videocall reminder: нет template сценария %s", VIDEOCALL_SCENARIO)
        await escalation.notify_error("scheduler.videocall", f"нет сценария {VIDEOCALL_SCENARIO}")
        return 0
    sent = 0
    for ld in leads:
        phone = ld["phone"]
        try:
            # Время звонка показываем в часовом поясе CDMX (в БД — UTC).
            hora = ld["videocall_at"].astimezone(CDMX_TZ).strftime("%H:%M")
            bubbles = _bubbles(tmpl.replace("[hora]", hora))
            ok = await sender.send(phone, bubbles)
            if ok == 0:
                continue  # Wazzup не принял — не помечаем, повторим на след. тике
            await db.mark_videocall_reminded(phone)
            sent += 1
            await _lead_pause(WARM_LEAD_PAUSE)  # антибан-пауза между лидами (без суточного лимита)
        except Exception as e:
            logger.exception("videocall reminder упал для %s", phone)
            await escalation.notify_error("scheduler.videocall", repr(e), phone)
    if sent:
        logger.info("videocall reminders: отправлено %d", sent)
    return sent


# ===== Тик и цикл =====

async def _bot_paused() -> bool:
    """Глобальная пауза (тумблер в CRM). Сбой чтения → False (не паузим, безопасный дефолт)."""
    try:
        return (await db.get_settings(["bot_paused"])).get("bot_paused") == "1"
    except Exception:
        return False


async def _scheduler_enabled() -> bool:
    """Тумблер «Авто-рассылки» в CRM (app_settings.scheduler_enabled). По умолчанию ВЫКЛ:
    нет ключа или сбой чтения → False (безопасно: лучше не отправить, чем разослать залп).
    Ответы на входящие от него не зависят — только проактивные отправки планировщика."""
    try:
        return (await db.get_settings([SCHEDULER_ENABLED_KEY])).get(SCHEDULER_ENABLED_KEY) == "1"
    except Exception:
        logger.exception("scheduler: не прочитала scheduler_enabled — считаю выключенным")
        return False


# ===== D) Сводка «пауза» =====
# На паузе входящие сохраняются и помечаются обработанными: после снятия паузы бот на них
# НЕ ответит. Раз в час шлём, кто писал. Пока только Арине (escalation.notify_dev), Ане нет:
# решение 2026-10-23, сводки приходили слишком часто.
PAUSE_DIGEST_KEY = "pause_digest_last_at"   # app_settings: время последней сводки (ISO, UTC)
PAUSE_DIGEST_INTERVAL = timedelta(hours=1)
PAUSE_DIGEST_MAX_LINES = 20


def _fmt_phone(phone: str) -> str:
    return "+" + phone[3:] if phone.startswith("wa_") else phone


async def run_pause_digest(now: datetime | None = None) -> int:
    """Сводка админам о входящих за время паузы. Не чаще раза в час, при нуле молчим.
    Вернуть число лидов в сводке. Вызывается из tick только при bot_paused=1."""
    now = now or datetime.now(timezone.utc)
    last = None
    raw = await db.get_setting(PAUSE_DIGEST_KEY)
    if raw:
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("pause digest: кривой %s=%r — считаю, что сводок не было",
                           PAUSE_DIGEST_KEY, raw)
    if last and now - last < PAUSE_DIGEST_INTERVAL:
        return 0
    # С прошлой сводки (без зазоров между тиками); первая — за последний час.
    # Начало паузы отсекает inbound_since: до неё бот ещё отвечал.
    since = last or now - PAUSE_DIGEST_INTERVAL
    bypass = await db.bypass_phone_set()
    rows = [r for r in await db.inbound_since(since)
            if canonical_phone(r["phone"]) not in bypass]
    if not rows:
        return 0
    total = sum(r["n"] for r in rows)
    lines = [f"⏸ Бот на паузе. С прошлой сводки написали {len(rows)} чел. "
             f"({total} сообщ.), бот им не ответит и после снятия паузы:"]
    for r in rows[:PAUSE_DIGEST_MAX_LINES]:
        name = f"{r['name']} " if r.get("name") else ""
        lines.append(f"• {name}{_fmt_phone(r['phone'])} ({r['n']})")
    if len(rows) > PAUSE_DIGEST_MAX_LINES:
        lines.append(f"… и ещё {len(rows) - PAUSE_DIGEST_MAX_LINES}")
    lines.append("Открой их вручную в WhatsApp или в CRM.")
    await escalation.notify_dev("\n".join(lines))
    await db.set_setting(PAUSE_DIGEST_KEY, now.isoformat())
    logger.info("pause digest: %d лидов, %d сообщений", len(rows), total)
    return len(rows)


async def run_memory_summaries(limit: int = 5) -> int:
    """Режим rag: обновить сводки памяти лидов, которые молчат > 6 ч (gpt-4.1-mini)."""
    if (await db.get_setting("reply_mode") or "").strip().lower() != "rag":
        return 0
    import ai_conversation
    return await ai_conversation.refresh_stale_summaries(limit=limit)


async def tick() -> None:
    """Один проход планировщика. Сбой любой части → лог + technical-алерт, не роняет цикл."""
    # Глобальная пауза (тех. режим): не шлём НИЧЕГО проактивно (догоны/напоминания) всем лидам.
    # Только сводка админам о том, кто писал на паузе (лидам ничего не уходит).
    if await _bot_paused():
        logger.info("scheduler: бот на паузе (тех. режим) — тик пропущен")
        try:
            await run_pause_digest()
        except Exception as e:
            logger.exception("run_pause_digest упал")
            await escalation.notify_error("scheduler.pause_digest", repr(e))
        return
    # Сводки памяти лидов (режим rag): лиду ничего не уходит, поэтому не зависит от
    # scheduler_enabled. Только при reply_mode=rag (при rag_test сводка строится перед ответом).
    try:
        await run_memory_summaries()
    except Exception as e:
        logger.exception("run_memory_summaries упал")
        await escalation.notify_error("scheduler.memory_summaries", repr(e))
    # Отдельный тумблер авто-рассылок: догоны #32/#33/#36/#38/#59, напоминания об ивенте
    # (+FOMO-медиа), напоминания о звонке #49. Пока 0 — ничего не отправляем.
    if not await _scheduler_enabled():
        logger.info("scheduler: авто-рассылки выключены (scheduler_enabled≠1) — тик пропущен")
        return
    try:
        await run_followups()
    except Exception as e:
        logger.exception("run_followups упал")
        await escalation.notify_error("scheduler.followups", repr(e))
    try:
        await run_event_reminders()
    except Exception as e:
        logger.exception("run_event_reminders упал")
        await escalation.notify_error("scheduler.events", repr(e))
    try:
        await run_videocall_reminders()
    except Exception as e:
        logger.exception("run_videocall_reminders упал")
        await escalation.notify_error("scheduler.videocall", repr(e))


async def run_loop() -> None:
    """Вечный цикл: тик сразу на старте (добьёт пропущенное за простой), затем каждый час.
    Останавливается отменой таска (CancelledError) в lifespan.shutdown."""
    while True:
        await tick()
        await asyncio.sleep(TICK_INTERVAL_SEC)
