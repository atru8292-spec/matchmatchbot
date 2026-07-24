"""Unit-тесты escalation.py (блок 8).

Стратегия изоляции:
- _send_telegram заменяется AsyncMock через monkeypatch — HTTP не идёт, Telegram не шлётся.
- Для тестов _send_telegram напрямую: httpx.AsyncClient мокается классом-заглушкой
  (паттерн из test_sender.py).
- Throttle: autouse-фикстура очищает escalation._last_sent перед каждым тестом
  чтобы изолировать тесты друг от друга.
- Токены задаются через monkeypatch.setattr(escalation.settings, attr, value),
  чтобы отличать manager-бота от alerts-бота по первому аргументу _send_telegram.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import escalation


# ---------------------------------------------------------------------------
# autouse: очистка in-memory throttle перед каждым тестом
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_last_sent():
    """Обнулить _last_sent до и после каждого теста."""
    escalation._last_sent.clear()
    yield
    escalation._last_sent.clear()


# ---------------------------------------------------------------------------
# Вспомогательная фабрика фейк httpx.AsyncClient (паттерн из test_sender.py)
# ---------------------------------------------------------------------------

def _make_http_client_cls(*, post_exc=None):
    """Фабрика класса-заглушки для httpx.AsyncClient.

    post_exc — если задан, client.post() бросает это исключение.
    """
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    _post = (
        AsyncMock(side_effect=post_exc)
        if post_exc is not None
        else AsyncMock(return_value=mock_response)
    )

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.post = _post

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    _FakeAsyncClient._post_mock = _post
    return _FakeAsyncClient


# ---------------------------------------------------------------------------
# Вспомогательная фабрика лида
# ---------------------------------------------------------------------------

def _lead(
    phone: str = "wa_79635378880",
    whatsapp_name: str = "Carlos",
    funnel_stage: str = "pitched",
) -> dict:
    return {"phone": phone, "whatsapp_name": whatsapp_name, "funnel_stage": funnel_stage}


# ---------------------------------------------------------------------------
# _send_business_alert — рассылка business-алертов ВСЕМ получателям (не одному)
# ---------------------------------------------------------------------------

class TestSendBusinessAlert:
    async def test_single_recipient_unchanged(self, monkeypatch):
        """Один chat_id (без запятой) — как раньше, один вызов _send_telegram."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "111")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation._send_business_alert("текст")

        send_mock.assert_awaited_once_with("MGR", "111", "текст", None)

    async def test_multiple_recipients_all_get_it(self, monkeypatch):
        """chat_id через запятую → _send_telegram вызван для КАЖДОГО получателя."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "111,222,333")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation._send_business_alert("текст", {"kb": 1})

        assert send_mock.await_count == 3
        chat_ids = [c.args[1] for c in send_mock.await_args_list]
        assert chat_ids == ["111", "222", "333"]
        for c in send_mock.await_args_list:
            assert c.args == ("MGR", c.args[1], "текст", {"kb": 1})

    async def test_notify_escalation_reaches_all_recipients(self, monkeypatch):
        """Реальный business-алерт (notify_escalation) уходит всем получателям списка."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "111,222")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_escalation(_lead(), "razón", "hola")

        assert send_mock.await_count == 2


# ---------------------------------------------------------------------------
# Тесты _wa_link
# ---------------------------------------------------------------------------

class TestWaLink:

    def test_wa_prefix_stripped(self):
        assert escalation._wa_link("wa_79991234567") == "https://wa.me/79991234567"

    def test_mx_number(self):
        assert escalation._wa_link("wa_5215551234567") == "https://wa.me/5215551234567"

    def test_empty_string(self):
        assert escalation._wa_link("") == "https://wa.me/"


# ---------------------------------------------------------------------------
# Тесты _lead_name
# ---------------------------------------------------------------------------

class TestLeadName:

    def test_whatsapp_name_priority(self):
        """whatsapp_name используется в первую очередь, name игнорируется."""
        assert escalation._lead_name({"whatsapp_name": "Carlos", "name": "X"}) == "Carlos"

    def test_name_fallback(self):
        """Нет whatsapp_name → name."""
        assert escalation._lead_name({"name": "Ana"}) == "Ana"

    def test_empty_whatsapp_name_falls_back_to_name(self):
        """whatsapp_name='' (falsy) → name."""
        assert escalation._lead_name({"whatsapp_name": "", "name": "Ana"}) == "Ana"

    def test_empty_dict_returns_lid(self):
        assert escalation._lead_name({}) == "лид"

    def test_none_returns_lid(self):
        assert escalation._lead_name(None) == "лид"

    def test_no_name_fields_returns_lid(self):
        assert escalation._lead_name({"age": 35}) == "лид"


# ---------------------------------------------------------------------------
# Тесты notify_escalation
# ---------------------------------------------------------------------------

class TestNotifyEscalation:

    async def test_uses_manager_token_not_alerts(self, monkeypatch):
        """notify_escalation → _send_telegram вызван с manager-токеном, не alerts."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_escalation(_lead(), "Хочет записаться", "Quiero agendar")

        send_mock.assert_awaited_once()
        assert send_mock.call_args.args[0] == "MGR"

    async def test_text_contains_required_parts(self, monkeypatch):
        """Текст: эмодзи, имя, название стадии, reason, last_msg, wa.me ссылка."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        lead = _lead(phone="wa_79635378880", whatsapp_name="Carlos", funnel_stage="pitched")
        await escalation.notify_escalation(lead, "Хочет записаться", "Quiero agendar")

        text = send_mock.call_args.args[2]
        assert "🤍 Клиент готов к следующему шагу" in text
        assert "Carlos" in text
        assert "Показала цену" in text   # stage_label("pitched")
        assert "Хочет записаться" in text
        assert "Quiero agendar" in text
        assert "https://wa.me/79635378880" in text

    async def test_empty_reason_no_arrow(self, monkeypatch):
        """reason='' → 'Стадия: <название>' без стрелки ' → '."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_escalation(_lead(funnel_stage="pitched"), "", "last msg")

        text = send_mock.call_args.args[2]
        assert "Стадия: Показала цену" in text
        assert " → " not in text

    async def test_no_throttle_two_calls_sends_twice(self, monkeypatch):
        """Business-алерты не throttlятся: два подряд → _send_telegram вызван дважды."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        lead = _lead()
        await escalation.notify_escalation(lead, "раз", "msg1")
        await escalation.notify_escalation(lead, "два", "msg2")

        assert send_mock.await_count == 2


# ---------------------------------------------------------------------------
# Тесты notify_vip
# ---------------------------------------------------------------------------

class TestNotifyVip:

    async def test_uses_manager_token(self, monkeypatch):
        """notify_vip → _send_telegram с manager-токеном."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_vip(_lead())

        assert send_mock.call_args.args[0] == "MGR"

    async def test_text_content(self, monkeypatch):
        """Текст: '🤍 Написал клиент из списка', имя, wa.me ссылка, без личного обращения."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        lead = _lead(phone="wa_79635378880", whatsapp_name="Carlos")
        await escalation.notify_vip(lead)

        text = send_mock.call_args.args[2]
        assert "🤍 Написал клиент из списка" in text
        assert "Carlos" in text
        assert "https://wa.me/79635378880" in text
        assert "твой" not in text.lower() and "ответь" not in text.lower()


# ---------------------------------------------------------------------------
# Тесты notify_block
# ---------------------------------------------------------------------------

class TestNotifyBlock:

    async def test_uses_manager_token(self, monkeypatch):
        """notify_block → _send_telegram с manager-токеном."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_block(_lead(), "Ищет интим-услуги")

        assert send_mock.call_args.args[0] == "MGR"

    async def test_text_content(self, monkeypatch):
        """Текст: '🔕 Бот прекратил диалог', 'Причина:', имя, wa.me ссылка."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        lead = _lead(phone="wa_79635378880", whatsapp_name="Carlos")
        await escalation.notify_block(lead, "Ищет интим-услуги")

        text = send_mock.call_args.args[2]
        assert "🔕 Бот прекратил диалог" in text
        assert "заблокир" not in text.lower()  # нейтральная формулировка для Ани
        assert "Причина: Ищет интим-услуги" in text
        assert "Лид: Carlos" in text
        assert "https://wa.me/79635378880" in text


# ---------------------------------------------------------------------------
# Тесты notify_error
# ---------------------------------------------------------------------------

class TestNotifyError:

    async def test_uses_alerts_token_not_manager(self, monkeypatch):
        """notify_error → _send_telegram с alerts-токеном, не manager."""
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_error("db.insert", "Connection refused", "wa_123")

        send_mock.assert_awaited_once()
        assert send_mock.call_args.args[0] == "ALR"

    async def test_text_with_phone(self, monkeypatch):
        """Текст с phone: '🔧 Ошибка:', 'Лид: wa_123', 'Время: ... UTC', текст ошибки."""
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_error("sender.send_one", "TimeoutError", "wa_123")

        text = send_mock.call_args.args[2]
        assert "🔧 Ошибка: sender.send_one" in text
        assert "Лид: wa_123" in text
        assert "Время:" in text
        assert "TimeoutError" in text

    async def test_text_without_phone_no_lid_line(self, monkeypatch):
        """phone=None → строка 'Лид:' ОТСУТСТВУЕТ."""
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_error("ai.generate_reply", "OpenAI timeout")

        text = send_mock.call_args.args[2]
        assert "Лид:" not in text
        assert "🔧 Ошибка: ai.generate_reply" in text

    async def test_throttle_same_key_sends_once(self, monkeypatch):
        """Два вызова с одним (where, phone) → _send_telegram вызван ТОЛЬКО раз."""
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_error("db.query", "err", "wa_111")
        await escalation.notify_error("db.query", "другая ошибка", "wa_111")

        assert send_mock.await_count == 1, (
            "Второй вызов с тем же ключом должен быть throttled"
        )

    async def test_throttle_different_where_sends_both(self, monkeypatch):
        """Разные where → разные ключи throttle → оба шлются."""
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_error("module.A", "err", "wa_111")
        await escalation.notify_error("module.B", "err", "wa_111")

        assert send_mock.await_count == 2

    async def test_throttle_different_phone_sends_both(self, monkeypatch):
        """Разные phone → разные ключи throttle → оба шлются."""
        monkeypatch.setattr(escalation.settings, "tg_alerts_bot_token", "ALR")
        monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "C2")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)

        await escalation.notify_error("module.X", "err", "wa_111")
        await escalation.notify_error("module.X", "err", "wa_222")

        assert send_mock.await_count == 2


# ---------------------------------------------------------------------------
# Тесты _send_telegram на уровне httpx (мокаем httpx.AsyncClient)
# ---------------------------------------------------------------------------

class TestSendTelegramDirect:

    async def test_empty_token_no_http_call(self, monkeypatch):
        """token='' → httpx.post НЕ вызывается, исключения нет."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(escalation.httpx, "AsyncClient", cls)

        await escalation._send_telegram("", "12345", "test text")

        cls._post_mock.assert_not_awaited()

    async def test_empty_chat_id_no_http_call(self, monkeypatch):
        """chat_id='' → httpx.post НЕ вызывается."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(escalation.httpx, "AsyncClient", cls)

        await escalation._send_telegram("some-token", "", "test text")

        cls._post_mock.assert_not_awaited()

    async def test_valid_token_calls_telegram_api(self, monkeypatch):
        """Валидный токен → httpx.post вызван с правильным Telegram URL."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(escalation.httpx, "AsyncClient", cls)

        await escalation._send_telegram("tok123", "chat999", "hello")

        cls._post_mock.assert_awaited_once()
        url = cls._post_mock.call_args.args[0]
        assert "api.telegram.org/bottok123/sendMessage" in url

    async def test_http_exception_does_not_raise(self, monkeypatch):
        """httpx.post бросает Exception → _send_telegram проглатывает, не бросает наружу."""
        cls = _make_http_client_cls(post_exc=Exception("network error"))
        monkeypatch.setattr(escalation.httpx, "AsyncClient", cls)

        # не должно бросить
        await escalation._send_telegram("valid-token", "12345", "test text")


# ---------------------------------------------------------------------------
# Блок 11: inline-клавиатуры + проброс reply_markup + notify_photo_review
# ---------------------------------------------------------------------------


class TestKeyboards:
    def test_lead_action_kb_has_takeover_and_block(self):
        kb = escalation.lead_action_kb("wa_1")
        data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        assert "mb:takeover:wa_1" in data
        assert "mb:block:wa_1" in data

    def test_photo_action_kb_actions(self):
        kb = escalation.photo_action_kb("wa_1")
        data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        assert "mb:photo_ok:wa_1" in data
        assert "mb:photo_retry:wa_1" in data
        assert "mb:photo_reject:wa_1" in data

    def test_card_action_kb_auto_shows_takeover(self):
        kb = escalation.card_action_kb("wa_1", is_manual=False)
        data = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]
        assert "mb:takeover:wa_1" in data

    def test_card_action_kb_manual_shows_release(self):
        kb = escalation.card_action_kb("wa_1", is_manual=True)
        data = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]
        assert "mb:release:wa_1" in data

    def test_card_action_kb_has_whatsapp_url_button(self):
        """Под карточкой — кнопка-ссылка «Открыть переписку в WhatsApp» (wa.me)."""
        kb = escalation.card_action_kb("wa_79635378880", is_manual=False)
        urls = [b.get("url") for row in kb["inline_keyboard"] for b in row if b.get("url")]
        assert any(u == "https://wa.me/79635378880" for u in urls)

    def test_callback_data_within_64_bytes(self):
        """Telegram лимит callback_data — 64 байта."""
        kb = escalation.photo_action_kb("wa_521234567890")
        for row in kb["inline_keyboard"]:
            for b in row:
                assert len(b["callback_data"].encode()) <= 64


class TestReplyMarkupPassthrough:
    async def test_notify_escalation_passes_keyboard(self, monkeypatch):
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)
        await escalation.notify_escalation(_lead(), "reason", "last")
        kb = send_mock.call_args.args[3]
        assert kb and "inline_keyboard" in kb

    async def test_notify_photo_review_uses_photo_kb(self, monkeypatch):
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)
        await escalation.notify_photo_review(_lead(), "размыто")
        text = send_mock.call_args.args[2]
        kb = send_mock.call_args.args[3]
        assert "ручной проверке" in text
        data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        assert "mb:photo_ok:wa_79635378880" in data

    async def test_send_to_manager_uses_manager_token(self, monkeypatch):
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)
        await escalation.send_to_manager("привет")
        assert send_mock.call_args.args[0] == "MGR"
        assert send_mock.call_args.args[1] == "C1"

    async def test_send_telegram_includes_reply_markup_in_payload(self, monkeypatch):
        """reply_markup доходит до payload запроса Telegram."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(escalation.httpx, "AsyncClient", cls)
        kb = {"inline_keyboard": [[{"text": "x", "callback_data": "mb:card:wa_1"}]]}
        await escalation._send_telegram("TOK", "CHAT", "text", kb)
        payload = cls._post_mock.call_args.kwargs["json"]
        assert payload["reply_markup"] == kb


class TestNotifyPayment:
    async def test_uses_manager_token_and_kb(self, monkeypatch):
        monkeypatch.setattr(escalation.settings, "tg_manager_bot_token", "MGR")
        monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "C1")
        send_mock = AsyncMock()
        monkeypatch.setattr(escalation, "_send_telegram", send_mock)
        await escalation.notify_payment(_lead(phone="wa_79635378880", whatsapp_name="Carlos"))
        assert send_mock.call_args.args[0] == "MGR"
        text = send_mock.call_args.args[2]
        kb = send_mock.call_args.args[3]
        assert "оплат" in text.lower()
        data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        assert "mb:payment_ok:wa_79635378880" in data

    def test_payment_target_kb_has_event_and_client(self):
        kb = escalation.payment_target_kb("wa_1")
        data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        assert "mb:payment_event:wa_1" in data
        assert "mb:payment_client:wa_1" in data
