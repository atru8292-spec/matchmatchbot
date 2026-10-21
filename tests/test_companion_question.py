"""Вопрос про спутника (2026-10-21, решение владелицы): друг, подруга, девушка, пара, группа,
цена на двоих → ручной режим + алерт Ане, лиду ничего, AI не вызывается. 5,500 бот не называет.
«¿Puedo ir solo?», «busco pareja», «tengo novia» (это №9) не срабатывают.

db/ai/sender/escalation замоканы, сети и БД нет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import ai
import db
import escalation
import filters
import main

PHONE = "wa_5215512345678"

COMPANION = [
    "¿puedo ir con un amigo?",
    "¿hay precio si voy con un amigo?",
    "puedo llevar a mi novia?",
    "¿puedo ir con mi novia?",
    "Quiero ir con mi pareja",
    "voy con mi amiga, ¿se puede?",
    "mi amigo también quiere ir",
    "Un amigo quiere ir, ¿cuánto le cuesta?",
    "¿puedo invitar a un compañero del trabajo?",
    "¿puedo traer a mi hermano?",
    "puedo ir con alguien?",
    "¿puedo ir acompañado?",
    "¿cuánto cuestan dos boletos?",
    "precio para 2",
    "¿cuánto es para dos?",
    "somos tres, ¿hay descuento?",
    "vamos dos amigos",
    "¿hay precio por pareja?",
    "¿pueden ir parejas? es para parejas?",
    "queremos ir en grupo",
    "¿tienen 2x1?",
    "¿puedo ir con mi esposa?",
    "¿llevo acompañante?",
    "y si voy con otra persona?",
]

NOT_COMPANION = [
    "¿puedo ir solo?",
    "¿tengo que ir solo?",
    "voy solo",
    "busco pareja",
    "quiero encontrar pareja seria",
    "tengo novia",
    "estoy casado",
    "un amigo me recomendó tu cuenta",
    "me pasó tu número un amigo",
    "terminé con mi novia hace un año",
    "me separé de mi esposa",
    "quiero formar una familia con mi pareja ideal",
    "¿cuántas personas van?",
    "¿cuánto cuesta?",
    "quiero conocer a una mujer eslava",
    "en dos semanas te aviso",
    "lo veo para dos semanas",
    "hola, vi el evento",
    "",
]


class TestRegex:
    @pytest.mark.parametrize("text", COMPANION)
    def test_companion(self, text):
        assert filters.is_companion_question(text), text

    @pytest.mark.parametrize("text", NOT_COMPANION)
    def test_not_companion(self, text):
        assert not filters.is_companion_question(text), text


class TestDecide:
    @pytest.mark.parametrize("text", COMPANION[:6])
    def test_action(self, text):
        d = filters.decide({}, False, text, PHONE)
        assert d.action == "companion_question" and d.alert_manager
        assert not d.block_permanent

    def test_novia_question_not_blocked_even_if_not_single_known(self):
        """№9 «не холост» не должен сработать раньше: лид спрашивает, можно ли с девушкой."""
        d = filters.decide({"is_single": False}, False, "¿puedo ir con mi novia?", PHONE)
        assert d.action == "companion_question"

    def test_tengo_novia_goes_to_ai(self):
        """Утверждение «tengo novia» — не вопрос про спутника: дальше №9 через AI."""
        assert filters.decide({}, False, "tengo novia", PHONE).action == "needs_ai"

    def test_solo_goes_to_ai(self):
        assert filters.decide({}, False, "¿puedo ir solo?", PHONE).action == "needs_ai"

    def test_call_request_wins(self):
        d = filters.decide({}, False, "mejor llámame, quiero ir con un amigo", PHONE)
        assert d.action == "call_request"

    def test_optout_wins(self):
        d = filters.decide({}, False, "ya no me escribas, voy con mi amigo a otro lado", PHONE)
        assert d.action == "optout"

    def test_whitelist_wins(self):
        assert filters.decide({}, True, "¿puedo ir con un amigo?", PHONE).action == "silent_whitelist"

    def test_paused_silent(self):
        d = filters.decide({}, False, "¿puedo ir con un amigo?", PHONE, bot_paused=True)
        assert d.action == "silent"

    def test_paused_bypass_still_companion(self):
        d = filters.decide({}, False, "¿puedo ir con un amigo?", PHONE,
                           bypass_phones=frozenset({PHONE}), bot_paused=True)
        assert d.action == "companion_question"

    def test_manual_silent(self):
        lead = {"mode": "manual",
                "manual_until": datetime.now(timezone.utc) + timedelta(hours=5)}
        assert filters.decide(lead, False, "¿puedo ir con un amigo?", PHONE).action == "silent"


def _msg(mid: str, text: str) -> dict:
    t = datetime.now(timezone.utc) - timedelta(minutes=3)
    return {"id": mid, "text": text, "created_at": datetime.now(timezone.utc),
            "meta": {"content_type": "text", "received_at": t.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}


@pytest.fixture()
def burst(monkeypatch):
    m = {
        "set_manual": AsyncMock(return_value=True),
        "block_lead": AsyncMock(),
        "send": AsyncMock(return_value=1),
        "gen": AsyncMock(),
        "companion_alert": AsyncMock(),
        "error_alert": AsyncMock(),
    }
    monkeypatch.setattr(db, "mark_messages_processed", AsyncMock())
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(
        return_value={"phone": PHONE, "whatsapp_name": "Carlos", "mode": "auto"}))
    monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "whitelist_no_alert", AsyncMock(return_value=True))
    monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "0"}))
    monkeypatch.setattr(db, "set_manual", m["set_manual"])
    monkeypatch.setattr(db, "block_lead", m["block_lead"])
    monkeypatch.setattr(main.sender, "send", m["send"])
    monkeypatch.setattr(ai, "generate_reply", m["gen"])
    monkeypatch.setattr(escalation, "notify_companion_question", m["companion_alert"])
    monkeypatch.setattr(escalation, "notify_error", m["error_alert"])
    return m


class TestBurst:
    async def test_manual_alert_nothing_sent(self, monkeypatch, burst):
        text = "¿hay precio si voy con un amigo?"
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[_msg("c", text)]))
        await main._process_burst(PHONE)
        burst["set_manual"].assert_awaited_once_with(PHONE)
        burst["companion_alert"].assert_awaited_once()
        lead, last = burst["companion_alert"].await_args.args
        assert lead["phone"] == PHONE and last == text
        assert burst["companion_alert"].await_args.kwargs["manual_ok"] is True
        burst["send"].assert_not_awaited()        # лиду ничего
        burst["gen"].assert_not_awaited()         # AI не зовём
        burst["block_lead"].assert_not_awaited()  # не блокируем

    async def test_manual_failure_warns_anya(self, monkeypatch, burst):
        burst["set_manual"].side_effect = RuntimeError("db")
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "¿puedo llevar a mi novia?")]))
        await main._process_burst(PHONE)
        burst["error_alert"].assert_awaited_once()
        assert burst["companion_alert"].await_args.kwargs["manual_ok"] is False
        burst["send"].assert_not_awaited()


class TestAlertText:
    async def test_alert_has_phone_and_message(self, monkeypatch):
        sent = AsyncMock()
        monkeypatch.setattr(escalation, "_send_business_alert", sent)
        await escalation.notify_companion_question({"phone": PHONE, "whatsapp_name": "Carlos"},
                                                   "¿puedo ir con un amigo?")
        text = sent.await_args.args[0]
        assert "спутника" in text and "¿puedo ir con un amigo?" in text
        assert "ручном режиме" in text
