"""Эхо-сообщения Wazzup (шаг 5а, 2026-10-15): Аня ответила лиду с телефона → чат в ручной
режим, бот молчит. Своё эхо (бот/CRM через API) режим не трогает.

db/sender замоканы, сети нет.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import db
import main
import sender
from normalize import parse_echo

PHONE = "wa_5215512345678"


def _echo(text="Hola, soy Anna", mid="e1", chat="5215512345678", type_="text"):
    return {"messageId": mid, "chatId": chat, "chatType": "whatsapp", "type": type_,
            "text": text, "isEcho": True, "status": "sent",
            "dateTime": "2026-10-15T12:00:00.000Z"}


class TestParseEcho:
    def test_parses_text_echo(self):
        e = parse_echo(_echo())
        assert e.phone == PHONE and e.text == "Hola, soy Anna" and e.external_message_id == "wa_e1"

    @pytest.mark.parametrize("msg", [
        {"isEcho": False, "chatType": "whatsapp", "chatId": "1", "type": "text", "text": "x"},
        {"isEcho": True, "chatType": "whatsgroup", "chatId": "1", "type": "text", "text": "x"},
        {"isEcho": True, "chatType": "whatsapp", "chatId": "", "type": "text", "text": "x"},
        "не dict", None,
    ])
    def test_not_echo_or_bad(self, msg):
        assert parse_echo(msg) is None

    def test_media_without_text(self):
        e = parse_echo(_echo(text=None, type_="image"))
        assert e.text == "" and e.content_type == "photo"


class TestRecentSentMemory:
    def test_remember_and_match_normalized(self):
        sender._recent_sent.clear()
        sender.remember_sent("5215512345678", "Hola  Carlos ✨")
        assert sender.was_sent_recently("wa_525512345678", "hola carlos ✨")  # другая форма номера
        assert not sender.was_sent_recently(PHONE, "otro texto")
        assert not sender.was_sent_recently("wa_79990000000", "Hola Carlos ✨")

    def test_window_expires(self, monkeypatch):
        sender._recent_sent.clear()
        t = [1000.0]
        monkeypatch.setattr(sender.time, "monotonic", lambda: t[0])
        sender.remember_sent(PHONE, "hola")
        t[0] += sender.RECENT_SENT_WINDOW_SEC + 1
        assert not sender.was_sent_recently(PHONE, "hola")

    def test_garbage_safe(self):
        sender.remember_sent("", "x")
        sender.remember_sent(PHONE, "")
        assert not sender.was_sent_recently("", "x")


@pytest.fixture()
def echo_db(monkeypatch):
    monkeypatch.setattr(db, "is_ready", lambda: True)
    monkeypatch.setattr(db, "resolve_lead_phone", AsyncMock(side_effect=lambda p: p))
    monkeypatch.setattr(db, "outbound_text_recent", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "upsert_lead", AsyncMock())
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": PHONE, "mode": "auto"}))
    set_manual = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "set_manual", set_manual)
    ins = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "insert_message", ins)
    sender._recent_sent.clear()
    return set_manual, ins


class TestHandleEcho:
    async def test_human_echo_switches_to_manual(self, echo_db):
        set_manual, ins = echo_db
        await main._handle_incoming(_echo())
        set_manual.assert_awaited_once_with(PHONE)
        args, kwargs = ins.await_args
        assert args[:3] == (PHONE, "outbound", "anna")
        assert kwargs["meta"]["source"] == "echo" and kwargs["processed"] is True

    async def test_own_bot_echo_in_memory_ignored(self, echo_db):
        set_manual, ins = echo_db
        sender.remember_sent(PHONE, "Hola, soy Anna")
        await main._handle_incoming(_echo())
        set_manual.assert_not_awaited(); ins.assert_not_awaited()

    async def test_own_echo_in_db_ignored(self, echo_db, monkeypatch):
        """После рестарта памяти нет — сверяемся с messages в БД."""
        set_manual, ins = echo_db
        monkeypatch.setattr(db, "outbound_text_recent", AsyncMock(return_value=True))
        await main._handle_incoming(_echo())
        set_manual.assert_not_awaited(); ins.assert_not_awaited()

    async def test_media_echo_ignored(self, echo_db):
        set_manual, ins = echo_db
        await main._handle_incoming(_echo(text=None, type_="image"))
        set_manual.assert_not_awaited(); ins.assert_not_awaited()

    async def test_already_manual_no_double_takeover(self, echo_db, monkeypatch):
        set_manual, ins = echo_db
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"mode": "manual"}))
        await main._handle_incoming(_echo())
        set_manual.assert_not_awaited()
        ins.assert_awaited_once()  # но сообщение Ани в историю пишем

    async def test_new_contact_created_manual(self, echo_db, monkeypatch):
        """Аня написала первой новому номеру → лид создаётся сразу в ручном режиме."""
        set_manual, ins = echo_db
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        await main._handle_incoming(_echo())
        db.upsert_lead.assert_awaited_once_with(PHONE)
        set_manual.assert_awaited_once_with(PHONE)

    async def test_echo_never_triggers_debounce(self, echo_db, monkeypatch):
        from unittest.mock import MagicMock
        deb = MagicMock(); deb.trigger = AsyncMock()
        monkeypatch.setattr(main, "debouncer", deb)
        await main._handle_incoming(_echo())
        deb.trigger.assert_not_awaited()

    async def test_db_error_does_not_raise(self, echo_db, monkeypatch):
        monkeypatch.setattr(db, "set_manual", AsyncMock(side_effect=RuntimeError("db")))
        await main._handle_incoming(_echo())  # не падает (лог внутри _handle_incoming)


class TestSendOneRemembers:
    async def test_send_one_remembers_before_post(self, monkeypatch):
        sender._recent_sent.clear()

        class _Fail:
            async def __aenter__(self): raise RuntimeError("no network in tests")
            async def __aexit__(self, *a): return False

        monkeypatch.setattr(sender.httpx, "AsyncClient", lambda **kw: _Fail())
        monkeypatch.setattr(sender.escalation, "notify_error", AsyncMock())
        assert await sender.send_one("5215512345678", "texto x") is False
        assert sender.was_sent_recently(PHONE, "texto x")
