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
    assert "нет данных" in env["escalation"].await_args.args[1]
    env["send"].assert_not_awaited()


async def test_rag_nothing_sent_sets_manual_and_alerts_anya(monkeypatch, env):
    """Sender отбросил все пузыри (например, ссылка уже была) → Аня, а не тишина."""
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag(
        "respond", ["Puedes comprar tu boleto aquí: [event_link]"])))
    await main._run_ai(PHONE, LEAD, "¿cómo compro?")
    env["fields"].assert_any_await(PHONE, mode="manual")
    env["escalation"].assert_awaited()
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
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag("respond", ["Es en Polanco."])))
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={**LEAD, "mode": "manual"}))
    await main._run_ai(PHONE, {**LEAD, "mode": "auto"}, "¿dónde es?")
    env["send"].assert_not_awaited()


async def test_rag_reply_sent_if_still_auto(monkeypatch, env):
    env["send"].return_value = 1
    monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=_rag("respond", ["Es en Polanco."])))
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={**LEAD, "mode": "auto"}))
    await main._run_ai(PHONE, {**LEAD, "mode": "auto"}, "¿dónde es?")
    env["send"].assert_awaited_once()
