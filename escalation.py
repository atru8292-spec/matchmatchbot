"""Telegram-алерты: business (бот «Лиды») и technical (бот «Ошибки»).

Business (Ане, TG_MANAGER_*): эскалация / VIP-клиент написал / блокировка. По-человечески,
с кликабельной wa.me-ссылкой, без throttle (каждый лид виден).
Technical (разработке, TG_ALERTS_*): рантайм-ошибки с диагностикой; throttle 5 мин на
(where, phone) — защита от шторма.

Никогда не роняет основной поток: сбой Telegram / пустой токен → лог, без исключения.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

import funnel
from config import settings

logger = logging.getLogger("matchmatch.escalation")

TECH_THROTTLE_SEC = 300  # 5 минут на (where, phone) для technical-алертов
# In-memory throttle: ключ → время последней отправки (loop.time()). 1 процесс — достаточно.
_last_sent: dict[tuple, float] = {}


def _wa_link(phone: str) -> str:
    """Кликабельная ссылка на чат: wa.me/<только цифры> (срезаем префикс wa_)."""
    digits = (phone or "").replace("wa_", "", 1)
    return f"https://wa.me/{digits}"


def _lead_name(lead: dict) -> str:
    return (lead or {}).get("whatsapp_name") or (lead or {}).get("name") or "лид"


async def _send_telegram(token: str, chat_id: str, text: str,
                         reply_markup: dict | None = None) -> None:
    """Отправить сообщение ботом. Пустой токен/сбой — лог, НЕ бросает.

    reply_markup — inline-клавиатура Telegram (кнопки под сообщением), опционально.
    """
    if not token or not chat_id:
        logger.debug("Telegram не настроен (нет токена/chat_id), алерт не отправлен: %s", text[:80])
        return
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # НЕ логируем исключение целиком: его текст содержит URL с токеном бота.
        logger.error("Telegram API вернул %s (алерт не отправлен)", e.response.status_code)
    except Exception:
        # Сетевые ошибки (таймаут/обрыв) URL с токеном не раскрывают.
        logger.exception("не смог отправить Telegram-алерт")


async def send_to_manager(text: str, reply_markup: dict | None = None) -> None:
    """Публичная отправка боту «Лиды» (Ане) — для ответов менеджер-бота (блок 11)."""
    await _send_telegram(settings.tg_manager_bot_token, settings.tg_manager_chat_id,
                         text, reply_markup)


# ===== Inline-клавиатуры под алертами (действия менеджер-бота, блок 11) =====
# callback_data: 'mb:<action>:<phone>' — парсит manager_bot. ≤64 байт (phone ~15).
CB = "mb"


def _btn(text: str, action: str, phone: str) -> dict:
    return {"text": text, "callback_data": f"{CB}:{action}:{phone}"}


def lead_action_kb(phone: str) -> dict:
    """Кнопки под эскалацией 'клиент готов': взять себе / прекратить диалог / карточка."""
    return {"inline_keyboard": [
        [_btn("🤝 Взять себе", "takeover", phone), _btn("🔕 Прекратить диалог", "block", phone)],
        [_btn("📇 Карточка", "card", phone)],
    ]}


def vip_action_kb(phone: str) -> dict:
    """Кнопки под VIP-алертом: взять себе / карточка (бот и так молчит)."""
    return {"inline_keyboard": [
        [_btn("🤝 Взять себе", "takeover", phone)],
        [_btn("📇 Карточка", "card", phone)],
    ]}


def block_action_kb(phone: str) -> dict:
    """Кнопки под алертом блокировки: вернуть боту (если ошибочно) / карточка."""
    return {"inline_keyboard": [
        [_btn("↩️ Вернуть боту", "release", phone)],
        [_btn("📇 Карточка", "card", phone)],
    ]}


def photo_action_kb(phone: str) -> dict:
    """Кнопки под фото на ручной проверке: одобрить / просить другое / прекратить диалог."""
    return {"inline_keyboard": [
        [_btn("✅ Одобрить фото", "photo_ok", phone)],
        [_btn("🔄 Просить другое", "photo_retry", phone),
         _btn("🔕 Прекратить диалог", "photo_reject", phone)],
        [_btn("📇 Карточка", "card", phone)],
    ]}


def card_action_kb(phone: str, is_manual: bool) -> dict:
    """Кнопки под карточкой лида: тумблер takeover/release по текущему режиму + блок."""
    toggle = (_btn("↩️ Вернуть боту", "release", phone) if is_manual
              else _btn("🤝 Взять себе", "takeover", phone))
    return {"inline_keyboard": [[toggle, _btn("🔕 Прекратить диалог", "block", phone)]]}


def _throttled(key: tuple, window_sec: int) -> bool:
    """True если по ключу уже слали в пределах окна (значит пропустить)."""
    try:
        now = asyncio.get_running_loop().time()
    except RuntimeError:
        return False
    last = _last_sent.get(key)
    if last is not None and (now - last) < window_sec:
        return True
    # Периодическая чистка устаревших записей, чтобы словарь не рос весь аптайм
    # (ключ включает phone — уникальных лидов много). Дёшево при редком превышении.
    if len(_last_sent) > 2000:
        for k, ts in list(_last_sent.items()):
            if now - ts >= window_sec:
                del _last_sent[k]
    _last_sent[key] = now
    return False


# ===== Business (бот «Лиды») =====

async def notify_escalation(lead: dict, reason: str, last_msg: str) -> None:
    """Эскалация: бот ответил, но нужен человек. reason — название сценария или фолбэк."""
    stage = funnel.stage_label((lead or {}).get("funnel_stage"))
    reason_line = f"Стадия: {stage} → {reason}" if reason else f"Стадия: {stage}"
    text = (
        "🤍 Клиент готов к следующему шагу\n"
        f"{_lead_name(lead)}\n"
        f"{reason_line}\n"
        f'Последнее сообщение: "{last_msg}"\n'
        f"👉 Написать: {_wa_link((lead or {}).get('phone', ''))}"
    )
    phone = (lead or {}).get("phone", "")
    await _send_telegram(settings.tg_manager_bot_token, settings.tg_manager_chat_id,
                         text, lead_action_kb(phone) if phone else None)


async def notify_vip(lead: dict) -> None:
    """VIP/клиент из whitelist написал — бот молчит, отвечает Аня лично."""
    text = (
        "🤍 Написал твой клиент\n"
        f"{_lead_name(lead)}\n"
        f"👉 Ответь лично: {_wa_link((lead or {}).get('phone', ''))}"
    )
    phone = (lead or {}).get("phone", "")
    await _send_telegram(settings.tg_manager_bot_token, settings.tg_manager_chat_id,
                         text, vip_action_kb(phone) if phone else None)


async def notify_block(lead: dict, reason: str) -> None:
    """Бот прекратил диалог с лидом (escort/агрессия/casual/фото) — Аня в курсе."""
    text = (
        "🔕 Бот прекратил диалог\n"
        f"Причина: {reason}\n"
        f"Лид: {_lead_name(lead)}\n"
        f"👉 Посмотреть переписку: {_wa_link((lead or {}).get('phone', ''))}"
    )
    phone = (lead or {}).get("phone", "")
    await _send_telegram(settings.tg_manager_bot_token, settings.tg_manager_chat_id,
                         text, block_action_kb(phone) if phone else None)


async def notify_photo_review(lead: dict, reason: str) -> None:
    """Фото на ручной проверке — Аня решает кнопками (одобрить/другое/прекратить диалог)."""
    text = (
        "📸 Фото на ручной проверке\n"
        f"Лид: {_lead_name(lead)}\n"
        f"Vision: {reason}\n"
        f"👉 Открыть чат: {_wa_link((lead or {}).get('phone', ''))}"
    )
    phone = (lead or {}).get("phone", "")
    await _send_telegram(settings.tg_manager_bot_token, settings.tg_manager_chat_id,
                         text, photo_action_kb(phone) if phone else None)


# ===== Technical (бот «Ошибки») =====

async def notify_error(where: str, error: str, phone: str | None = None) -> None:
    """Рантайм-ошибка. Throttle 5 мин на (where, phone) — защита от шторма."""
    if _throttled(("error", where, phone), TECH_THROTTLE_SEC):
        logger.debug("technical-алерт throttled: %s / %s", where, phone)
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"🔧 Ошибка: {where}"]
    if phone:
        lines.append(f"Лид: {phone}")
    lines.append(f"Время: {ts} UTC")
    lines.append(error)
    await _send_telegram(settings.tg_alerts_bot_token, settings.tg_alerts_chat_id, "\n".join(lines))
