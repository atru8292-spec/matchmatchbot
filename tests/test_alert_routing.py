"""Алерты 2026-10-22: «бот промолчал» простым текстом; по номеру разработки (Арина) все
business-алерты только в её чат, по остальным номерам — команде. Никогда не в оба."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import escalation
from config import settings

ARINA = "wa_79635708880"
ANYA_TEST = "wa_5215500008289"
REAL = "wa_5215614513426"


@pytest.fixture
def chats(monkeypatch):
    monkeypatch.setattr(settings, "tg_manager_chat_id", "1112311,2221513")
    monkeypatch.setattr(settings, "tg_alerts_chat_id", "1112311")
    monkeypatch.setattr(settings, "tg_manager_bot_token", "x")
    sent = []

    async def fake(token, chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
    monkeypatch.setattr(escalation, "_send_telegram", fake)
    return sent


def test_recipients():
    settings.tg_manager_chat_id, old = "1112311,2221513", settings.tg_manager_chat_id
    settings.tg_alerts_chat_id, old_a = "1112311", settings.tg_alerts_chat_id
    try:
        assert escalation.alert_recipients(ARINA) == ("1112311",)
        assert escalation.alert_recipients("wa_+7 963 570 88 80") == ("1112311",)
        assert escalation.alert_recipients(ANYA_TEST) == ("1112311", "2221513")
        assert escalation.alert_recipients(REAL) == ("1112311", "2221513")
    finally:
        settings.tg_manager_chat_id, settings.tg_alerts_chat_id = old, old_a


async def test_arina_silent_alert_only_to_her(chats):
    await escalation.notify_bot_silent({"phone": ARINA, "whatsapp_name": "Арина"}, "Hola",
                                       "Бот не нашел подтвержденного ответа и ничего не отправил, "
                                       "чтобы не соврать", "«Ayudo a hombres que buscan una relación seria…»")
    assert [c for c, _, _ in chats] == ["1112311"]
    text = chats[0][1]
    assert text.splitlines()[0] == "⚠️ Бот промолчал, ответь лиду сам"
    assert "Арина +79635708880" in text and "Последнее сообщение: «Hola»" in text
    assert "Причина: Бот не нашел подтвержденного ответа" in text
    assert "Что не прошло проверку: «Ayudo a hombres" in text
    assert "Написать в WhatsApp: https://wa.me/79635708880" in text
    for bad in ("rag", "Режим", "Стадия", "Клиент готов", "['"):
        assert bad not in text
    assert chats[0][2]["inline_keyboard"]                   # кнопки остаются


async def test_real_lead_silent_alert_to_team(chats):
    await escalation.notify_bot_silent({"phone": REAL, "name": "Sid"}, "¿aceptan tarjeta?",
                                       "Лид спрашивает про скидку, способ оплаты или возврат, это решаешь ты")
    assert [c for c, _, _ in chats] == ["1112311", "2221513"]
    assert "Что не прошло проверку" not in chats[0][1]


@pytest.mark.parametrize("phone,expected", [(ARINA, ["1112311"]), (ANYA_TEST, ["1112311", "2221513"])])
async def test_other_alerts_follow_same_routing(chats, phone, expected):
    await escalation.notify_companion_question({"phone": phone}, "voy con mi novia")
    assert [c for c, _, _ in chats] == expected


async def test_no_tester_chat_never_falls_back_to_team(chats, monkeypatch):
    monkeypatch.setattr(settings, "tg_alerts_chat_id", "")
    await escalation.notify_bot_silent({"phone": ARINA}, "Hola", "x")
    assert chats == []
