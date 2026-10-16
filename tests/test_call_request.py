"""Просьба позвонить вместо переписки (2026-10-16): «no me escribas por aquí, mejor llámame»
— НЕ opt-out. Ручной режим + бизнес-алерт Ане, лиду ничего, do_not_contact НЕ ставится.

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

CALL_PHRASES = [
    "no me escribas por aquí, mejor llámame",
    "mejor llámame",
    "Mejor llamame",                       # без ударения
    "prefiero que me llames",
    "Prefiero que me llame por la tarde",  # usted
    "háblame por teléfono",
    "llámame mejor",
    "don't text me, call me",
    "call me instead",
    "I'd prefer a call",
    "prefer a call",
    "no me escribas, llámame",             # opt-out + голый глагол
    "ya no me mandes mensajes, márcame",
    "mejor una llamada",
    "prefiero una llamada",
    "no me escribas más, mejor hablemos por teléfono",
    "mejor me llamas tú",
    "no me llames al trabajo, mejor llámame al celular",  # явная форма сильнее отрицания
    "no me hables de precios, mejor llámame",
]

PURE_OPTOUT = [
    "no me mandes mensajes",
    "stop",
    "unsubscribe",
    "ya no me escribas",
    "basta",
    "no me llames, ya no me escribas",     # отказ и от звонка — это opt-out
    "don't text me and don't call me",
]

NOT_CALL = [
    "llámame Juan",                        # голый глагол без opt-out — не просьба
    "¿me puedes llamar después?",
    "no me molestaría que me mandes más info",
    "no me llames, mejor escríbeme",
    "hola, ¿cuánto cuesta?",
    "",
]


class TestIsCallRequest:
    @pytest.mark.parametrize("text", CALL_PHRASES)
    def test_call_phrases(self, text):
        assert filters.is_call_request(text), text

    @pytest.mark.parametrize("text", PURE_OPTOUT + NOT_CALL)
    def test_not_call(self, text):
        assert not filters.is_call_request(text), text

    @pytest.mark.parametrize("text", PURE_OPTOUT)
    def test_pure_optout_still_optout(self, text):
        assert filters.is_optout(text), text

    def test_molestaria_not_optout(self):
        assert not filters.is_optout("no me molestaría que me mandes más info")


class TestDecide:
    @pytest.mark.parametrize("text", CALL_PHRASES)
    def test_call_request_action(self, text):
        d = filters.decide({}, False, text, PHONE, frozenset())
        assert d.action == "call_request" and d.alert_manager and not d.block_permanent

    def test_mixed_call_wins_over_optout(self):
        text = "no me escribas por aquí, mejor llámame"
        assert filters.is_optout(text)  # opt-out тоже совпал …
        assert filters.decide({}, False, text, PHONE).action == "call_request"  # … но звонок важнее

    @pytest.mark.parametrize("text", PURE_OPTOUT)
    def test_pure_optout_unchanged(self, text):
        assert filters.decide({}, False, text, PHONE).action == "optout"

    def test_whitelist_silent_no_alert(self):
        d = filters.decide({}, True, "mejor llámame", PHONE, whitelist_no_alert=True)
        assert d.action == "silent_whitelist" and not d.alert_manager

    def test_do_not_contact_stays_silent(self):
        d = filters.decide({"do_not_contact": True}, False, "mejor llámame", PHONE)
        assert d.action == "silent"

    def test_on_pause_still_handed_to_anna(self):
        d = filters.decide({}, False, "mejor llámame", PHONE, bot_paused=True)
        assert d.action == "call_request"

    def test_in_manual_still_alert(self):
        d = filters.decide({"mode": "manual", "manual_until": None}, False, "mejor llámame", PHONE)
        assert d.action == "call_request"

    def test_english_call_not_language_silence(self):
        """«call me instead» — не просто английский лид: Аня должна позвонить."""
        assert filters.decide({}, False, "call me instead", PHONE).action == "call_request"

    def test_molestaria_goes_to_ai(self):
        d = filters.decide({}, False, "no me molestaría que me mandes más info", PHONE)
        assert d.action == "needs_ai"


def _msg(mid: str, text: str, hours_ago: float = 0.05) -> dict:
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"id": mid, "text": text, "created_at": datetime.now(timezone.utc),
            "meta": {"content_type": "text", "received_at": t.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}


@pytest.fixture()
def burst(monkeypatch):
    """Обычный лид в auto, бот не на паузе. Возвращает моки для проверок."""
    m = {
        "set_manual": AsyncMock(return_value=True),
        "block_lead": AsyncMock(),
        "send": AsyncMock(return_value=1),
        "gen": AsyncMock(),
        "call_alert": AsyncMock(),
        "optout_alert": AsyncMock(),
        "vip_alert": AsyncMock(),
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
    monkeypatch.setattr(escalation, "notify_call_request", m["call_alert"])
    monkeypatch.setattr(escalation, "notify_optout", m["optout_alert"])
    monkeypatch.setattr(escalation, "notify_vip", m["vip_alert"])
    monkeypatch.setattr(escalation, "notify_error", m["error_alert"])
    return m


class TestBurst:
    async def test_call_request_manual_alert_no_block_no_send(self, monkeypatch, burst):
        text = "no me escribas por aquí, mejor llámame"
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[_msg("c", text)]))
        await main._process_burst(PHONE)
        burst["set_manual"].assert_awaited_once_with(PHONE)
        burst["call_alert"].assert_awaited_once()
        lead, last = burst["call_alert"].await_args.args
        assert lead["phone"] == PHONE and last == text
        burst["block_lead"].assert_not_awaited()   # do_not_contact НЕ ставим
        burst["optout_alert"].assert_not_awaited()
        burst["send"].assert_not_awaited()         # лиду ничего
        burst["gen"].assert_not_awaited()          # AI не зовём

    async def test_call_request_on_pause(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "1"}))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "mejor llámame")]))
        await main._process_burst(PHONE)
        burst["set_manual"].assert_awaited_once()
        burst["call_alert"].assert_awaited_once()
        burst["send"].assert_not_awaited()

    async def test_new_lead_alert_has_phone(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "call me instead")]))
        await main._process_burst(PHONE)
        assert burst["call_alert"].await_args.args[0]["phone"] == PHONE

    async def test_set_manual_error_still_alerts(self, monkeypatch, burst):
        monkeypatch.setattr(db, "set_manual", AsyncMock(side_effect=RuntimeError("db")))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "mejor llámame")]))
        await main._process_burst(PHONE)
        burst["call_alert"].assert_awaited_once()
        assert burst["call_alert"].await_args.kwargs["manual_ok"] is False  # Аня предупреждена
        burst["error_alert"].assert_awaited_once()                          # техалерт
        burst["send"].assert_not_awaited()

    async def test_manual_ok_flag_on_success(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "mejor llámame")]))
        await main._process_burst(PHONE)
        assert burst["call_alert"].await_args.kwargs["manual_ok"] is True
        burst["error_alert"].assert_not_awaited()

    async def test_pure_optout_unchanged(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("o", "no me mandes mensajes")]))
        await main._process_burst(PHONE)
        burst["block_lead"].assert_awaited_once()
        burst["optout_alert"].assert_awaited_once()
        burst["send"].assert_awaited_once_with(PHONE, [main._OPTOUT_CONFIRM])
        burst["set_manual"].assert_not_awaited()
        burst["call_alert"].assert_not_awaited()

    async def test_whitelist_silent_no_alert(self, monkeypatch, burst):
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("w", "no me escribas por aquí, mejor llámame")]))
        await main._process_burst(PHONE)
        for k in ("set_manual", "block_lead", "send", "call_alert", "optout_alert", "vip_alert"):
            burst[k].assert_not_awaited()

    async def test_molestaria_not_blocked(self, monkeypatch, burst):
        burst["gen"].return_value = {
            "messages": ["ok"], "funnel_stage": None, "action": "respond",
            "extracted": {}, "needs_escalation": False, "used_scenario_id": 1}
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(db, "reset_followup_timer", AsyncMock())
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("m", "no me molestaría que me mandes más info")]))
        await main._process_burst(PHONE)
        burst["block_lead"].assert_not_awaited()
        burst["call_alert"].assert_not_awaited()
        burst["gen"].assert_awaited_once()         # обычный разговор через AI


class TestStale:
    async def test_stale_call_request_manual_alert_no_block(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("s", "no me escribas, mejor llámame", hours_ago=48)]))
        await main._process_burst(PHONE)
        burst["set_manual"].assert_awaited_once_with(PHONE)
        burst["call_alert"].assert_awaited_once()
        burst["block_lead"].assert_not_awaited()
        burst["send"].assert_not_awaited()

    async def test_stale_call_request_whitelist_silent(self, monkeypatch, burst):
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("s", "mejor llámame", hours_ago=48)]))
        await main._process_burst(PHONE)
        burst["set_manual"].assert_not_awaited()
        burst["call_alert"].assert_not_awaited()

    async def test_stale_pure_optout_still_blocks(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("s", "ya no me escribas", hours_ago=48)]))
        await main._process_burst(PHONE)
        burst["block_lead"].assert_awaited_once()
        burst["call_alert"].assert_not_awaited()


class TestStaleErrors:
    async def test_stale_db_error_does_not_drop_fresh(self, monkeypatch, burst):
        """Сбой в ветке старых входящих — техалерт, свежие сообщения залпа всё равно обработаны."""
        # первый вызов (ветка старых) падает, второй (свежий залп) — нормальный лид
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(side_effect=[
            RuntimeError("db"), {"phone": PHONE, "whatsapp_name": "Carlos", "mode": "auto"}]))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[
            _msg("s", "mejor llámame", hours_ago=48), _msg("f", "mejor llámame")]))
        await main._process_burst(PHONE)
        burst["error_alert"].assert_awaited()
        burst["call_alert"].assert_awaited_once()   # свежее «mejor llámame» обработано
        burst["send"].assert_not_awaited()


class TestAlertText:
    async def test_business_alert_text(self, monkeypatch):
        sent = AsyncMock()
        monkeypatch.setattr(escalation, "_send_business_alert", sent)
        await escalation.notify_call_request(
            {"phone": PHONE, "whatsapp_name": "Carlos"}, "no me escribas, mejor llámame")
        text = sent.await_args.args[0]
        assert "просит позвонить" in text
        assert "Carlos" in text and "5215512345678" in text
        assert "no me escribas, mejor llámame" in text

    async def test_manual_failed_warning(self, monkeypatch):
        sent = AsyncMock()
        monkeypatch.setattr(escalation, "_send_business_alert", sent)
        await escalation.notify_call_request({"phone": PHONE}, "mejor llámame", manual_ok=False)
        assert "НЕ включился" in sent.await_args.args[0]

    async def test_no_name_shows_number(self, monkeypatch):
        sent = AsyncMock()
        monkeypatch.setattr(escalation, "_send_business_alert", sent)
        await escalation.notify_call_request({"phone": PHONE}, "mejor llámame")
        assert "5215512345678" in sent.await_args.args[0]


# ===== Отказ сразу от звонков и переписки = opt-out (2026-10-16) =====

CALL_AND_TEXT_OPTOUT = [
    "no me llames ni me escribas",
    "No me llames ni me escribas más",
    "ni me llames ni me escribas",
    "no me escribas ni me llames",
    "no me llames ni escribas",
    "no me llames, ni me escribas",
    "no me marques ni me mandes mensajes",
    "don't call or text me",
    "Don't call me or text me",
    "do not call or message me",
    "don't text or call me",
    "no more calls or messages",
    "No more messages or calls please",
]

# Не opt-out и не просьба позвонить — идут дальше по decide.
NOT_OPTOUT = [
    "no me molestaría que me mandes más info",
    "no me molestaría que me llames",
    "llámame Juan",
    "escríbeme cuando puedas",
    "no me llamaste ni me escribiste el link",  # жалоба в прошедшем, не императив
    "no me llames, mejor escríbeme",            # просит писать, а не прекратить контакт
    "don't call me and text me instead",        # «and» — одно вместо другого, не отказ
    "don't text and call me",
    "no more calls and messages",
]


class TestCallAndTextOptout:
    @pytest.mark.parametrize("text", CALL_AND_TEXT_OPTOUT)
    def test_is_optout(self, text):
        assert filters.is_optout(text), text
        assert not filters.is_call_request(text), text

    @pytest.mark.parametrize("text", CALL_AND_TEXT_OPTOUT)
    def test_decide_optout(self, text):
        d = filters.decide({}, False, text, PHONE)
        assert d.action == "optout" and d.alert_manager

    @pytest.mark.parametrize("text", NOT_OPTOUT)
    def test_false_positives(self, text):
        assert not filters.is_optout(text), text
        assert not filters.is_call_request(text), text
        assert filters.decide({}, False, text, PHONE).action not in ("optout", "call_request")

    @pytest.mark.parametrize("text", [
        "no me llames al trabajo, mejor llámame al celular",
        "no me llames ni me escribas por aquí, mejor llámame al celular",
        "no me escribas, mejor llámame",
    ])
    def test_explicit_call_still_wins(self, text):
        assert filters.decide({}, False, text, PHONE).action == "call_request"

    @pytest.mark.parametrize("no_alert", [True, False])
    def test_whitelist_silent(self, no_alert):
        d = filters.decide({}, True, "no me llames ni me escribas", PHONE,
                           whitelist_no_alert=no_alert)
        assert d.action == "silent_whitelist"
        assert d.alert_manager is (not no_alert)

    def test_on_pause_quiet_optout(self):
        d = filters.decide({}, False, "no me llames ni me escribas", PHONE, bot_paused=True)
        assert d.action == "optout" and d.quiet

    async def test_burst_blocks_without_call_alert(self, monkeypatch, burst):
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "no me llames ni me escribas más")]))
        await main._process_burst(PHONE)
        burst["block_lead"].assert_awaited_once()   # do_not_contact
        burst["optout_alert"].assert_awaited_once()
        burst["call_alert"].assert_not_awaited()
        burst["set_manual"].assert_not_awaited()
        burst["gen"].assert_not_awaited()

    async def test_burst_whitelist_no_block(self, monkeypatch, burst):
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(
            return_value=[_msg("c", "no me llames ni me escribas")]))
        await main._process_burst(PHONE)
        for k in ("block_lead", "send", "call_alert", "optout_alert", "vip_alert", "gen"):
            burst[k].assert_not_awaited()
