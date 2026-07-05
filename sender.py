"""Отправка ответов в WhatsApp через Wazzup24 с антибан-задержками.

Бабблы (messages[] от AI, 1-4) шлём ПОСЛЕДОВАТЕЛЬНО, перед каждым — пауза
clamp(len/25, 2, 8) + рандом 1.5-3.5с (имитация чтения/набора, как WF1). Реального
typing-статуса у Wazzup нет — только задержка. Успешно отправленное пишем в messages.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

import db
import escalation
from config import settings

logger = logging.getLogger("matchmatch.sender")

WAZZUP_SEND_URL = "https://api.wazzup24.com/v3/message"

MIN_DELAY = 2.0
MAX_DELAY = 8.0
CHARS_PER_SEC = 25.0     # base = len/25
RANDOM_MIN = 1.5
RANDOM_MAX = 3.5


def compute_delay(text: str) -> float:
    """Антибан-задержка перед сообщением (сек): clamp(len/25, 2, 8) + рандом 1.5-3.5."""
    base = len(text or "") / CHARS_PER_SEC
    base = max(MIN_DELAY, min(MAX_DELAY, base))
    total = base + random.uniform(RANDOM_MIN, RANDOM_MAX)
    return round(total * 10) / 10


async def send_one(chat_id: str, text: str) -> bool:
    """Отправить одно сообщение в Wazzup. True при успехе, False при ошибке (не бросает).

    chat_id — номер БЕЗ префикса wa_ (срезает вызывающий send).
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                WAZZUP_SEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.wazzup_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "channelId": settings.wazzup_channel_id,
                    "chatType": "whatsapp",
                    "chatId": chat_id,
                    "text": text,
                },
            )
            r.raise_for_status()
        return True
    except Exception as e:
        logger.exception("Wazzup send_one failed: chat_id=%s", chat_id)
        # technical-алерт (throttled внутри notify_error); не роняем отправку.
        await escalation.notify_error("sender.send_one", repr(e), "wa_" + chat_id)
        return False


_LINK_PLACEHOLDERS = (("[course_link]", "course_link"), ("[event_link]", "event_link"),
                      ("[call_link]", "call_link"))


async def _fill_link_placeholders(text: str) -> str | None:
    """Подставить ссылки из app_settings в [course_link]/[event_link].

    Ссылка задана → подставляем. Ссылка пустая → возвращаем None (баббл не отправляем,
    чтобы лид не получил «... aquí: » без ссылки). Плейсхолдеры вынесены в отдельные
    бабблы (см. миграцию 005), поэтому дроп баббла не рвёт остальной текст.
    """
    present = [(ph, key) for ph, key in _LINK_PLACEHOLDERS if ph in text]
    if not present:
        return text
    settings_map = await db.get_settings([key for _, key in present])
    # Сначала проверяем ВСЕ: если хоть одна нужная ссылка пуста — дропаем баббл целиком
    # (не подставляем частично, чтобы не потерять уже вставленное и не оставить дыру).
    if any(not (settings_map.get(key) or "").strip() for _, key in present):
        return None
    for ph, key in present:
        text = text.replace(ph, settings_map[key].strip())
    return text


async def send_image(phone: str, image_url: str) -> bool:
    """Отправить картинку в WhatsApp через Wazzup (contentUri = публичный URL). Не бросает.

    Медиа-сообщение: тот же POST /v3/message, но поле contentUri вместо text
    (подтверждено докой Wazzup v3). Успех пишем в messages как исходящее.
    """
    if not image_url:
        return False
    chat_id = phone.replace("wa_", "", 1)
    await asyncio.sleep(compute_delay(""))  # антибан-пауза (минимальная для медиа)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                WAZZUP_SEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.wazzup_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "channelId": settings.wazzup_channel_id,
                    "chatType": "whatsapp",
                    "chatId": chat_id,
                    "contentUri": image_url,
                },
            )
            r.raise_for_status()
    except Exception as e:
        logger.exception("Wazzup send_image failed: chat_id=%s", chat_id)
        await escalation.notify_error("sender.send_image", repr(e), phone)
        return False
    await db.save_outbound(phone, "[изображение отправлено]")
    logger.info("картинка отправлена лиду %s", phone)
    return True


async def send(phone: str, messages: list) -> int:
    """Отправить бабблы лиду последовательно с задержками. Вернуть число отправленных.

    phone — 'wa_<digits>'; для Wazzup срезаем префикс. Успешные пишем в messages.
    Одно упавшее сообщение не прерывает остальные и не роняет вызов.
    """
    chat_id = phone.replace("wa_", "", 1)
    sent = 0
    for text in messages:
        text = await _fill_link_placeholders(text)
        if not text or not text.strip():
            continue  # баббл был только про ссылку, а ссылка не задана — пропускаем
        await asyncio.sleep(compute_delay(text))
        ok = await send_one(chat_id, text)
        if ok:
            sent += 1
            # save_outbound сама логирует и НЕ бросает (потеря записи не критична)
            await db.save_outbound(phone, text)
    logger.info("отправлено %d/%d лиду %s", sent, len(messages), phone)
    return sent
