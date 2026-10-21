"""Переключатель reply_mode (legacy | rag | rag_test) и диспетчер в ai.generate_reply.
Плюс чистая функция сверки scripts/restore_scenarios.diff_rows. Сети и БД нет."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import ai
import ai_conversation
import db
import escalation
import reply_mode
from scripts import restore_scenarios

TEST_PHONE = "wa_5215500000001"
OTHER_PHONE = "wa_5215599999999"


def _setting(value=None, exc=None):
    return AsyncMock(return_value=value, side_effect=exc)


class TestResolve:
    async def test_no_key_is_legacy(self, monkeypatch):
        monkeypatch.setattr(db, "get_setting", _setting(None))
        assert await reply_mode.resolve(TEST_PHONE) == "legacy"

    async def test_read_error_is_legacy(self, monkeypatch):
        monkeypatch.setattr(db, "get_setting", _setting(exc=RuntimeError("db down")))
        assert await reply_mode.resolve(TEST_PHONE) == "legacy"

    @pytest.mark.parametrize("raw", ["", "  ", "legacy", "LEGACY", "conversation", "1", "rag2"])
    async def test_unknown_or_legacy_values(self, monkeypatch, raw):
        monkeypatch.setattr(db, "get_setting", _setting(raw))
        assert await reply_mode.resolve(TEST_PHONE) == "legacy"

    async def test_rag_for_everyone(self, monkeypatch):
        monkeypatch.setattr(db, "get_setting", _setting(" RAG "))
        assert await reply_mode.resolve(OTHER_PHONE) == "rag"
        assert await reply_mode.resolve(None) == "rag"

    async def test_rag_test_only_for_test_numbers(self, monkeypatch):
        monkeypatch.setattr(db, "get_setting", _setting("rag_test"))
        monkeypatch.setattr(db, "bypass_phone_set",
                            AsyncMock(return_value=frozenset({TEST_PHONE})))
        assert await reply_mode.resolve(TEST_PHONE) == "rag"
        assert await reply_mode.resolve(OTHER_PHONE) == "legacy"
        assert await reply_mode.resolve(None) == "legacy"  # песочница мини-CRM без номера

    async def test_rag_test_canonical_phone(self, monkeypatch):
        """'wa_52155…' и 'wa_5255…' — один мексиканский номер (канон)."""
        monkeypatch.setattr(db, "get_setting", _setting("rag_test"))
        monkeypatch.setattr(db, "bypass_phone_set",
                            AsyncMock(return_value=frozenset({"wa_5215500000001"})))
        assert await reply_mode.resolve("wa_525500000001") == "rag"

    async def test_rag_test_bypass_error_is_legacy(self, monkeypatch):
        monkeypatch.setattr(db, "get_setting", _setting("rag_test"))
        monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(side_effect=RuntimeError("x")))
        assert await reply_mode.resolve(TEST_PHONE) == "legacy"

    async def test_reads_every_time_no_cache(self, monkeypatch):
        gs = _setting("rag")
        monkeypatch.setattr(db, "get_setting", gs)
        await reply_mode.resolve(TEST_PHONE)
        gs.return_value = "legacy"
        assert await reply_mode.resolve(TEST_PHONE) == "legacy"
        assert gs.await_count == 2


class TestDispatch:
    LEGACY_REPLY = {"messages": ["legacy"], "action": "respond"}
    RAG_REPLY = {"messages": ["rag"], "action": "respond"}

    @pytest.fixture
    def legacy(self, monkeypatch):
        m = AsyncMock(return_value=self.LEGACY_REPLY)
        monkeypatch.setattr(ai, "_generate_reply_legacy", m)
        return m

    async def test_legacy_mode_never_calls_rag(self, monkeypatch, legacy):
        monkeypatch.setattr(reply_mode, "resolve", AsyncMock(return_value="legacy"))
        rag = AsyncMock(return_value=self.RAG_REPLY)
        monkeypatch.setattr(ai_conversation, "generate_reply", rag)
        assert await ai.generate_reply({"phone": TEST_PHONE}, [], "hola") == self.LEGACY_REPLY
        rag.assert_not_awaited()

    async def test_rag_mode_uses_rag(self, monkeypatch, legacy):
        monkeypatch.setattr(reply_mode, "resolve", AsyncMock(return_value="rag"))
        monkeypatch.setattr(ai_conversation, "generate_reply", AsyncMock(return_value=self.RAG_REPLY))
        assert await ai.generate_reply({"phone": TEST_PHONE}, [], "hola") == self.RAG_REPLY
        legacy.assert_not_awaited()

    async def test_rag_not_ready_is_silent_with_alert(self, monkeypatch, legacy):
        """Нет промпта режима rag → старый ответ НЕ уходит: молчание (main: ручной режим +
        алерт Ане) и техалерт (решение владелицы 2026-10-22)."""
        monkeypatch.setattr(reply_mode, "resolve", AsyncMock(return_value="rag"))

        def no_prompt():
            raise NotImplementedError("нет anna_prompt_conversation.md")
        monkeypatch.setattr(ai_conversation, "load_prompt", no_prompt)
        err = AsyncMock()
        monkeypatch.setattr(escalation, "notify_error", err)
        r = await ai.generate_reply({"phone": TEST_PHONE}, [], "hola")
        assert r["action"] == "silent" and r["messages"] == [] and r["silent_reason"]
        err.assert_awaited_once()
        legacy.assert_not_awaited()

    async def test_rag_crash_is_silent_with_alert(self, monkeypatch, legacy):
        monkeypatch.setattr(reply_mode, "resolve", AsyncMock(return_value="rag"))
        monkeypatch.setattr(ai_conversation, "generate_reply",
                            AsyncMock(side_effect=ValueError("boom")))
        err = AsyncMock()
        monkeypatch.setattr(escalation, "notify_error", err)
        r = await ai.generate_reply({"phone": TEST_PHONE}, [], "hola")
        assert r["action"] == "silent" and "сбой" in r["silent_reason"]
        err.assert_awaited_once()
        legacy.assert_not_awaited()

    async def test_no_db_is_legacy(self, monkeypatch, legacy):
        """Пула нет (как в большинстве юнит-тестов) → legacy, без исключения наружу."""
        monkeypatch.setattr(db, "get_setting", AsyncMock(side_effect=RuntimeError("no pool")))
        assert await ai.generate_reply({}, [], "hola") == self.LEGACY_REPLY


class TestRestoreDiff:
    ROW = {"id": 2, "title": "t", "trigger_description": None, "trigger_es": "evento",
           "template_es": "Hola", "links": None, "rules": "", "mode": "bot_auto",
           "ai_allowed": True, "blocks_lead": False, "is_active": True,
           "trigger_type": "reply", "embedding": "[0.1,0.2]"}

    def test_identical_no_diff(self):
        plan = restore_scenarios.diff_rows([self.ROW], [dict(self.ROW)])
        assert plan == {"update": [], "insert": [], "extra": []}

    def test_changed_fields_listed(self):
        cur = dict(self.ROW, template_es="otro", ai_allowed=False)
        plan = restore_scenarios.diff_rows([self.ROW], [cur])
        assert plan["update"] == [(2, ["template_es", "ai_allowed"])]

    def test_missing_row_inserted_extra_row_never_deleted(self):
        extra = dict(self.ROW, id=99)
        plan = restore_scenarios.diff_rows([self.ROW], [extra])
        assert plan["insert"] == [2]
        assert plan["extra"] == [99]   # только показываем, не удаляем

    def test_ids_filter(self):
        other = dict(self.ROW, id=3)
        cur = [dict(self.ROW, template_es="x"), dict(other, template_es="y")]
        plan = restore_scenarios.diff_rows([self.ROW, other], cur, ids={3})
        assert plan["update"] == [(3, ["template_es"])]


class _FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *params):
        self.calls.append((sql, params))

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Tx()


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Acq()


class TestRestoreApply:
    async def test_update_sql_and_params(self):
        row = dict(TestRestoreDiff.ROW, rules="regla")
        pool = _FakePool()
        await restore_scenarios.apply(pool, [row], {"update": [(2, ["rules", "embedding"])],
                                                     "insert": [], "extra": []})
        sql, params = pool.conn.calls[0]
        assert sql == ("UPDATE scenarios SET rules = $2, embedding = $3::vector, "
                       "updated_at = now() WHERE id = $1")
        assert params == (2, "regla", "[0.1,0.2]")   # text как есть, без json.dumps

    async def test_insert_sql_and_params(self):
        pool = _FakePool()
        await restore_scenarios.apply(pool, [TestRestoreDiff.ROW],
                                      {"update": [], "insert": [2], "extra": []})
        sql, params = pool.conn.calls[0]
        assert sql.startswith("INSERT INTO scenarios (id, title,")
        assert "::jsonb" not in sql and sql.count("::vector") == 1
        assert params[0] == 2 and len(params) == 1 + len(restore_scenarios.RESTORE_FIELDS)
