"""Отдельный Telegram-бот для живого теста AI — переписка как в WhatsApp, без
интерфейса CRM. В отличие от /api/mini/test-chat (песочница в памяти), этот пишет
в РЕАЛЬНУЮ БД (leads/messages) — история переживает перезапуск процесса, лида видно
в мини-CRM. Изоляция от боевых лидов: телефон = 'tg_<chat_id>' (не 'wa_...'),
source='telegram_test' — не путается с реальными WhatsApp-лидами, свой namespace.

Дебаунс: как в проде (main.py, delay=90.0/max_wait=120.0) — серия быстрых сообщений
от лида склеивается в один залп перед ответом, вместо мгновенного ответа на каждое.
Фото — ВНЕ дебаунса (отдельная немедленная ветка, как и в проде).

Фото: скачивается из Telegram и прогоняется через реальный vision.analyze_photo
(тот же Vision, что в проде) — ok/retry/reject/manual ветки. Специально НЕ вызывает
escalation.*/booking.*/db.block_lead — те шлют реальные алерты Ане/Миле в боевой
менеджер-бот или создают реальные события в календаре, а это песочница.

Видео ивента: если сценарий помечает send_event_video — реально скачиваем/шлём
случайное видео из event_media (тот же пул, что в проде), с тем же дедупом.

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
from debounce import Debouncer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("matchmatch.telegram_test_bot")

API = f"https://api.telegram.org/bot{settings.tg_test_bot_token}"
FILE_API = f"https://api.telegram.org/file/bot{settings.tg_test_bot_token}"
POLL_TIMEOUT = 30
SOURCE = "telegram_test"
PHOTO_MARKER = "[photo received]"
DEBOUNCE_DELAY = 90.0
DEBOUNCE_MAX_WAIT = 120.0

_client: httpx.AsyncClient | None = None


def _phone(chat_id: int) -> str:
    return f"tg_{chat_id}"


def _chat_id_from_phone(phone: str) -> int:
    return int(phone.removeprefix("tg_"))


RESET_LABEL = "🔄 Начать заново"

# Постоянная клавиатура внизу экрана (не нужно печатать /reset руками) — для Ани,
# которая с командами не очень дружит. resize_keyboard — компактная, не на весь экран.
_KEYBOARD = {
    "keyboard": [[{"text": RESET_LABEL}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


async def _send(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    await client.post(f"{API}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "reply_markup": _KEYBOARD})


async def _register_commands(client: httpx.AsyncClient) -> None:
    """Команды в меню "/" Telegram — видны без объяснений, что вообще можно ввести."""
    commands = [{"command": "reset", "description": "Начать заново (снести историю)"}]
    await client.post(f"{API}/setMyCommands", json={"commands": commands})


async def _reset(phone: str) -> None:
    # Свой namespace (tg_) — не через db.reset_lead_history (та форсит 'wa_' префикс).
    await db._get_pool().execute("DELETE FROM leads WHERE phone = $1", phone)


async def _get_or_create_lead(phone: str, tg_name: str) -> dict:
    lead = await db.get_lead_by_phone(phone)
    if lead is None:
        lead = await db.upsert_lead(phone, source=SOURCE, whatsapp_name=tg_name,
                                     funnel_stage="new")
    return lead


async def _maybe_send_event_video(client: httpx.AsyncClient, chat_id: int, phone: str,
                                  result: dict) -> None:
    """Если сценарий просит explainer-видео ивента — реально скачать/отправить, с дедупом
    (тот же пул event_media, что в проде). Сбой не должен ронять уже отправленный ответ."""
    if not result.get("send_event_video"):
        return
    try:
        s = await db.get_settings(["event_date"])
        event_date = s.get("event_date") or None
        if await db.event_media_sent(phone, "video", event_date):
            return
        items = await db.random_event_media("video", 1)
        if not items:
            logger.info("нет видео ивента в пуле — пропуск (chat_id=%s)", chat_id)
            return
        url = items[0]["storage_url"]
        r = await client.post(f"{API}/sendVideo",
                              json={"chat_id": chat_id, "video": url, "reply_markup": _KEYBOARD})
        r.raise_for_status()
        marker = db.media_marker("video", event_date) or "[video ивента отправлено]"
        await db.insert_message(phone, "outbound", "anna", marker)
        logger.info("видео ивента отправлено (chat_id=%s)", chat_id)
    except Exception:
        logger.exception("не смогла отправить видео ивента (chat_id=%s)", chat_id)


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

    await _maybe_send_event_video(client, chat_id, phone, result)

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


async def _on_flush(phone: str) -> None:
    """Callback дебаунсера: склеить залп непроцессенных inbound и ответить ОДИН раз."""
    msgs = await db.get_unprocessed_inbound(phone)
    if not msgs:
        return  # уже обработано (например, ручным /reset между сообщением и флашем)
    combined = "\n".join((m.get("text") or "") for m in msgs)
    await db.mark_messages_processed([m["id"] for m in msgs])
    logger.info("склеенный залп %s (%d сообщ.): %r", phone, len(msgs), combined)

    lead = await db.get_lead_by_phone(phone)
    if lead is None:
        return  # лид снесён (/reset) до флаша — нечего обрабатывать
    chat_id = _chat_id_from_phone(phone)
    history = await db.get_conversation_history(phone, limit=30)
    await _reply_via_ai(_client, chat_id, phone, lead, history, combined)


_debouncer = Debouncer(_on_flush, delay=DEBOUNCE_DELAY, max_wait=DEBOUNCE_MAX_WAIT)


async def _handle_message(chat_id: int, text: str, tg_name: str) -> None:
    phone = _phone(chat_id)

    if text.strip() in ("/start", "/reset", RESET_LABEL):
        await _reset(phone)
        await _send(_client, chat_id,
                     "Привет! Пиши как обычному лиду в WhatsApp — отвечу по реальному "
                     "сценарию бота. "
                     f'Кнопка "{RESET_LABEL}" внизу — начать с чистого листа в любой момент.')
        return

    await _get_or_create_lead(phone, tg_name)
    await db.insert_message(phone, "inbound", "lead", text)
    await _debouncer.trigger(phone)


async def _download_telegram_photo(client: httpx.AsyncClient, file_id: str) -> bytes:
    r = await client.get(f"{API}/getFile", params={"file_id": file_id})
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    r2 = await client.get(f"{FILE_API}/{file_path}")
    r2.raise_for_status()
    return r2.content


async def _handle_photo(chat_id: int, photo_sizes: list[dict], tg_name: str) -> None:
    """Фото → реальный Vision (как в проде), без escalation/booking/block (песочница).
    Вне дебаунса — отдельная немедленная ветка (как и в проде)."""
    phone = _phone(chat_id)
    lead = await _get_or_create_lead(phone, tg_name)
    await db.insert_message(phone, "inbound", "lead", PHOTO_MARKER)

    largest = photo_sizes[-1]  # Telegram отдаёт по возрастанию размера
    try:
        img = await _download_telegram_photo(_client, largest["file_id"])
        res = await vision.analyze_photo(img)
    except Exception:
        logger.exception("ошибка Vision-анализа (chat_id=%s)", chat_id)
        await _send(_client, chat_id, "Ой, не смогла обработать фото 🤍 попробуй ещё раз")
        return

    verdict = res["verdict"]
    logger.info("фото [%s]: verdict=%s (%s)", phone, verdict, res.get("reason", ""))

    if verdict == "ok":
        await db.upsert_lead(phone, photo_received=True, funnel_stage="qualified")
        history = await db.get_conversation_history(phone, limit=30)
        lead = await db.get_lead_by_phone(phone)
        await _reply_via_ai(_client, chat_id, phone, lead, history, "[фото одобрено]")
    elif verdict == "retry":
        await _send_scenario_text(_client, chat_id, phone, 11)  # "mándame otra foto..."
    elif verdict == "reject":
        await _send_scenario_text(_client, chat_id, phone, 12)  # despedida
    else:  # manual — в проде решает Аня по кнопке; в песочнице просто отвечаем нейтрально
        await _send(_client, chat_id,
                     "Déjame revisar tu foto con calma y te confirmo en un momento 🤍 "
                     f"(vision: manual — {res.get('reason', 'sin motivo')})")


async def main() -> None:
    global _client
    if not settings.tg_test_bot_token:
        raise RuntimeError("TG_TEST_BOT_TOKEN не задан в .env")
    await db.init_pool()
    offset = 0
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
        _client = client
        await _register_commands(client)
        logger.info("тест-бот запущен (дебаунс %.0fs/%.0fs), жду сообщений...",
                    DEBOUNCE_DELAY, DEBOUNCE_MAX_WAIT)
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
                    await _handle_photo(chat_id, photo, tg_name)
                elif text:
                    await _handle_message(chat_id, text, tg_name)


if __name__ == "__main__":
    asyncio.run(main())
