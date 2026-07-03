"""Слой доступа к БД (Supabase Postgres) на asyncpg.

Все запросы параметризованные ($1,$2...). Имена колонок в динамических
UPDATE/UPSERT берутся только из whitelist LEAD_COLUMNS — защита от инъекции
в имя колонки (значения и так идут параметрами).

Пул создаётся один раз через init_pool() (FastAPI lifespan), не коннект-на-запрос.
Подключение — по DSN из .env (Session pooler Supabase, порт 5432, sslmode=require).
"""
from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
from asyncpg.exceptions import UniqueViolationError

import funnel
from config import settings

logger = logging.getLogger("matchmatch.db")

# Глобальный пул. None до init_pool() и после close_pool().
_pool: asyncpg.Pool | None = None
# Защита от гонки при конкурентном init_pool() (создать пул ровно один раз).
_pool_lock = asyncio.Lock()

# Колонки leads, которые разрешено писать через upsert_lead/update_lead_fields.
# Из DB_SCHEMA.md, исключены: id, phone (ключ), created_at, updated_at (служебные).
LEAD_COLUMNS: frozenset[str] = frozenset({
    "name", "whatsapp_name", "source", "status", "interest", "age", "profession",
    "is_single", "city", "country", "tags", "mode", "do_not_contact",
    "escalate_reason", "next_followup_at", "followup_sent_count", "manual_until",
    "last_inbound_at", "last_ai_message_at", "last_human_message_at",
    "last_message_at", "last_intent", "calendar_link", "notes", "budget_signal",
    "objection_count", "last_objection_type", "source_campaign", "funnel_stage",
    "imported_at", "import_batch_id", "extra_data", "photo_received",
    "escort_mention_count", "last_name", "email", "date_of_birth",
    "marital_status", "business_link", "desired_partner_age", "selected_service",
    "invitation_sent_at",
})


# ===== Управление пулом =====

async def _init_connection(conn: asyncpg.Connection) -> None:
    """Настройка каждого соединения в пуле: jsonb/json как dict (а не str)."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
    )


async def init_pool() -> None:
    """Создать глобальный пул. Вызывать один раз при старте приложения.

    DSN должен содержать sslmode=require (строка из Supabase → Connect → Session pooler).
    """
    global _pool
    if _pool is not None:
        return
    dsn = settings.supabase_db_dsn
    if not dsn:
        raise RuntimeError("SUPABASE_DB_DSN не задан в .env")
    # Lock + повторная проверка: два конкурентных вызова не создадут два пула.
    async with _pool_lock:
        if _pool is not None:
            return
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
            init=_init_connection,
        )
    logger.info("DB pool created (min=1, max=10)")


async def close_pool() -> None:
    """Закрыть пул при остановке приложения. Безопасно вызывать если пула нет."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")


def _get_pool() -> asyncpg.Pool:
    """Вернуть активный пул или упасть с понятной ошибкой."""
    if _pool is None:
        raise RuntimeError("DB pool не инициализирован — вызови init_pool()")
    return _pool


def is_ready() -> bool:
    """Готова ли БД (пул создан). Позволяет обработчику не падать без БД."""
    return _pool is not None


def _validate_columns(fields: dict) -> None:
    """Проверить, что все имена колонок из whitelist (защита от инъекции в имя)."""
    unknown = set(fields) - LEAD_COLUMNS
    if unknown:
        raise ValueError(f"Недопустимые колонки leads: {sorted(unknown)}")


# ===== Запросы =====

async def get_lead_by_phone(phone: str) -> dict | None:
    """Вернуть лида по телефону (бизнес-ключ) или None."""
    try:
        row = await _get_pool().fetchrow("SELECT * FROM leads WHERE phone = $1", phone)
    except Exception:
        logger.exception("get_lead_by_phone failed: phone=%s", phone)
        raise
    return dict(row) if row else None


async def upsert_lead(phone: str, **fields) -> dict:
    """INSERT нового лида или UPDATE существующего по phone.

    ON CONFLICT (phone) — работает благодаря UNIQUE-констрейнту leads_phone_key.
    При конфликте обновляются только переданные поля (+ updated_at). Возвращает строку.
    """
    _validate_columns(fields)
    cols = list(fields.keys())
    insert_cols = ["phone"] + cols
    placeholders = [f"${i}" for i in range(1, len(insert_cols) + 1)]
    values = [phone] + [fields[c] for c in cols]

    if cols:
        # EXCLUDED.<col> — значение, которое пытались вставить (не дублируем параметры).
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols) + ", updated_at = now()"
    else:
        update_set = "updated_at = now()"

    sql = (
        f"INSERT INTO leads ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (phone) DO UPDATE SET {update_set} "
        f"RETURNING *"
    )
    try:
        row = await _get_pool().fetchrow(sql, *values)
    except Exception:
        logger.exception("upsert_lead failed: phone=%s fields=%s", phone, list(fields))
        raise
    return dict(row)


async def insert_message(
    lead_phone: str,
    direction: str,
    sender: str,
    text: str,
    *,
    external_message_id: str | None = None,
    message_uid: str | None = None,
    meta: dict | None = None,
    processed: bool = False,
) -> bool:
    """Вставить сообщение идемпотентно. Вернуть True если вставлено, False если дубль.

    Идемпотентность:
      - external_message_id задан → ON CONFLICT (lead_phone, external_message_id) DO NOTHING
        (составной UNIQUE messages_external_uid; lead_phone у нас всегда есть)
      - иначе message_uid задан    → ON CONFLICT (message_uid) DO NOTHING (idx_messages_uid_unique)
      - иначе                      → обычный INSERT
    Дубль определяется по отсутствию RETURNING-строки (DO NOTHING не вернёт id).
    """
    cols = ["lead_phone", "direction", "sender", "text", "processed"]
    values: list = [lead_phone, direction, sender, text, processed]

    if external_message_id is not None:
        cols.append("external_message_id")
        values.append(external_message_id)
    if message_uid is not None:
        cols.append("message_uid")
        values.append(message_uid)
    if meta is not None:
        cols.append("meta")
        values.append(meta)  # dict → jsonb (кодек в _init_connection)

    placeholders = [f"${i}" for i in range(1, len(values) + 1)]

    if external_message_id is not None:
        conflict = "ON CONFLICT (lead_phone, external_message_id) DO NOTHING"
    elif message_uid is not None:
        conflict = "ON CONFLICT (message_uid) DO NOTHING"
    else:
        conflict = ""

    sql = (
        f"INSERT INTO messages ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"{conflict} "
        f"RETURNING id"
    )
    try:
        row = await _get_pool().fetchrow(sql, *values)
    except UniqueViolationError:
        # Страховка: конфликт по ДРУГОМУ UNIQUE, чем нацелен ON CONFLICT.
        # Например заданы оба ID: таргет — (lead_phone, external_message_id),
        # а дублируется message_uid по отдельному индексу. Это тоже дубль → False.
        logger.info(
            "insert_message: дубль по другому UNIQUE (lead_phone=%s ext_id=%s uid=%s)",
            lead_phone, external_message_id, message_uid,
        )
        return False
    except Exception:
        logger.exception(
            "insert_message failed: lead_phone=%s ext_id=%s uid=%s",
            lead_phone, external_message_id, message_uid,
        )
        raise
    return row is not None


async def get_conversation_history(phone: str, limit: int = 30) -> list[dict]:
    """Вернуть историю переписки лида в хронологическом порядке (старые → новые)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT * FROM messages WHERE lead_phone = $1 ORDER BY created_at ASC LIMIT $2",
            phone,
            limit,
        )
    except Exception:
        logger.exception("get_conversation_history failed: phone=%s limit=%s", phone, limit)
        raise
    return [dict(r) for r in rows]


async def update_lead_fields(phone: str, **fields) -> dict | None:
    """Обновить только переданные поля лида (+ updated_at). Вернуть строку или None.

    Имена колонок — только из whitelist. Пустой fields → просто вернуть текущего
    лида (без пустого SET). None если лида нет.
    """
    if not fields:
        return await get_lead_by_phone(phone)

    _validate_columns(fields)
    cols = list(fields.keys())
    set_parts = [f"{c} = ${i}" for i, c in enumerate(cols, start=1)]
    set_clause = ", ".join(set_parts) + ", updated_at = now()"
    phone_param = f"${len(cols) + 1}"
    values = [fields[c] for c in cols] + [phone]

    sql = f"UPDATE leads SET {set_clause} WHERE phone = {phone_param} RETURNING *"
    try:
        row = await _get_pool().fetchrow(sql, *values)
    except Exception:
        logger.exception("update_lead_fields failed: phone=%s fields=%s", phone, list(fields))
        raise
    return dict(row) if row else None


async def get_unprocessed_inbound(phone: str) -> list[dict]:
    """Непроцессенные входящие сообщения лида (для склейки залпа на флаше debounce)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT * FROM messages "
            "WHERE lead_phone = $1 AND direction = 'inbound' AND processed = false "
            "ORDER BY created_at ASC",
            phone,
        )
    except Exception:
        logger.exception("get_unprocessed_inbound failed: phone=%s", phone)
        raise
    return [dict(r) for r in rows]


async def mark_messages_processed(ids: list) -> int:
    """Пометить сообщения processed=true, processed_at=now(). Вернуть число обновлённых."""
    if not ids:
        return 0
    try:
        rows = await _get_pool().fetch(
            "UPDATE messages SET processed = true, processed_at = now() "
            "WHERE id = ANY($1::uuid[]) RETURNING id",
            ids,
        )
    except Exception:
        logger.exception("mark_messages_processed failed: ids=%s", ids)
        raise
    return len(rows)


async def block_lead(phone: str, reason: str, escort: bool = False) -> None:
    """Заблокировать лида навсегда И перевести в 'lost' — атомарно (одна транзакция).

    Как WF1: manual_until = now()+100 лет (в auto бот не вернётся). Для escort
    инкрементим escort_mention_count (счёт с первого упоминания — баг WF1 исправлен).
    Стадию 'lost' и запись в funnel_events делаем ЗДЕСЬ же, чтобы не было десинхрона
    (заблокирован, но стадия не сменилась). next_followup_at обнуляем (не догоняем).
    """
    escort_sql = (
        "escort_mention_count = COALESCE(escort_mention_count, 0) + 1, " if escort else ""
    )
    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT funnel_stage FROM leads WHERE phone = $1 FOR UPDATE", phone
                )
                if current is None:
                    logger.warning("block_lead: лид %s не найден", phone)
                    return
                await conn.execute(
                    "UPDATE leads SET do_not_contact = true, mode = 'manual', "
                    "manual_until = now() + interval '100 years', escalate_reason = $2, "
                    f"{escort_sql}"
                    "funnel_stage = 'lost', next_followup_at = NULL, "
                    "updated_at = now() WHERE phone = $1",
                    phone, reason,
                )
                if current != "lost":
                    await conn.execute(
                        "INSERT INTO funnel_events (lead_phone, from_stage, to_stage, meta) "
                        "VALUES ($1, $2, 'lost', $3)",
                        phone, current, {"reason": reason},
                    )
    except Exception:
        logger.exception("block_lead failed: phone=%s reason=%s", phone, reason)
        raise


async def search_scenarios_by_vector(vector_literal: str, top_k: int = 3) -> list[dict]:
    """RAG: top-K активных сценариев по косинусной близости (pgvector <=>).

    vector_literal — эмбеддинг запроса в формате '[..]'. score = 1 - cosine_distance.
    """
    try:
        # trigger_type IS DISTINCT FROM 'scheduled' — исключаем служебные исходящие
        # сценарии (утренние/фоллоу-ап по таймеру), их берёт планировщик отдельно.
        rows = await _get_pool().fetch(
            "SELECT id, template_es, mode, ai_allowed, blocks_lead, "
            "1 - (embedding <=> $1::vector) AS score "
            "FROM scenarios WHERE embedding IS NOT NULL AND is_active = true "
            "AND trigger_type IS DISTINCT FROM 'scheduled' "
            "ORDER BY embedding <=> $1::vector LIMIT $2",
            vector_literal, top_k,
        )
    except Exception:
        logger.exception("search_scenarios_by_vector failed")
        raise
    return [dict(r) for r in rows]


async def save_outbound(lead_phone: str, text: str, sender: str = "anna") -> None:
    """Сохранить исходящее сообщение бота в messages (processed=true, без external_id).

    НЕ бросает: сообщение уже отправлено лиду, потеря записи в БД не критична и не
    должна прерывать отправку остальных бабблов. Ошибку логируем (единственное место).
    """
    try:
        await _get_pool().execute(
            "INSERT INTO messages (lead_phone, direction, sender, text, processed, processed_at) "
            "VALUES ($1, 'outbound', $2, $3, true, now())",
            lead_phone, sender, text,
        )
    except Exception:
        logger.exception("save_outbound failed (сообщение отправлено, запись не сохранена): "
                         "lead_phone=%s", lead_phone)


async def save_photo(lead_phone: str, storage_url: str | None, storage_path: str | None,
                     verdict: str, analysis: dict | None = None,
                     reasons: list | None = None, channel: str = "whatsapp") -> None:
    """Сохранить фото + результат Vision в lead_photos. is_primary если первое фото лида.

    Транзакция с FOR UPDATE на лида: сериализует конкурентные save_photo одного номера,
    иначе два одновременных фото могли бы оба стать is_primary (гонка NOT EXISTS).
    """
    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # блокируем строку лида — сериализуем вычисление is_primary
                await conn.execute("SELECT 1 FROM leads WHERE phone = $1 FOR UPDATE", lead_phone)
                has_primary = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM lead_photos WHERE lead_phone = $1 AND is_primary = true)",
                    lead_phone,
                )
                await conn.execute(
                    "INSERT INTO lead_photos "
                    "(lead_phone, channel, storage_url, storage_path, vision_analyzed, "
                    " vision_analysis, vision_verdict, vision_reasons, received_at, analyzed_at, is_primary) "
                    "VALUES ($1, $2, $3, $4, true, $5, $6, $7, now(), now(), $8)",
                    lead_phone, channel, storage_url, storage_path,
                    analysis or {}, verdict, reasons or [], not has_primary,
                )
    except Exception:
        logger.exception("save_photo failed: lead_phone=%s", lead_phone)
        raise


async def mark_photo_received(phone: str, received: bool) -> None:
    """Отметить leads.photo_received (флаг «фото прислано и одобрено»)."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET photo_received = $2, updated_at = now() WHERE phone = $1",
            phone, received,
        )
    except Exception:
        logger.exception("mark_photo_received failed: phone=%s", phone)
        raise


async def count_recent_photos(phone: str, hours: int = 1) -> int:
    """Сколько фото прислал лид за последние N часов (флуд-защита)."""
    try:
        return await _get_pool().fetchval(
            "SELECT count(*) FROM lead_photos "
            "WHERE lead_phone = $1 AND received_at > now() - make_interval(hours => $2)",
            phone, hours,
        )
    except Exception:
        logger.exception("count_recent_photos failed: phone=%s", phone)
        raise


async def get_scenario_title(scenario_id) -> str | None:
    """Заголовок сценария по id (для reason при блокировке). None если нет/невалидный id."""
    if not isinstance(scenario_id, int):
        return None
    try:
        return await _get_pool().fetchval("SELECT title FROM scenarios WHERE id = $1", scenario_id)
    except Exception:
        logger.exception("get_scenario_title failed: id=%s", scenario_id)
        return None


async def get_scenario_template(scenario_id) -> str | None:
    """template_es сценария по id (для фото retry/reject — детерминированный текст)."""
    if not isinstance(scenario_id, int):
        return None
    try:
        return await _get_pool().fetchval("SELECT template_es FROM scenarios WHERE id = $1", scenario_id)
    except Exception:
        logger.exception("get_scenario_template failed: id=%s", scenario_id)
        return None


async def is_whitelisted(phone: str) -> bool:
    """Есть ли номер в bot_whitelist (бот для него молчит)."""
    try:
        row = await _get_pool().fetchrow("SELECT 1 FROM bot_whitelist WHERE phone = $1", phone)
    except Exception:
        logger.exception("is_whitelisted failed: phone=%s", phone)
        raise
    return row is not None


async def touch_last_inbound(phone: str) -> None:
    """Обновить last_inbound_at=now() (метка для фоллоу-апов)."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET last_inbound_at = now(), updated_at = now() WHERE phone = $1",
            phone,
        )
    except Exception:
        logger.exception("touch_last_inbound failed: phone=%s", phone)
        raise


async def set_funnel_stage(phone: str, to_stage: str, meta: dict | None = None) -> bool:
    """Сменить стадию воронки лида атомарно (в одной транзакции).

    Если стадия РЕАЛЬНО меняется:
      - UPDATE leads.funnel_stage
      - INSERT funnel_events(from_stage, to_stage, meta)
      - проставить next_followup_at по funnel.FOLLOWUP_FIRST_DELAY_HOURS
        (или NULL для NO_FOLLOWUP_STAGES) и сбросить followup_sent_count=0
    Если стадия та же — ничего не делаем (не плодим funnel_events). Вернуть: менялось ли.
    """
    if to_stage not in funnel.FUNNEL_STAGES:
        raise ValueError(f"Неизвестная стадия воронки: {to_stage!r}")

    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT funnel_stage FROM leads WHERE phone = $1 FOR UPDATE", phone
                )
                if current is None:
                    logger.warning("set_funnel_stage: лид %s не найден", phone)
                    return False
                if current == to_stage:
                    return False  # без изменения — не логируем событие

                # next_followup_at: для no-followup стадий NULL, иначе now()+delay
                if to_stage in funnel.NO_FOLLOWUP_STAGES:
                    await conn.execute(
                        "UPDATE leads SET funnel_stage = $1, next_followup_at = NULL, "
                        "followup_sent_count = 0, updated_at = now() WHERE phone = $2",
                        to_stage, phone,
                    )
                else:
                    hours = funnel.FOLLOWUP_FIRST_DELAY_HOURS.get(to_stage)
                    if hours is not None:
                        await conn.execute(
                            "UPDATE leads SET funnel_stage = $1, "
                            "next_followup_at = now() + make_interval(hours => $2), "
                            "followup_sent_count = 0, updated_at = now() WHERE phone = $3",
                            to_stage, hours, phone,
                        )
                    else:
                        # активная стадия без расписания догона (напр. new/qualifying)
                        await conn.execute(
                            "UPDATE leads SET funnel_stage = $1, updated_at = now() WHERE phone = $2",
                            to_stage, phone,
                        )

                await conn.execute(
                    "INSERT INTO funnel_events (lead_phone, from_stage, to_stage, meta) "
                    "VALUES ($1, $2, $3, $4)",
                    phone, current, to_stage, meta or {},
                )
        return True
    except Exception:
        logger.exception("set_funnel_stage failed: phone=%s to=%s", phone, to_stage)
        raise
