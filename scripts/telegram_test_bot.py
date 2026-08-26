"""Отдельный Telegram-бот для живого теста AI — переписка как в WhatsApp, без
интерфейса CRM. В отличие от /api/mini/test-chat (песочница в памяти), этот пишет
в РЕАЛЬНУЮ БД (leads/messages) — история переживает перезапуск процесса, лида видно
в мини-CRM. Изоляция от боевых лидов: телефон = 'tg_<chat_id>' (не 'wa_...'),
source='telegram_test' — не путается с реальными WhatsApp-лидами, свой namespace.

Фото: скачивается из Telegram и прогоняется через реальный vision.analyze_photo
(тот же Vision, что в проде) — ok/retry/reject/manual веток. Специально НЕ вызывает
escalation.*/booking.*/db.block_lead — те шлют реальные алерты Ане/Миле в боевой
менеджер-бот или создают реальные события в календаре, а это песочница.

Запуск:
  venv/bin/python -m scripts.telegram_test_bot

Токен — TG_TEST_BOT_TOKEN в .env (бот @matchbottestbot, отдельный от
TG_MANAGER_BOT_TOKEN/TG_ALERTS_BOT_TOKEN — те для эскалаций менеджеру, не для лидов).

Команды в чате: /reset — снести лида и всю историю этого chat_id, начать с нуля.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import httpx

sys.path.insert(0, ".")

import ai
import db
import sender
import vision
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("matchmatch.telegram_test_bot")

API = f"https://api.telegram.org/bot{settings.tg_test_bot_token}"
FILE_API = f"https://api.telegram.org/file/bot{settings.tg_test_bot_token}"
POLL_TIMEOUT = 30
SOURCE = "telegram_test"
PHOTO_MARKER = "[photo received]"


def _phone(chat_id: int) -> str:
    return f"tg_{chat_id}"


async def _send(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})


async def _reset(phone: str) -> None:
    # Свой namespace (tg_) — не через db.reset_lead_history (та форсит 'wa_' префикс).
    await db._get_pool().execute("DELETE FROM leads WHERE phone = $1", phone)


async def _get_or_create_lead(phone: str, tg_name: str) -> dict:
    lead = await db.get_lead_by_phone(phone)
    if lead is None:
        lead = await db.upsert_lead(phone, source=SOURCE, whatsapp_name=tg_name,
                                     funnel_stage="new")
    return lead


async def _reply_via_ai(client: httpx.AsyncClient, chat_id: int, phone: str,
                        lead: dict, history: list[dict], user_text: str) -> None:
    """Прогнать user_text через реальный ai.generate_reply, отправить бабблы, сохранить."""
    try:
        result = await ai.generate_reply(lead, history, user_text)
        bubbles = await sender.render_bubbles(result.get("messages") or [], phone=phone)
    except Exception:
        logger.exception("ошибка генерации ответа (chat_id=%s)", chat_id)
        await _send(client, chat_id, "Ой, что-то сломалось на моей стороне 🤍")
        return

    for b in bubbles:
        await db.insert_message(phone, "outbound", "anna", b)
        await _send(client, chat_id, b)

    extracted = result.get("extracted") or {}
    update_fields = {k: v for k, v in extracted.items() if v is not None}
    if result.get("funnel_stage"):
        update_fields["funnel_stage"] = result["funnel_stage"]
    if update_fields:
        await db.upsert_lead(phone, **update_fields)


async def _send_scenario_text(client: httpx.AsyncClient, chat_id: int, phone: str,
                              scenario_id: int) -> None:
    """Ответ сценарием дословно (как _fixed_reply в ai.py) — без вызова OpenAI."""
    tmpl = await db.get_scenario_template(scenario_id)
    for b in ai._split_template(tmpl or ""):
        await db.insert_message(phone, "outbound", "anna", b)
        await _send(client, chat_id, b)


async def _handle_message(client: httpx.AsyncClient, chat_id: int, text: str,
                           tg_name: str) -> None:
    phone = _phone(chat_id)

    if text.strip() in ("/start", "/reset"):
        await _reset(phone)
        await _send(client, chat_id,
                     "Привет! Пиши как обычному лиду в WhatsApp — отвечу по реальному "
                     "сценарию бота. /reset — начать с чистого листа.")
        return

    lead = await _get_or_create_lead(phone, tg_name)
    history = await db.get_conversation_history(phone, limit=30)
    await db.insert_message(phone, "inbound", "lead", text)
    await _reply_via_ai(client, chat_id, phone, lead, history, text)


async def _download_telegram_photo(client: httpx.AsyncClient, file_id: str) -> bytes:
    r = await client.get(f"{API}/getFile", params={"file_id": file_id})
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    r2 = await client.get(f"{FILE_API}/{file_path}")
    r2.raise_for_status()
    return r2.content


async def _handle_photo(client: httpx.AsyncClient, chat_id: int, photo_sizes: list[dict],
                        tg_name: str) -> None:
    """Фото → реальный Vision (как в проде), без escalation/booking/block (песочница)."""
    phone = _phone(chat_id)
    lead = await _get_or_create_lead(phone, tg_name)
    await db.insert_message(phone, "inbound", "lead", PHOTO_MARKER)

    largest = photo_sizes[-1]  # Telegram отдаёт по возрастанию размера
    try:
        img = await _download_telegram_photo(client, largest["file_id"])
        res = await vision.analyze_photo(img)
    except Exception:
        logger.exception("ошибка Vision-анализа (chat_id=%s)", chat_id)
        await _send(client, chat_id, "Ой, не смогла обработать фото 🤍 попробуй ещё раз")
        return

    verdict = res["verdict"]
    logger.info("фото [%s]: verdict=%s (%s)", phone, verdict, res.get("reason", ""))

    if verdict == "ok":
        await db.upsert_lead(phone, photo_received=True, funnel_stage="qualified")
        history = await db.get_conversation_history(phone, limit=30)
        lead = await db.get_lead_by_phone(phone)
        await _reply_via_ai(client, chat_id, phone, lead, history, "[фото одобрено]")
    elif verdict == "retry":
        await _send_scenario_text(client, chat_id, phone, 11)  # "mándame otra foto..."
    elif verdict == "reject":
        await _send_scenario_text(client, chat_id, phone, 12)  # despedida
    else:  # manual — в проде решает Аня по кнопке; в песочнице просто отвечаем нейтрально
        await _send(client, chat_id,
                     "Déjame revisar tu foto con calma y te confirmo en un momento 🤍 "
                     f"(vision: manual — {res.get('reason', 'sin motivo')})")


async def main() -> None:
    if not settings.tg_test_bot_token:
        raise RuntimeError("TG_TEST_BOT_TOKEN не задан в .env")
    await db.init_pool()
    offset = 0
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
        logger.info("тест-бот запущен, жду сообщений...")
        while True:
            r = await client.get(f"{API}/getUpdates",
                                  params={"offset": offset, "timeout": POLL_TIMEOUT})
            r.raise_for_status()
            updates = r.json().get("result", [])
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if not chat_id:
                    continue
                tg_name = (msg.get("from") or {}).get("first_name") or "Test"
                text = msg.get("text")
                photo = msg.get("photo")
                if photo:
                    await _handle_photo(client, chat_id, photo, tg_name)
                elif text:
                    await _handle_message(client, chat_id, text, tg_name)


if __name__ == "__main__":
    asyncio.run(main())
