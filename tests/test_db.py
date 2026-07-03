"""Тесты слоя db.py — только моки, без реального Postgres.

Подменяем db._pool на FakePool (fetchrow/fetch = AsyncMock).
Проверяем: корректность SQL-подстрок, порядок параметров,
идемпотентность insert_message, whitelist-защиту upsert/update.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import db


# ---------------------------------------------------------------------------
# Вспомогательный класс и фикстура
# ---------------------------------------------------------------------------


class FakePool:
    """Заглушка asyncpg.Pool: fetchrow, fetch, execute и fetchval — AsyncMock."""

    def __init__(self):
        self.fetchrow = AsyncMock()
        self.fetch = AsyncMock()
        self.execute = AsyncMock()   # нужен для block_lead / touch_last_inbound
        self.fetchval = AsyncMock()  # нужен для count_recent_photos / get_scenario_title


@pytest.fixture()
def pool():
    """Подменить db._pool на FakePool на время одного теста."""
    fake = FakePool()
    original = db._pool
    db._pool = fake
    yield fake
    db._pool = original


# ---------------------------------------------------------------------------
# _get_pool
# ---------------------------------------------------------------------------


class TestGetPool:
    def test_raises_runtime_error_when_pool_is_none(self):
        """db._pool is None → _get_pool() поднимает RuntimeError."""
        original = db._pool
        db._pool = None
        try:
            with pytest.raises(RuntimeError, match="init_pool"):
                db._get_pool()
        finally:
            db._pool = original


# ---------------------------------------------------------------------------
# get_lead_by_phone
# ---------------------------------------------------------------------------


class TestGetLeadByPhone:
    async def test_returns_dict_when_row_found(self, pool):
        """fetchrow вернул строку-заглушку → результат dict с теми же полями."""
        fake_row = {"phone": "521234567890", "name": "Carlos", "age": 35}
        pool.fetchrow.return_value = fake_row

        result = await db.get_lead_by_phone("521234567890")

        assert isinstance(result, dict)
        assert result["phone"] == "521234567890"
        assert result["name"] == "Carlos"

    async def test_returns_none_when_not_found(self, pool):
        """fetchrow вернул None (лида нет) → результат None."""
        pool.fetchrow.return_value = None

        result = await db.get_lead_by_phone("521234567890")

        assert result is None

    async def test_phone_passed_as_separate_arg_not_in_sql(self, pool):
        """phone не вставляется в строку SQL, а передаётся отдельным параметром $1."""
        pool.fetchrow.return_value = None
        phone = "521234567890"

        await db.get_lead_by_phone(phone)

        sql, *params = pool.fetchrow.call_args.args
        # Сам номер не должен фигурировать в тексте запроса
        assert phone not in sql
        # Плейсхолдер присутствует
        assert "$1" in sql
        # Номер — первый позиционный параметр
        assert params[0] == phone


# ---------------------------------------------------------------------------
# upsert_lead
# ---------------------------------------------------------------------------


class TestUpsertLead:
    async def test_returns_dict_with_valid_fields(self, pool):
        """Валидные поля → upsert_lead возвращает dict."""
        pool.fetchrow.return_value = {"phone": "wa_1", "name": "Juan", "age": 35}

        result = await db.upsert_lead("wa_1", name="Juan", age=35)

        assert isinstance(result, dict)
        assert result["name"] == "Juan"

    async def test_sql_contains_on_conflict_and_excluded_cols(self, pool):
        """SQL содержит ON CONFLICT (phone) DO UPDATE, EXCLUDED.name, EXCLUDED.age, updated_at = now()."""
        pool.fetchrow.return_value = {"phone": "wa_1", "name": "Juan", "age": 35}

        await db.upsert_lead("wa_1", name="Juan", age=35)

        sql = pool.fetchrow.call_args.args[0]
        assert "ON CONFLICT (phone) DO UPDATE" in sql
        assert "EXCLUDED.name" in sql
        assert "EXCLUDED.age" in sql
        assert "updated_at = now()" in sql
        assert "RETURNING" in sql

    async def test_params_order_phone_first_then_field_values(self, pool):
        """phone=$1 первый, потом значения полей в порядке передачи."""
        pool.fetchrow.return_value = {"phone": "wa_1", "name": "Juan", "age": 35}

        await db.upsert_lead("wa_1", name="Juan", age=35)

        _, *params = pool.fetchrow.call_args.args
        assert params[0] == "wa_1"    # phone → $1
        assert params[1] == "Juan"    # name  → $2
        assert params[2] == 35        # age   → $3

    async def test_empty_fields_no_excluded_only_updated_at(self, pool):
        """Пустой upsert (только phone) → DO UPDATE SET updated_at = now() без EXCLUDED."""
        pool.fetchrow.return_value = {"phone": "wa_1"}

        await db.upsert_lead("wa_1")

        sql = pool.fetchrow.call_args.args[0]
        assert "ON CONFLICT (phone) DO UPDATE" in sql
        assert "EXCLUDED." not in sql
        assert "updated_at = now()" in sql

    async def test_injection_column_raises_value_error(self, pool):
        """Имя колонки вне whitelist → ValueError, fetchrow не вызван."""
        with pytest.raises(ValueError, match="Недопустимые колонки"):
            await db.upsert_lead("wa_1", **{"age; DROP TABLE leads": 1})

        pool.fetchrow.assert_not_called()

    async def test_unknown_column_raises_value_error(self, pool):
        """Произвольное несуществующее поле → ValueError до вызова fetchrow."""
        with pytest.raises(ValueError):
            await db.upsert_lead("wa_1", totally_unknown_column=42)

        pool.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# insert_message
# ---------------------------------------------------------------------------


class TestInsertMessage:
    async def test_with_external_id_returns_true(self, pool):
        """external_message_id задан, fetchrow вернул строку → True (вставлено)."""
        pool.fetchrow.return_value = {"id": 7}

        result = await db.insert_message(
            "wa_1", "inbound", "lead", "Hola",
            external_message_id="ext-001",
        )

        assert result is True

    async def test_with_external_id_sql_has_lead_phone_conflict(self, pool):
        """SQL содержит ON CONFLICT (lead_phone, external_message_id) DO NOTHING."""
        pool.fetchrow.return_value = {"id": 7}

        await db.insert_message(
            "wa_1", "inbound", "lead", "Hola",
            external_message_id="ext-001",
        )

        sql = pool.fetchrow.call_args.args[0]
        assert "ON CONFLICT (lead_phone, external_message_id) DO NOTHING" in sql

    async def test_duplicate_returns_false(self, pool):
        """DO NOTHING (дубль) — fetchrow вернул None → функция вернула False."""
        pool.fetchrow.return_value = None

        result = await db.insert_message(
            "wa_1", "inbound", "lead", "Hola",
            external_message_id="ext-001",
        )

        assert result is False

    async def test_message_uid_conflict_when_no_external_id(self, pool):
        """Нет external_message_id, есть message_uid → ON CONFLICT (message_uid) DO NOTHING."""
        pool.fetchrow.return_value = {"id": 8}

        await db.insert_message(
            "wa_1", "inbound", "lead", "Hola",
            message_uid="uid-abc",
        )

        sql = pool.fetchrow.call_args.args[0]
        assert "ON CONFLICT (message_uid) DO NOTHING" in sql
        assert "ON CONFLICT (lead_phone, external_message_id)" not in sql

    async def test_no_conflict_clause_without_any_id(self, pool):
        """Без external_message_id и message_uid — ON CONFLICT отсутствует в SQL."""
        pool.fetchrow.return_value = {"id": 9}

        await db.insert_message("wa_1", "inbound", "lead", "Hola")

        sql = pool.fetchrow.call_args.args[0]
        assert "ON CONFLICT" not in sql

    async def test_unique_violation_by_other_index_returns_false(self, pool):
        """Конфликт по ДРУГОМУ UNIQUE (заданы оба ID, дубль по message_uid):
        ON CONFLICT нацелен на (lead_phone, external_message_id), Postgres поднимает
        UniqueViolationError — функция должна вернуть False (тоже дубль), не пробросить."""
        from asyncpg.exceptions import UniqueViolationError
        pool.fetchrow.side_effect = UniqueViolationError("duplicate message_uid")

        result = await db.insert_message(
            "wa_1", "outbound", "anna", "Hola",
            external_message_id="ext-002", message_uid="uid-dup",
        )

        assert result is False

    async def test_meta_dict_passed_as_dict_not_json_string(self, pool):
        """meta=dict попадает в args как dict (кодек asyncpg делает jsonb), не строка."""
        pool.fetchrow.return_value = {"id": 10}
        meta_data = {"channel": "whatsapp", "score": 5}

        await db.insert_message(
            "wa_1", "inbound", "lead", "Hola",
            meta=meta_data,
        )

        _, *params = pool.fetchrow.call_args.args
        # dict должен присутствовать среди параметров
        assert meta_data in params
        # и это именно dict, а не JSON-строка
        meta_param = next(p for p in params if isinstance(p, dict))
        assert meta_param == meta_data

    async def test_base_columns_present_in_insert_sql(self, pool):
        """lead_phone, direction, sender, text, processed всегда в INSERT."""
        pool.fetchrow.return_value = {"id": 11}

        await db.insert_message("wa_1", "inbound", "lead", "Hola")

        sql = pool.fetchrow.call_args.args[0]
        for col in ("lead_phone", "direction", "sender", "text", "processed"):
            assert col in sql, f"Ожидали колонку {col!r} в SQL INSERT"


# ---------------------------------------------------------------------------
# get_conversation_history
# ---------------------------------------------------------------------------


class TestGetConversationHistory:
    async def test_returns_list_of_dicts(self, pool):
        """fetch вернул 2 записи → список из 2 dict."""
        pool.fetch.return_value = [
            {"id": 1, "lead_phone": "wa_1", "text": "Hola"},
            {"id": 2, "lead_phone": "wa_1", "text": "Soy Carlos"},
        ]

        result = await db.get_conversation_history("wa_1")

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(r, dict) for r in result)

    async def test_sql_has_order_by_created_at_asc(self, pool):
        """SQL содержит ORDER BY created_at ASC."""
        pool.fetch.return_value = []

        await db.get_conversation_history("wa_1")

        sql = pool.fetch.call_args.args[0]
        assert "ORDER BY created_at ASC" in sql

    async def test_sql_has_limit_placeholder(self, pool):
        """SQL содержит LIMIT $2."""
        pool.fetch.return_value = []

        await db.get_conversation_history("wa_1")

        sql = pool.fetch.call_args.args[0]
        assert "LIMIT $2" in sql

    async def test_phone_and_limit_in_args(self, pool):
        """phone передаётся как $1, limit как $2 в отдельных позиционных аргументах."""
        pool.fetch.return_value = []

        await db.get_conversation_history("wa_1", limit=10)

        _, *params = pool.fetch.call_args.args
        assert params[0] == "wa_1"
        assert params[1] == 10

    async def test_default_limit_is_30(self, pool):
        """Без явного limit используется дефолт 30."""
        pool.fetch.return_value = []

        await db.get_conversation_history("wa_1")

        _, *params = pool.fetch.call_args.args
        assert params[1] == 30


# ---------------------------------------------------------------------------
# update_lead_fields
# ---------------------------------------------------------------------------


class TestUpdateLeadFields:
    async def test_returns_dict_with_valid_fields(self, pool):
        """Валидные поля → UPDATE выполнен, возвращает dict."""
        pool.fetchrow.return_value = {"phone": "wa_1", "name": "Carlos"}

        result = await db.update_lead_fields("wa_1", name="Carlos")

        assert isinstance(result, dict)
        assert result["name"] == "Carlos"

    async def test_sql_is_update_with_updated_at_and_returning(self, pool):
        """SQL: UPDATE leads SET ... updated_at = now() WHERE phone = $N RETURNING *."""
        pool.fetchrow.return_value = {"phone": "wa_1", "name": "Carlos"}

        await db.update_lead_fields("wa_1", name="Carlos")

        sql = pool.fetchrow.call_args.args[0]
        assert sql.strip().upper().startswith("UPDATE LEADS SET")
        assert "updated_at = now()" in sql
        assert "RETURNING *" in sql

    async def test_params_order_field_values_then_phone_last(self, pool):
        """Порядок параметров: значения полей ($1..$n), phone последним ($n+1)."""
        pool.fetchrow.return_value = {"phone": "wa_1", "name": "Carlos", "age": 40}

        await db.update_lead_fields("wa_1", name="Carlos", age=40)

        _, *params = pool.fetchrow.call_args.args
        # phone — последний параметр
        assert params[-1] == "wa_1"
        # значения полей идут раньше phone
        assert "Carlos" in params
        assert 40 in params
        name_idx = params.index("Carlos")
        phone_idx = params.index("wa_1")
        assert name_idx < phone_idx

    async def test_empty_fields_calls_select_not_update(self, pool):
        """Пустые поля → делегирует get_lead_by_phone (SELECT), UPDATE не выполняется."""
        pool.fetchrow.return_value = {"phone": "wa_1"}

        await db.update_lead_fields("wa_1")

        sql = pool.fetchrow.call_args.args[0]
        assert "SELECT" in sql
        assert "UPDATE" not in sql

    async def test_bad_column_raises_value_error_before_query(self, pool):
        """Неизвестная колонка → ValueError, fetchrow не вызывается вообще."""
        with pytest.raises(ValueError, match="Недопустимые колонки"):
            await db.update_lead_fields("wa_1", nonexistent_field="oops")

        pool.fetchrow.assert_not_called()

    async def test_returns_none_when_lead_not_found(self, pool):
        """fetchrow вернул None (лида нет) → результат None."""
        pool.fetchrow.return_value = None

        result = await db.update_lead_fields("wa_1", name="Ghost")

        assert result is None


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------


class TestIsReady:
    def test_false_when_pool_is_none(self):
        """db._pool is None → is_ready() возвращает False."""
        original = db._pool
        db._pool = None
        try:
            assert db.is_ready() is False
        finally:
            db._pool = original

    def test_true_when_pool_is_set(self):
        """db._pool = <объект> → is_ready() возвращает True."""
        original = db._pool
        db._pool = object()  # любой не-None объект
        try:
            assert db.is_ready() is True
        finally:
            db._pool = original


# ---------------------------------------------------------------------------
# get_unprocessed_inbound
# ---------------------------------------------------------------------------


class TestGetUnprocessedInbound:
    async def test_returns_list_of_dicts(self, pool):
        """fetch вернул 2 записи → список из 2 dict."""
        pool.fetch.return_value = [
            {"id": "uuid-1", "lead_phone": "wa_1", "text": "Hola", "processed": False},
            {"id": "uuid-2", "lead_phone": "wa_1", "text": "Como estas", "processed": False},
        ]

        result = await db.get_unprocessed_inbound("wa_1")

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(r, dict) for r in result)

    async def test_empty_returns_empty_list(self, pool):
        """fetch вернул [] → пустой список (уже обработано или нет сообщений)."""
        pool.fetch.return_value = []

        result = await db.get_unprocessed_inbound("wa_1")

        assert result == []

    async def test_sql_contains_direction_inbound(self, pool):
        """SQL фильтрует direction = 'inbound'."""
        pool.fetch.return_value = []

        await db.get_unprocessed_inbound("wa_1")

        sql = pool.fetch.call_args.args[0]
        assert "direction = 'inbound'" in sql

    async def test_sql_contains_processed_false(self, pool):
        """SQL фильтрует processed = false."""
        pool.fetch.return_value = []

        await db.get_unprocessed_inbound("wa_1")

        sql = pool.fetch.call_args.args[0]
        assert "processed = false" in sql

    async def test_sql_contains_order_by_created_at_asc(self, pool):
        """SQL содержит ORDER BY created_at ASC (хронологический порядок)."""
        pool.fetch.return_value = []

        await db.get_unprocessed_inbound("wa_1")

        sql = pool.fetch.call_args.args[0]
        assert "ORDER BY created_at ASC" in sql

    async def test_phone_passed_as_param_not_in_sql_text(self, pool):
        """phone передаётся отдельным параметром $1, не вставляется в строку SQL."""
        pool.fetch.return_value = []
        phone = "wa_521234567890"

        await db.get_unprocessed_inbound(phone)

        sql, *params = pool.fetch.call_args.args
        assert phone not in sql
        assert "$1" in sql
        assert params[0] == phone


# ---------------------------------------------------------------------------
# mark_messages_processed
# ---------------------------------------------------------------------------


class TestMarkMessagesProcessed:
    async def test_empty_ids_returns_zero_without_pool(self, pool):
        """Пустой список ids → немедленный return 0, fetch НЕ вызван."""
        result = await db.mark_messages_processed([])

        assert result == 0
        pool.fetch.assert_not_called()

    async def test_non_empty_ids_returns_count(self, pool):
        """Непустой список → fetch вызван, возвращает len(результата)."""
        pool.fetch.return_value = [
            {"id": "uuid-1"},
            {"id": "uuid-2"},
        ]

        result = await db.mark_messages_processed(["uuid-1", "uuid-2"])

        assert result == 2

    async def test_sql_contains_set_processed_true(self, pool):
        """SQL содержит SET processed = true."""
        pool.fetch.return_value = [{"id": "uuid-1"}]

        await db.mark_messages_processed(["uuid-1"])

        sql = pool.fetch.call_args.args[0]
        assert "processed = true" in sql

    async def test_sql_contains_processed_at_now(self, pool):
        """SQL содержит processed_at = now()."""
        pool.fetch.return_value = [{"id": "uuid-1"}]

        await db.mark_messages_processed(["uuid-1"])

        sql = pool.fetch.call_args.args[0]
        assert "processed_at = now()" in sql

    async def test_sql_contains_any_uuid_array(self, pool):
        """SQL содержит ANY($1::uuid[]) — параметризованный массив UUID."""
        pool.fetch.return_value = [{"id": "uuid-1"}]

        await db.mark_messages_processed(["uuid-1"])

        sql = pool.fetch.call_args.args[0]
        assert "ANY($1::uuid[])" in sql

    async def test_sql_contains_returning_id(self, pool):
        """SQL содержит RETURNING id для подсчёта реально обновлённых строк."""
        pool.fetch.return_value = []

        await db.mark_messages_processed(["uuid-x"])

        sql = pool.fetch.call_args.args[0]
        assert "RETURNING id" in sql

    async def test_ids_passed_as_single_param(self, pool):
        """Список ids передаётся как единственный параметр $1."""
        ids = ["uuid-a", "uuid-b", "uuid-c"]
        pool.fetch.return_value = [{"id": i} for i in ids]

        await db.mark_messages_processed(ids)

        _, *params = pool.fetch.call_args.args
        assert params[0] == ids


# ---------------------------------------------------------------------------
# is_whitelisted
# ---------------------------------------------------------------------------


class TestIsWhitelisted:
    async def test_returns_true_when_row_found(self, pool):
        """fetchrow вернул строку → True (номер в whitelist)."""
        pool.fetchrow.return_value = {"phone": "wa_1"}

        result = await db.is_whitelisted("wa_1")

        assert result is True

    async def test_returns_false_when_not_found(self, pool):
        """fetchrow вернул None (нет в whitelist) → False."""
        pool.fetchrow.return_value = None

        result = await db.is_whitelisted("wa_1")

        assert result is False

    async def test_sql_contains_bot_whitelist(self, pool):
        """SQL обращается к таблице bot_whitelist."""
        pool.fetchrow.return_value = None

        await db.is_whitelisted("wa_1")

        sql = pool.fetchrow.call_args.args[0]
        assert "bot_whitelist" in sql

    async def test_phone_passed_as_param(self, pool):
        """phone передаётся отдельным параметром $1, не вставляется в SQL."""
        pool.fetchrow.return_value = None
        phone = "wa_521234567890"

        await db.is_whitelisted(phone)

        sql, *params = pool.fetchrow.call_args.args
        assert phone not in sql
        assert "$1" in sql
        assert params[0] == phone


# ---------------------------------------------------------------------------
# block_lead — транзакционная реализация (acquire → conn.fetchval → conn.execute)
# ---------------------------------------------------------------------------


class TestBlockLead:
    """block_lead теперь транзакционный: pool.acquire() → FakeConn.
    Используем transactional_pool fixture (те же FakeConn/FakePoolWithAcquire что у TestSetFunnelStage).
    conn.execute вызывается 1 или 2 раза: [0] = UPDATE leads, [1] = INSERT funnel_events (если current != 'lost').
    """

    async def test_escort_true_sql_contains_escort_count(self, transactional_pool):
        """escort=True → UPDATE SQL содержит escort_mention_count (инкремент)."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"  # лид найден, стадия не 'lost'

        await db.block_lead("wa_1", "Ищет интим-услуги", escort=True)

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "escort_mention_count" in update_sql

    async def test_escort_true_sql_contains_do_not_contact(self, transactional_pool):
        """escort=True → UPDATE SQL содержит do_not_contact = true."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"

        await db.block_lead("wa_1", "Ищет интим-услуги", escort=True)

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "do_not_contact = true" in update_sql

    async def test_escort_true_sql_contains_100_years(self, transactional_pool):
        """escort=True → UPDATE SQL содержит interval '100 years' (блок навсегда)."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"

        await db.block_lead("wa_1", "Ищет интим-услуги", escort=True)

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "interval '100 years'" in update_sql

    async def test_escort_true_sql_contains_lost_and_null(self, transactional_pool):
        """escort=True → UPDATE SQL содержит funnel_stage = 'lost' и next_followup_at = NULL."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"

        await db.block_lead("wa_1", "Ищет интим-услуги", escort=True)

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "funnel_stage = 'lost'" in update_sql
        assert "next_followup_at = NULL" in update_sql

    async def test_escort_true_inserts_funnel_event(self, transactional_pool):
        """current != 'lost' + escort=True → второй execute INSERT в funnel_events с to_stage 'lost'."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"

        await db.block_lead("wa_1", "Ищет интим-услуги", escort=True)

        all_sql = [call.args[0] for call in conn.execute.call_args_list]
        assert any("funnel_events" in sql for sql in all_sql), (
            f"INSERT в funnel_events не найден, вызовы execute: {all_sql}"
        )
        funnel_sql = next(sql for sql in all_sql if "funnel_events" in sql)
        assert "'lost'" in funnel_sql

    async def test_escort_false_no_escort_count(self, transactional_pool):
        """escort=False → UPDATE SQL НЕ содержит escort_mention_count."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"

        await db.block_lead("wa_1", "Агрессия", escort=False)

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "escort_mention_count" not in update_sql

    async def test_escort_false_still_blocks(self, transactional_pool):
        """escort=False → do_not_contact + manual_until 100 лет — блок всё равно ставится."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"

        await db.block_lead("wa_1", "Агрессия", escort=False)

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "do_not_contact = true" in update_sql
        assert "interval '100 years'" in update_sql

    async def test_not_found_returns_without_execute(self, transactional_pool):
        """fetchval вернул None (лид не найден) → ранний return, conn.execute НЕ вызван."""
        conn = transactional_pool
        conn.fetchval.return_value = None  # лид не найден

        await db.block_lead("wa_missing", "test reason", escort=False)

        conn.execute.assert_not_called()

    async def test_already_lost_no_funnel_event(self, transactional_pool):
        """current='lost' → UPDATE выполняется, но INSERT в funnel_events НЕ вызывается."""
        conn = transactional_pool
        conn.fetchval.return_value = "lost"  # уже заблокирован

        await db.block_lead("wa_1", "Агрессия", escort=False)

        # UPDATE должен быть вызван ровно один раз
        assert conn.execute.call_count == 1, (
            f"Ожидали ровно 1 execute (UPDATE), получили {conn.execute.call_count}"
        )
        # И это не INSERT в funnel_events
        only_sql = conn.execute.call_args_list[0].args[0]
        assert "funnel_events" not in only_sql, (
            "INSERT в funnel_events не ожидался (current='lost' уже), SQL: {only_sql}"
        )

    async def test_phone_and_reason_passed_as_params(self, transactional_pool):
        """phone и reason передаются параметрами ($1,$2), не вставляются в SQL-строку."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"
        phone = "wa_555"
        reason = "тест"

        await db.block_lead(phone, reason, escort=False)

        # Первый execute — UPDATE leads SET ... WHERE phone = $1
        update_call = conn.execute.call_args_list[0]
        sql, *params = update_call.args
        assert phone not in sql
        assert phone in params
        assert reason in params


# ---------------------------------------------------------------------------
# touch_last_inbound
# ---------------------------------------------------------------------------


class TestTouchLastInbound:
    async def test_sql_contains_last_inbound_at_now(self, pool):
        """SQL обновляет last_inbound_at = now()."""
        await db.touch_last_inbound("wa_1")

        sql = pool.execute.call_args.args[0]
        assert "last_inbound_at = now()" in sql

    async def test_sql_contains_phone_param(self, pool):
        """phone передаётся параметром $1."""
        phone = "wa_999"

        await db.touch_last_inbound(phone)

        sql, *params = pool.execute.call_args.args
        assert phone not in sql
        assert params[0] == phone


# ---------------------------------------------------------------------------
# set_funnel_stage — транзакционный мок
# ---------------------------------------------------------------------------


class _FakeAsyncCtxManager:
    """Async context manager-заглушка для мокинга acquire() и transaction()."""

    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


class FakeConn:
    """Заглушка asyncpg.Connection: fetchval + execute."""

    def __init__(self):
        self.fetchval = AsyncMock()
        self.execute = AsyncMock()

    def transaction(self):
        return _FakeAsyncCtxManager(None)


class FakePoolWithAcquire:
    """Заглушка pool с acquire() → async ctx → FakeConn."""

    def __init__(self, conn: "FakeConn"):
        self._conn = conn

    def acquire(self):
        return _FakeAsyncCtxManager(self._conn)


@pytest.fixture()
def transactional_pool():
    """Подменить db._pool на FakePoolWithAcquire, вернуть FakeConn для проверок."""
    conn = FakeConn()
    fake_pool = FakePoolWithAcquire(conn)
    original = db._pool
    db._pool = fake_pool
    yield conn
    db._pool = original


class TestSetFunnelStage:
    async def test_unknown_stage_raises_value_error(self):
        """to_stage не в FUNNEL_STAGES → ValueError до обращения к пулу."""
        with pytest.raises(ValueError, match="bogus"):
            await db.set_funnel_stage("wa_1", "bogus")

    async def test_same_stage_returns_false_no_execute(self, transactional_pool):
        """current == to_stage → возвращает False и НЕ вызывает ни одного execute."""
        conn = transactional_pool
        conn.fetchval.return_value = "qualified"  # текущая стадия = то, что хотим

        result = await db.set_funnel_stage("wa_1", "qualified")

        assert result is False
        conn.execute.assert_not_called()

    async def test_stage_change_returns_true_and_inserts_funnel_event(self, transactional_pool):
        """Реальная смена стадии → возвращает True, один из execute содержит 'funnel_events'."""
        conn = transactional_pool
        conn.fetchval.return_value = "new"  # было "new", будет "qualifying"

        result = await db.set_funnel_stage("wa_1", "qualifying")

        assert result is True
        all_sql = [call.args[0] for call in conn.execute.call_args_list]
        assert any("funnel_events" in sql for sql in all_sql), (
            f"INSERT в funnel_events не найден среди вызовов execute: {all_sql}"
        )

    async def test_lost_stage_sets_next_followup_null(self, transactional_pool):
        """Стадия 'lost' (NO_FOLLOWUP) → SQL UPDATE содержит 'next_followup_at = NULL'."""
        conn = transactional_pool
        conn.fetchval.return_value = "pitched"

        await db.set_funnel_stage("wa_1", "lost")

        all_sql = [call.args[0] for call in conn.execute.call_args_list]
        assert any("next_followup_at = NULL" in sql for sql in all_sql), (
            f"Ожидали next_followup_at = NULL, SQL-вызовы: {all_sql}"
        )

    async def test_qualified_stage_uses_make_interval(self, transactional_pool):
        """Стадия 'qualified' (FOLLOWUP_FIRST_DELAY_HOURS=48) → SQL содержит 'make_interval'."""
        conn = transactional_pool
        conn.fetchval.return_value = "photo_pending"

        await db.set_funnel_stage("wa_1", "qualified")

        all_sql = [call.args[0] for call in conn.execute.call_args_list]
        assert any("make_interval" in sql for sql in all_sql), (
            f"Ожидали make_interval в SQL для qualified, вызовы: {all_sql}"
        )


class TestSearchScenariosByVector:
    """RAG-запрос по эмбеддингу: cosine, исключение scheduled, top_k."""

    async def test_sql_excludes_scheduled_and_uses_cosine(self, pool):
        pool.fetch.return_value = [{"id": 1, "template_es": "x", "mode": "bot_auto",
                                    "ai_allowed": True, "blocks_lead": False, "score": 0.7}]
        res = await db.search_scenarios_by_vector("[0.1,0.2]", top_k=3)
        sql, *params = pool.fetch.call_args.args
        # служебные исключены
        assert "trigger_type IS DISTINCT FROM 'scheduled'" in sql
        # косинус + только активные с эмбеддингом
        assert "embedding <=> $1::vector" in sql
        assert "is_active = true" in sql
        assert "embedding IS NOT NULL" in sql
        # параметры: вектор $1, top_k $2
        assert params[0] == "[0.1,0.2]"
        assert params[1] == 3
        assert isinstance(res, list) and res[0]["id"] == 1

    async def test_returns_list_of_dicts(self, pool):
        pool.fetch.return_value = []
        res = await db.search_scenarios_by_vector("[0.1]")
        assert res == []


# ---------------------------------------------------------------------------
# save_photo
# ---------------------------------------------------------------------------


class TestSavePhoto:
    """save_photo транзакционный: FOR UPDATE лида → EXISTS is_primary → INSERT.
    INSERT — последний conn.execute; is_primary = not has_primary (fetchval)."""

    def _insert_call(self, conn):
        """Аргументы INSERT-вызова (последний execute; первый — SELECT ... FOR UPDATE)."""
        return conn.execute.call_args_list[-1].args

    async def test_sql_contains_required_keywords(self, transactional_pool):
        conn = transactional_pool
        conn.fetchval.return_value = False  # ещё нет primary
        await db.save_photo("wa_1", "https://url", "wa_1/abc.jpg", "ok")

        sql = self._insert_call(conn)[0]
        for keyword in ("lead_photos", "vision_analyzed", "vision_verdict", "is_primary"):
            assert keyword in sql, f"Keyword '{keyword}' не найден в SQL"
        # FOR UPDATE на лида — в первом execute
        assert "FOR UPDATE" in conn.execute.call_args_list[0].args[0]

    async def test_param_order(self, transactional_pool):
        conn = transactional_pool
        conn.fetchval.return_value = False
        await db.save_photo(
            "wa_phone", "https://storage_url", "wa_phone/abc.jpg", "ok",
            analysis={"detail": "fine"}, reasons=["clear face"], channel="whatsapp",
        )
        args = self._insert_call(conn)
        assert args[1] == "wa_phone"
        assert args[2] == "whatsapp"
        assert args[3] == "https://storage_url"
        assert args[4] == "wa_phone/abc.jpg"
        assert args[5] == {"detail": "fine"}
        assert args[6] == "ok"
        assert args[7] == ["clear face"]
        assert args[8] is True  # is_primary = not has_primary(False)

    async def test_is_primary_false_when_already_has_primary(self, transactional_pool):
        conn = transactional_pool
        conn.fetchval.return_value = True  # у лида уже есть primary
        await db.save_photo("wa_1", None, None, "ok")
        assert self._insert_call(conn)[8] is False

    async def test_analysis_none_defaults_to_empty_dict(self, transactional_pool):
        conn = transactional_pool
        conn.fetchval.return_value = False
        await db.save_photo("wa_1", None, None, "manual", analysis=None)
        assert self._insert_call(conn)[5] == {}

    async def test_reasons_none_defaults_to_empty_list(self, transactional_pool):
        conn = transactional_pool
        conn.fetchval.return_value = False
        await db.save_photo("wa_1", None, None, "manual", reasons=None)
        assert self._insert_call(conn)[7] == []

    async def test_db_error_raises(self, transactional_pool):
        conn = transactional_pool
        conn.execute.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError, match="db down"):
            await db.save_photo("wa_1", None, None, "ok")


# ---------------------------------------------------------------------------
# mark_photo_received
# ---------------------------------------------------------------------------


class TestMarkPhotoReceived:

    async def test_sql_contains_update_leads_photo_received(self, pool):
        """SQL содержит UPDATE leads SET photo_received."""
        await db.mark_photo_received("wa_1", True)

        sql = pool.execute.call_args.args[0]
        assert "UPDATE leads" in sql
        assert "photo_received" in sql

    async def test_params_phone_and_received_bool(self, pool):
        """Параметры: args[1]=phone, args[2]=received (bool)."""
        await db.mark_photo_received("wa_9999", False)

        args = pool.execute.call_args.args
        assert args[1] == "wa_9999", f"$1 phone: {args[1]}"
        assert args[2] is False,     f"$2 received: {args[2]}"

    async def test_db_error_raises(self, pool):
        """Ошибка пула → пробрасывается наружу."""
        pool.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            await db.mark_photo_received("wa_1", True)


# ---------------------------------------------------------------------------
# count_recent_photos
# ---------------------------------------------------------------------------


class TestCountRecentPhotos:

    async def test_sql_contains_lead_photos_and_make_interval(self, pool):
        """SQL содержит lead_photos и make_interval."""
        pool.fetchval.return_value = 0

        await db.count_recent_photos("wa_1")

        sql = pool.fetchval.call_args.args[0]
        assert "lead_photos" in sql,    "lead_photos не найден в SQL"
        assert "make_interval" in sql,  "make_interval не найден в SQL"

    async def test_returns_fetchval_result(self, pool):
        """Возвращает именно то, что вернул fetchval (счётчик)."""
        pool.fetchval.return_value = 3

        result = await db.count_recent_photos("wa_1", hours=2)

        assert result == 3

    async def test_default_hours_is_one(self, pool):
        """По умолчанию hours=1 (второй параметр SQL)."""
        pool.fetchval.return_value = 0

        await db.count_recent_photos("wa_1")

        _, *params = pool.fetchval.call_args.args
        assert params[0] == "wa_1"
        assert params[1] == 1, f"Ожидали hours=1 по умолчанию, получили {params[1]}"

    async def test_custom_hours_passed_as_param(self, pool):
        """Кастомный hours передаётся вторым SQL-параметром."""
        pool.fetchval.return_value = 0

        await db.count_recent_photos("wa_1", hours=6)

        _, *params = pool.fetchval.call_args.args
        assert params[1] == 6, f"Ожидали hours=6, получили {params[1]}"


# ---------------------------------------------------------------------------
# get_scenario_template
# ---------------------------------------------------------------------------


class TestGetScenarioTemplate:
    """get_scenario_template: int id → SELECT template_es; не-int → None без запроса; ошибка → None."""

    async def test_int_id_returns_fetchval_result(self, pool):
        """int id → fetchval вызван, возвращает значение template_es (строку)."""
        pool.fetchval.return_value = "Hola, por favor envía otra foto"

        result = await db.get_scenario_template(5)

        assert result == "Hola, por favor envía otra foto"
        pool.fetchval.assert_awaited_once()

    async def test_sql_contains_template_es(self, pool):
        """SQL содержит 'template_es' — правильная колонка (не title, не template_en)."""
        pool.fetchval.return_value = "template text"

        await db.get_scenario_template(5)

        sql = pool.fetchval.call_args.args[0]
        assert "template_es" in sql, f"'template_es' не найден в SQL: {sql!r}"

    async def test_none_id_returns_none_without_query(self, pool):
        """id=None (не int) → немедленно None, fetchval НЕ вызван."""
        result = await db.get_scenario_template(None)

        assert result is None
        pool.fetchval.assert_not_called()

    async def test_string_id_returns_none_without_query(self, pool):
        """Строковый id → немедленно None, fetchval НЕ вызван."""
        result = await db.get_scenario_template("5")

        assert result is None
        pool.fetchval.assert_not_called()

    async def test_db_error_returns_none_no_raise(self, pool):
        """Ошибка БД → возвращает None (не пробрасывает), соответствуя поведению get_scenario_title."""
        pool.fetchval.side_effect = RuntimeError("db error")

        result = await db.get_scenario_template(5)

        assert result is None


# ---------------------------------------------------------------------------
# add_to_whitelist / remove_from_whitelist (блок 10)
# ---------------------------------------------------------------------------


class TestAddToWhitelist:
    async def test_inserts_into_bot_whitelist(self, pool):
        """SQL — INSERT в bot_whitelist с ON CONFLICT (повторное добавление не падает)."""
        await db.add_to_whitelist("wa_521234567890", "VIP-клиент", "anna")

        sql = pool.execute.call_args.args[0]
        assert "INSERT INTO bot_whitelist" in sql
        assert "ON CONFLICT" in sql

    async def test_phone_normalized_to_wa_digits(self, pool):
        """Любой формат телефона → 'wa_<digits>' (как ищет is_whitelisted)."""
        await db.add_to_whitelist("+52 123 456-7890", "VIP", "anna")

        params = pool.execute.call_args.args[1:]
        assert params[0] == "wa_521234567890"

    async def test_already_wa_prefixed_stays_same(self, pool):
        """Вход уже 'wa_...' → не задваивается префикс."""
        await db.add_to_whitelist("wa_521234567890", "VIP", "anna")

        params = pool.execute.call_args.args[1:]
        assert params[0] == "wa_521234567890"

    async def test_reason_and_added_by_passed_as_params(self, pool):
        """reason/added_by идут параметрами $2/$3, не в SQL."""
        await db.add_to_whitelist("wa_1", "клиент агентства", "anna")

        params = pool.execute.call_args.args[1:]
        assert params == ("wa_1", "клиент агентства", "anna")

    async def test_db_error_raises(self, pool):
        """Ошибка БД пробрасывается (в отличие от get_scenario_*)."""
        pool.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError):
            await db.add_to_whitelist("wa_1", "VIP", "anna")

    async def test_empty_phone_raises_no_sql(self, pool):
        """Пустой/бесцифровой телефон → ValueError, SQL не выполняется."""
        with pytest.raises(ValueError):
            await db.add_to_whitelist("", "VIP", "anna")
        pool.execute.assert_not_called()

    async def test_none_phone_raises_no_sql(self, pool):
        with pytest.raises(ValueError):
            await db.add_to_whitelist(None, "VIP", "anna")  # type: ignore[arg-type]
        pool.execute.assert_not_called()


class TestRemoveFromWhitelist:
    async def test_deletes_from_bot_whitelist(self, pool):
        """SQL — DELETE из bot_whitelist по phone."""
        await db.remove_from_whitelist("wa_521234567890")

        sql = pool.execute.call_args.args[0]
        assert "DELETE FROM bot_whitelist" in sql

    async def test_phone_normalized(self, pool):
        """Телефон нормализуется в 'wa_<digits>' перед DELETE."""
        await db.remove_from_whitelist("+52 123 456-7890")

        params = pool.execute.call_args.args[1:]
        assert params[0] == "wa_521234567890"

    async def test_phone_passed_as_param(self, pool):
        """phone параметром $1, не в SQL."""
        await db.remove_from_whitelist("wa_1")

        sql, *params = pool.execute.call_args.args
        assert "wa_1" not in sql
        assert params[0] == "wa_1"

    async def test_db_error_raises(self, pool):
        pool.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError):
            await db.remove_from_whitelist("wa_1")

    async def test_empty_phone_raises_no_sql(self, pool):
        """Пустой телефон → ValueError, DELETE не выполняется."""
        with pytest.raises(ValueError):
            await db.remove_from_whitelist("")
        pool.execute.assert_not_called()

    async def test_delete_zero_logs_warning(self, pool, caplog):
        """execute вернул 'DELETE 0' (не было записи) → предупреждение, не 'удалён'."""
        import logging
        pool.execute.return_value = "DELETE 0"
        with caplog.at_level(logging.WARNING, logger="matchmatch.db"):
            await db.remove_from_whitelist("wa_1")
        assert any("не найден при удалении" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# set_manual / set_auto / list_active_leads / list_whitelist (блок 11)
# ---------------------------------------------------------------------------


class TestSetManual:
    async def test_returns_true_when_found(self, pool):
        pool.fetchrow.return_value = {"phone": "wa_1"}
        assert await db.set_manual("wa_1") is True

    async def test_returns_false_when_not_found(self, pool):
        pool.fetchrow.return_value = None
        assert await db.set_manual("wa_1") is False

    async def test_sql_sets_manual_and_nulls_until(self, pool):
        pool.fetchrow.return_value = {"phone": "wa_1"}
        await db.set_manual("wa_1")
        sql = pool.fetchrow.call_args.args[0]
        assert "mode = 'manual'" in sql
        assert "manual_until = NULL" in sql

    async def test_error_raises(self, pool):
        pool.fetchrow.side_effect = RuntimeError("db")
        with pytest.raises(RuntimeError):
            await db.set_manual("wa_1")


class TestSetAuto:
    async def test_returns_true_when_found(self, pool):
        pool.fetchrow.return_value = {"phone": "wa_1"}
        assert await db.set_auto("wa_1") is True

    async def test_returns_false_when_not_found(self, pool):
        pool.fetchrow.return_value = None
        assert await db.set_auto("wa_1") is False

    async def test_sql_sets_auto(self, pool):
        pool.fetchrow.return_value = {"phone": "wa_1"}
        await db.set_auto("wa_1")
        sql = pool.fetchrow.call_args.args[0]
        assert "mode = 'auto'" in sql


class TestListActiveLeads:
    async def test_no_stage_uses_active_stages_array(self, pool):
        pool.fetch.return_value = []
        await db.list_active_leads()
        sql, *params = pool.fetch.call_args.args
        assert "funnel_stage = ANY" in sql
        # первый параметр — список активных стадий
        assert isinstance(params[0], list)
        assert "new" in params[0]

    async def test_with_stage_filters(self, pool):
        pool.fetch.return_value = []
        await db.list_active_leads(stage="qualifying")
        sql, *params = pool.fetch.call_args.args
        assert "funnel_stage = $1" in sql
        assert params[0] == "qualifying"

    async def test_limit_passed(self, pool):
        pool.fetch.return_value = []
        await db.list_active_leads(limit=5)
        params = pool.fetch.call_args.args[1:]
        assert 5 in params

    async def test_returns_dicts(self, pool):
        pool.fetch.return_value = [{"phone": "wa_1"}]
        out = await db.list_active_leads()
        assert out == [{"phone": "wa_1"}]


class TestListWhitelist:
    async def test_queries_bot_whitelist(self, pool):
        pool.fetch.return_value = []
        await db.list_whitelist()
        sql = pool.fetch.call_args.args[0]
        assert "FROM bot_whitelist" in sql

    async def test_returns_dicts(self, pool):
        pool.fetch.return_value = [{"phone": "wa_1", "reason": "VIP"}]
        out = await db.list_whitelist()
        assert out[0]["reason"] == "VIP"
