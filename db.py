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
import re
from datetime import datetime, timezone

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
    "invitation_sent_at", "videocall_at", "videocall_reminded_at",
    "videocall_event_id",
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


async def update_message_text(message_id, text: str) -> None:
    """Обновить текст сообщения по id (для транскрипта голосового вместо плейсхолдера).

    Не критично для ответа текущего залпа (combined уже несёт транскрипт), но чинит
    историю: следующий вызов увидит реальный текст, а не '[voice message]'.
    """
    try:
        await _get_pool().execute(
            "UPDATE messages SET text = $1 WHERE id = $2", text, message_id,
        )
    except Exception:
        # Не роняем обработку залпа из-за косметики истории — только лог.
        logger.exception("update_message_text failed: id=%s", message_id)


async def phones_with_unprocessed_inbound() -> list[str]:
    """Уникальные номера с непроцессенными входящими (для startup-sweep после рестарта).

    debounce-таймеры живут в памяти и теряются при рестарте — эти лиды иначе зависнут
    с processed=false навсегда. На старте прогоняем их через debounce заново.
    """
    try:
        rows = await _get_pool().fetch(
            "SELECT DISTINCT lead_phone FROM messages "
            "WHERE direction = 'inbound' AND processed = false"
        )
    except Exception:
        logger.exception("phones_with_unprocessed_inbound failed")
        raise
    return [r["lead_phone"] for r in rows]


# ===== Планировщик и настройки ивента (блок 13) =====

async def get_setting(key: str) -> str | None:
    """Значение из app_settings по ключу (или None)."""
    try:
        return await _get_pool().fetchval("SELECT value FROM app_settings WHERE key = $1", key)
    except Exception:
        logger.exception("get_setting failed: key=%s", key)
        raise


async def get_settings(keys: list[str]) -> dict:
    """Bulk: {key: value} для набора ключей (отсутствующие — не в словаре)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])", keys)
    except Exception:
        logger.exception("get_settings failed: keys=%s", keys)
        raise
    return {r["key"]: r["value"] for r in rows}


async def set_setting(key: str, value: str) -> None:
    """Записать app_settings (upsert)."""
    try:
        await _get_pool().execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ($1, $2, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            key, value,
        )
    except Exception:
        logger.exception("set_setting failed: key=%s", key)
        raise
    logger.info("app_settings: %s = %r", key, value)


async def due_followups(no_followup_stages: list[str], max_followups: int,
                        quiet_hours: int, limit: int = 50) -> list[dict]:
    """Лиды, которым пора фоллоу-ап: next_followup_at<=now, auto, не do_not_contact,
    стадия не в no_followup_stages, попыток < max, НЕ в whitelist. Свежие сверху по сроку.

    Страховка от нуджа активного лида: НЕ шлём, если last_inbound_at свежее «тихого окна»
    (лид писал за последние quiet_hours часов — значит не молчун). Двойная защита к сбросу
    таймера на входящем (main.reset_followup_timer)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT l.phone, l.funnel_stage, COALESCE(l.followup_sent_count, 0) AS followup_sent_count, "
            "       l.whatsapp_name, l.name, "
            # анкета-сигналы + звонок + теги → для стадийного выбора догона (funnel.followup_scenario_for)
            "       l.email, l.date_of_birth, l.country, l.desired_partner_age, l.videocall_at, l.tags "
            "FROM leads l LEFT JOIN bot_whitelist w ON w.phone = l.phone "
            "WHERE l.next_followup_at IS NOT NULL AND l.next_followup_at <= now() "
            "  AND l.mode = 'auto' AND COALESCE(l.do_not_contact, false) = false "
            "  AND l.funnel_stage <> ALL($1::text[]) "
            "  AND COALESCE(l.followup_sent_count, 0) < $2 "
            "  AND (l.last_inbound_at IS NULL OR l.last_inbound_at <= now() - make_interval(hours => $3)) "
            "  AND w.phone IS NULL "
            "ORDER BY l.next_followup_at ASC LIMIT $4",
            no_followup_stages, max_followups, quiet_hours, limit,
        )
    except Exception:
        logger.exception("due_followups failed")
        raise
    return [dict(r) for r in rows]


async def anketa_saved(phone: str) -> bool:
    """Записана ли уже анкета лида в Google Sheet (дедуп: extra_data.anketa_saved).

    Сбой → False (лучше записать повторно, чем потерять; но обычно дедуп сработает)."""
    try:
        v = await _get_pool().fetchval(
            "SELECT COALESCE((extra_data->>'anketa_saved')::boolean, false) "
            "FROM leads WHERE phone = $1", phone)
        return bool(v)
    except Exception:
        logger.exception("anketa_saved failed: phone=%s", phone)
        return False


async def mark_anketa_saved(phone: str) -> None:
    """Пометить, что анкета лида записана в Sheet (extra_data.anketa_saved=true)."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET extra_data = COALESCE(extra_data, '{}'::jsonb) "
            "|| '{\"anketa_saved\": true}'::jsonb, updated_at = now() WHERE phone = $1",
            phone)
    except Exception:
        logger.exception("mark_anketa_saved failed: phone=%s", phone)
        raise


async def arm_followup_if_missing(phone: str, hours: int) -> None:
    """Взвести первый таймер догона, ЕСЛИ он ещё не стоит (next_followup_at IS NULL).

    Нужно для лидов, застрявших на 'new' (стадия проставляется дефолтом при INSERT, не через
    set_funnel_stage, поэтому таймер не ставится). Зовём после ответа бота. Существующий
    таймер (например от смены стадии) НЕ трогаем — не сбиваем расписание догонов."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET next_followup_at = now() + make_interval(hours => $2), "
            "followup_sent_count = 0, updated_at = now() "
            "WHERE phone = $1 AND next_followup_at IS NULL",
            phone, hours,
        )
    except Exception:
        logger.exception("arm_followup_if_missing failed: phone=%s", phone)
        raise


async def reset_followup_timer(phone: str, hours: int) -> None:
    """Сброс таймера тишины на ЛЮБОМ входящем от лида (он активен → не нудим).

    В отличие от arm_followup_if_missing (ставит только если NULL) — ставит ВСЕГДА:
    next_followup_at = now()+hours, followup_sent_count=0 (свежий отсчёт молчания).
    Так догон = «N дней РЕАЛЬНОГО молчания», а не «N дней с одной установки»."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET next_followup_at = now() + make_interval(hours => $2), "
            "followup_sent_count = 0, updated_at = now() WHERE phone = $1",
            phone, hours,
        )
    except Exception:
        logger.exception("reset_followup_timer failed: phone=%s", phone)
        raise


# ===== Суточные счётчики (антибан-лимит рассылки) — в app_settings, ключ по дате UTC =====

def _daily_counter_key(name: str) -> str:
    return f"counter:{name}:{datetime.now(timezone.utc).date().isoformat()}"


async def get_daily_counter(name: str) -> int:
    """Сколько отправлено сегодня (UTC) по счётчику name. Нет ключа → 0."""
    v = await get_setting(_daily_counter_key(name))
    return int(v) if v and v.strip().isdigit() else 0


async def incr_daily_counter(name: str) -> int:
    """Инкремент сегодняшнего счётчика name (+1). Вернуть новое значение. Персистентно
    в app_settings (переживает рестарт), ключ по дате → авто-сброс на новые сутки."""
    new = await get_daily_counter(name) + 1
    await set_setting(_daily_counter_key(name), str(new))
    return new


async def mark_followup_sent(phone: str, next_followup_at) -> None:
    """Инкремент followup_sent_count + новый next_followup_at (или None → NULL, финиш)."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET followup_sent_count = COALESCE(followup_sent_count, 0) + 1, "
            "next_followup_at = $2, updated_at = now() WHERE phone = $1",
            phone, next_followup_at,
        )
    except Exception:
        logger.exception("mark_followup_sent failed: phone=%s", phone)
        raise


async def event_recipients(exclude_stages: list[str], limit: int = 30) -> list[dict]:
    """Кому слать напоминания об ивенте: selected_service='event', mode='auto',
    не do_not_contact, стадия не в exclude_stages, НЕ в whitelist (клиенты агентства
    исключаются полностью — их ведёт Аня напрямую). LIMIT — антибан-порция (остаток
    догонит след. тик; идемпотентность не даст дублей). mode='auto' — не пишем тем,
    кого Аня ведёт вручную (/takeover). funnel_stage — для выбора шаблона в день ивента
    (оплатившие событие/члены → без ссылки, остальные → со ссылкой)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT l.phone, l.whatsapp_name, l.name, l.funnel_stage "
            "FROM leads l LEFT JOIN bot_whitelist w ON w.phone = l.phone "
            "WHERE l.selected_service = 'event' "
            "  AND l.mode = 'auto' "
            "  AND COALESCE(l.do_not_contact, false) = false "
            "  AND l.funnel_stage <> ALL($1::text[]) "
            "  AND w.phone IS NULL "
            "LIMIT $2",
            exclude_stages, limit,
        )
    except Exception:
        logger.exception("event_recipients failed")
        raise
    return [dict(r) for r in rows]


async def event_reminder_sent(phone: str, kind: str, event_date: str) -> bool:
    """Уже слали это напоминание (kind) на эту дату ивента? Идемпотентность через events."""
    try:
        row = await _get_pool().fetchrow(
            "SELECT 1 FROM events WHERE lead_phone = $1 AND event_type = $2 "
            "AND meta->>'event_date' = $3",
            phone, kind, event_date,
        )
    except Exception:
        logger.exception("event_reminder_sent failed: phone=%s kind=%s", phone, kind)
        raise
    return row is not None


async def log_event_reminder(phone: str, kind: str, event_date: str) -> None:
    """Отметить отправку напоминания (kind) на дату ивента — маркер идемпотентности."""
    try:
        await _get_pool().execute(
            "INSERT INTO events (lead_phone, event_type, meta) "
            "VALUES ($1, $2, jsonb_build_object('event_date', $3::text))",
            phone, kind, event_date,
        )
    except Exception:
        logger.exception("log_event_reminder failed: phone=%s kind=%s", phone, kind)
        raise


async def event_reminder_sent_at(phone: str, kind: str, event_date: str):
    """Когда слали это напоминание (kind) на эту дату — created_at маркера или None.

    Для предупреждения о дубле в CRM («уже отправлено такого-то числа»)."""
    try:
        row = await _get_pool().fetchrow(
            "SELECT created_at FROM events WHERE lead_phone = $1 AND event_type = $2 "
            "AND meta->>'event_date' = $3 ORDER BY created_at LIMIT 1",
            phone, kind, event_date,
        )
    except Exception:
        logger.exception("event_reminder_sent_at failed: phone=%s kind=%s", phone, kind)
        raise
    return row["created_at"] if row else None


async def event_lead_candidates(limit: int = 200) -> list[dict]:
    """Кандидаты для ручного напоминания дня ивента: selected_service='event' и бот
    им пишет (не do_not_contact). БЕЗ фильтров авто-рассылки (mode/стадия) — Аня
    выбирает сама; whitelist-клиенты сюда не попадают (у них нет selected_service='event').
    """
    try:
        rows = await _get_pool().fetch(
            "SELECT phone, whatsapp_name, name, funnel_stage FROM leads "
            "WHERE selected_service = 'event' AND COALESCE(do_not_contact, false) = false "
            "ORDER BY funnel_stage, name LIMIT $1",
            limit,
        )
    except Exception:
        logger.exception("event_lead_candidates failed")
        raise
    return [dict(r) for r in rows]


async def set_videocall_at(phone: str, when) -> None:
    """Назначить/перенести время видеозвонка лида (сценарий 49).

    Сбрасываем videocall_reminded_at → NULL, чтобы напоминание за 2ч ушло заново
    на новое время (при переносе). when=None снимает назначение.
    """
    try:
        await _get_pool().execute(
            "UPDATE leads SET videocall_at = $2, videocall_reminded_at = NULL, "
            "updated_at = now() WHERE phone = $1",
            phone, when,
        )
    except Exception:
        logger.exception("set_videocall_at failed: phone=%s", phone)
        raise


async def set_videocall_booking(phone: str, when, event_id: str, link: str,
                                conn=None) -> None:
    """Сохранить автозабронированный звонок: время + id события Google + ссылка на событие.

    videocall_event_id нужен для переноса/отмены (patch/delete). calendar_link — ссылка
    на само событие в Google Calendar (для Ани; лиду Meet шлёт она вручную).
    videocall_reminded_at сбрасываем → напоминание #49 уйдёт на новое время. conn — опц.
    соединение (когда вызывается внутри транзакции с advisory-lock от гонки).
    """
    executor = conn or _get_pool()
    try:
        await executor.execute(
            "UPDATE leads SET videocall_at = $2, videocall_event_id = $3, "
            "calendar_link = $4, videocall_reminded_at = NULL, updated_at = now() "
            "WHERE phone = $1",
            phone, when, event_id, link,
        )
    except Exception:
        logger.exception("set_videocall_booking failed: phone=%s", phone)
        raise


async def due_videocall_reminders(window_start, window_end, limit: int = 30) -> list[dict]:
    """Лиды, которым пора напомнить о звонке: videocall_at в окне [start, end],
    ещё не напоминали, mode='auto', не do_not_contact, НЕ в whitelist.

    Окно задаёт планировщик (≈ [now+1.5ч, now+2.5ч]) — попадание в «за 2 часа»
    с допуском на дискретность тика. Идемпотентность — по videocall_reminded_at.
    """
    try:
        rows = await _get_pool().fetch(
            "SELECT l.phone, l.whatsapp_name, l.name, l.videocall_at "
            "FROM leads l LEFT JOIN bot_whitelist w ON w.phone = l.phone "
            "WHERE l.videocall_at IS NOT NULL AND l.videocall_reminded_at IS NULL "
            "  AND l.videocall_at >= $1 AND l.videocall_at <= $2 "
            "  AND l.mode = 'auto' AND COALESCE(l.do_not_contact, false) = false "
            "  AND w.phone IS NULL "
            "ORDER BY l.videocall_at ASC LIMIT $3",
            window_start, window_end, limit,
        )
    except Exception:
        logger.exception("due_videocall_reminders failed")
        raise
    return [dict(r) for r in rows]


async def mark_videocall_reminded(phone: str) -> None:
    """Отметить, что напоминание о звонке отправлено (идемпотентность — не дублируем)."""
    try:
        await _get_pool().execute(
            "UPDATE leads SET videocall_reminded_at = now(), updated_at = now() "
            "WHERE phone = $1",
            phone,
        )
    except Exception:
        logger.exception("mark_videocall_reminded failed: phone=%s", phone)
        raise


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


async def link_already_sent(lead_phone: str, url: str) -> bool:
    """Слали ли уже этому лиду исходящее сообщение с этой ссылкой (дедуп ссылок, Layer 2).

    url в messages хранится уже подставленным (sender пишет финальный текст).
    Пустой url → False. Сбой → False (лучше отправить, чем молчать по ошибке БД).
    """
    if not url or not url.strip():
        return False
    try:
        row = await _get_pool().fetchrow(
            "SELECT 1 FROM messages "
            "WHERE lead_phone = $1 AND direction = 'outbound' "
            "AND text LIKE '%' || $2 || '%' LIMIT 1",
            lead_phone, url.strip(),
        )
        return row is not None
    except Exception:
        logger.exception("link_already_sent failed [%s] — считаю не отправленной", lead_phone)
        return False


# ===== Медиа с ивентов (event_media) — фото/видео для отправки ботом =====

async def add_event_media(storage_url: str, storage_path: str, media_type: str,
                          size_bytes: int) -> dict:
    """Добавить медиа с ивента (после загрузки в Storage). Вернуть строку."""
    try:
        row = await _get_pool().fetchrow(
            "INSERT INTO event_media (storage_url, storage_path, media_type, size_bytes) "
            "VALUES ($1, $2, $3, $4) RETURNING *",
            storage_url, storage_path, media_type, size_bytes,
        )
        return dict(row)
    except Exception:
        logger.exception("add_event_media failed")
        raise


async def list_event_media() -> list[dict]:
    """Все медиа с ивентов (для CRM), свежие сверху."""
    try:
        rows = await _get_pool().fetch(
            "SELECT id, storage_url, storage_path, media_type, size_bytes, is_active, created_at "
            "FROM event_media ORDER BY created_at DESC")
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("list_event_media failed")
        raise


async def delete_event_media(media_id: int) -> bool:
    """Удалить медиа по id. Вернуть True если удалили."""
    try:
        res = await _get_pool().execute("DELETE FROM event_media WHERE id = $1", media_id)
        return res.endswith(" 1")
    except Exception:
        logger.exception("delete_event_media failed: id=%s", media_id)
        raise


# Маркеры отправленного медиа в messages — для пер-типового дедупа (не повторяем тип).
# Пишет sender.send_media, читает event_media_sent.
MEDIA_MARKERS = {"image": "[foto ивента отправлено]", "video": "[video ивента отправлено]"}


async def event_media_sent(lead_phone: str, media_type: str) -> bool:
    """Слали ли уже этому лиду медиа этого типа (дедуп по типу — не повторяем).

    Ищем типовой маркер в исходящих. Сбой → False (лучше отправить, чем молчать по ошибке)."""
    marker = MEDIA_MARKERS.get(media_type)
    if not marker:
        return False
    try:
        row = await _get_pool().fetchrow(
            "SELECT 1 FROM messages WHERE lead_phone = $1 AND direction = 'outbound' "
            "AND text = $2 LIMIT 1", lead_phone, marker)
        return row is not None
    except Exception:
        logger.exception("event_media_sent failed [%s] type=%s", lead_phone, media_type)
        return False


async def random_event_media(media_type: str, limit: int) -> list[dict]:
    """Активные медиа заданного типа: ОСНОВНОЕ (is_primary) первым, остальные случайно.

    Для видео так гарантируем, что explainer-видео Ани всегда идёт на детальный вопрос
    про ивент (а не случайный клип, если позже добавят атмосферные). Для фото is_primary
    обычно false → чистый random. Пусто если медиа типа нет.
    """
    try:
        rows = await _get_pool().fetch(
            "SELECT storage_url, media_type FROM event_media "
            "WHERE is_active = true AND media_type = $1 "
            "ORDER BY is_primary DESC, random() LIMIT $2",
            media_type, limit)
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("random_event_media failed")
        return []


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


async def get_scenario_row(scenario_id) -> dict | None:
    """Строка сценария по id (для роутинга: холодный лид + №51 → крючок №2)."""
    if not isinstance(scenario_id, int):
        return None
    try:
        r = await _get_pool().fetchrow(
            "SELECT id, template_es, mode, ai_allowed, blocks_lead FROM scenarios WHERE id = $1",
            scenario_id)
    except Exception:
        logger.exception("get_scenario_row failed: id=%s", scenario_id)
        return None
    return dict(r) if r else None


async def is_whitelisted(phone: str) -> bool:
    """Есть ли номер в bot_whitelist (бот для него молчит)."""
    try:
        row = await _get_pool().fetchrow("SELECT 1 FROM bot_whitelist WHERE phone = $1", phone)
    except Exception:
        logger.exception("is_whitelisted failed: phone=%s", phone)
        raise
    return row is not None


def _wa_phone(phone: str) -> str:
    """Нормализовать телефон в бизнес-ключ 'wa_<digits>' (как в normalize.py).

    Принимает любой формат: '+7 963 537-88-80', '79635378880', 'wa_79635378880'.
    Убираем все нецифры (буквы 'wa' тоже) → префикс 'wa_'. Так add/remove пишут
    ровно тот ключ, по которому ищет is_whitelisted. Пустой/бесцифровой вход →
    ValueError (иначе в whitelist попадёт мусорный ключ 'wa_', который ни с чем
    не совпадёт — вызывающий код должен узнать об ошибке).
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise ValueError(f"Невозможно нормализовать телефон: {phone!r}")
    return "wa_" + digits


async def add_to_whitelist(phone: str, reason: str, added_by: str) -> None:
    """Добавить номер в bot_whitelist (бот замолчит, VIP ведёт Аня лично).

    Повторное добавление того же номера не падает — обновляет reason/added_by
    (ON CONFLICT). Телефон нормализуется в 'wa_<digits>'.
    """
    key = _wa_phone(phone)
    try:
        await _get_pool().execute(
            "INSERT INTO bot_whitelist (phone, reason, added_by) VALUES ($1, $2, $3) "
            "ON CONFLICT (phone) DO UPDATE SET reason = EXCLUDED.reason, "
            "added_by = EXCLUDED.added_by, added_at = now()",
            key, reason, added_by,
        )
    except Exception:
        logger.exception("add_to_whitelist failed: phone=%s", key)
        raise
    logger.info("whitelist: добавлен %s (by=%s, reason=%s)", key, added_by, reason)


async def remove_from_whitelist(phone: str) -> None:
    """Убрать номер из bot_whitelist (бот снова отвечает). Телефон нормализуется."""
    key = _wa_phone(phone)
    try:
        result = await _get_pool().execute("DELETE FROM bot_whitelist WHERE phone = $1", key)
    except Exception:
        logger.exception("remove_from_whitelist failed: phone=%s", key)
        raise
    # execute() возвращает 'DELETE <n>' — не врём в лог, если удалять было нечего.
    if result == "DELETE 0":
        logger.warning("whitelist: телефон не найден при удалении: %s", key)
    else:
        logger.info("whitelist: удалён %s", key)


async def list_whitelist(limit: int = 100) -> list[dict]:
    """Текущий whitelist (для /whitelist_list менеджер-бота), новые сверху."""
    try:
        rows = await _get_pool().fetch(
            "SELECT phone, reason, added_by, added_at FROM bot_whitelist "
            "ORDER BY added_at DESC LIMIT $1",
            limit,
        )
    except Exception:
        logger.exception("list_whitelist failed")
        raise
    return [dict(r) for r in rows]


# ===== Менеджер-бот: takeover / release / список лидов (блок 11) =====

async def set_manual(phone: str) -> bool:
    """Takeover: mode='manual', manual_until=NULL (бессрочно) — бот молчит, ведёт Аня.

    manual_until=NULL трактуется filters._manual_active как активный бессрочный manual
    (в отличие от block_lead, где ставится now()+100 лет). Возврат — найден ли лид.
    """
    try:
        row = await _get_pool().fetchrow(
            "UPDATE leads SET mode = 'manual', manual_until = NULL, updated_at = now() "
            "WHERE phone = $1 RETURNING phone",
            phone,
        )
    except Exception:
        logger.exception("set_manual failed: phone=%s", phone)
        raise
    found = row is not None
    logger.info("takeover: %s → manual (найден=%s)", phone, found)
    return found


async def set_auto(phone: str) -> bool:
    """Release: mode='auto', manual_until=NULL — бот снова отвечает. Возврат — найден ли лид."""
    try:
        row = await _get_pool().fetchrow(
            "UPDATE leads SET mode = 'auto', manual_until = NULL, updated_at = now() "
            "WHERE phone = $1 RETURNING phone",
            phone,
        )
    except Exception:
        logger.exception("set_auto failed: phone=%s", phone)
        raise
    found = row is not None
    logger.info("release: %s → auto (найден=%s)", phone, found)
    return found


async def list_active_leads(limit: int = 15, stage: str | None = None) -> list[dict]:
    """Активные лиды для /leads: свежие сверху (по last_inbound_at). Опц. фильтр по стадии.

    Без stage — все стадии из funnel.ACTIVE_STAGES. NULLS LAST: у кого нет времени
    входящего — в конце.
    """
    try:
        if stage:
            rows = await _get_pool().fetch(
                "SELECT phone, whatsapp_name, name, funnel_stage, mode, last_inbound_at "
                "FROM leads WHERE funnel_stage = $1 "
                "ORDER BY last_inbound_at DESC NULLS LAST LIMIT $2",
                stage, limit,
            )
        else:
            rows = await _get_pool().fetch(
                "SELECT phone, whatsapp_name, name, funnel_stage, mode, last_inbound_at "
                "FROM leads WHERE funnel_stage = ANY($1::text[]) "
                "ORDER BY last_inbound_at DESC NULLS LAST LIMIT $2",
                list(funnel.ACTIVE_STAGES), limit,
            )
    except Exception:
        logger.exception("list_active_leads failed: stage=%s limit=%s", stage, limit)
        raise
    return [dict(r) for r in rows]


async def get_lead_photos(phone: str, limit: int = 10) -> list[dict]:
    """Фото лида (public URL) для карточки менеджер-бота, старые → новые.

    Только строки с непустым storage_url (есть что показать). vision_verdict —
    для подписи под фото (ok/reject/manual).
    """
    try:
        rows = await _get_pool().fetch(
            "SELECT storage_url, vision_verdict, received_at FROM lead_photos "
            "WHERE lead_phone = $1 AND storage_url IS NOT NULL "
            "ORDER BY received_at ASC LIMIT $2",
            phone, limit,
        )
    except Exception:
        logger.exception("get_lead_photos failed: phone=%s", phone)
        raise
    return [dict(r) for r in rows]


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
                        # активная стадия, для которой нет записи в FOLLOWUP_FIRST_DELAY_HOURS
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


# ===== Мини-CRM: список лидов с фильтрами/поиском/пагинацией (/api/mini/leads) =====

# Разрешённые режимы сортировки — защита от инъекции в ORDER BY (значение не может
# идти параметром, поэтому маппим ключ на заранее заданный безопасный SQL-фрагмент).
_LEADS_SORT_SQL: dict[str, str] = {
    "recent": "l.last_message_at DESC NULLS LAST, l.created_at DESC",
    "stage": "l.funnel_stage ASC, l.last_message_at DESC NULLS LAST",
}

# Базовый SELECT листинга (колонки + JOINs), без WHERE/ORDER/LIMIT — общий для
# страницы и экспорта (одинаковая выборка). LATERAL — последнее сообщение лида.
_LEADS_SELECT = (
    "SELECT l.phone, l.whatsapp_name, l.name, l.funnel_stage, l.mode, l.interest, "
    "l.age, l.profession, l.city, l.last_message_at, l.last_inbound_at, "
    "(w.phone IS NOT NULL) AS is_client, "
    "m.text AS last_message_text, m.sender AS last_message_sender, "
    "m.direction AS last_message_direction, m.created_at AS last_message_created_at "
    "FROM leads l "
    "LEFT JOIN bot_whitelist w ON w.phone = l.phone "
    "LEFT JOIN LATERAL ("
    "  SELECT text, sender, direction, created_at FROM messages "
    "  WHERE lead_phone = l.phone ORDER BY created_at DESC LIMIT 1"
    ") m ON true "
)


def _leads_where(stages, mode, interest, since, search) -> tuple[str, list]:
    """Собрать WHERE + args для фильтра лидов (общее для листинга и экспорта).

    Возвращает (where_sql, args). Инъекция-безопасно: значения идут параметрами,
    поиск экранирует LIKE-метасимволы (%, _, \\)."""
    where: list[str] = []
    args: list = []
    if stages:
        args.append(list(stages))
        where.append(f"l.funnel_stage = ANY(${len(args)}::text[])")
    if mode in ("auto", "manual"):
        args.append(mode)
        where.append(f"l.mode = ${len(args)}")
    if interest:
        args.append(interest)
        where.append(f"l.interest = ${len(args)}")
    if since:
        args.append(since)
        where.append(f"l.last_message_at >= ${len(args)}::timestamptz")
    if search and search.strip():
        esc = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{esc}%")
        p = len(args)
        where.append(
            f"(l.name ILIKE ${p} ESCAPE '\\' OR l.whatsapp_name ILIKE ${p} ESCAPE '\\' "
            f"OR l.phone ILIKE ${p} ESCAPE '\\')"
        )
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, args


async def list_leads_page(
    *,
    stages: list[str] | None = None,
    mode: str | None = None,
    interest: str | None = None,
    since: str | None = None,
    search: str | None = None,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Страница лидов для мини-CRM. Возвращает {"leads": [...], "total": int}.

    Фильтры (все опциональны, комбинируются через AND):
      - stages: список кодов funnel_stage (OR внутри списка)
      - mode: 'auto' | 'manual'
      - interest: 'event' | 'agency' | 'both'
      - since: ISO-дата/время — last_message_at >= since
      - search: подстрока по имени/whatsapp_name/телефону (ILIKE)
    sort: 'recent' (по last_message_at, свежие сверху — дефолт) | 'stage'.
    Каждый лид: превью последнего сообщения (LATERAL к messages) и is_client
    (есть ли в bot_whitelist). total — общее число под фильтром (для пагинации).
    """
    where_sql, args = _leads_where(stages, mode, interest, since, search)
    order_sql = _LEADS_SORT_SQL.get(sort, _LEADS_SORT_SQL["recent"])

    limit = max(1, min(int(limit), 100))  # жёсткий потолок страницы
    offset = max(0, int(offset))
    data_args = args + [limit, offset]
    data_sql = _LEADS_SELECT + (
        f"{where_sql} ORDER BY {order_sql} LIMIT ${len(args)+1} OFFSET ${len(args)+2}"
    )
    count_sql = f"SELECT count(*) FROM leads l {where_sql}"

    try:
        pool = _get_pool()
        rows = await pool.fetch(data_sql, *data_args)
        total = await pool.fetchval(count_sql, *args) if args else await pool.fetchval(count_sql)
    except Exception:
        logger.exception(
            "list_leads_page failed: stages=%s mode=%s interest=%s search=%r sort=%s",
            stages, mode, interest, search, sort,
        )
        raise
    return {"leads": [dict(r) for r in rows], "total": int(total or 0)}


async def list_leads_for_export(
    *,
    stages: list[str] | None = None,
    mode: str | None = None,
    interest: str | None = None,
    since: str | None = None,
    search: str | None = None,
    sort: str = "recent",
    limit: int = 10000,
) -> list[dict]:
    """Все лиды под фильтром (без пагинации) для CSV-экспорта. Те же фильтры/колонки,
    что и list_leads_page. Жёсткий потолок limit — защита от гигантской выгрузки."""
    where_sql, args = _leads_where(stages, mode, interest, since, search)
    order_sql = _LEADS_SORT_SQL.get(sort, _LEADS_SORT_SQL["recent"])
    limit = max(1, min(int(limit), 20000))
    sql = _LEADS_SELECT + f"{where_sql} ORDER BY {order_sql} LIMIT ${len(args)+1}"
    try:
        rows = await _get_pool().fetch(sql, *(args + [limit]))
    except Exception:
        logger.exception("list_leads_for_export failed: search=%r", search)
        raise
    return [dict(r) for r in rows]


# ===== Мини-CRM: статистика (/api/mini/stats) — переиспользуем вьюхи БД =====

async def get_funnel_stats() -> list[dict]:
    """Воронка по стадиям из вьюхи v_funnel_stats (агрегаты считает БД).
    Возвращает строки только по непустым стадиям: funnel_stage, total, last_24h, last_7d."""
    try:
        rows = await _get_pool().fetch(
            "SELECT funnel_stage, total, last_24h, last_7d FROM v_funnel_stats"
        )
    except Exception:
        logger.exception("get_funnel_stats failed")
        raise
    return [dict(r) for r in rows]


async def get_lead_counts() -> dict:
    """Счётчики лидов одним запросом: всего / новых сегодня / новых за 7 дней."""
    try:
        row = await _get_pool().fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE created_at >= date_trunc('day', now())) AS today, "
            "count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS week "
            "FROM leads"
        )
    except Exception:
        logger.exception("get_lead_counts failed")
        raise
    return dict(row)


async def get_pending_escalations(limit: int = 50) -> list[dict]:
    """Зависшие эскалации (ждут ответа менеджера) из v_pending_escalations,
    самые срочные сверху (minutes_left по возрастанию)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT phone, whatsapp_name, escalate_reason, minutes_left, last_inbound_at "
            "FROM v_pending_escalations ORDER BY minutes_left ASC NULLS LAST LIMIT $1",
            limit,
        )
    except Exception:
        logger.exception("get_pending_escalations failed")
        raise
    return [dict(r) for r in rows]


async def count_pending_escalations() -> int:
    """Число зависших эскалаций (для счётчика на дашборде)."""
    try:
        n = await _get_pool().fetchval("SELECT count(*) FROM v_pending_escalations")
    except Exception:
        logger.exception("count_pending_escalations failed")
        raise
    return int(n or 0)


# ===== Мини-CRM: экран «Клиенты» (whitelist) (/api/mini/whitelist) =====

async def list_whitelist_with_names(limit: int = 500) -> list[dict]:
    """Whitelist для экрана «Клиенты»: имя лида (если он есть в leads) + причина/
    кто добавил/дата. Новые сверху. bot_whitelist.phone == leads.phone (оба wa_)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT w.phone, w.reason, w.added_by, w.added_at, l.name, l.whatsapp_name "
            "FROM bot_whitelist w LEFT JOIN leads l ON l.phone = w.phone "
            "ORDER BY w.added_at DESC LIMIT $1",
            limit,
        )
    except Exception:
        logger.exception("list_whitelist_with_names failed")
        raise
    return [dict(r) for r in rows]


# ===== Мини-CRM: карточка лида — таймлайн, заметки, действия (/api/mini/lead/*) =====

async def get_funnel_events(phone: str) -> list[dict]:
    """Смены стадий лида для таймлайна (старые → новые). ВНИМАНИЕ: столбец времени
    в funnel_events — changed_at (не created_at)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT id, from_stage, to_stage, meta, changed_at FROM funnel_events "
            "WHERE lead_phone = $1 ORDER BY changed_at ASC",
            phone,
        )
    except Exception:
        logger.exception("get_funnel_events failed: phone=%s", phone)
        raise
    return [dict(r) for r in rows]


async def get_manager_actions(phone: str) -> list[dict]:
    """Действия менеджера по лиду для таймлайна (старые → новые)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT id, action, performed_by, meta, created_at FROM manager_actions "
            "WHERE lead_phone = $1 ORDER BY created_at ASC",
            phone,
        )
    except Exception:
        logger.exception("get_manager_actions failed: phone=%s", phone)
        raise
    return [dict(r) for r in rows]


async def log_manager_action(phone: str, action: str, performed_by: str,
                             meta: dict | None = None) -> None:
    """Записать действие менеджера в manager_actions (для таймлайна и аудита).

    Вызывается из мини-аппа при takeover/release/stop/resume/whitelist — иначе
    системных строк «Взято в работу» и т.п. в истории неоткуда взять (сами
    set_manual/set_auto только меняют mode, ничего не логируя)."""
    try:
        await _get_pool().execute(
            "INSERT INTO manager_actions (lead_phone, action, performed_by, meta) "
            "VALUES ($1, $2, $3, $4)",
            phone, action, performed_by, meta or {},
        )
    except Exception:
        logger.exception("log_manager_action failed: phone=%s action=%s", phone, action)
        raise


async def get_lead_notes(phone: str) -> list[dict]:
    """Внутренние заметки лида (старые → новые)."""
    try:
        rows = await _get_pool().fetch(
            "SELECT id, text, created_at FROM lead_notes "
            "WHERE lead_phone = $1 ORDER BY created_at ASC",
            phone,
        )
    except Exception:
        logger.exception("get_lead_notes failed: phone=%s", phone)
        raise
    return [dict(r) for r in rows]


async def add_lead_note(phone: str, text: str) -> dict:
    """Добавить заметку. Вернуть созданную строку (id, text, created_at)."""
    try:
        row = await _get_pool().fetchrow(
            "INSERT INTO lead_notes (lead_phone, text) VALUES ($1, $2) "
            "RETURNING id, text, created_at",
            phone, text,
        )
    except Exception:
        logger.exception("add_lead_note failed: phone=%s", phone)
        raise
    return dict(row)


async def save_manual_message(phone: str, text: str, delivered: bool) -> dict:
    """Сохранить РУЧНОЕ исходящее сообщение (менеджер из мини-CRM).

    sender='anna' + meta.manual=true → в таймлайне подпишется «Anna» (как ручные
    ответы после takeover). meta.status = 'sent'|'failed' — доставлено ли в Wazzup,
    чтобы неудачная отправка была видна в истории, а не терялась молча.
    Возвращает созданную строку (id, text, created_at, meta)."""
    meta = {"manual": True, "status": "sent" if delivered else "failed"}
    try:
        row = await _get_pool().fetchrow(
            "INSERT INTO messages (lead_phone, direction, sender, text, processed, "
            "processed_at, meta) VALUES ($1, 'outbound', 'anna', $2, true, now(), $3) "
            "RETURNING id, text, created_at, meta",
            phone, text, meta,
        )
    except Exception:
        logger.exception("save_manual_message failed: phone=%s", phone)
        raise
    return dict(row)


async def get_whitelist_entry(phone: str) -> dict | None:
    """Запись whitelist по телефону (reason/added_by/added_at) или None. Телефон
    нормализуется в 'wa_<digits>' — как хранит add_to_whitelist."""
    key = _wa_phone(phone)
    try:
        row = await _get_pool().fetchrow(
            "SELECT phone, reason, added_by, added_at FROM bot_whitelist WHERE phone = $1",
            key,
        )
    except Exception:
        logger.exception("get_whitelist_entry failed: phone=%s", key)
        raise
    return dict(row) if row else None


async def resume_lead(phone: str) -> bool:
    """Контр-действие к block_lead (кнопка «Вернуть боту»): снять do_not_contact,
    вернуть в auto. Атомарный условный UPDATE (без read-then-write) — безопасно при
    нескольких менеджерах на одном лиде. Возврат — найден ли лид."""
    try:
        row = await _get_pool().fetchrow(
            "UPDATE leads SET do_not_contact = false, mode = 'auto', "
            "manual_until = NULL, updated_at = now() WHERE phone = $1 RETURNING phone",
            phone,
        )
    except Exception:
        logger.exception("resume_lead failed: phone=%s", phone)
        raise
    found = row is not None
    logger.info("resume: %s → auto, do_not_contact=false (найден=%s)", phone, found)
    return found
