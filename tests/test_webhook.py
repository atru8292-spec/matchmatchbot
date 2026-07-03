"""Тесты вебхука Wazzup24 — Блок 1.

Используем FastAPI TestClient (httpx). Реальных сетевых вызовов нет.
Секрет берём из settings, не хардкодим.
"""
import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app

# Клиент создаётся один раз на модуль — нет состояния между тестами.
client = TestClient(app)

# Правильный секрет из .env / дефолта config.
GOOD_SECRET = settings.wazzup_webhook_secret
# Строка, которая гарантированно не совпадёт.
BAD_SECRET = "totally-wrong-secret-000"


# ---------------------------------------------------------------------------
# Health-check
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_health_returns_200(self):
        """GET /health → 200."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_returns_status_ok(self):
        """GET /health → тело {"status": "ok"}."""
        response = client.get("/health")
        assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Авторизация по секрету
# ---------------------------------------------------------------------------

class TestSecretAuth:
    def test_wrong_secret_returns_403(self):
        """Неверный секрет в пути → 403 Forbidden."""
        response = client.post(
            f"/webhook/wazzup/{BAD_SECRET}",
            json={"test": True},
        )
        assert response.status_code == 403

    def test_correct_secret_is_not_403(self):
        """Верный секрет → не 403 (любой другой код, включая 200)."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json={"test": True},
        )
        assert response.status_code != 403


# ---------------------------------------------------------------------------
# Тестовый пинг
# ---------------------------------------------------------------------------

class TestTestPing:
    def test_test_ping_returns_200(self):
        """Пинг {"test": true} с верным секретом → 200."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json={"test": True},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Валидные payload'ы сообщений
# ---------------------------------------------------------------------------

class TestMessagesPayload:
    def test_text_message_returns_200(self):
        """Валидное текстовое сообщение → 200."""
        payload = {
            "messages": [
                {
                    "messageId": "msg-001",
                    "channelId": "ch-abc",
                    "chatType": "whatsapp",
                    "chatId": "521234567890@c.us",
                    "type": "text",
                    "text": "Hola, me interesa el servicio",
                    "isEcho": False,
                    "status": "inbound",
                }
            ]
        }
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json=payload,
        )
        assert response.status_code == 200

    def test_image_message_with_content_uri_returns_200(self):
        """Медиа-сообщение type=image с contentUri → 200."""
        payload = {
            "messages": [
                {
                    "messageId": "msg-002",
                    "channelId": "ch-abc",
                    "chatType": "whatsapp",
                    "chatId": "521234567890@c.us",
                    "type": "image",
                    "contentUri": "https://cdn.wazzup24.com/media/photo.jpg",
                    "isEcho": False,
                    "status": "inbound",
                }
            ]
        }
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json=payload,
        )
        assert response.status_code == 200

    def test_multiple_messages_in_one_payload_returns_200(self):
        """Несколько сообщений в одном payload → 200."""
        payload = {
            "messages": [
                {
                    "messageId": "msg-003",
                    "type": "text",
                    "text": "Primer mensaje",
                    "chatId": "521111111111@c.us",
                    "channelId": "ch-abc",
                    "chatType": "whatsapp",
                    "isEcho": False,
                    "status": "inbound",
                },
                {
                    "messageId": "msg-004",
                    "type": "text",
                    "text": "Segundo mensaje",
                    "chatId": "521111111111@c.us",
                    "channelId": "ch-abc",
                    "chatType": "whatsapp",
                    "isEcho": False,
                    "status": "inbound",
                },
            ]
        }
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json=payload,
        )
        assert response.status_code == 200

    def test_message_element_not_dict_does_not_crash(self):
        """Элемент messages не dict (строка) — не падает, возвращает 200."""
        payload = {"messages": ["unexpected-string-item"]}
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json=payload,
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Статусы доставки
# ---------------------------------------------------------------------------

class TestStatusesPayload:
    def test_statuses_payload_returns_200(self):
        """Payload со statuses (апдейты доставки) → 200, не падает."""
        payload = {
            "statuses": [
                {
                    "messageId": "msg-001",
                    "channelId": "ch-abc",
                    "chatId": "521234567890@c.us",
                    "status": "delivered",
                    "timestamp": 1700000000,
                }
            ]
        }
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json=payload,
        )
        assert response.status_code == 200

    def test_statuses_and_messages_together_returns_200(self):
        """Смешанный payload: messages + statuses → 200."""
        payload = {
            "messages": [
                {
                    "messageId": "msg-010",
                    "type": "text",
                    "text": "Hola",
                    "chatId": "522222222222@c.us",
                    "channelId": "ch-xyz",
                    "chatType": "whatsapp",
                    "isEcho": False,
                    "status": "inbound",
                }
            ],
            "statuses": [
                {
                    "messageId": "msg-009",
                    "channelId": "ch-xyz",
                    "chatId": "522222222222@c.us",
                    "status": "read",
                    "timestamp": 1700000001,
                }
            ],
        }
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json=payload,
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Битый/неожиданный payload
# ---------------------------------------------------------------------------

class TestMalformedAndEdgePayloads:
    def test_broken_json_returns_200_not_500(self):
        """Битый JSON (не-JSON тело) с верным секретом → 200, не 500."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            content=b"this is { not valid json !!!",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200

    def test_empty_object_returns_200(self):
        """Пустой объект {} → 200."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json={},
        )
        assert response.status_code == 200

    def test_unexpected_keys_payload_returns_200(self):
        """Payload с неизвестными ключами {"foo": "bar"} → 200."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json={"foo": "bar", "baz": 42},
        )
        assert response.status_code == 200

    def test_empty_messages_list_returns_200(self):
        """Payload с пустым списком messages → 200."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json={"messages": []},
        )
        assert response.status_code == 200

    def test_null_body_field_does_not_crash(self):
        """messages=null → 200, не падает."""
        response = client.post(
            f"/webhook/wazzup/{GOOD_SECRET}",
            json={"messages": None},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Telegram webhook менеджер-бота (блок 11)
# ---------------------------------------------------------------------------

import main
from unittest.mock import AsyncMock


class TestTelegramWebhook:
    def test_empty_secret_config_forbids(self, monkeypatch):
        """Пустой tg_webhook_secret в конфиге → любой запрос 403 (fail-safe)."""
        monkeypatch.setattr(main.settings, "tg_webhook_secret", "")
        r = client.post("/webhook/telegram/anything", json={"update_id": 1})
        assert r.status_code == 403

    def test_wrong_secret_forbidden(self, monkeypatch):
        monkeypatch.setattr(main.settings, "tg_webhook_secret", "tgsecret")
        r = client.post("/webhook/telegram/WRONG", json={"update_id": 1})
        assert r.status_code == 403

    def test_correct_secret_calls_handle_update(self, monkeypatch):
        monkeypatch.setattr(main.settings, "tg_webhook_secret", "tgsecret")
        handle_mock = AsyncMock()
        monkeypatch.setattr(main.manager_bot, "handle_update", handle_mock)
        update = {"update_id": 5, "message": {"text": "/help"}}
        r = client.post("/webhook/telegram/tgsecret", json=update)
        assert r.status_code == 200
        handle_mock.assert_awaited_once_with(update)

    def test_handler_exception_still_200(self, monkeypatch):
        """Сбой обработки update не должен уронить ответ (иначе Telegram ретраит)."""
        monkeypatch.setattr(main.settings, "tg_webhook_secret", "tgsecret")
        monkeypatch.setattr(main.manager_bot, "handle_update",
                            AsyncMock(side_effect=RuntimeError("boom")))
        r = client.post("/webhook/telegram/tgsecret", json={"update_id": 1})
        assert r.status_code == 200
