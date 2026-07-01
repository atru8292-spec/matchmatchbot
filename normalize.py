"""Нормализация входящего сообщения Wazzup24 во внутренний контракт.

Чистая функция без БД и сети: одно сообщение из массива `messages` вебхука →
NormalizedMessage или None (отбросить). Тело вебхука может содержать и `messages`,
и `statuses` — сюда передаём только элементы `messages` (main.py их итерирует).

Отбрасываем (None):
- isEcho == True  — это НАШЕ исходящее;
- status != 'inbound' — апдейт доставки, не сообщение;
- chatType != 'whatsapp' — бот работает только с WhatsApp;
- type == 'text' с пустым текстом;
- неизвестный type;
- не удалось получить номер (пустой chatId и contact.phone).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("matchmatch.normalize")

# Wazzup type → наш content_type
_TYPE_MAP = {
    "text": "text",
    "image": "photo",
    "audio": "voice",
    "video": "video",
    "document": "document",
}

# Placeholder-текст для медиа (реальный контент обработают vision/voice-ветки).
_MEDIA_PLACEHOLDER = {
    "photo": "[photo received]",
    "voice": "[voice message]",
    "video": "[video]",
    "document": "[document]",
}


@dataclass(frozen=True)
class NormalizedMessage:
    """Внутренний контракт входящего сообщения."""
    phone: str                     # бизнес-ключ, 'wa_' + цифры номера
    chat_id: str                   # только цифры номера (для отправки в Wazzup)
    channel: str                   # 'whatsapp'
    content_type: str              # text | photo | voice | video | document
    user_text: str                 # текст или placeholder для медиа
    user_name: str                 # имя контакта или 'WA Lead'
    external_message_id: str       # 'wa_' + messageId (для идемпотентности)
    received_at: str | None        # dateTime из Wazzup (ISO) или None
    media_info: dict | None        # {'content_uri', 'message_id'} для медиа, иначе None


def _only_digits(value) -> str:
    """Оставить только цифры. Номер НЕ переформатируем (код страны/единицу не трогаем)."""
    return re.sub(r"\D", "", str(value or ""))


def normalize_wazzup_message(msg: dict) -> NormalizedMessage | None:
    """Разобрать одно сообщение Wazzup. Вернуть NormalizedMessage или None (отбросить)."""
    if not isinstance(msg, dict):
        logger.debug("normalize: элемент не dict, пропуск")
        return None

    # --- фильтры отбрасывания ---
    if msg.get("isEcho") is True:
        logger.debug("normalize: isEcho=True, пропуск (наше исходящее)")
        return None

    if msg.get("status") != "inbound":
        logger.debug("normalize: status=%r != inbound, пропуск", msg.get("status"))
        return None

    if msg.get("chatType") != "whatsapp":
        logger.debug("normalize: chatType=%r != whatsapp, пропуск", msg.get("chatType"))
        return None

    content_type = _TYPE_MAP.get(msg.get("type"))
    if content_type is None:
        logger.debug("normalize: неизвестный type=%r, пропуск", msg.get("type"))
        return None

    # --- текст / placeholder ---
    if content_type == "text":
        raw_text = msg.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text:
            logger.debug("normalize: пустой/нестроковый text, пропуск")
            return None
        user_text = text
        media_info = None
    else:
        user_text = _MEDIA_PLACEHOLDER[content_type]
        content_uri = msg.get("contentUri")
        if not content_uri:
            # медиа без ссылки downstream (vision/voice) скачать не сможет — видно в логах
            logger.warning(
                "normalize: медиа type=%r без contentUri (message_id=%r)",
                msg.get("type"), msg.get("messageId"),
            )
        media_info = {"content_uri": content_uri, "message_id": msg.get("messageId")}

    # --- номер: chatId как есть (только цифры), запасной источник — contact.phone ---
    # contact защищаем от не-dict (кривой payload не должен ронять цикл в main.py).
    contact = msg.get("contact")
    if not isinstance(contact, dict):
        contact = {}
    digits = _only_digits(msg.get("chatId"))
    if not digits:
        digits = _only_digits(contact.get("phone"))
    if not digits:
        logger.warning("normalize: пустой chatId и contact.phone, пропуск")
        return None
    phone = "wa_" + digits

    # --- имя контакта ---
    raw_name = contact.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    user_name = name.title() if name else "WA Lead"

    # --- id сообщения для идемпотентности ---
    message_id = msg.get("messageId")
    date_time = msg.get("dateTime")
    if message_id:
        external_message_id = "wa_" + str(message_id)
    else:
        # фолбэк: нет messageId → собираем из номера и времени.
        # Без dateTime разные сообщения дадут одинаковый ключ (риск потери на дедупе) —
        # логируем, чтобы аномалия была заметна (для Wazzup messageId почти всегда есть).
        external_message_id = "wa_" + digits + "_" + str(date_time or "")
        logger.warning(
            "normalize: нет messageId, external_message_id по фолбэку=%r",
            external_message_id,
        )

    return NormalizedMessage(
        phone=phone,
        chat_id=digits,
        channel="whatsapp",
        content_type=content_type,
        user_text=user_text,
        user_name=user_name,
        external_message_id=external_message_id,
        received_at=date_time,
        media_info=media_info,
    )
