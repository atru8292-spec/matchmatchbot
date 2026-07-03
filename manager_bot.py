"""Менеджер-бот (блок 11): приём команд и кнопок из Telegram (бот «Лиды»).

Аня и разработка управляют ботом прямо из Telegram, не заходя в БД:
  /leads, /lead, /takeover, /release, /block, /whitelist_add/remove/list
плюс inline-кнопки под алертами эскалации (общаться лично / больше не отвечать / решение по фото).

Транспорт — Telegram webhook: эндпоинт в main.py принимает update и зовёт handle_update.
Команды доступны только authorized user_id (config.manager_admin_ids). Модуль НИКОГДА
не роняет вебхук: любой сбой ловим, логируем, по возможности отвечаем текстом об ошибке.

Импортирует db и escalation (одностороннее — escalation про manager_bot не знает).
Фото-действия (одобрить/другое/больше не отвечать) переиспользуют main._run_ai/_send_scenario
через ленивый импорт внутри функции (main грузит manager_bot для эндпоинта — избегаем цикла).
"""
from __future__ import annotations

import logging

import httpx

import actions
import db
import escalation
import funnel
from config import settings

logger = logging.getLogger("matchmatch.manager_bot")

HELP_TEXT = (
    "👋 Привет! Я помогаю вести переписку с новыми людьми.\n\n"
    "Активные лиды:\n"
    "📋 /leads — список активных\n"
    "📇 /lead и номер — карточка лида (переписка, статус)\n\n"
    "Переписка:\n"
    "✋ /takeover и номер — вести переписку вручную (бот замолкает)\n"
    "🤖 /release и номер — вернуть боту (бот снова отвечает)\n"
    "🔕 /stop и номер — перестать отвечать человеку\n\n"
    "Клиенты из списка (бот им не пишет):\n"
    "⭐ /client_add и номер — отметить клиента\n"
    "➖ /client_remove и номер — убрать из клиентов\n"
    "📃 /clients — список клиентов\n\n"
    "Ивент:\n"
    "🎟 /event — настройки ивента и ссылок\n"
    "📅 /set_event дата время | адрес — задать ивент\n"
    "💳 /set_event_link url — ссылка оплаты/брони\n"
    "🖼 /set_invitation url — картинка-приглашение (+ /invitation_on)\n"
    "📚 /set_course_link url — ссылка на курсы\n\n"
    "💡 Проще всего — жать кнопки под сообщениями."
)


# ===== Утилиты =====

def _digits(phone: str) -> str:
    """Читаемый номер для вывода: срезаем префикс wa_."""
    return (phone or "").replace("wa_", "", 1)


def _norm_phone(raw: str) -> str | None:
    """Нормализовать телефон в 'wa_<digits>' или None (если цифр нет — сообщим об ошибке)."""
    try:
        return db._wa_phone(raw)
    except ValueError:
        return None


def is_admin(user_id) -> bool:
    """Разрешён ли Telegram user_id управлять ботом."""
    try:
        return int(user_id) in settings.manager_admin_ids
    except (TypeError, ValueError):
        return False


def _actor(frm: dict) -> str:
    """Кто выполнил действие — для added_by в whitelist / логов."""
    if not frm:
        return "tg:?"
    uname = frm.get("username")
    if uname:
        return f"@{uname}"
    name = frm.get("first_name") or ""
    return f"{name} (tg:{frm.get('id')})".strip()


async def _reply(chat_id, text: str, reply_markup: dict | None = None) -> None:
    """Ответить в исходный чат ботом «Лиды» (не обязательно чат Ани)."""
    await escalation._send_telegram(
        settings.tg_manager_bot_token, str(chat_id), text, reply_markup
    )


def _access_request_kb() -> dict | None:
    """Кнопка «Написать для доступа» → t.me/<support_contact>. None если контакт не задан."""
    handle = (settings.support_contact or "").lstrip("@").strip()
    if not handle:
        return None
    return {"inline_keyboard": [
        [{"text": "✍️ Написать для доступа", "url": f"https://t.me/{handle}"}],
    ]}


async def _answer_callback(callback_id: str, text: str = "") -> None:
    """Погасить «часики» на нажатой кнопке (answerCallbackQuery). Токен в логи не пишем."""
    token = settings.tg_manager_bot_token
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error("answerCallbackQuery вернул %s", e.response.status_code)
    except Exception:
        logger.exception("не смог ответить на callback")


async def _send_photo(chat_id, photo_url: str, caption: str | None = None,
                      reply_markup: dict | None = None) -> None:
    """Показать фото в чате (sendPhoto по public URL из Storage). Сбой — лог, не бросает.

    caption — подпись под фото (для карточки-обложки, лимит Telegram 1024 симв.).
    reply_markup — inline-кнопки под фото.
    """
    token = settings.tg_manager_bot_token
    if not token or not photo_url:
        return
    payload: dict = {"chat_id": str(chat_id), "photo": photo_url}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto", json=payload
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error("sendPhoto вернул %s", e.response.status_code)
    except Exception:
        logger.exception("не смог отправить фото")


async def _edit_reply_markup(chat_id, message_id, reply_markup: dict) -> None:
    """Обновить кнопки на уже отправленном сообщении (editMessageReplyMarkup).

    Нужно, чтобы после takeover/release кнопка на карточке сразу менялась
    (Общаться лично ↔ Вернуть боту), без повторного /lead. Сбой — лог, не бросает.
    """
    token = settings.tg_manager_bot_token
    if not token or not message_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
                json={"chat_id": str(chat_id), "message_id": message_id,
                      "reply_markup": reply_markup},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error("editMessageReplyMarkup вернул %s", e.response.status_code)
    except Exception:
        logger.exception("не смог обновить кнопки")


# ===== Форматтеры =====

def format_leads_list(leads: list[dict], stage: str | None) -> str:
    """Текст /leads: имя, номер, стадия, режим — по-человечески."""
    head = "📋 Активные лиды"
    if stage:
        head += f" · {funnel.stage_label(stage)}"
    if not leads:
        return head + "\n\nПока никого."
    lines = [head, ""]
    for ld in leads:
        name = ld.get("whatsapp_name") or ld.get("name") or "без имени"
        mode = ld.get("mode") or "auto"
        mode_mark = " ✋ вручную" if mode == "manual" else ""
        lines.append(f"• {name} — {funnel.stage_label(ld.get('funnel_stage'))}{mode_mark}")
        lines.append(f"    {_digits(ld.get('phone', ''))}")
    lines.append("\n👉 Подробнее: /lead и номер")
    return "\n".join(lines)


def format_lead_card(lead: dict, history: list[dict], whitelisted: bool) -> tuple[str, dict]:
    """Текст+клавиатура карточки лида для /lead и кнопки 'Карточка'."""
    name = lead.get("whatsapp_name") or lead.get("name") or "лид"
    phone = lead.get("phone", "")
    mode = lead.get("mode") or "auto"
    lines = [
        f"📇 {name}",
        f"📱 {_digits(phone)}",
        f"Этап: {funnel.stage_label(lead.get('funnel_stage'))}",
    ]
    # Состояние переписки: один текст для всех случаев «бот не пишет» (ручной режим/клиент).
    if lead.get("do_not_contact"):
        lines.append("🔕 Бот больше не пишет")
    elif mode == "manual" or whitelisted:
        lines.append("Переписка ведётся вручную")
    else:
        lines.append("Отвечает бот 🤖")
    # Необязательные поля — показываем только заполненные.
    extras = []
    if lead.get("age"):
        extras.append(f"возраст {lead['age']}")
    if lead.get("is_single") is not None:
        extras.append("холост" if lead["is_single"] else "не холост")
    if lead.get("city"):
        extras.append(str(lead["city"]))
    if lead.get("profession"):
        extras.append(str(lead["profession"]))
    if extras:
        lines.append("О нём: " + ", ".join(extras))

    lines.append("\n💬 Последняя переписка:")
    if not history:
        lines.append("  (пусто)")
    else:
        for m in history:
            # Кто написал: клиент / бот (авто-ответ) / оператор (вручную).
            who = {"lead": "Клиент", "manager": "Оператор", "anna": "Бот"}.get(m.get("sender"))
            if who is None:
                who = "Клиент" if m.get("direction") == "inbound" else "Бот"
            body = (m.get("text") or "[медиа]").replace("\n", " ")
            if len(body) > 80:
                body = body[:77] + "…"
            lines.append(f"  {who}: {body}")

    kb = escalation.card_action_kb(phone, is_manual=(mode == "manual"))
    return "\n".join(lines), kb


# ===== Точка входа (из вебхука) =====

async def handle_update(update: dict) -> None:
    """Разобрать Telegram update. Никогда не бросает (вебхук должен вернуть ok)."""
    try:
        if "callback_query" in update:
            await _handle_callback(update["callback_query"])
        elif "message" in update:
            await _handle_message(update["message"])
        else:
            logger.debug("manager_bot: update без message/callback_query, пропуск")
    except Exception:
        logger.exception("manager_bot: сбой обработки update")


# ===== Команды =====

async def _handle_message(message: dict) -> None:
    from_id = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()

    if not is_admin(from_id):
        logger.warning("manager_bot: команда от неавторизованного id=%s", from_id)
        contact = settings.support_contact or "администратору"
        await _reply(
            chat_id,
            "🔒 Это закрытый личный бот — доступ только у своих.\n\n"
            f"Если нужен доступ, напиши {contact}.",
            _access_request_kb(),
        )
        return

    if not text.startswith("/"):
        await _reply(chat_id, HELP_TEXT)
        return

    parts = text.split()
    cmd = parts[0].lower().split("@", 1)[0]  # /cmd@bot в группах → /cmd
    args = parts[1:]
    logger.info("manager_bot: команда %r от id=%s", text, from_id)

    handlers = {
        "/start": _cmd_help,
        "/help": _cmd_help,
        "/leads": _cmd_leads,
        "/lead": _cmd_lead,
        "/takeover": _cmd_takeover,
        "/release": _cmd_release,
        "/stop": _cmd_block,
        "/client_add": _cmd_wl_add,
        "/client_remove": _cmd_wl_remove,
        "/clients": _cmd_wl_list,
        "/event": _cmd_event,
        "/set_event": _cmd_set_event,
        "/event_off": _cmd_event_off,
        "/set_event_link": _cmd_set_event_link,
        "/set_course_link": _cmd_set_course_link,
        "/set_invitation": _cmd_set_invitation,
        "/invitation_on": _cmd_invitation_on,
        "/invitation_off": _cmd_invitation_off,
    }
    handler = handlers.get(cmd)
    if not handler:
        await _reply(chat_id, "Не знаю такой команды 🤔\n\n" + HELP_TEXT)
        return
    try:
        await handler(chat_id, args, message.get("from") or {})
    except Exception:
        logger.exception("manager_bot: команда %s упала", cmd)
        await _reply(chat_id, "⚠️ Что-то пошло не так.")


async def _cmd_help(chat_id, args, frm) -> None:
    await _reply(chat_id, HELP_TEXT)


async def _cmd_leads(chat_id, args, frm) -> None:
    # Фильтр по этапу — необязательный; если непонятный, просто показываем всех.
    stage = args[0] if (args and args[0] in funnel.FUNNEL_STAGES) else None
    leads = await db.list_active_leads(15, stage)
    await _reply(chat_id, format_leads_list(leads, stage))


async def _show_card(chat_id, phone: str) -> None:
    """Отправить карточку лида + его фото (если присылал). Общее для /lead и кнопки 'Карточка'."""
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    history = await db.get_conversation_history(phone, 10)
    whitelisted = await db.is_whitelisted(phone)
    text, kb = format_lead_card(lead, history, whitelisted)
    photos = await db.get_lead_photos(phone)
    urls = [p.get("storage_url") for p in photos if p.get("storage_url")]
    # Есть фото → карточка идёт ПОДПИСЬЮ к первому фото (кнопки там же), остальные — следом.
    # Лимит подписи Telegram 1024 симв.: если карточка длиннее — шлём текстом, фото отдельно.
    if urls and len(text) <= 1024:
        await _send_photo(chat_id, urls[0], caption=text, reply_markup=kb)
        for u in urls[1:]:
            await _send_photo(chat_id, u, caption="📸 ещё фото")
    else:
        await _reply(chat_id, text, kb)
        for u in urls:
            await _send_photo(chat_id, u, caption="📸 Фото")


async def _cmd_lead(chat_id, args, frm) -> None:
    if not args:
        await _reply(chat_id, "Формат: /lead и номер")
        return
    phone = _norm_phone(args[0])
    if not phone:
        await _reply(chat_id, f"Не похоже на номер: {args[0]}")
        return
    await _show_card(chat_id, phone)


async def _cmd_takeover(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Формат: /takeover и номер")
        return
    found = await db.set_manual(phone)
    await _reply(chat_id, f"✋ Бот молчит в чате с {_digits(phone)}."
                 if found else f"Не нашла такого человека: {_digits(phone)}")


async def _cmd_release(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Формат: /release и номер")
        return
    found = await db.set_auto(phone)
    await _reply(chat_id, f"🤖 Бот снова отвечает {_digits(phone)}."
                 if found else f"Не нашла такого человека: {_digits(phone)}")


async def _cmd_block(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Формат: /stop и номер")
        return
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    reason = " ".join(args[1:]) or "прекращено вручную"
    await db.block_lead(phone, reason)
    await _reply(chat_id, f"🔕 Бот больше не пишет {_digits(phone)}.")


async def _cmd_wl_add(chat_id, args, frm) -> None:
    if len(args) < 2:
        await _reply(chat_id, "Формат: /client_add, номер и кто это (например: /client_add 5215512345678 клиент с прошлого месяца)")
        return
    reason = " ".join(args[1:])
    try:
        await db.add_to_whitelist(args[0], reason, _actor(frm))
    except ValueError:
        await _reply(chat_id, f"Не похоже на номер: {args[0]}")
        return
    await _reply(chat_id, f"⭐ {_digits(_norm_phone(args[0]))} — добавлен в клиенты. Бот ему не пишет.")


async def _cmd_wl_remove(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Формат: /client_remove и номер")
        return
    await db.remove_from_whitelist(phone)
    await _reply(chat_id, f"{_digits(phone)} — убран из клиентов. Бот снова отвечает.")


async def _cmd_wl_list(chat_id, args, frm) -> None:
    rows = await db.list_whitelist()
    if not rows:
        await _reply(chat_id, "Пока нет клиентов.")
        return
    lines = ["⭐ Клиенты:", ""]
    for r in rows:
        note = r.get("reason")
        lines.append(f"• {_digits(r.get('phone', ''))}" + (f" — {note}" if note else ""))
    await _reply(chat_id, "\n".join(lines))


# ===== Настройки ивента (блок 13) =====

async def _cmd_event(chat_id, args, frm) -> None:
    """Показать текущие настройки ивента, ссылок и приглашения."""
    s = await db.get_settings(["event_active", "event_date", "event_time",
                               "event_address", "event_link", "course_link",
                               "invitation_url", "invitation_ready"])
    active = s.get("event_active") == "1"
    inv_ready = s.get("invitation_ready") == "1"
    lines = [
        "🎟 Ивент",
        f"Статус: {'включён ✅' if active else 'выключен ⛔'}",
        f"Дата: {s.get('event_date') or '—'}",
        f"Время: {s.get('event_time') or '—'}",
        f"Адрес: {s.get('event_address') or '—'}",
        f"Ссылка оплаты/брони: {s.get('event_link') or '—'}",
        "",
        f"Картинка-приглашение: {'готова ✅' if inv_ready else 'не готова ⛔'}",
        f"URL картинки: {s.get('invitation_url') or '—'}",
        "",
        f"📚 Ссылка на курсы: {s.get('course_link') or '— (не задана, курсы не предлагаются)'}",
        "",
        "Изменить: /set_event 2026-08-15 20:30 | Av. Reforma 123, CDMX",
        "Оплата: /set_event_link <url>   Курсы: /set_course_link <url>",
        "Картинка: /set_invitation <url>, потом /invitation_on",
    ]
    await _reply(chat_id, "\n".join(lines))


async def _cmd_set_event_link(chat_id, args, frm) -> None:
    """Ссылка на оплату/бронь ивента — попадает в напоминания через [event_link]."""
    if not args or not args[0].lower().startswith("http"):
        await _reply(chat_id, "Формат: /set_event_link <url>")
        return
    await db.set_setting("event_link", args[0].strip())
    await _reply(chat_id, "💳 Ссылка на оплату/бронь сохранена — попадёт в напоминания об ивенте.")


async def _cmd_set_course_link(chat_id, args, frm) -> None:
    """Ссылка на курсы — применяется во всех отказных сценариях через [course_link]."""
    if not args or not args[0].lower().startswith("http"):
        await _reply(chat_id, "Формат: /set_course_link <url>")
        return
    await db.set_setting("course_link", args[0].strip())
    await _reply(chat_id, "📚 Ссылка на курсы сохранена — применится сразу во всех сообщениях, "
                          "где предлагаем курсы.")


async def _cmd_set_event(chat_id, args, frm) -> None:
    """Задать дату/время/адрес: /set_event 2026-08-15 20:30 | Адрес. Включает ивент."""
    raw = " ".join(args)
    if "|" not in raw:
        await _reply(chat_id, "Формат: /set_event ГГГГ-ММ-ДД ЧЧ:ММ | Адрес\n"
                              "Например: /set_event 2026-08-15 20:30 | Av. Reforma 123, CDMX")
        return
    left, address = raw.split("|", 1)
    address = address.strip()
    parts = left.split()
    date = parts[0] if parts else ""
    time = " ".join(parts[1:]).strip()
    # Валидация даты (иначе планировщик не распознает и просто пропустит).
    import datetime as _dt
    try:
        _dt.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await _reply(chat_id, f"Дата должна быть ГГГГ-ММ-ДД, а не {date!r}")
        return
    if not address:
        await _reply(chat_id, "Не хватает адреса после «|».")
        return
    if not time:
        await _reply(chat_id, "Не хватает времени. Формат: /set_event 2026-08-15 20:30 | Адрес")
        return
    await db.set_setting("event_date", date)
    await db.set_setting("event_time", time)
    await db.set_setting("event_address", address)
    await db.set_setting("event_active", "1")
    await _reply(chat_id, f"🎟 Ивент включён.\nДата: {date}  Время: {time or '—'}\n"
                          f"Адрес: {address}\n\nНапоминания уйдут за день и в день ивента.")


async def _cmd_event_off(chat_id, args, frm) -> None:
    await db.set_setting("event_active", "0")
    await _reply(chat_id, "⛔ Ивент выключен — напоминания слаться не будут.")


async def _cmd_set_invitation(chat_id, args, frm) -> None:
    """Задать URL картинки-приглашения. Готовность включается отдельно /invitation_on."""
    if not args:
        await _reply(chat_id, "Формат: /set_invitation <url картинки>")
        return
    url = args[0].strip()
    if not url.lower().startswith("http"):
        await _reply(chat_id, f"Нужен URL (http…), а не {url!r}")
        return
    await db.set_setting("invitation_url", url)
    await db.set_setting("invitation_ready", "0")  # после смены URL — заново подтвердить
    await _reply(chat_id, "🖼 URL картинки сохранён. Пока НЕ отправляется — включи /invitation_on, "
                          "когда убедишься что картинка та.")


async def _cmd_invitation_on(chat_id, args, frm) -> None:
    url = await db.get_setting("invitation_url")
    if not url:
        await _reply(chat_id, "Сначала задай картинку: /set_invitation <url>")
        return
    await db.set_setting("invitation_ready", "1")
    await _reply(chat_id, "✅ Картинка-приглашение готова — бот будет её отправлять.")


async def _cmd_invitation_off(chat_id, args, frm) -> None:
    await db.set_setting("invitation_ready", "0")
    await _reply(chat_id, "⛔ Отправка картинки-приглашения выключена.")


# ===== Callback-кнопки =====

async def _handle_callback(cq: dict) -> None:
    from_id = (cq.get("from") or {}).get("id")
    cb_id = cq.get("id", "")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if not is_admin(from_id):
        logger.warning("manager_bot: callback от неавторизованного id=%s", from_id)
        await _answer_callback(cb_id, "Нет доступа")
        return

    # message=None у Telegram, если сообщение с кнопкой удалено/старше 48ч — отвечать некуда.
    if not chat_id:
        await _answer_callback(cb_id, "Сообщение недоступно")
        return

    data = cq.get("data") or ""
    logger.info("manager_bot: callback %r от id=%s", data, from_id)
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != escalation.CB:
        await _answer_callback(cb_id, "Не понял действие")
        return
    _, action, phone = parts

    dispatch = {
        "takeover": _cb_takeover,
        "release": _cb_release,
        "block": _cb_block,
        "card": _cb_card,
        "photo_ok": _cb_photo_ok,
        "photo_retry": _cb_photo_retry,
        "photo_reject": _cb_photo_reject,
        "payment_ok": _cb_payment_ok,
        "payment_event": _cb_payment_event,
        "payment_sub": _cb_payment_sub,
    }
    handler = dispatch.get(action)
    if not handler:
        await _answer_callback(cb_id, "Неизвестное действие")
        return
    try:
        await handler(chat_id, cb_id, phone, message_id)
    except Exception:
        logger.exception("manager_bot: callback %s упал", action)
        await _answer_callback(cb_id, "Ошибка")
        await _reply(chat_id, "⚠️ Что-то пошло не так.")


async def _cb_takeover(chat_id, cb_id, phone, message_id=None) -> None:
    found = await db.set_manual(phone)
    await _answer_callback(cb_id, "Готово" if found else "Не нашла")
    if not found:
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    await _reply(chat_id, f"✋ Бот молчит в чате с {_digits(phone)}.")
    # Кнопка на этом же сообщении сразу становится «Вернуть боту».
    await _edit_reply_markup(chat_id, message_id, escalation.card_action_kb(phone, is_manual=True))


async def _cb_release(chat_id, cb_id, phone, message_id=None) -> None:
    found = await db.set_auto(phone)
    await _answer_callback(cb_id, "Готово" if found else "Не нашла")
    if not found:
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    await _reply(chat_id, f"🤖 Бот снова отвечает в чате с {_digits(phone)}.")
    # Кнопка возвращается к «Общаться лично».
    await _edit_reply_markup(chat_id, message_id, escalation.card_action_kb(phone, is_manual=False))


async def _cb_block(chat_id, cb_id, phone, message_id=None) -> None:
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _answer_callback(cb_id, "Не нашла")
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    await db.block_lead(phone, "прекращено кнопкой (менеджер)")
    await _answer_callback(cb_id, "Готово")
    await _reply(chat_id, f"🔕 Бот больше не пишет {_digits(phone)}.")


async def _cb_card(chat_id, cb_id, phone, message_id=None) -> None:
    await _answer_callback(cb_id)
    await _show_card(chat_id, phone)


async def _cb_photo_ok(chat_id, cb_id, phone, message_id=None) -> None:
    """Одобрить фото вручную = путь вердикта 'ok': вернуть бота, квалифицировать, питч."""
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _answer_callback(cb_id, "Не нашла")
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    # Диалог могли уже прекратить, пока фото ждало решения — не пишем в do_not_contact
    # (иначе бот отправит сообщение, т.к. _run_ai минует filters.decide).
    if lead.get("do_not_contact"):
        await _answer_callback(cb_id, "Уже не общаюсь")
        await _reply(chat_id, f"⚠️ Бот уже не пишет {_digits(phone)} — одобрение отменено.")
        return

    await db.set_auto(phone)                       # бот снова ведёт диалог
    await db.mark_photo_received(phone, True)
    # Возврат set_funnel_stage — идемпотентный флаг: при повторном нажатии (второй admin/
    # дабл-клик) стадия уже 'qualified' → changed=False → питч НЕ шлём второй раз.
    changed = await db.set_funnel_stage(phone, "qualified", meta={"photo": "manual_ok"})
    await _answer_callback(cb_id, "Одобрено")
    await _reply(chat_id, f"✅ Фото ок! Бот продолжает переписку с {_digits(phone)}.")
    if changed:
        # Свежий лид: стадия/поля изменились выше, AI должен видеть 'qualified' для RAG.
        lead = await db.get_lead_by_phone(phone) or lead
        import main  # ленивый импорт: избегаем цикла main↔manager_bot
        await main._run_ai(phone, lead, "[фото одобрено вручную]")


async def _cb_photo_retry(chat_id, cb_id, phone, message_id=None) -> None:
    """Просить другое фото: вернуть бота (следующее фото снова через Vision) + сценарий 5."""
    lead = await db.get_lead_by_phone(phone)
    if lead and lead.get("do_not_contact"):
        # Лида уже прекратили вести (напр. вторым действием) — не пишем заблокированному.
        await _answer_callback(cb_id, "Уже не общаюсь")
        await _reply(chat_id, f"⚠️ Бот уже не пишет {_digits(phone)} — действие отменено.")
        return
    await db.set_auto(phone)
    await _answer_callback(cb_id, "Просим другое")
    await _reply(chat_id, f"🔄 Бот попросил у {_digits(phone)} другое фото.")
    import main
    await main._send_scenario(phone, 5)


async def _cb_photo_reject(chat_id, cb_id, phone, message_id=None) -> None:
    """Отклонить фото = путь вердикта 'reject': блок навсегда + прощание (сценарий 12)."""
    title = await db.get_scenario_title(12)
    await db.block_lead(phone, f"Vision (ручной отказ): {title or 'фото неприемлемо'}")
    await _answer_callback(cb_id, "Отклонено")
    await _reply(chat_id, f"🔕 Фото не подошло, бот больше не пишет {_digits(phone)}.")
    import main
    await main._send_scenario(phone, 12)


# ===== Подтверждение оплаты (блок 13) =====

async def _cb_payment_ok(chat_id, cb_id, phone, message_id=None) -> None:
    """Подтвердить оплату. Стадия по selected_service; если неоднозначно — спросить кнопками."""
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _answer_callback(cb_id, "Не нашла")
        await _reply(chat_id, f"Не нашла такого человека: {_digits(phone)}")
        return
    target = actions.stage_for_service(lead.get("selected_service"))
    if target is None:
        # Непонятно за что оплата → уточняющие кнопки (ивент / подписка).
        await _answer_callback(cb_id, "За что оплата?")
        await _reply(chat_id, f"За что оплата у {_digits(phone)}?",
                     escalation.payment_target_kb(phone))
        return
    await actions.confirm_payment(phone, target, source="manual")
    await _answer_callback(cb_id, "Оплата подтверждена")
    await _reply(chat_id, f"✅ Оплата подтверждена, {_digits(phone)} → {funnel.stage_label(target)}.")


async def _cb_payment_event(chat_id, cb_id, phone, message_id=None) -> None:
    await actions.confirm_payment(phone, "event_attended", source="manual")
    await _answer_callback(cb_id, "Подтверждено")
    await _reply(chat_id, f"✅ Оплата за ивент подтверждена, {_digits(phone)} → {funnel.stage_label('event_attended')}.")


async def _cb_payment_sub(chat_id, cb_id, phone, message_id=None) -> None:
    await actions.confirm_payment(phone, "client_starter", source="manual")
    await _answer_callback(cb_id, "Подтверждено")
    await _reply(chat_id, f"✅ Оплата подписки подтверждена, {_digits(phone)} → {funnel.stage_label('client_starter')}.")
