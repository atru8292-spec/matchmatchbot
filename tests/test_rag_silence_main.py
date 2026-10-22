"""Инвариант молчания режима rag на уровне main (решение владелицы 2026-10-22):
лид не остаётся без ответа и без человека — ручной режим + алерт Ане."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import ai
import db
import main

PHONE = "wa_5215500000009"
LEAD = {"phone": PHONE, "name": "Carlos", "funnel_stage": "qualifying"}


@pytest.fixture
def env(monkeypatch):
    m = {
        "history": AsyncMock(return_value=[]),
        "fields": AsyncMock(),
        "title": AsyncMock(return_value=None),
        "escalation": AsyncMock(),
        "bot_silent": AsyncMock(),
        "error": AsyncMock(),
        "send": AsyncMock(return_value=0),
        "stage": AsyncMock(),
        "timer": AsyncMock(),
        "rag_state": AsyncMock(),
    }
    monkeypatch.setattr(db, "get_conversation_history", m["history"])
    monkeypatch.setattr(db, "update_lead_fields", m["fields"])
    monkeypatch.setattr(db, "get_scenario_title", m["title"])
    monkeypatch.setattr(db, "set_funnel_stage", m["stage"])
    monkeypatch.setattr(db, "reset_followup_timer", m["timer"])
    monkeypatch.setattr(db, "update_rag_state", m["rag_state"])
    monkeypatch.setattr(main.escalation, "notify_escalation", m["escalation"])
    monkeypatch.setattr(main.escalation, "notify_bot_silent", m["bot_silent"])
    monkeypatch.setattr(main.escalation, "notify_error", m["error"])
    monkeypatch.setattr(main.sender, "send", m["send"])
    return m


def _rag(action, messages, **extra):
    return {"messages": messages, "funnel_stage": None, "action": action, "extracted": {},
            "needs_escalation": action == "silent", "used_scenario_id": None,
            "send_event_photo": False, "send_event_video": False,
            "proposed_videocall_at": None, "rag_meta": {}, **extra}


async def test_silent_sets_manual_and_alerts_anya(monkeypatch, env):
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag(
        "silent", [], silent_reason="Режим rag: нет данных в базе знаний")))
    await main._run_ai(PHONE, LEAD, "¿hay estacionamiento?")
    env["fields"].assert_any_await(PHONE, mode="manual")
    # новый алерт простым текстом: причина одной фразой, старый «Клиент готов…» не шлётся
    assert env["bot_silent"].await_args.args[2] == "На этот вопрос в базе нет ответа"
    env["escalation"].assert_not_awaited()
    env["send"].assert_not_awaited()


async def test_rag_nothing_sent_sets_manual_and_alerts_anya(monkeypatch, env):
    """Sender отбросил все пузыри (например, ссылка уже была) → Аня, а не тишина."""
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag(
        "respond", ["Puedes comprar tu boleto aquí: [event_link]"])))
    await main._run_ai(PHONE, LEAD, "¿cómo compro?")
    env["fields"].assert_any_await(PHONE, mode="manual")
    env["bot_silent"].assert_awaited()
    env["escalation"].assert_not_awaited()
    env["error"].assert_awaited()


async def test_legacy_nothing_sent_unchanged(monkeypatch, env):
    """Старый режим ведёт себя как раньше: только техалерт, без ручного режима."""
    reply = _rag("respond", ["x"])
    reply.pop("rag_meta")
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=reply))
    await main._run_ai(PHONE, LEAD, "hola")
    env["error"].assert_awaited()
    env["escalation"].assert_not_awaited()
    assert all(c.kwargs.get("mode") != "manual" for c in env["fields"].await_args_list)


async def test_rag_reply_dropped_if_anya_took_over(monkeypatch, env):
    """Аня ответила вручную, пока бот генерировал (эхо → manual): ответ бота не уходит."""
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag("respond", ["Es en Roma Norte."])))
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={**LEAD, "mode": "manual"}))
    await main._run_ai(PHONE, {**LEAD, "mode": "auto"}, "¿dónde es?")
    env["send"].assert_not_awaited()


async def test_rag_reply_sent_if_still_auto(monkeypatch, env):
    env["send"].return_value = 1
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag("respond", ["Es en Roma Norte."])))
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={**LEAD, "mode": "auto"}))
    await main._run_ai(PHONE, {**LEAD, "mode": "auto"}, "¿dónde es?")
    env["send"].assert_awaited_once()


async def test_rag_failure_after_generation_manual_and_alert(monkeypatch, env):
    """Ревью 2026-10-22 (4.7): ответ сгенерирован, отправка упала — ручной режим и алерт Ане
    (техалерт — во внешнем обработчике), а не только техалерт."""
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag("respond", ["Hola"])))
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"mode": "auto"}))
    monkeypatch.setattr(main.sender, "send", AsyncMock(side_effect=RuntimeError("wazzup down")))
    with pytest.raises(RuntimeError):
        await main._run_ai(PHONE, LEAD, "hola")
    env["fields"].assert_any_await(PHONE, mode="manual")
    assert "Не получилось отправить ответ" in env["bot_silent"].await_args.args[2]


async def test_legacy_failure_after_generation_no_manual(monkeypatch, env):
    legacy = {k: v for k, v in _rag("respond", ["Hola"]).items() if k != "rag_meta"}
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=legacy))
    monkeypatch.setattr(main.sender, "send", AsyncMock(side_effect=RuntimeError("wazzup down")))
    with pytest.raises(RuntimeError):
        await main._run_ai(PHONE, LEAD, "hola")
    env["escalation"].assert_not_awaited()
