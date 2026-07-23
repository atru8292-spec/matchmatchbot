"""Отправка ответов в WhatsApp через Wazzup24 с антибан-задержками.

Бабблы (messages[] от AI, 1-4) шлём ПОСЛЕДОВАТЕЛЬНО, перед каждым — пауза
clamp(len/25, 2, 8) + рандом 1.5-3.5с (имитация чтения/набора, как WF1). Реального
typing-статуса у Wazzup нет — только задержка. Успешно отправленное пишем в messages.

Медиа ивента (фото/видео, several в ряд — actions.py _send_event_media) шлётся с
короткой паузой MEDIA_MIN/MAX_DELAY (0.5-1.2с), а не текстовой: Wazzup не умеет слать
несколько вложений одним запросом (нет альбома в API), поэтому чтобы WhatsApp визуально
сгруппировал фото рядом — паузу между ними держим короче, чем между текстовыми бабблами.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime

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

# Пауза между фото/видео ивента (галерея из нескольких медиа подряд, actions.py
# _send_event_media) — короче обычной текстовой паузы, чтобы WhatsApp визуально
# группировал их рядом (Wazzup не умеет слать альбом одним запросом — см. sender.py:1).
MEDIA_MIN_DELAY = 0.5
MEDIA_MAX_DELAY = 1.2


def compute_delay(text: str) -> float:
    """Антибан-задержка перед сообщением (сек): clamp(len/25, 2, 8) + рандом 1.5-3.5."""
    base = len(text or "") / CHARS_PER_SEC
    base = max(MIN_DELAY, min(MAX_DELAY, base))
    total = base + random.uniform(RANDOM_MIN, RANDOM_MAX)
    return round(total * 10) / 10


def compute_media_delay() -> float:
    """Антибан-задержка между медиа ивента (сек): рандом 0.5-1.2 (см. MEDIA_MIN/MAX_DELAY)."""
    return round(random.uniform(MEDIA_MIN_DELAY, MEDIA_MAX_DELAY) * 10) / 10


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


_LINK_PLACEHOLDERS = (("[course_link]", "course_link"), ("[event_link]", "event_link"))

# Лид ЯВНО просит ссылку → разрешаем повтор (иначе Layer 2 дропнет уже отправленную).
_LINK_REQUEST_RE = re.compile(
    r"\b(manda|m[aá]ndame|p[aá]sa(me)?|env[ií]a(me)?|dame|comparte)\b.{0,20}\b(link|enlace|liga)\b|"
    r"\bd[oó]nde\s+(reservo|pago|compro|es)\b|\bel\s+link\b|\bla\s+liga\b",
    re.IGNORECASE)


def _is_link_request(text: str) -> bool:
    """Явная просьба лида прислать ссылку — тогда дедуп Layer 2 не применяем."""
    return bool(_LINK_REQUEST_RE.search(text or ""))

# Переменные события для сценариев приглашения/цены (№2/№15/№51). Подставляются из
# app_settings в пути отправки бота (аналог _fill_event в планировщике для №47).
_EVENT_VAR_KEYS = ("event_address", "event_date", "event_time",
                   "event_start", "event_end",
                   "event_price_member", "event_price_nonmember")

# Испанские месяцы для человекочитаемой даты в сообщениях лиду.
_ES_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _fmt_date_es(iso: str) -> str:
    """'2026-07-22' → '22 de julio de 2026'. Не-ISO строку возвращаем как есть.

    event_date в app_settings хранится в ISO (единый источник для планировщика и
    формы CRM); лиду показываем по-испански.
    """
    try:
        d = datetime.strptime((iso or "").strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return iso or ""
    return f"{d.day} de {_ES_MONTHS[d.month - 1]} de {d.year}"


async def _fill_event_vars(text: str) -> str:
    """Подставить переменные события [event_address]/[event_date]/… из app_settings.

    Незаданный ключ → пустая строка (как _fill_event в планировщике). Запрос в БД —
    только если в тексте реально есть такой плейсхолдер. event_date форматируется
    из ISO в испанскую дату (_fmt_date_es).
    """
    present = [k for k in _EVENT_VAR_KEYS if f"[{k}]" in text]
    # [event_promo] — условный: «(antes <старая цена>)», если задана event_price_old,
    # иначе пустая строка (акция кончилась → Аня очистила поле → «было» исчезает само).
    promo = "[event_promo]" in text
    if not present and not promo:
        return text
    keys = present + (["event_price_old"] if promo else [])
    s = await db.get_settings(keys)
    for k in present:
        val = (s.get(k) or "").strip()
        if k == "event_date":
            val = _fmt_date_es(val)
        text = text.replace(f"[{k}]", val)
    if promo:
        old = (s.get("event_price_old") or "").strip()
        text = text.replace("[event_promo]", f" (antes {old})" if old else "")
    return text


async def _fill_link_placeholders(text: str, phone: str | None = None,
                                  allow_repeat: bool = False) -> str | None:
    """Подставить ссылки из app_settings в [course_link]/[event_link].

    Ссылка задана → подставляем. Ссылка пустая → возвращаем None (баббл не отправляем,
    чтобы лид не получил «... aquí: » без ссылки). Плейсхолдеры вынесены в отдельные
    бабблы (см. миграцию 005), поэтому дроп баббла не рвёт остальной текст.

    Layer 2 (дедуп): если баббл только про ссылку, а её уже слали этому лиду — дропаем,
    чтобы не спамить повтором. Исключение — allow_repeat=True (лид явно просит ссылку).
    """
    present = [(ph, key) for ph, key in _LINK_PLACEHOLDERS if ph in text]
    if not present:
        return text
    settings_map = await db.get_settings([key for _, key in present])
    # Сначала проверяем ВСЕ: если хоть одна нужная ссылка пуста — дропаем баббл целиком
    # (не подставляем частично, чтобы не потерять уже вставленное и не оставить дыру).
    if any(not (settings_map.get(key) or "").strip() for _, key in present):
        return None
    if phone and not allow_repeat:
        for _ph, key in present:
            if await db.link_already_sent(phone, settings_map[key].strip()):
                logger.info("ссылка [%s] уже отправлена %s — дропаю баббл", key, phone)
                return None
    for ph, key in present:
        text = text.replace(ph, settings_map[key].strip())
    return text


async def _send_content_uri(phone: str, url: str, where: str, delay: float) -> bool:
    """Отправить медиа (contentUri) в WhatsApp через Wazzup. Не бросает → bool.

    Фото и видео шлются одним и тем же полем contentUri (Wazzup определяет тип по файлу).
    delay — антибан-пауза перед отправкой, вызывающий сам решает какая (обычная или
    короткая для галереи, см. compute_media_delay).
    """
    if not url:
        return False
    chat_id = phone.replace("wa_", "", 1)
    await asyncio.sleep(delay)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
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
                    "contentUri": url,
                },
            )
            r.raise_for_status()
    except Exception as e:
        logger.exception("Wazzup %s failed: chat_id=%s", where, chat_id)
        await escalation.notify_error(f"sender.{where}", repr(e), phone)
        return False
    return True


async def send_image(phone: str, image_url: str) -> bool:
    """Отправить картинку-приглашение в WhatsApp через Wazzup (contentUri). Не бросает.

    Одиночное сообщение (не галерея) — обычная антибан-пауза, как у текстовых бабблов.
    """
    ok = await _send_content_uri(phone, image_url, "send_image", compute_delay(""))
    if ok:
        await db.save_outbound(phone, "[изображение отправлено]")
        logger.info("картинка отправлена лиду %s", phone)
    return ok


async def send_media(phone: str, url: str, media_type: str = "image",
                     event_date: str | None = None) -> bool:
    """Отправить медиа с ивента (фото/видео) лиду. Маркер в messages — дедуп по типу.

    event_date → дедуп в рамках конкретного ивента (вар. B): маркер с датой.
    Короткая пауза (compute_media_delay) — шлётся пачкой (галерея), см. actions.py
    _send_event_media."""
    ok = await _send_content_uri(phone, url, "send_media", compute_media_delay())
    if ok:
        marker = db.media_marker(media_type, event_date) or "[media ивента отправлено]"
        await db.save_outbound(phone, marker)
        logger.info("media ивента (%s) отправлено лиду %s", media_type, phone)
    return ok


async def render_bubbles(messages: list, phone: str | None = None,
                         allow_repeat_links: bool = True) -> list[str]:
    """Подставить переменные события и ссылки в бабблы → готовые к отправке строки.

    Пустые бабблы и те, где ссылку дропнул дедуп/пустое значение, отсеиваются.
    Используется и реальной отправкой (send), и предпросмотром в CRM (phone=None,
    allow_repeat_links=True — дедуп ссылок не применяем, показываем как есть).
    """
    out = []
    for text in messages:
        text = await _fill_event_vars(text)          # переменные события (№2/№15/№51)
        # ссылки (course/event); пусто ИЛИ уже слали (дедуп) → дроп баббла
        text = await _fill_link_placeholders(text, phone, allow_repeat_links)
        if text and text.strip():
            out.append(text)
    return out


async def send(phone: str, messages: list, allow_repeat_links: bool = False) -> int:
    """Отправить бабблы лиду последовательно с задержками. Вернуть число отправленных.

    phone — 'wa_<digits>'; для Wazzup срезаем префикс. Успешные пишем в messages.
    Одно упавшее сообщение не прерывает остальные и не роняет вызов.

    allow_repeat_links — разрешить повтор уже отправленной ссылки (лид явно просит).
    По умолчанию False: Layer 2 дедуп дропает баббл с уже отправленной лиду ссылкой.
    """
    chat_id = phone.replace("wa_", "", 1)
    bubbles = await render_bubbles(messages, phone, allow_repeat_links)
    sent = 0
    for text in bubbles:
        await asyncio.sleep(compute_delay(text))
        ok = await send_one(chat_id, text)
        if ok:
            sent += 1
            # save_outbound сама логирует и НЕ бросает (потеря записи не критична)
            await db.save_outbound(phone, text)
    logger.info("отправлено %d/%d лиду %s", sent, len(messages), phone)
    return sent
