"""Защита от старых входящих (шаг 4, 2026-10-15): после простоя канала Wazzup может
дослать старые сообщения — сохраняем, но бот на них НЕ отвечает.

db/ai/sender замоканы, сети и БД нет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import ai
import db
import main

PHONE = "wa_5215512345678"


def _iso(hours_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _msg(mid: str, hours_ago: float | None, text: str = "Hola") -> dict:
    meta = {"content_type": "text"}
    if hours_ago is not None:
        meta["received_at"] = _iso(hours_ago)
    return {"id": mid, "text": text, "meta": meta,
            "created_at": datetime.now(timezone.utc)}


@pytest.fixture()
def burst(monkeypatch):
    """Обычный лид, AI отвечает 'ok'. Возвращает (mark, gen, send) для проверок."""
    mark = AsyncMock()
    monkeypatch.setattr(db, "mark_messages_processed", mark)
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(
        return_value={"whatsapp_name": "Test", "age": 35, "is_single": True}))
    monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
    monkeypatch.setattr(db, "reset_followup_timer", AsyncMock())
    monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "0"}))
    gen = AsyncMock(return_value={
        "messages": ["ok"], "funnel_stage": None, "action": "respond",
        "extracted": {}, "needs_escalation": False, "used_scenario_id": 1})
    monkeypatch.setattr(ai, "generate_reply", gen)
    send = AsyncMock(return_value=1)
    monkeypatch.setattr(main.sender, "send", send)
    return mark, gen, send


class TestParse:
    def test_wazzup_iso_z(self):
        assert main._parse_dt("2026-10-15T12:00:00.000Z") == datetime(2026, 10, 15, 12, tzinfo=timezone.utc)

    @pytest.mark.parametrize("bad", [None, "", "  ", "вчера", 123, "2026-13-45"])
    def test_garbage_none(self, bad):
        assert main._parse_dt(bad) is None

    def test_naive_datetime_treated_utc(self):
        assert main._parse_dt(datetime(2026, 1, 1)).tzinfo is not None


class TestSplitStale:
    NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)

    def _m(self, hours_ago):
        return {"meta": {"received_at": (self.NOW - timedelta(hours=hours_ago)).isoformat()}}

    def test_threshold(self):
        fresh, stale = main._split_stale([self._m(1), self._m(13), self._m(11.9)], 12, self.NOW)
        assert len(fresh) == 2 and len(stale) == 1

    def test_zero_disables(self):
        fresh, stale = main._split_stale([self._m(500)], 0, self.NOW)
        assert len(fresh) == 1 and not stale

    def test_no_time_is_fresh(self):
        """Нет ни dateTime, ни created_at → не теряем сообщение, считаем свежим."""
        fresh, stale = main._split_stale([{"meta": {}}], 12, self.NOW)
        assert len(fresh) == 1 and not stale

    def test_falls_back_to_created_at(self):
        m = {"meta": {}, "created_at": self.NOW - timedelta(days=3)}
        fresh, stale = main._split_stale([m], 12, self.NOW)
        assert stale == [m]


class TestStaleHoursSetting:
    async def test_default_when_missing(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={}))
        assert await main._stale_inbound_hours() == 12

    async def test_custom(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"stale_inbound_hours": "24"}))
        assert await main._stale_inbound_hours() == 24

    @pytest.mark.parametrize("raw", ["abc", "-5"])
    async def test_garbage_or_negative(self, monkeypatch, raw):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"stale_inbound_hours": raw}))
        assert await main._stale_inbound_hours() == 12  # опечатка не выключает защиту

    async def test_db_error_default(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(side_effect=RuntimeError("db")))
        assert await main._stale_inbound_hours() == 12


class TestBurstStale:
    async def test_all_stale_no_reply(self, monkeypatch, burst):
        mark, gen, send = burst
        msgs = [_msg("a", 48), _msg("b", 30)]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        await main._process_burst(PHONE)
        mark.assert_awaited_once_with(["a", "b"])  # помечены обработанными
        gen.assert_not_awaited()                   # AI не зовём
        send.assert_not_awaited()                  # лиду ничего не уходит

    async def test_mixed_only_fresh_answered(self, monkeypatch, burst):
        mark, gen, send = burst
        msgs = [_msg("old", 72, "hace mucho"), _msg("new", 0.1, "Hola hoy")]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        await main._process_burst(PHONE)
        assert mark.await_args_list[0].args[0] == ["old"]
        gen.assert_awaited_once()
        # в AI ушёл только свежий текст
        assert "hace mucho" not in str(gen.await_args)
        send.assert_awaited()

    async def test_fresh_answered(self, monkeypatch, burst):
        mark, gen, send = burst
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[_msg("x", 0.5)]))
        await main._process_burst(PHONE)
        gen.assert_awaited_once()

    async def test_bypass_phone_ignores_rule(self, monkeypatch, burst):
        """Тестовый номер (bypass) — правило не мешает тестам, даже со старым временем."""
        mark, gen, send = burst
        monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset({PHONE})))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[_msg("t", 100)]))
        await main._process_burst(PHONE)
        gen.assert_awaited_once()

    async def test_zero_threshold_disables(self, monkeypatch, burst):
        mark, gen, send = burst
        monkeypatch.setattr(db, "get_settings", AsyncMock(
            return_value={"bot_paused": "0", "stale_inbound_hours": "0"}))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[_msg("t", 100)]))
        await main._process_burst(PHONE)
        gen.assert_awaited_once()


class TestReceivedAtSaved:
    async def test_meta_has_received_at(self, monkeypatch):
        """dateTime из Wazzup сохраняется в meta.received_at (по нему и судим о давности)."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(db, "resolve_lead_phone", AsyncMock(side_effect=lambda p: p))
        monkeypatch.setattr(db, "upsert_lead", AsyncMock())
        monkeypatch.setattr(db, "touch_last_inbound", AsyncMock())
        ins = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "insert_message", ins)
        deb = MagicMock(); deb.trigger = AsyncMock()
        monkeypatch.setattr(main, "debouncer", deb)
        await main._handle_incoming({
            "messageId": "m1", "chatId": "5215512345678", "chatType": "whatsapp",
            "type": "text", "text": "Hola", "isEcho": False, "status": "inbound",
            "dateTime": "2026-09-20T10:00:00.000Z"})
        assert ins.await_args.kwargs["meta"]["received_at"] == "2026-09-20T10:00:00.000Z"
