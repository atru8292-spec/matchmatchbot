"""FastAPI-сервер бота Anna.

Входящая труба (без AI): вебхук Wazzup → normalize → insert в БД → debounce → on_flush.
on_flush пока просто читает склеенный залп из БД и логирует (AI встанет сюда в блоке 6).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

import ai
import db
import filters
import sender
from config import settings
from debounce import Debouncer
from normalize import normalize_wazzup_message

# Логи в stdout → journald (systemd). Помечаем время/уровень/модуль.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("matchmatch")

# Debouncer создаётся в lifespan (склейка серии быстрых сообщений одного лида).
debouncer: Debouncer | None = None


async def _process_burst(phone: str) -> None:
    """on_flush для debounce: собрать залп, принять детерминированное решение.

    Читаем склеенный залп непроцессенных inbound из БД (источник истины — не память),
    помечаем processed, затем filters.decide. AI-ветки (needs_ai/respond) пока логируют
    заглушку — реальный вызов AI встанет сюда в блоке 6.
    """
    msgs = await db.get_unprocessed_inbound(phone)
    if not msgs:
        # уже обработано параллельным флашем — нечего делать
        return
    combined = "\n".join((m.get("text") or "") for m in msgs)
    content_types = [(m.get("meta") or {}).get("content_type") for m in msgs]
    logger.info(
        "склеенный залп от %s (%d сообщ., типы: %s): %r",
        phone, len(msgs), content_types, combined,
    )
    await db.mark_messages_processed([m["id"] for m in msgs])

    # Детерминированное решение по залпу (whitelist/блок/отказ/AI).
    lead = await db.get_lead_by_phone(phone) or {}
    whitelisted = await db.is_whitelisted(phone)
    decision = filters.decide(lead, whitelisted, combined)
    await _apply_decision(phone, decision, lead, combined)


async def _apply_decision(phone: str, decision: "filters.Decision", lead: dict,
                          combined: str) -> None:
    """Выполнить решение filters.decide. needs_ai → реальный вызов AI (ai.py)."""
    name = lead.get("whatsapp_name") or lead.get("name") or phone

    if decision.action == "silent_whitelist":
        # Бот молчит; сообщение уже в истории. TODO (блок 8): алерт Ане в Telegram.
        logger.info("РЕШЕНИЕ silent_whitelist [%s]: %s | TODO-алерт Ане: 'написал %s'",
                    phone, decision.reason, name)
        return

    if decision.action == "blocked":
        # Блок навсегда + стадия lost — атомарно внутри block_lead. is_escort из
        # Decision (не парсим текст reason). TODO (блок 8): алерт Ане.
        await db.block_lead(phone, decision.reason, escort=decision.is_escort)
        logger.info("РЕШЕНИЕ blocked [%s]: %s | TODO-алерт Ане", phone, decision.reason)
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
        except Exception:
            logger.exception(
                "обработка AI упала [%s] — лид без ответа, сообщения уже processed. "
                "TODO (блок 8): алерт Ане", phone,
            )
        return

    logger.warning("неизвестное решение %s [%s]", decision.action, phone)


async def _run_ai(phone: str, lead: dict, combined: str) -> None:
    """Сгенерировать и применить ответ AI: extracted → стадия → action → отправка."""
    history = await db.get_conversation_history(phone, 30)
    result = await ai.generate_reply(lead, history, combined)

    # 1. Извлечённые поля лида (age/profession/is_single/city/interest) — уже
    #    провалидированы в ai (только реальные непустые поля из whitelist LEAD_COLUMNS).
    if result["extracted"]:
        try:
            await db.update_lead_fields(phone, **result["extracted"])
        except Exception:
            logger.exception("не смог обновить extracted для %s: %s", phone, result["extracted"])

    action = result["action"]
    messages = result["messages"]

    if action == "block":
        # AI-блок через fixed-сценарий. reason из сценария (за что), без escort-инкремента
        # (escort-счётчик ведёт filters). block_lead сам ставит стадию 'lost' в транзакции.
        # Порядок: сначала block (гарантирован), потом прощальное сообщение.
        title = await db.get_scenario_title(result["used_scenario_id"])
        reason = f"AI: {title}" if title else "AI-блок по сценарию"
        await db.block_lead(phone, reason)
        await sender.send(phone, messages)  # прощальное сообщение лиду
        logger.info("AI block [%s]: %s (scenario=%s) | TODO-алерт Ане",
                    phone, reason, result["used_scenario_id"])
        return

    # respond / escalate — стадию ставит AI (если вернул валидную).
    if result["funnel_stage"]:
        await db.set_funnel_stage(phone, result["funnel_stage"],
                                  meta={"scenario_id": result["used_scenario_id"]})

    await sender.send(phone, messages)  # messages лиду
    if action == "escalate":
        # НЕ молчаливо: лид получил messages, плюс поднимаем флаг Ане.
        logger.info("AI escalate [%s] (scenario=%s) | TODO-алерт Ане (сообщения лиду отправлены)",
                    phone, result["used_scenario_id"])
    else:
        logger.info("AI respond [%s] (scenario=%s, funnel=%s)",
                    phone, result["used_scenario_id"], result["funnel_stage"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Поднять пул БД и debouncer при старте, аккуратно закрыть при остановке."""
    global debouncer
    if settings.supabase_db_dsn:
        await db.init_pool()
    else:
        logger.warning("SUPABASE_DB_DSN не задан — БД не подключена")
    debouncer = Debouncer(_process_burst, delay=4.0, max_wait=15.0)
    yield
    if debouncer is not None:
        await debouncer.shutdown()
    await db.close_pool()


app = FastAPI(title="MatchMatch Anna Bot", lifespan=lifespan)


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


async def _handle_incoming(msg) -> None:
    """Обработать одно входящее: normalize → persist → debounce.

    Ошибка одного сообщения не должна рушить остальной батч и ответ 200.
    Порядок: insert ДО trigger (сообщение persist'ится раньше ack и раньше on_flush).
    trigger только если сообщение реально вставлено (не дубль Wazzup-ретрая).
    """
    try:
        nm = normalize_wazzup_message(msg)
        if nm is None:
            return  # дроп (echo/статус/не-whatsapp/пустой/неизвестный тип)

        if not db.is_ready():
            logger.warning("Webhook: БД не готова, сообщение %s не сохранено", nm.external_message_id)
            return

        # Лид должен существовать ДО вставки сообщения: messages.lead_phone имеет
        # FK на leads.phone. upsert создаёт лида (или обновляет имя); status/mode/
        # funnel_stage/source проставятся дефолтами схемы при INSERT.
        await db.upsert_lead(nm.phone, whatsapp_name=nm.user_name)
        # Метка времени входящего — для планировщика фоллоу-апов (блок позже).
        await db.touch_last_inbound(nm.phone)

        inserted = await db.insert_message(
            nm.phone,
            "inbound",
            "lead",
            nm.user_text,
            external_message_id=nm.external_message_id,
            meta={"content_type": nm.content_type},
        )
        logger.info(
            "inbound %s: phone=%s type=%s inserted=%s",
            nm.external_message_id, nm.phone, nm.content_type, inserted,
        )

        if inserted and debouncer is not None:
            await debouncer.trigger(nm.phone)

    except Exception:
        logger.exception("Webhook: ошибка обработки сообщения")
