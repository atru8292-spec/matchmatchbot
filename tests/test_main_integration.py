"""Тесты интеграции входящей трубы: main._handle_incoming, main._process_burst,
эндпоинт POST /webhook/wazzup/{secret}.

Изоляция от БД:
  - lifespan НЕ запускается (TestClient создаётся без `with`)
  - db.is_ready, db.insert_message, db.get_unprocessed_inbound,
    db.mark_messages_processed — всегда через monkeypatch/AsyncMock
  - main.debouncer подменяется через monkeypatch или фикстуру

Структура:
  TestHandleIncoming  — прямой вызов await main._handle_incoming (async)
  TestProcessBurst    — прямой вызов await main._process_burst (async)
  TestWebhookWithMocks — TestClient + подняты моки (sync)
  TestWebhookRegression — регресс пинг/403/bitый JSON/statuses (sync)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import db
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
        self, monkeypatch, mock_debouncer
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
        self, monkeypatch, mock_debouncer
    ):
        """insert_message → False (дубль) → trigger НЕ вызван."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(db, "insert_message", AsyncMock(return_value=False))

        await main._handle_incoming(_text_msg())

        mock_debouncer.trigger.assert_not_awaited()

    async def test_echo_true_drops(self, monkeypatch, mock_debouncer):
        """isEcho=True → normalize возвращает None → нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        msg = _text_msg()
        msg["isEcho"] = True
        await main._handle_incoming(msg)

        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_status_delivered_drops(self, monkeypatch, mock_debouncer):
        """status=delivered → normalize → None → нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        msg = _text_msg()
        msg["status"] = "delivered"
        await main._handle_incoming(msg)

        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_telegram_chattype_drops(self, monkeypatch, mock_debouncer):
        """chatType=telegram → normalize → None → нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        msg = _text_msg()
        msg["chatType"] = "telegram"
        await main._handle_incoming(msg)

        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_empty_text_drops(self, monkeypatch, mock_debouncer):
        """text='' → normalize → None → нет insert, нет trigger."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        await main._handle_incoming(_text_msg(text=""))

        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_db_not_ready_no_insert_no_trigger_no_exception(
        self, monkeypatch, mock_debouncer
    ):
        """db.is_ready() → False → нет insert, нет trigger, исключения нет."""
        monkeypatch.setattr(db, "is_ready", lambda: False)
        insert_mock = AsyncMock()
        monkeypatch.setattr(db, "insert_message", insert_mock)

        # не должен бросать
        await main._handle_incoming(_text_msg())

        insert_mock.assert_not_awaited()
        mock_debouncer.trigger.assert_not_awaited()

    async def test_insert_exception_does_not_propagate(
        self, monkeypatch, mock_debouncer
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
        self, monkeypatch, mock_debouncer
    ):
        """type=image → insert с meta={'content_type': 'photo'}."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
        insert_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "insert_message", insert_mock)

        await main._handle_incoming(_image_msg("msg-img-1", "529998887777@c.us"))

        insert_mock.assert_awaited_once()
        meta_arg = insert_mock.call_args.kwargs["meta"]
        assert meta_arg == {"content_type": "photo"}
        mock_debouncer.trigger.assert_awaited_once_with("wa_529998887777")


# ---------------------------------------------------------------------------
# Часть 2: async-тесты _process_burst (on_flush)
# ---------------------------------------------------------------------------

class TestProcessBurst:

    async def test_non_empty_burst_calls_mark_with_ids(self, monkeypatch):
        """Непустой список сообщений → mark_messages_processed вызван со списком id."""
        msgs = [
            {"id": "uuid-a1", "text": "Hola", "meta": {"content_type": "text"}},
            {"id": "uuid-a2", "text": "Como estas", "meta": {"content_type": "text"}},
        ]
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        mark_mock = AsyncMock()
        monkeypatch.setattr(db, "mark_messages_processed", mark_mock)

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
        """POST с двумя сообщениями → insert_message вызван 2 раза → 200."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
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
        assert insert_mock.await_count == 2

    def test_image_message_insert_with_photo_meta_via_endpoint(self, monkeypatch):
        """type=image через эндпоинт → meta content_type='photo'."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
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
        assert insert_mock.call_args.kwargs["meta"] == {"content_type": "photo"}

    def test_insert_exception_in_first_second_still_processed(self, monkeypatch):
        """Исключение в insert первого сообщения → второе всё равно обрабатывается → 200."""
        monkeypatch.setattr(db, "is_ready", lambda: True)
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
