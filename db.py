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
