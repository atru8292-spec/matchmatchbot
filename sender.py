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


async def send(phone: str, messages: list) -> int:
    """Отправить бабблы лиду последовательно с задержками. Вернуть число отправленных.

    phone — 'wa_<digits>'; для Wazzup срезаем префикс. Успешные пишем в messages.
    Одно упавшее сообщение не прерывает остальные и не роняет вызов.
    """
    chat_id = phone.replace("wa_", "", 1)
    sent = 0
    for text in messages:
        await asyncio.sleep(compute_delay(text))
        ok = await send_one(chat_id, text)
        if ok:
            sent += 1
            # save_outbound сама логирует и НЕ бросает (потеря записи не критична)
            await db.save_outbound(phone, text)
    logger.info("отправлено %d/%d лиду %s", sent, len(messages), phone)
    return sent
