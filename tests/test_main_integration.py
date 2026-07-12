"""Тесты интеграции входящей трубы: main._handle_incoming, main._process_burst,
эндпоинт POST /webhook/wazzup/{secret}.

Изоляция от БД:
  - lifespan НЕ запускается (TestClient создаётся без `with`)
  - db.is_ready, db.upsert_lead, db.insert_message, db.get_unprocessed_inbound,
    db.mark_messages_processed — всегда через monkeypatch/AsyncMock
  - main.debouncer подменяется через monkeypatch или фикстуру

Структура:
  TestHandleIncoming  — прямой вызов await main._handle_incoming (async)
  TestProcessBurst    — прямой вызов await main._process_burst (async)
  TestWebhookWithMocks — TestClient + подняты моки (sync)
  TestWebhookRegression — регресс пинг/403/битый JSON/statuses (sync)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import ai
import db
import filters
import main
from config import settings

GOOD_SECRET = settings.wazzup_webhook_secret
BAD_SECRET = "totally-wrong-secret-000"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _text_msg(msg_id: str = "msg-100",
              phone: str = "521234567890@c.us",
              text: str = "Hola") -> dict:
    """Минимальный валидный текстовый payload Wazzup."""
    return {
        "messageId": msg_id,
        "chatId": phone,
        "chatType": "whatsapp",
        "type": "text",
        "text": text,
        "isEcho": False,
        "status": "inbound",
    }


def _image_msg(msg_id: str = "msg-200",
               phone: str = "521234567890@c.us") -> dict:
    """Минимальный валидный image payload Wazzup."""
    return {
        "messageId": msg_id,
        "chatId": phone,
        "chatType": "whatsapp",
        "type": "image",
        "contentUri": "https://cdn.wazzup24.com/media/photo.jpg",
        "isEcho": False,
        "status": "inbound",
    }


def _whatsgroup_msg(msg_id: str = "msg-grp-1",
                    chat_id: str = "79132123789-1581764243",
                    text: str = "Hola grupo") -> dict:
    """Сообщение из группового WhatsApp-чата — normalize должен дропнуть (chatType != whatsapp)."""
    return {
        "messageId": msg_id,
        "chatId": chat_id,
        "chatType": "whatsgroup",
        "type": "text",
        "text": text,
        "isEcho": False,
        "status": "inbound",
    }


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_debouncer(monkeypatch):
    """Подменить main.debouncer на MagicMock с trigger=AsyncMock."""
    fake = MagicMock()
    fake.trigger = AsyncMock()
    monkeypatch.setattr(main, "debouncer", fake)
    return fake


@pytest.fixture()
def mock_upsert_lead(monkeypatch):
    """db.upsert_lead → AsyncMock(return_value=dict с phone).

    Нужен во ВСЕХ тестах где normalize возвращает не-None и is_ready=True:
    upsert_lead вызывается первым (FK-гарантия), без мока ронит RuntimeError из _get_pool().
    """
    m = AsyncMock(return_value={"phone": "wa_mock"})
    monkeypatch.setattr(db, "upsert_lead", m)
    return m


@pytest.fixture()
def mock_touch_inbound(monkeypatch):
    """db.touch_last_inbound → AsyncMock().

    Нужен во ВСЕХ тестах _handle_incoming где is_ready=True и normalize вернул не-None:
    touch_last_inbound вызывается после upsert_lead (перед insert_message), без мока
    ронит RuntimeError из _get_pool() и прерывает цепочку вызовов.
    """
    m = AsyncMock()
    monkeypatch.setattr(db, "touch_last_inbound", m)
    return m


@pytest.fixture()
def mock_insert_true(monkeypatch):
    """db.insert_message → AsyncMock(return_value=True), db.is_ready → True."""
    monkeypatch.setattr(db, "is_ready", lambda: True)
    m = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "insert_message", m)
    return m


# ---------------------------------------------------------------------------
# Часть 1: прямые async-тесты _handle_incoming
# ---------------------------------------------------------------------------

class TestHandleIncoming:

    async def test_valid_text_insert_args_and_trigger(
        self, monkeypatch, mock_debouncer, mock_upsert_lead, mock_touch_inbound
    ):
        """Валидный текст → insert вызван ровно раз с верными аргументами, trigger — раз."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "insert_message", insert_mock)

        await main._handle_incoming(_text_msg("msg-001", "521234567890@c.us", "Hola amigo"))

        insert_mock.assert_awaited_once()
        args = insert_mock.call_args
        # позиционные: phone, direction, sender, text
        assert args.args[0] == "wa_521234567890"
        assert args.args[1] == "inbound"
        assert args.args[2] == "lead"
        assert args.args[3] == "Hola amigo"
        # именованные
        assert args.kwargs["external_message_id"] == "wa_msg-001"
        assert args.kwargs["meta"] == {"content_type": "text"}

        mock_debouncer.trigger.assert_awaited_once_with("wa_521234567890")

    async def test_duplicate_insert_false_no_trigger(
        self, monkeypatch, mock_debouncer, mock_upsert_lead, mock_touch_inbound
    ):
        """insert_message → False (дубль) → trigger НЕ вызван."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(db, "insert_message", AsyncMock(return_value=False))

        await main._handle_incoming(_text_msg())

        mock_debouncer.trigger.assert_not_awaited()

    async def test_echo_true_drops(self, monkeypatch, mock_debouncer):
        """isEcho=True → normalize возвращает None → нет upsert, нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        msg = _text_msg()
        msg["isEcho"] = True
        await main._handle_incoming(msg)

        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_status_delivered_drops(self, monkeypatch, mock_debouncer):
        """status=delivered → normalize → None → нет upsert, нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        msg = _text_msg()
        msg["status"] = "delivered"
        await main._handle_incoming(msg)

        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_telegram_chattype_drops(self, monkeypatch, mock_debouncer):
        """chatType=telegram → normalize → None → нет upsert, нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        msg = _text_msg()
        msg["chatType"] = "telegram"
        await main._handle_incoming(msg)

        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_empty_text_drops(self, monkeypatch, mock_debouncer):
        """text='' → normalize → None → нет upsert, нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        await main._handle_incoming(_text_msg(text=""))

        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_db_not_ready_no_insert_no_trigger_no_exception(
        self, monkeypatch, mock_debouncer
    ):
        """db.is_ready() → False → нет upsert, нет insert, нет trigger, исключения нет."""
        monkeypatch.setattr(db, "is_ready", lambda: False)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        # не должен бросать
        await main._handle_incoming(_text_msg())

        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_insert_exception_does_not_propagate(
        self, monkeypatch, mock_debouncer, mock_upsert_lead, mock_touch_inbound
    ):
        """Исключение в insert_message → поглощается обработчиком, не бросается наружу."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(
            db, "insert_message", AsyncMock(side_effect=Exception("DB exploded"))
        )

        # _handle_incoming должен завершиться без исключения
        await main._handle_incoming(_text_msg())

        mock_debouncer.trigger.assert_not_awaited()

    async def test_image_message_insert_with_photo_meta(
        self, monkeypatch, mock_debouncer, mock_upsert_lead, mock_touch_inbound
    ):
        """type=image → insert с meta={'content_type': 'photo'}."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "insert_message", insert_mock)

        await main._handle_incoming(_image_msg("msg-img-1", "529998887777@c.us"))

        insert_mock.assert_awaited_once()
        meta_arg = insert_mock.call_args.kwargs["meta"]
        assert meta_arg == {"content_type": "photo", "content_uri": "https://cdn.wazzup24.com/media/photo.jpg"}
        mock_debouncer.trigger.assert_awaited_once_with("wa_529998887777")

    # --- Новые тесты: FK-регрессия и порядок вызовов ---

    async def test_upsert_called_before_insert(
        self, monkeypatch, mock_debouncer
    ):
        """upsert_lead → touch_last_inbound → insert_message (FK-гарантия + метка времени).

        Реализовано через общий список call_order, заполняемый side_effect всех трёх моков.
        Регрессионный тест: если кто-то переставит строки местами — тест упадёт.
        """
        monkeypatch.setattr(db, "is_ready", lambda: True)

        call_order: list[str] = []

        async def _upsert_side(phone, **kwargs):
            call_order.append("upsert")
            return {"phone": phone}

        async def _touch_side(phone):
            call_order.append("touch")

        async def _insert_side(*args, **kwargs):
            call_order.append("insert")
            return True

        monkeypatch.setattr(db, "upsert_lead", _upsert_side)
        monkeypatch.setattr(db, "touch_last_inbound", _touch_side)
        monkeypatch.setattr(db, "insert_message", _insert_side)

        await main._handle_incoming(_text_msg("msg-order-1", "521234567890@c.us", "Hola"))

        assert call_order == ["upsert", "touch", "insert"], (
            f"Ожидали ['upsert','touch','insert'], получили {call_order}. "
            "FK на leads.phone нарушен или touch_last_inbound не между upsert и insert."
        )

    async def test_upsert_lead_called_with_correct_args(
        self, monkeypatch, mock_debouncer, mock_touch_inbound
    ):
        """upsert_lead вызван с правильным phone ('wa_'+цифры) и whatsapp_name (title-case).

        Добавляем contact.name в payload → normalize сделает .title() → "Juan Gomez".
        """
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock(return_value={"phone": "wa_521234567890"})
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        monkeypatch.setattr(db, "insert_message", AsyncMock(return_value=True))

        msg = _text_msg("msg-args-1", "521234567890@c.us", "Hola")
        msg["contact"] = {"name": "juan gomez", "phone": "521234567890"}

        await main._handle_incoming(msg)

        upsert_mock.assert_awaited_once_with(
            "wa_521234567890", whatsapp_name="Juan Gomez"
        )

    async def test_upsert_lead_no_contact_name_uses_wa_lead(
        self, monkeypatch, mock_debouncer, mock_touch_inbound
    ):
        """Без contact.name → whatsapp_name='WA Lead' (фолбэк из normalize)."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock(return_value={"phone": "wa_521234567890"})
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        monkeypatch.setattr(db, "insert_message", AsyncMock(return_value=True))

        # _text_msg не содержит contact → normalize → user_name = "WA Lead"
        await main._handle_incoming(_text_msg("msg-args-2", "521234567890@c.us", "Hola"))

        upsert_mock.assert_awaited_once_with(
            "wa_521234567890", whatsapp_name="WA Lead"
        )

    async def test_whatsgroup_drops_no_upsert_no_insert(
        self, monkeypatch, mock_debouncer
    ):
        """chatType=whatsgroup → normalize → None → ни upsert_lead, ни insert_message, ни trigger.

        Реальный кейс: Wazzup присылает сообщения из групп с chatId вида '79132123789-1581764243'.
        Бот не должен их обрабатывать, создавать лида и не должен падать.
        """
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        await main._handle_incoming(_whatsgroup_msg())

        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# Часть 2: async-тесты _process_burst (on_flush)
# ---------------------------------------------------------------------------

class TestProcessBurst:

    async def test_personal_contact_no_lead_silent_no_alert(self, monkeypatch):
        """Номер из личной базы Anna (whitelist personal_contact) БЕЗ строки-лида пишет:
        бот молчит — ни AI, ни отправки, ни алерта Ане. Проверяет, что whitelist работает
        и для номеров, которых нет в leads (у большинства этих контактов строки нет)."""
        msgs = [{"id": "uuid-pc1", "text": "Hola Anna", "meta": {"content_type": "text"}}]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        monkeypatch.setattr(db, "mark_messages_processed", AsyncMock())
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))  # нет лида
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "whitelist_no_alert", AsyncMock(return_value=True))
        gen = AsyncMock(); monkeypatch.setattr(ai, "generate_reply", gen)
        send = AsyncMock(); monkeypatch.setattr(main.sender, "send", send)
        vip = AsyncMock(); monkeypatch.setattr(main.escalation, "notify_vip", vip)

        await main._process_burst("wa_5215512345678")

        gen.assert_not_awaited()   # AI не вызываем
        send.assert_not_awaited()  # ничего не отправляем
        vip.assert_not_awaited()   # алерта Ане нет (personal_contact)

    async def test_non_empty_burst_calls_mark_with_ids(self, monkeypatch):
        """Непустой список сообщений → mark_messages_processed вызван со списком id."""
        msgs = [
            {"id": "uuid-a1", "text": "Hola", "meta": {"content_type": "text"}},
            {"id": "uuid-a2", "text": "Como estas", "meta": {"content_type": "text"}},
        ]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        mark_mock = AsyncMock()
        monkeypatch.setattr(db, "mark_messages_processed", mark_mock)
        # _process_burst теперь вызывает get_lead_by_phone и is_whitelisted после mark
        monkeypatch.setattr(
            db, "get_lead_by_phone",
            AsyncMock(return_value={"whatsapp_name": "Test", "age": 35, "is_single": True}),
        )
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(
            ai, "generate_reply",
            AsyncMock(return_value={
                "messages": ["ok"], "funnel_stage": None, "action": "respond",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 1,
            }),
        )
        # sender.send мокаем чтобы не было реального asyncio.sleep внутри
        monkeypatch.setattr(main.sender, "send", AsyncMock(return_value=1))

        await main._process_burst("wa_521000000000")

        mark_mock.assert_awaited_once_with(["uuid-a1", "uuid-a2"])

    async def test_empty_burst_no_mark(self, monkeypatch):
        """Пустой список → mark_messages_processed НЕ вызван (ранний return)."""
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=[]))
        mark_mock = AsyncMock()
        monkeypatch.setattr(db, "mark_messages_processed", mark_mock)

        await main._process_burst("wa_521000000000")

        mark_mock.assert_not_awaited()

    async def test_meta_none_does_not_crash(self, monkeypatch):
        """meta=None у сообщения → content_type берётся как None, функция не падает."""
        msgs = [{"id": "uuid-b1", "text": "test", "meta": None}]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        mark_mock = AsyncMock()
        monkeypatch.setattr(db, "mark_messages_processed", mark_mock)
        # _process_burst теперь вызывает get_lead_by_phone и is_whitelisted после mark
        monkeypatch.setattr(
            db, "get_lead_by_phone",
            AsyncMock(return_value={"whatsapp_name": "Test", "age": 35, "is_single": True}),
        )
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(
            ai, "generate_reply",
            AsyncMock(return_value={
                "messages": ["ok"], "funnel_stage": None, "action": "respond",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 1,
            }),
        )
        # sender.send мокаем чтобы не было реального asyncio.sleep внутри
        monkeypatch.setattr(main.sender, "send", AsyncMock(return_value=1))

        await main._process_burst("wa_999")

        mark_mock.assert_awaited_once_with(["uuid-b1"])

    async def test_combined_text_passed_to_log(self, monkeypatch, caplog):
        """Тексты сообщений склеиваются через \\n (проверяем через caplog).

        logger.info(..., %r, combined) форматирует combined через repr(), поэтому
        в caplog.text символ новой строки виден как буквальные два символа \\n,
        а не как реальный перевод строки.
        """
        import logging
        msgs = [
            {"id": "uuid-c1", "text": "Hola", "meta": {"content_type": "text"}},
            {"id": "uuid-c2", "text": "mundo", "meta": {"content_type": "text"}},
        ]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        monkeypatch.setattr(db, "mark_messages_processed", AsyncMock())
        # _process_burst теперь вызывает get_lead_by_phone и is_whitelisted после mark
        monkeypatch.setattr(
            db, "get_lead_by_phone",
            AsyncMock(return_value={"whatsapp_name": "Test", "age": 35, "is_single": True}),
        )
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(
            ai, "generate_reply",
            AsyncMock(return_value={
                "messages": ["ok"], "funnel_stage": None, "action": "respond",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 1,
            }),
        )
        # sender.send мокаем чтобы не было реального asyncio.sleep внутри
        monkeypatch.setattr(main.sender, "send", AsyncMock(return_value=1))

        with caplog.at_level(logging.INFO, logger="matchmatch"):
            await main._process_burst("wa_521000000001")

        # %r экранирует \n → в caplog.text ищем буквальные символы \n (r"...")
        assert r"Hola\nmundo" in caplog.text


# ---------------------------------------------------------------------------
# Часть 3: TestClient с моками (sync)
# ---------------------------------------------------------------------------

class TestWebhookWithMocks:
    """Тесты через TestClient — lifespan не стартует, все зависимости замоканы."""

    def test_two_valid_messages_insert_called_twice(self, monkeypatch):
        """POST с двумя сообщениями → upsert+insert вызваны по 2 раза → 200."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock(return_value={"phone": "wa_mock"})
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        monkeypatch.setattr(db, "touch_last_inbound", AsyncMock())
        insert_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "insert_message", insert_mock)
        fake_deb = MagicMock()
        fake_deb.trigger = AsyncMock()
        monkeypatch.setattr(main, "debouncer", fake_deb)

        client = TestClient(main.app)
        payload = {
            "messages": [
                _text_msg("msg-300", "521111111111@c.us", "Primer mensaje"),
                _text_msg("msg-301", "522222222222@c.us", "Segundo mensaje"),
            ]
        }
        response = client.post(f"/webhook/wazzup/{GOOD_SECRET}", json=payload)

        assert response.status_code == 200
        assert upsert_mock.await_count == 2
        assert insert_mock.await_count == 2

    def test_image_message_insert_with_photo_meta_via_endpoint(self, monkeypatch):
        """type=image через эндпоинт → meta content_type='photo'."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(db, "upsert_lead", AsyncMock(return_value={"phone": "wa_mock"}))
        monkeypatch.setattr(db, "touch_last_inbound", AsyncMock())
        insert_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "insert_message", insert_mock)
        fake_deb = MagicMock()
        fake_deb.trigger = AsyncMock()
        monkeypatch.setattr(main, "debouncer", fake_deb)

        client = TestClient(main.app)
        payload = {"messages": [_image_msg("msg-400", "523333333333@c.us")]}
        response = client.post(f"/webhook/wazzup/{GOOD_SECRET}", json=payload)

        assert response.status_code == 200
        insert_mock.assert_awaited_once()
        assert insert_mock.call_args.kwargs["meta"] == {"content_type": "photo", "content_uri": "https://cdn.wazzup24.com/media/photo.jpg"}

    def test_insert_exception_in_first_second_still_processed(self, monkeypatch):
        """Исключение в insert первого сообщения → второе всё равно обрабатывается → 200."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(db, "upsert_lead", AsyncMock(return_value={"phone": "wa_mock"}))
        monkeypatch.setattr(db, "touch_last_inbound", AsyncMock())
        # Первый вызов — Exception, второй — True
        insert_mock = AsyncMock(side_effect=[Exception("boom"), True])
        monkeypatch.setattr(db, "insert_message", insert_mock)
        fake_deb = MagicMock()
        fake_deb.trigger = AsyncMock()
        monkeypatch.setattr(main, "debouncer", fake_deb)

        client = TestClient(main.app)
        payload = {
            "messages": [
                _text_msg("msg-500", "521111111111@c.us", "Primero"),
                _text_msg("msg-501", "521111111111@c.us", "Segundo"),
            ]
        }
        response = client.post(f"/webhook/wazzup/{GOOD_SECRET}", json=payload)

        assert response.status_code == 200
        # оба вызова состоялись (второй — несмотря на исключение первого)
        assert insert_mock.await_count == 2

    def test_whatsgroup_via_endpoint_200(self, monkeypatch):
        """chatType=whatsgroup → normalize дропает → ни upsert, ни insert, ни trigger, 200.

        Реальный кейс из живого теста: Wazzup присылает групповые чаты.
        Бот молчит, endpoint отвечает 200, никаких ошибок.
        """
        monkeypatch.setattr(db, "is_ready", lambda: True)
        upsert_mock = AsyncMock()
        monkeypatch.setattr(db, "upsert_lead", upsert_mock)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)
        fake_deb = MagicMock()
        fake_deb.trigger = AsyncMock()
        monkeypatch.setattr(main, "debouncer", fake_deb)

        client = TestClient(main.app)
        payload = {
            "messages": [_whatsgroup_msg("msg-grp-99", "79132123789-1581764243", "Hola grupo")]
        }
        response = client.post(f"/webhook/wazzup/{GOOD_SECRET}", json=payload)

        assert response.status_code == 200
        upsert_mock.assert_not_awaited()
        insert_mock.assert_not_awaited()
        fake_deb.trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# Часть 4: Регресс (ping, 403, broken JSON, statuses) — TestClient
# ---------------------------------------------------------------------------

class TestWebhookRegression:
    """Убеждаемся, что существующие сценарии не сломались при новом коде."""

    def test_ping_returns_200(self):
        """Тестовый пинг Wazzup {test: true} → 200."""
        client = TestClient(main.app)
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}", json={"test": True}
        )
        assert response.status_code == 200

    def test_bad_secret_returns_403(self):
        """Неверный секрет → 403."""
        client = TestClient(main.app)
        response = client.post(
            f"/webhook/wazzup/{BAD_SECRET}", json={"test": True}
        )
        assert response.status_code == 403

    def test_broken_json_returns_200(self):
        """Битый JSON тело → 200, не 500."""
        client = TestClient(main.app)
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            content=b"this is { broken json !!!",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200

    def test_statuses_only_returns_200(self):
        """Payload только со statuses → 200."""
        client = TestClient(main.app)
        payload = {
            "statuses": [
                {"messageId": "msg-999", "status": "delivered", "timestamp": 1700000000}
            ]
        }
        response = client.post(f"/webhook/wazzup/{GOOD_SECRET}", json=payload)
        assert response.status_code == 200

    def test_db_not_ready_messages_still_200(self, monkeypatch):
        """db.is_ready()=False при messages → 200, без исключения."""
        # is_ready уже False по умолчанию (пул не инициализирован),
        # но явно ставим для ясности
        monkeypatch.setattr(db, "is_ready", lambda: False)
        client = TestClient(main.app)
        payload = {"messages": [_text_msg()]}
        response = client.post(f"/webhook/wazzup/{GOOD_SECRET}", json=payload)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Часть 5: _apply_decision blocked — блок через db.block_lead (не set_funnel_stage)
# ---------------------------------------------------------------------------

class TestApplyDecisionBlocked:
    """Проверяем, что action='blocked' вызывает ТОЛЬКО db.block_lead (с is_escort из Decision),
    а db.set_funnel_stage НЕ вызывается — стадия 'lost' теперь ставится внутри block_lead.
    """

    async def test_blocked_escort_true_calls_block_lead_escort_true(self, monkeypatch):
        """decision.is_escort=True → block_lead(phone, reason, escort=True) вызван."""
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        set_funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", set_funnel_mock)

        phone = "wa_555"
        decision = filters.Decision(
            action="blocked", reason="Ищет интим-услуги",
            alert_manager=True, block_permanent=True, is_escort=True,
        )
        lead = {}

        await main._apply_decision(phone, decision, lead, "текст")

        block_mock.assert_awaited_once_with(phone, decision.reason, escort=True)
        set_funnel_mock.assert_not_awaited()

    async def test_blocked_escort_false_calls_block_lead_escort_false(self, monkeypatch):
        """decision.is_escort=False → block_lead(phone, reason, escort=False) вызван."""
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        set_funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", set_funnel_mock)

        phone = "wa_666"
        decision = filters.Decision(
            action="blocked", reason="Агрессивное поведение",
            alert_manager=True, block_permanent=True, is_escort=False,
        )
        lead = {}

        await main._apply_decision(phone, decision, lead, "текст")

        block_mock.assert_awaited_once_with(phone, decision.reason, escort=False)
        set_funnel_mock.assert_not_awaited()

    async def test_blocked_set_funnel_stage_never_called(self, monkeypatch):
        """action='blocked' — set_funnel_stage НЕ вызывается ни в каком случае (стадия ставится внутри block_lead)."""
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        set_funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", set_funnel_mock)

        for is_escort in (True, False):
            set_funnel_mock.reset_mock()
            decision = filters.Decision(
                action="blocked", reason="тест", is_escort=is_escort,
            )
            await main._apply_decision("wa_777", decision, {}, "текст")
            set_funnel_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Часть 6: TestRunAI — прямые async-тесты main._run_ai
# ---------------------------------------------------------------------------

class TestRunAI:
    """Юнит-тесты _run_ai: AI генерирует ответ → extracted/funnel_stage/action отрабатывают корректно."""

    _DEFAULT_RESULT = {
        "messages": ["Hola!"],
        "funnel_stage": "qualifying",
        "action": "respond",
        "extracted": {"age": 40},
        "needs_escalation": False,
        "used_scenario_id": 5,
    }

    def _mock_ai_deps(self, monkeypatch, *, result: dict | None = None,
                      update_lead_side_effect=None):
        """Замокать все AI/DB зависимости _run_ai; вернуть ключевые моки."""
        res = result if result is not None else dict(self._DEFAULT_RESULT)
        gen_mock = AsyncMock(return_value=res)
        monkeypatch.setattr(ai, "generate_reply", gen_mock)
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        update_mock = AsyncMock(side_effect=update_lead_side_effect)
        monkeypatch.setattr(db, "update_lead_fields", update_mock)
        funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", funnel_mock)
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        title_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(db, "get_scenario_title", title_mock)
        send_mock = AsyncMock(return_value=1)
        monkeypatch.setattr(main.sender, "send", send_mock)
        monkeypatch.setattr(db, "reset_followup_timer", AsyncMock())  # блок 13
        return gen_mock, update_mock, funnel_mock, block_mock, title_mock, send_mock

    # --- 1. respond + extracted → update_lead_fields вызван ---

    async def test_respond_with_extracted_calls_update_lead_fields(self, monkeypatch):
        """action=respond, extracted={age:40} → db.update_lead_fields(phone, age=40) вызван."""
        _, update_mock, funnel_mock, block_mock, _, send_mock = self._mock_ai_deps(
            monkeypatch,
            result={
                "messages": ["Hola!"], "funnel_stage": "qualifying", "action": "respond",
                "extracted": {"age": 40}, "needs_escalation": False, "used_scenario_id": 5,
            },
        )

        await main._run_ai("wa_test", {}, "Hola, tengo 40 años")

        update_mock.assert_awaited_once_with("wa_test", age=40)
        funnel_mock.assert_awaited_once()
        send_mock.assert_awaited_once()
        block_mock.assert_not_awaited()

    # --- 2. respond без extracted → update_lead_fields НЕ вызван ---

    async def test_respond_empty_extracted_no_update(self, monkeypatch):
        """extracted={} → db.update_lead_fields НЕ вызывается."""
        _, update_mock, _, _, _, _ = self._mock_ai_deps(
            monkeypatch,
            result={
                "messages": ["Hola!"], "funnel_stage": "qualifying", "action": "respond",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 5,
            },
        )

        await main._run_ai("wa_test", {}, "Hola")

        update_mock.assert_not_awaited()

    # --- 3. respond без funnel_stage → set_funnel_stage НЕ вызван ---

    async def test_respond_no_funnel_stage_skips_set_funnel(self, monkeypatch):
        """funnel_stage=None → db.set_funnel_stage НЕ вызывается."""
        _, _, funnel_mock, _, _, _ = self._mock_ai_deps(
            monkeypatch,
            result={
                "messages": ["Hola!"], "funnel_stage": None, "action": "respond",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 5,
            },
        )

        await main._run_ai("wa_test", {}, "Hola")

        funnel_mock.assert_not_awaited()

    # --- 4. block с used_scenario_id=7 → get_scenario_title(7); reason="AI: <title>" ---

    async def test_block_with_scenario_id_uses_title_as_reason(self, monkeypatch):
        """action=block, scenario_id=7, title="Лиду меньше 28" → block_lead(reason="AI: Лиду меньше 28")."""
        _, _, funnel_mock, block_mock, title_mock, send_mock = self._mock_ai_deps(
            monkeypatch,
            result={
                "messages": ["Adiós"], "funnel_stage": None, "action": "block",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 7,
            },
        )
        title_mock.return_value = "Лиду меньше 28"

        await main._run_ai("wa_block", {}, "tengo 20")

        title_mock.assert_awaited_once_with(7)
        block_mock.assert_awaited_once_with("wa_block", "AI: Лиду меньше 28")
        # escort НЕ передаётся → по умолчанию False внутри db.block_lead
        assert "escort" not in (block_mock.call_args.kwargs or {})
        funnel_mock.assert_not_awaited()
        send_mock.assert_awaited_once()

    # --- 5. block с scenario_id=None и title=None → reason="AI-блок по сценарию" ---

    async def test_block_no_scenario_id_uses_fallback_reason(self, monkeypatch):
        """used_scenario_id=None, get_scenario_title→None → reason="AI-блок по сценарию"."""
        _, _, _, block_mock, title_mock, _ = self._mock_ai_deps(
            monkeypatch,
            result={
                "messages": ["Adiós"], "funnel_stage": None, "action": "block",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": None,
            },
        )
        title_mock.return_value = None

        await main._run_ai("wa_block2", {}, "texto")

        block_mock.assert_awaited_once_with("wa_block2", "AI-блок по сценарию")

    # --- 6. escalate → _send_stub вызван, block_lead НЕ вызван, лог Ане ---

    async def test_escalate_sends_messages_no_block(self, monkeypatch, caplog):
        """action=escalate → _send_stub вызван, block_lead НЕ вызван, лог про флаг Ане."""
        import logging
        _, _, funnel_mock, block_mock, _, send_mock = self._mock_ai_deps(
            monkeypatch,
            result={
                # funnel_stage=None: ai.py валидирует стадию против FUNNEL_STAGES, а
                # "escalate" — это action, не стадия. С None set_funnel_stage не зовётся.
                "messages": ["Te contactaré"], "funnel_stage": None, "action": "escalate",
                "extracted": {}, "needs_escalation": True, "used_scenario_id": 3,
            },
        )

        with caplog.at_level(logging.INFO, logger="matchmatch.ai"):
            # matchmatch.ai ловим тоже, но основной логгер "matchmatch"
            pass

        with caplog.at_level(logging.INFO):
            await main._run_ai("wa_esc", {}, "quiero más info")

        send_mock.assert_awaited_once_with("wa_esc", ["Te contactaré"], allow_repeat_links=False)
        block_mock.assert_not_awaited()
        # _run_ai логирует "escalate" + "TODO-алерт Ане"
        assert "escalate" in caplog.text.lower() or "Ане" in caplog.text

    # --- 7. update_lead_fields бросает Exception → _run_ai не падает, action отрабатывает ---

    async def test_update_lead_fields_exception_does_not_crash_run_ai(self, monkeypatch):
        """update_lead_fields → Exception: _run_ai не бросает, action=respond продолжает."""
        _, _, funnel_mock, block_mock, _, send_mock = self._mock_ai_deps(
            monkeypatch,
            result={
                "messages": ["Hola!"], "funnel_stage": "qualifying", "action": "respond",
                "extracted": {"age": 40}, "needs_escalation": False, "used_scenario_id": 5,
            },
            update_lead_side_effect=Exception("DB connection lost"),
        )

        # не должно бросать
        await main._run_ai("wa_crash", {}, "tengo 40")

        # несмотря на падение update_lead_fields, дальнейший action отрабатывает
        funnel_mock.assert_awaited_once()
        send_mock.assert_awaited_once()
        block_mock.assert_not_awaited()

    # --- 8. rejected ветка _apply_decision: set_funnel_stage("rejected") И _run_ai вызван ---

    async def test_apply_decision_rejected_calls_set_funnel_and_run_ai(self, monkeypatch):
        """action=rejected → set_funnel_stage("rejected") И ai.generate_reply awaited."""
        funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", funnel_mock)
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "update_lead_fields", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "get_scenario_title", AsyncMock(return_value=None))
        monkeypatch.setattr(main.sender, "send", AsyncMock(return_value=1))
        gen_mock = AsyncMock(return_value={
            "messages": ["Lo siento"], "funnel_stage": None, "action": "respond",
            "extracted": {}, "needs_escalation": False, "used_scenario_id": None,
        })
        monkeypatch.setattr(ai, "generate_reply", gen_mock)

        decision = filters.Decision(action="rejected", reason="Возраст 25 вне 28-65")

        await main._apply_decision("wa_rej", decision, {}, "tengo 25")

        # set_funnel_stage вызван с "rejected" И метой
        funnel_mock.assert_awaited_once()
        call_args = funnel_mock.call_args
        assert call_args.args[0] == "wa_rej"
        assert call_args.args[1] == "rejected"
        # _run_ai вызван → generate_reply awaited
        gen_mock.assert_awaited_once()


class TestRunAIFailureIsolation:
    """Падение _run_ai не должно пробрасываться (сообщения уже processed, поток должен жить)."""

    async def test_run_ai_exception_caught_in_apply_decision(self, monkeypatch, caplog):
        import logging, filters as _f
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(side_effect=RuntimeError("db down")))
        decision = _f.Decision(action="needs_ai", reason="test")
        with caplog.at_level(logging.ERROR, logger="matchmatch"):
            # не должно бросить наружу
            await main._apply_decision("wa_x", decision, {}, "hola")
        assert any("обработка AI упала" in r.message for r in caplog.records)

    async def test_rejected_run_ai_exception_caught(self, monkeypatch):
        import filters as _f
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(side_effect=RuntimeError("boom")))
        decision = _f.Decision(action="rejected", reason="возраст")
        # не должно бросить
        await main._apply_decision("wa_y", decision, {}, "tengo 20")


# ---------------------------------------------------------------------------
# Часть 7: _apply_decision silent — бот молчит, ничего не вызывает
# ---------------------------------------------------------------------------

class TestApplyDecisionSilent:
    """action='silent' → бот полностью молчит: не отвечает, не блокирует, стадию не трогает."""

    async def test_silent_no_sender_send(self, monkeypatch, caplog):
        """action='silent' → sender.send НЕ вызван."""
        import logging
        send_mock = AsyncMock()
        monkeypatch.setattr(main.sender, "send", send_mock)
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(ai, "generate_reply", AsyncMock())

        decision = filters.Decision(action="silent", reason="молчу — русский номер +7, не целевой регион")

        with caplog.at_level(logging.INFO, logger="matchmatch"):
            await main._apply_decision("wa_79991234567", decision, {}, "привет")

        send_mock.assert_not_awaited()

    async def test_silent_no_block_lead(self, monkeypatch):
        """action='silent' → db.block_lead НЕ вызван (не блокируем, вдруг ошибка)."""
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(ai, "generate_reply", AsyncMock())

        decision = filters.Decision(action="silent", reason="молчу — русский номер +7, не целевой регион")

        await main._apply_decision("wa_79991234567", decision, {}, "привет")

        block_mock.assert_not_awaited()

    async def test_silent_no_set_funnel_stage(self, monkeypatch):
        """action='silent' → db.set_funnel_stage НЕ вызван (стадию не меняем)."""
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", funnel_mock)
        monkeypatch.setattr(ai, "generate_reply", AsyncMock())

        decision = filters.Decision(action="silent", reason="молчу — кириллица/русский язык, не целевой лид")

        await main._apply_decision("wa_5215551234567", decision, {}, "привет")

        funnel_mock.assert_not_awaited()

    async def test_silent_no_generate_reply(self, monkeypatch):
        """action='silent' → ai.generate_reply НЕ вызван (экономия токенов)."""
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        gen_mock = AsyncMock()
        monkeypatch.setattr(ai, "generate_reply", gen_mock)

        decision = filters.Decision(action="silent", reason="молчу — русский номер +7, не целевой регион")

        await main._apply_decision("wa_79991234567", decision, {}, "привет")

        gen_mock.assert_not_awaited()

    async def test_silent_does_not_raise(self, monkeypatch):
        """action='silent' → функция завершается без исключения."""
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(ai, "generate_reply", AsyncMock())

        decision = filters.Decision(action="silent", reason="тест")

        # не должно бросать
        await main._apply_decision("wa_79991234567", decision, {}, "привет")

    async def test_silent_logs_decision(self, monkeypatch, caplog):
        """action='silent' → в логе присутствует 'РЕШЕНИЕ silent'."""
        import logging
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(ai, "generate_reply", AsyncMock())

        decision = filters.Decision(action="silent", reason="молчу — русский номер +7, не целевой регион")

        with caplog.at_level(logging.INFO, logger="matchmatch"):
            await main._apply_decision("wa_79991234567", decision, {}, "привет")

        assert "РЕШЕНИЕ silent" in caplog.text

    async def test_silent_cyrillic_reason_logs(self, monkeypatch, caplog):
        """Проверяем что reason из Decision попадает в лог (кириллица-ветка)."""
        import logging
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(ai, "generate_reply", AsyncMock())

        reason = "молчу — кириллица/русский язык, не целевой лид"
        decision = filters.Decision(action="silent", reason=reason)

        with caplog.at_level(logging.INFO, logger="matchmatch"):
            await main._apply_decision("wa_5215551234567", decision, {}, "привет как дела")

        assert "РЕШЕНИЕ silent" in caplog.text


# ---------------------------------------------------------------------------
# Часть 8: интеграция escalation в main._apply_decision и _run_ai
# ---------------------------------------------------------------------------

class TestEscalationIntegration:
    """Проверяем, что main вызывает нужные escalation.notify_* функции."""

    # Общий хелпер: замокать все зависимости _run_ai + escalation.notify_*
    def _mock_all(self, monkeypatch):
        """Замокать AI/DB/sender + все escalation.notify_* как AsyncMock."""
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "update_lead_fields", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        monkeypatch.setattr(db, "get_scenario_title", AsyncMock(return_value="Хочу контакт девушки"))
        monkeypatch.setattr(main.sender, "send", AsyncMock(return_value=1))
        monkeypatch.setattr(db, "reset_followup_timer", AsyncMock())  # блок 13
        # Эскалация
        vip_mock = AsyncMock()
        block_mock = AsyncMock()
        esc_mock = AsyncMock()
        err_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_vip", vip_mock)
        monkeypatch.setattr(main.escalation, "notify_block", block_mock)
        monkeypatch.setattr(main.escalation, "notify_escalation", esc_mock)
        monkeypatch.setattr(main.escalation, "notify_error", err_mock)
        return vip_mock, block_mock, esc_mock, err_mock

    # 13. silent_whitelist → notify_vip вызван с lead

    async def test_silent_whitelist_calls_notify_vip(self, monkeypatch):
        """_apply_decision silent_whitelist + alert_manager (VIP) → notify_vip(lead)."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)

        lead = {"phone": "wa_client1", "whatsapp_name": "Marco"}
        decision = filters.Decision(action="silent_whitelist", reason="VIP-клиент",
                                    alert_manager=True)

        await main._apply_decision("wa_client1", decision, lead, "Hola")

        vip_mock.assert_awaited_once()
        call_lead = vip_mock.call_args.args[0]
        assert call_lead == lead
        # остальные не вызваны
        block_mock.assert_not_awaited()
        esc_mock.assert_not_awaited()
        err_mock.assert_not_awaited()

    async def test_silent_whitelist_personal_contact_no_alert(self, monkeypatch):
        """silent_whitelist БЕЗ alert_manager (personal_contact) → notify_vip НЕ вызван."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)

        lead = {"phone": "wa_pc1", "whatsapp_name": "Diego"}
        decision = filters.Decision(action="silent_whitelist",
                                    reason="whitelist: написал Diego", alert_manager=False)

        await main._apply_decision("wa_pc1", decision, lead, "Hola")

        vip_mock.assert_not_awaited()   # личная база Anna — тишина без алерта
        block_mock.assert_not_awaited()
        esc_mock.assert_not_awaited()
        err_mock.assert_not_awaited()

    # 14. blocked → notify_block вызван

    async def test_blocked_calls_notify_block(self, monkeypatch):
        """_apply_decision action=blocked → escalation.notify_block(lead, reason)."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)

        lead = {"phone": "wa_escort1", "whatsapp_name": "Bad Guy"}
        decision = filters.Decision(
            action="blocked", reason="Ищет интим-услуги", is_escort=True
        )

        await main._apply_decision("wa_escort1", decision, lead, "texto")

        block_mock.assert_awaited_once()
        call_lead = block_mock.call_args.args[0]
        assert call_lead == lead
        vip_mock.assert_not_awaited()
        esc_mock.assert_not_awaited()

    # 15. _run_ai action=block → notify_block вызван

    async def test_run_ai_block_calls_notify_block(self, monkeypatch):
        """_run_ai, AI вернул action=block → escalation.notify_block вызван."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)
        monkeypatch.setattr(
            ai, "generate_reply",
            AsyncMock(return_value={
                "messages": ["Adiós"], "funnel_stage": None, "action": "block",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 7,
            }),
        )

        await main._run_ai("wa_block", {"whatsapp_name": "Test"}, "quiero escort")

        block_mock.assert_awaited_once()
        esc_mock.assert_not_awaited()

    # 16. _run_ai action=escalate → notify_escalation вызван

    async def test_run_ai_escalate_calls_notify_escalation(self, monkeypatch):
        """_run_ai, AI вернул action=escalate → escalation.notify_escalation вызван (awaited)."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)
        monkeypatch.setattr(
            ai, "generate_reply",
            AsyncMock(return_value={
                "messages": ["Te contactaré pronto"], "funnel_stage": None, "action": "escalate",
                "extracted": {}, "needs_escalation": True, "used_scenario_id": 3,
            }),
        )

        lead = {"whatsapp_name": "Pedro"}
        await main._run_ai("wa_esc", lead, "quiero más info")

        esc_mock.assert_awaited_once()
        block_mock.assert_not_awaited()

    # 17. _run_ai action=respond → notify_escalation НЕ вызван

    async def test_run_ai_respond_does_not_call_notify_escalation(self, monkeypatch):
        """_run_ai, AI вернул action=respond → escalation.notify_escalation НЕ вызван."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)
        monkeypatch.setattr(
            ai, "generate_reply",
            AsyncMock(return_value={
                "messages": ["Hola!"], "funnel_stage": "qualifying", "action": "respond",
                "extracted": {}, "needs_escalation": False, "used_scenario_id": 5,
            }),
        )

        await main._run_ai("wa_resp", {}, "Hola")

        esc_mock.assert_not_awaited()
        block_mock.assert_not_awaited()

    # 18. needs_ai + _run_ai бросает → notify_error вызван

    async def test_needs_ai_run_ai_exception_calls_notify_error(self, monkeypatch):
        """action=needs_ai + get_conversation_history бросает → escalation.notify_error вызван."""
        vip_mock, block_mock, esc_mock, err_mock = self._mock_all(monkeypatch)
        # Перекрыть get_conversation_history на ошибку
        monkeypatch.setattr(
            db, "get_conversation_history",
            AsyncMock(side_effect=RuntimeError("db down")),
        )

        decision = filters.Decision(action="needs_ai", reason="тест")
        # не должно бросить наружу
        await main._apply_decision("wa_crash", decision, {}, "hola")

        err_mock.assert_awaited_once()
        # проверяем что вызван с правильным where
        call_where = err_mock.call_args.args[0]
        assert "main._run_ai" in call_where


# ---------------------------------------------------------------------------
# Часть B: TestProcessPhoto — юнит-тесты _process_photo (все вердикты + краевые случаи)
# ---------------------------------------------------------------------------

class TestProcessPhoto:
    """Проверяем каждую ветку _process_photo: ok/retry/reject/manual/флуд/сбой download/manual-фолбэк."""

    PHONE = "wa_521111111111"
    LEAD = {"phone": "wa_521111111111", "whatsapp_name": "Carlos"}
    CONTENT_URI = "https://cdn.wazzup24.com/photo.jpg"
    FAKE_IMG = b"fakeimagedata"
    FAKE_URL = "https://storage.example.com/photo.jpg"
    FAKE_PATH = "wa_521111111111/abc123.jpg"

    def _mock_all(self, monkeypatch, *,
                  count_photos: int = 0,
                  download_raises=None,
                  analyze_return: dict | None = None,
                  scenario_title: str = "Foto inaceptable",
                  scenario_template: str = "Bloque 1\n\nBloque 2") -> dict:
        """Замокать все зависимости _process_photo. Вернуть словарь с моками для проверок."""
        if analyze_return is None:
            analyze_return = {"verdict": "ok", "reason": ""}

        count_mock = AsyncMock(return_value=count_photos)
        monkeypatch.setattr(db, "count_recent_photos", count_mock)

        if download_raises is not None:
            dl_mock = AsyncMock(side_effect=download_raises)
        else:
            dl_mock = AsyncMock(return_value=self.FAKE_IMG)
        monkeypatch.setattr(main.vision, "download_media", dl_mock)

        analyze_mock = AsyncMock(return_value=analyze_return)
        monkeypatch.setattr(main.vision, "analyze_photo", analyze_mock)

        upload_mock = AsyncMock(return_value=(self.FAKE_URL, self.FAKE_PATH))
        monkeypatch.setattr(main.vision, "upload_to_storage", upload_mock)

        save_mock = AsyncMock()
        monkeypatch.setattr(db, "save_photo", save_mock)
        received_mock = AsyncMock()
        monkeypatch.setattr(db, "mark_photo_received", received_mock)
        funnel_mock = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", funnel_mock)
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        update_mock = AsyncMock()
        monkeypatch.setattr(db, "update_lead_fields", update_mock)
        title_mock = AsyncMock(return_value=scenario_title)
        monkeypatch.setattr(db, "get_scenario_title", title_mock)
        template_mock = AsyncMock(return_value=scenario_template)
        monkeypatch.setattr(db, "get_scenario_template", template_mock)

        send_mock = AsyncMock()
        monkeypatch.setattr(main.sender, "send", send_mock)
        notify_block_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_block", notify_block_mock)
        notify_esc_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_escalation", notify_esc_mock)
        notify_photo_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_photo_review", notify_photo_mock)
        notify_err_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_error", notify_err_mock)
        run_ai_mock = AsyncMock()
        monkeypatch.setattr(main, "_run_ai", run_ai_mock)

        return {
            "count_recent_photos": count_mock,
            "download_media": dl_mock,
            "analyze_photo": analyze_mock,
            "upload_to_storage": upload_mock,
            "save_photo": save_mock,
            "mark_photo_received": received_mock,
            "set_funnel_stage": funnel_mock,
            "block_lead": block_mock,
            "update_lead_fields": update_mock,
            "get_scenario_title": title_mock,
            "get_scenario_template": template_mock,
            "send": send_mock,
            "notify_block": notify_block_mock,
            "notify_escalation": notify_esc_mock,
            "notify_photo_review": notify_photo_mock,
            "notify_error": notify_err_mock,
            "_run_ai": run_ai_mock,
        }

    # --- 1. verdict=ok ---

    async def test_verdict_ok_marks_photo_received_and_runs_ai(self, monkeypatch):
        """verdict=ok → mark_photo_received(True) + set_funnel_stage(qualified) + _run_ai("[фото одобрено]"); save_photo вызван."""
        mocks = self._mock_all(monkeypatch, analyze_return={"verdict": "ok", "reason": ""})

        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["mark_photo_received"].assert_awaited_once_with(self.PHONE, True)
        mocks["set_funnel_stage"].assert_awaited_once()
        stage_call = mocks["set_funnel_stage"].call_args
        assert stage_call.args[0] == self.PHONE
        assert stage_call.args[1] == "qualified"
        mocks["_run_ai"].assert_awaited_once()
        run_ai_call = mocks["_run_ai"].call_args
        assert run_ai_call.args[2] == "[фото одобрено]"
        mocks["save_photo"].assert_awaited_once()
        mocks["block_lead"].assert_not_awaited()
        mocks["notify_block"].assert_not_awaited()

    # --- 2. verdict=retry ---

    async def test_verdict_retry_sends_scenario_5(self, monkeypatch):
        """verdict=retry → _send_scenario(5): get_scenario_template(5) + sender.send вызван; block_lead НЕ вызван."""
        mocks = self._mock_all(monkeypatch, analyze_return={"verdict": "retry", "reason": "blurry"})

        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["get_scenario_template"].assert_awaited_once_with(5)
        mocks["send"].assert_awaited_once()
        mocks["block_lead"].assert_not_awaited()
        mocks["mark_photo_received"].assert_not_awaited()
        mocks["_run_ai"].assert_not_awaited()
        mocks["save_photo"].assert_awaited_once()

    # --- 3. verdict=reject ---

    async def test_verdict_reject_blocks_and_notifies(self, monkeypatch):
        """verdict=reject → block_lead вызван + sender.send (сценарий 12) + notify_block вызван."""
        mocks = self._mock_all(monkeypatch, analyze_return={"verdict": "reject", "reason": "nudity"})

        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["block_lead"].assert_awaited_once()
        mocks["get_scenario_template"].assert_awaited_once_with(12)
        mocks["send"].assert_awaited_once()
        mocks["notify_block"].assert_awaited_once()
        mocks["mark_photo_received"].assert_not_awaited()
        mocks["_run_ai"].assert_not_awaited()
        mocks["save_photo"].assert_awaited_once()

    # --- 4. verdict=manual ---

    async def test_verdict_manual_updates_lead_and_escalates(self, monkeypatch):
        """verdict=manual → update_lead_fields(mode="manual") + notify_photo_review (кнопки, блок 11); block_lead НЕ вызван."""
        mocks = self._mock_all(monkeypatch, analyze_return={"verdict": "manual", "reason": "unclear"})

        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["update_lead_fields"].assert_awaited_once_with(self.PHONE, mode="manual")
        mocks["notify_photo_review"].assert_awaited_once()
        mocks["notify_escalation"].assert_not_awaited()
        mocks["block_lead"].assert_not_awaited()
        mocks["_run_ai"].assert_not_awaited()
        mocks["send"].assert_not_awaited()
        mocks["save_photo"].assert_awaited_once()

    # --- 5. флуд (count_recent_photos=6) ---

    async def test_photo_flood_skips_vision_and_escalates(self, monkeypatch):
        """count_recent_photos=6 → update_lead_fields(mode="manual") + notify_escalation; download_media НЕ вызван."""
        mocks = self._mock_all(monkeypatch, count_photos=6)

        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["update_lead_fields"].assert_awaited_once_with(self.PHONE, mode="manual")
        mocks["notify_escalation"].assert_awaited_once()
        mocks["download_media"].assert_not_awaited()
        mocks["analyze_photo"].assert_not_awaited()
        mocks["save_photo"].assert_not_awaited()

    # --- 6. download_media бросает ---

    async def test_download_media_raises_calls_notify_error_no_analyze(self, monkeypatch):
        """download_media бросает → notify_error вызван, analyze_photo НЕ вызван, функция не бросает."""
        mocks = self._mock_all(monkeypatch, download_raises=IOError("network error"))

        # не должно бросать
        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["notify_error"].assert_awaited_once()
        mocks["analyze_photo"].assert_not_awaited()
        mocks["save_photo"].assert_not_awaited()
        mocks["_run_ai"].assert_not_awaited()

    # --- 7. analyze_photo возвращает manual-фолбэк ---

    async def test_analyze_returns_manual_triggers_manual_branch(self, monkeypatch):
        """analyze_photo возвращает {"verdict":"manual","reason":"vision failed"} → ветка manual: update_lead_fields + notify_photo_review."""
        mocks = self._mock_all(monkeypatch, analyze_return={"verdict": "manual", "reason": "vision failed"})

        await main._process_photo(self.PHONE, self.LEAD, self.CONTENT_URI)

        mocks["update_lead_fields"].assert_awaited_once_with(self.PHONE, mode="manual")
        mocks["notify_photo_review"].assert_awaited_once()
        mocks["block_lead"].assert_not_awaited()
        mocks["send"].assert_not_awaited()
        mocks["save_photo"].assert_awaited_once()


# ---------------------------------------------------------------------------
# Часть C: TestProcessBurstPhotoRouting — ветвление _process_burst (фото vs текст vs silent)
# ---------------------------------------------------------------------------

class TestProcessBurstPhotoRouting:
    """Проверяем маршрутизацию внутри _process_burst: фото → _process_photo, текст → _apply_decision, silent перехватывает фото."""

    PHONE = "wa_521000001111"
    LEAD = {"phone": "wa_521000001111", "whatsapp_name": "Test"}
    CONTENT_URI = "https://cdn.wazzup24.com/media/photo.jpg"

    def _mock_burst_deps(self, monkeypatch, *, msgs: list, decision_action: str = "needs_ai"):
        """Замокать все зависимости _process_burst. Вернуть (photo_mock, apply_mock, notify_err_mock)."""
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        monkeypatch.setattr(db, "mark_messages_processed", AsyncMock())
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=self.LEAD))
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))

        fake_decision = filters.Decision(action=decision_action, reason="test")
        monkeypatch.setattr(filters, "decide", lambda *args, **kwargs: fake_decision)

        photo_mock = AsyncMock()
        monkeypatch.setattr(main, "_process_photo", photo_mock)
        apply_mock = AsyncMock()
        monkeypatch.setattr(main, "_apply_decision", apply_mock)
        notify_err_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_error", notify_err_mock)

        return photo_mock, apply_mock, notify_err_mock

    # --- 8. photo msg + decision needs_ai → _process_photo called ---

    async def test_photo_with_uri_and_needs_ai_calls_process_photo(self, monkeypatch):
        """Залп с photo (content_uri) + decision needs_ai → _process_photo вызван с content_uri, _apply_decision НЕ вызван."""
        msgs = [
            {"id": "uuid-photo-1", "text": None,
             "meta": {"content_type": "photo", "content_uri": self.CONTENT_URI}},
        ]
        photo_mock, apply_mock, _ = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="needs_ai"
        )

        await main._process_burst(self.PHONE)

        photo_mock.assert_awaited_once_with(self.PHONE, self.LEAD, self.CONTENT_URI)
        apply_mock.assert_not_awaited()

    # --- 9. photo msg + decision silent → _apply_decision called, _process_photo NOT ---

    async def test_photo_with_silent_decision_calls_apply_decision(self, monkeypatch):
        """Залп с photo + decision silent → _apply_decision вызван (молчим), _process_photo НЕ вызван."""
        msgs = [
            {"id": "uuid-photo-2", "text": None,
             "meta": {"content_type": "photo", "content_uri": self.CONTENT_URI}},
        ]
        photo_mock, apply_mock, _ = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="silent"
        )

        await main._process_burst(self.PHONE)

        apply_mock.assert_awaited_once()
        photo_mock.assert_not_awaited()

    # --- 10. text burst → _apply_decision called, _process_photo NOT ---

    async def test_text_only_burst_calls_apply_decision(self, monkeypatch):
        """Залп без фото → _apply_decision вызван, _process_photo НЕ вызван."""
        msgs = [
            {"id": "uuid-text-1", "text": "Hola", "meta": {"content_type": "text"}},
        ]
        photo_mock, apply_mock, _ = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="needs_ai"
        )

        await main._process_burst(self.PHONE)

        apply_mock.assert_awaited_once()
        photo_mock.assert_not_awaited()

    # --- 11. photo msg без content_uri → notify_error, _process_photo NOT ---

    async def test_photo_without_content_uri_calls_notify_error(self, monkeypatch):
        """photo msg без content_uri → notify_error вызван, _process_photo НЕ вызван."""
        msgs = [
            {"id": "uuid-photo-3", "text": None,
             "meta": {"content_type": "photo"}},  # нет content_uri
        ]
        photo_mock, apply_mock, notify_err_mock = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="needs_ai"
        )

        await main._process_burst(self.PHONE)

        notify_err_mock.assert_awaited_once()
        photo_mock.assert_not_awaited()
        apply_mock.assert_not_awaited()

    # --- регресс ревью 9.2 ---

    async def test_blocked_decision_with_photo_goes_to_apply_not_photo(self, monkeypatch):
        """escort/агрессия в тексте + фото в залпе → blocked (apply), НЕ фото-ветка."""
        msgs = [
            {"id": "t", "text": "busco sexo", "meta": {"content_type": "text"}},
            {"id": "p", "text": None,
             "meta": {"content_type": "photo", "content_uri": self.CONTENT_URI}},
        ]
        photo_mock, apply_mock, _ = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="blocked"
        )
        await main._process_burst(self.PHONE)
        apply_mock.assert_awaited_once()
        photo_mock.assert_not_awaited()

    async def test_last_photo_no_uri_falls_back_to_earlier(self, monkeypatch):
        """Последнее фото без content_uri, раннее с — берём раннее (не теряем валидное)."""
        msgs = [
            {"id": "p1", "text": None,
             "meta": {"content_type": "photo", "content_uri": "https://early.jpg"}},
            {"id": "p2", "text": None, "meta": {"content_type": "photo"}},  # без uri
        ]
        photo_mock, _, notify_err_mock = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="needs_ai"
        )
        await main._process_burst(self.PHONE)
        photo_mock.assert_awaited_once_with(self.PHONE, self.LEAD, "https://early.jpg")
        notify_err_mock.assert_not_awaited()

    async def test_process_photo_raises_calls_notify_error(self, monkeypatch):
        """_process_photo падает (сообщения уже processed) → notify_error, не тихо."""
        msgs = [
            {"id": "p", "text": None,
             "meta": {"content_type": "photo", "content_uri": self.CONTENT_URI}},
        ]
        photo_mock, _, notify_err_mock = self._mock_burst_deps(
            monkeypatch, msgs=msgs, decision_action="needs_ai"
        )
        photo_mock.side_effect = RuntimeError("save_photo down")
        await main._process_burst(self.PHONE)  # не должно бросить
        notify_err_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Startup-sweep в lifespan (блок 12)
# ---------------------------------------------------------------------------


class TestStartupSweep:
    async def test_sweep_triggers_debounce_for_unprocessed(self, monkeypatch):
        """На старте лиды с непроцессенными inbound прогоняются через debounce.trigger."""
        monkeypatch.setattr(main.settings, "supabase_db_dsn", "")  # пропустить init_pool
        monkeypatch.setattr(main.db, "is_ready", lambda: True)
        monkeypatch.setattr(main.db, "phones_with_unprocessed_inbound",
                            AsyncMock(return_value=["wa_1", "wa_2", "wa_3"]))
        monkeypatch.setattr(main.db, "close_pool", AsyncMock())

        fake_deb = MagicMock()
        fake_deb.trigger = AsyncMock()
        fake_deb.shutdown = AsyncMock()
        monkeypatch.setattr(main, "Debouncer", lambda *a, **k: fake_deb)

        async with main.lifespan(main.app):
            pass

        assert fake_deb.trigger.await_count == 3

    async def test_sweep_skipped_when_db_not_ready(self, monkeypatch):
        """БД не готова → sweep не дёргает phones_with_unprocessed_inbound."""
        monkeypatch.setattr(main.settings, "supabase_db_dsn", "")
        monkeypatch.setattr(main.db, "is_ready", lambda: False)
        phones_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(main.db, "phones_with_unprocessed_inbound", phones_mock)
        monkeypatch.setattr(main.db, "close_pool", AsyncMock())
        fake_deb = MagicMock(); fake_deb.trigger = AsyncMock(); fake_deb.shutdown = AsyncMock()
        monkeypatch.setattr(main, "Debouncer", lambda *a, **k: fake_deb)

        async with main.lifespan(main.app):
            pass

        phones_mock.assert_not_awaited()
        fake_deb.trigger.assert_not_awaited()

    async def test_sweep_failure_alerts_and_starts(self, monkeypatch):
        """Сбой sweep → notify_error, но старт не падает."""
        monkeypatch.setattr(main.settings, "supabase_db_dsn", "")
        monkeypatch.setattr(main.db, "is_ready", lambda: True)
        monkeypatch.setattr(main.db, "phones_with_unprocessed_inbound",
                            AsyncMock(side_effect=RuntimeError("db down")))
        monkeypatch.setattr(main.db, "close_pool", AsyncMock())
        err_mock = AsyncMock()
        monkeypatch.setattr(main.escalation, "notify_error", err_mock)
        fake_deb = MagicMock(); fake_deb.trigger = AsyncMock(); fake_deb.shutdown = AsyncMock()
        monkeypatch.setattr(main, "Debouncer", lambda *a, **k: fake_deb)

        async with main.lifespan(main.app):
            pass

        err_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Блок 13: payment_claim ветка + send_invitation
# ---------------------------------------------------------------------------

import filters as _filters


class TestPaymentClaimBranch:
    async def test_acks_and_escalates_no_block(self, monkeypatch):
        dec = _filters.Decision("payment_claim", "оплата", alert_manager=True)
        send = AsyncMock(); monkeypatch.setattr(main.sender, "send", send)
        npay = AsyncMock(); monkeypatch.setattr(main.escalation, "notify_payment", npay)
        block = AsyncMock(); monkeypatch.setattr(main.db, "block_lead", block)
        await main._apply_decision("wa_1", dec, {"phone": "wa_1"}, "ya pagué")
        send.assert_awaited_once()          # ack лиду
        npay.assert_awaited_once()          # эскалация Ане
        block.assert_not_awaited()          # стадию НЕ трогаем сами


class TestOptoutBranch:
    async def test_confirms_blocks_and_alerts(self, monkeypatch):
        dec = _filters.Decision("optout", "opt-out", alert_manager=True)
        send = AsyncMock(); monkeypatch.setattr(main.sender, "send", send)
        block = AsyncMock(); monkeypatch.setattr(main.db, "block_lead", block)
        alert = AsyncMock(); monkeypatch.setattr(main.escalation, "notify_optout", alert)
        await main._apply_decision("wa_1", dec, {"phone": "wa_1"}, "no me escribas más")
        # одно подтверждение лиду
        send.assert_awaited_once()
        assert send.call_args.args[1] == [main._OPTOUT_CONFIRM]
        # do_not_contact навсегда (block_lead) + алерт Ане
        block.assert_awaited_once()
        assert "opt-out" in block.call_args.args[1]
        alert.assert_awaited_once()


class TestRunAiSendInvitation:
    async def test_send_invitation_triggers_maybe_send(self, monkeypatch):
        monkeypatch.setattr(main.ai, "generate_reply", AsyncMock(return_value={
            "messages": ["aquí está"], "action": "respond", "funnel_stage": None,
            "extracted": {}, "used_scenario_id": None, "needs_escalation": False,
            "send_invitation": True}))
        monkeypatch.setattr(main.db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        inv = AsyncMock(); monkeypatch.setattr(main.actions, "maybe_send_invitation", inv)
        await main._run_ai("wa_1", {"phone": "wa_1"}, "dónde es el evento?")
        inv.assert_awaited_once_with("wa_1")

    async def test_no_invitation_when_flag_absent(self, monkeypatch):
        monkeypatch.setattr(main.ai, "generate_reply", AsyncMock(return_value={
            "messages": ["hola"], "action": "respond", "funnel_stage": None,
            "extracted": {}, "used_scenario_id": None, "needs_escalation": False,
            "send_invitation": False}))
        monkeypatch.setattr(main.db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        inv = AsyncMock(); monkeypatch.setattr(main.actions, "maybe_send_invitation", inv)
        await main._run_ai("wa_1", {"phone": "wa_1"}, "hola")
        inv.assert_not_awaited()


class TestRunAiSendMedia:
    def _reply(self, **flags):
        base = {"messages": ["cómo se ve? mira 🤍"], "action": "respond", "funnel_stage": None,
                "extracted": {}, "used_scenario_id": None, "needs_escalation": False,
                "send_invitation": False, "send_event_photo": False, "send_event_video": False}
        base.update(flags)
        return base

    async def test_video_sent_after_text_separately(self, monkeypatch):
        """send_event_video=true → видео шлётся ОТДЕЛЬНО и ПОСЛЕ текста (порядок вызовов)."""
        calls = []
        monkeypatch.setattr(main.ai, "generate_reply", AsyncMock(return_value=self._reply(send_event_video=True)))
        monkeypatch.setattr(main.db, "get_conversation_history", AsyncMock(return_value=[]))
        async def rec_send(*a, **k): calls.append("text")
        async def rec_vid(*a, **k): calls.append("video")
        monkeypatch.setattr(main.sender, "send", rec_send)
        monkeypatch.setattr(main.actions, "send_event_video", rec_vid)
        monkeypatch.setattr(main.actions, "send_event_photos", AsyncMock())
        await main._run_ai("wa_1", {"phone": "wa_1"}, "cómo se ve el evento?")
        assert calls == ["text", "video"]

    async def test_photo_tool_triggers_photos(self, monkeypatch):
        monkeypatch.setattr(main.ai, "generate_reply", AsyncMock(return_value=self._reply(send_event_photo=True)))
        monkeypatch.setattr(main.db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        ph = AsyncMock(); monkeypatch.setattr(main.actions, "send_event_photos", ph)
        vid = AsyncMock(); monkeypatch.setattr(main.actions, "send_event_video", vid)
        await main._run_ai("wa_1", {"phone": "wa_1"}, "mándame fotos")
        ph.assert_awaited_once(); vid.assert_not_awaited()

    async def test_no_media_when_both_flags_false(self, monkeypatch):
        monkeypatch.setattr(main.ai, "generate_reply", AsyncMock(return_value=self._reply()))
        monkeypatch.setattr(main.db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        ph = AsyncMock(); monkeypatch.setattr(main.actions, "send_event_photos", ph)
        vid = AsyncMock(); monkeypatch.setattr(main.actions, "send_event_video", vid)
        await main._run_ai("wa_1", {"phone": "wa_1"}, "hola")
        ph.assert_not_awaited(); vid.assert_not_awaited()


class TestProcessBurstWrapper:
    async def test_impl_exception_alerts_not_raises(self, monkeypatch):
        """Непойманный сбой в _process_burst_impl → notify_error, наружу не пробрасывается."""
        monkeypatch.setattr(main, "_process_burst_impl",
                            AsyncMock(side_effect=RuntimeError("boom")))
        err = AsyncMock(); monkeypatch.setattr(main.escalation, "notify_error", err)
        await main._process_burst("wa_1")   # не должно бросить
        err.assert_awaited_once()


class TestRunAiArmsFollowup:
    async def test_resets_followup_timer_on_reply(self, monkeypatch):
        """Любой ответ лида → СБРАСываем таймер догона (не нудим активному)."""
        monkeypatch.setattr(main.ai, "generate_reply", AsyncMock(return_value={
            "messages": ["hola"], "action": "respond", "funnel_stage": None,
            "extracted": {}, "used_scenario_id": None, "needs_escalation": False,
            "send_invitation": False}))
        monkeypatch.setattr(main.db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(main.sender, "send", AsyncMock())
        reset = AsyncMock(); monkeypatch.setattr(main.db, "reset_followup_timer", reset)
        await main._run_ai("wa_1", {"phone": "wa_1", "funnel_stage": "new"}, "hola")
        reset.assert_awaited_once_with("wa_1", main.funnel.FOLLOWUP_FIRST_DELAY_HOURS["new"])
