"""Менеджер-бот (блок 11): приём команд и кнопок из Telegram (бот «Лиды»).

Аня и разработка управляют ботом прямо из Telegram, не заходя в БД:
  /leads, /lead, /takeover, /release, /block, /whitelist_add/remove/list
плюс inline-кнопки под алертами эскалации (взять себе / заблокировать / решение по фото).

Транспорт — Telegram webhook: эндпоинт в main.py принимает update и зовёт handle_update.
Команды доступны только authorized user_id (config.manager_admin_ids). Модуль НИКОГДА
не роняет вебхук: любой сбой ловим, логируем, по возможности отвечаем текстом об ошибке.

Импортирует db и escalation (одностороннее — escalation про manager_bot не знает).
Фото-действия (одобрить/другое/заблокировать) переиспользуют main._run_ai/_send_scenario
через ленивый импорт внутри функции (main грузит manager_bot для эндпоинта — избегаем цикла).
"""
from __future__ import annotations

import logging

import httpx

import db
import escalation
import funnel
from config import settings

logger = logging.getLogger("matchmatch.manager_bot")

HELP_TEXT = (
    "🤖 Управление ботом Anna\n\n"
    "/leads [стадия] — активные лиды (опц. фильтр по стадии)\n"
    "/lead <phone> — карточка лида (история, стадия, статус)\n"
    "/takeover <phone> — забрать лида себе (бот молчит)\n"
    "/release <phone> — вернуть лида боту\n"
    "/block <phone> [причина] — заблокировать\n"
    "/whitelist_add <phone> <причина> — добавить в whitelist\n"
    "/whitelist_remove <phone> — убрать из whitelist\n"
    "/whitelist_list — показать whitelist\n"
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


# ===== Форматтеры =====

def format_leads_list(leads: list[dict], stage: str | None) -> str:
    """Текст /leads: имя, номер, стадия, режим."""
    head = "📋 Активные лиды"
    if stage:
        head += f" · {funnel.stage_label(stage)}"
    if not leads:
        return head + "\n\nПусто."
    lines = [head, ""]
    for ld in leads:
        name = ld.get("whatsapp_name") or ld.get("name") or "лид"
        mode = ld.get("mode") or "auto"
        mode_mark = " 🖐 manual" if mode == "manual" else ""
        lines.append(f"• {name} — {funnel.stage_label(ld.get('funnel_stage'))}{mode_mark}")
        lines.append(f"    {_digits(ld.get('phone', ''))}")
    lines.append("\nКарточка: /lead <phone>")
    return "\n".join(lines)


def format_lead_card(lead: dict, history: list[dict], whitelisted: bool) -> tuple[str, dict]:
    """Текст+клавиатура карточки лида для /lead и кнопки 'Карточка'."""
    name = lead.get("whatsapp_name") or lead.get("name") or "лид"
    phone = lead.get("phone", "")
    mode = lead.get("mode") or "auto"
    lines = [
        f"📇 {name}",
        f"📱 {_digits(phone)}",
        f"Стадия: {funnel.stage_label(lead.get('funnel_stage'))}",
        f"Режим: {'🖐 ручной (бот молчит)' if mode == 'manual' else '🤖 авто'}",
    ]
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
        lines.append("Инфо: " + ", ".join(extras))
    if whitelisted:
        lines.append("⭐ В whitelist (VIP/клиент — бот молчит)")
    if lead.get("do_not_contact"):
        lines.append("⛔ do_not_contact")

    lines.append("\n💬 Последние сообщения:")
    if not history:
        lines.append("  (пусто)")
    else:
        for m in history:
            arrow = "→" if m.get("direction") == "outbound" else "←"
            body = (m.get("text") or "[медиа]").replace("\n", " ")
            if len(body) > 80:
                body = body[:77] + "…"
            lines.append(f"  {arrow} {body}")

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
        await _reply(chat_id, "⛔ Нет доступа.")
        return

    if not text.startswith("/"):
        await _reply(chat_id, HELP_TEXT)
        return

    parts = text.split()
    cmd = parts[0].lower().split("@", 1)[0]  # /cmd@bot в группах → /cmd
    args = parts[1:]

    handlers = {
        "/start": _cmd_help,
        "/help": _cmd_help,
        "/leads": _cmd_leads,
        "/lead": _cmd_lead,
        "/takeover": _cmd_takeover,
        "/release": _cmd_release,
        "/block": _cmd_block,
        "/whitelist_add": _cmd_wl_add,
        "/whitelist_remove": _cmd_wl_remove,
        "/whitelist_list": _cmd_wl_list,
    }
    handler = handlers.get(cmd)
    if not handler:
        await _reply(chat_id, "Неизвестная команда.\n\n" + HELP_TEXT)
        return
    try:
        await handler(chat_id, args, message.get("from") or {})
    except Exception:
        logger.exception("manager_bot: команда %s упала", cmd)
        await _reply(chat_id, "⚠️ Ошибка при выполнении команды (детали в логах).")


async def _cmd_help(chat_id, args, frm) -> None:
    await _reply(chat_id, HELP_TEXT)


async def _cmd_leads(chat_id, args, frm) -> None:
    stage = args[0] if args else None
    if stage and stage not in funnel.FUNNEL_STAGES:
        await _reply(chat_id, f"Неизвестная стадия: {stage}\nДоступные: "
                              + ", ".join(funnel.FUNNEL_STAGES.keys()))
        return
    leads = await db.list_active_leads(15, stage)
    await _reply(chat_id, format_leads_list(leads, stage))


async def _cmd_lead(chat_id, args, frm) -> None:
    if not args:
        await _reply(chat_id, "Использование: /lead <phone>")
        return
    phone = _norm_phone(args[0])
    if not phone:
        await _reply(chat_id, f"Некорректный номер: {args[0]}")
        return
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _reply(chat_id, f"Лид не найден: {_digits(phone)}")
        return
    history = await db.get_conversation_history(phone, 10)
    whitelisted = await db.is_whitelisted(phone)
    text, kb = format_lead_card(lead, history, whitelisted)
    await _reply(chat_id, text, kb)


async def _cmd_takeover(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Использование: /takeover <phone>")
        return
    found = await db.set_manual(phone)
    await _reply(chat_id, f"🤝 Взял(а) себе {_digits(phone)} — бот молчит."
                 if found else f"Лид не найден: {_digits(phone)}")


async def _cmd_release(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Использование: /release <phone>")
        return
    found = await db.set_auto(phone)
    await _reply(chat_id, f"↩️ Вернул(а) {_digits(phone)} боту — снова отвечает."
                 if found else f"Лид не найден: {_digits(phone)}")


async def _cmd_block(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Использование: /block <phone> [причина]")
        return
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _reply(chat_id, f"Лид не найден: {_digits(phone)}")
        return
    reason = " ".join(args[1:]) or "ручной блок (менеджер)"
    await db.block_lead(phone, reason)
    await _reply(chat_id, f"⛔ Заблокирован {_digits(phone)}: {reason}")


async def _cmd_wl_add(chat_id, args, frm) -> None:
    if len(args) < 2:
        await _reply(chat_id, "Использование: /whitelist_add <phone> <причина>")
        return
    reason = " ".join(args[1:])
    try:
        await db.add_to_whitelist(args[0], reason, _actor(frm))
    except ValueError:
        await _reply(chat_id, f"Некорректный номер: {args[0]}")
        return
    await _reply(chat_id, f"⭐ Добавлен в whitelist {_digits(_norm_phone(args[0]))}: {reason}")


async def _cmd_wl_remove(chat_id, args, frm) -> None:
    phone = _norm_phone(args[0]) if args else None
    if not phone:
        await _reply(chat_id, "Использование: /whitelist_remove <phone>")
        return
    await db.remove_from_whitelist(phone)
    await _reply(chat_id, f"Убран из whitelist: {_digits(phone)}")


async def _cmd_wl_list(chat_id, args, frm) -> None:
    rows = await db.list_whitelist()
    if not rows:
        await _reply(chat_id, "Whitelist пуст.")
        return
    lines = ["⭐ Whitelist:", ""]
    for r in rows:
        lines.append(f"• {_digits(r.get('phone', ''))} — {r.get('reason') or '—'} "
                     f"(by {r.get('added_by') or '?'})")
    await _reply(chat_id, "\n".join(lines))


# ===== Callback-кнопки =====

async def _handle_callback(cq: dict) -> None:
    from_id = (cq.get("from") or {}).get("id")
    cb_id = cq.get("id", "")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")

    if not is_admin(from_id):
        logger.warning("manager_bot: callback от неавторизованного id=%s", from_id)
        await _answer_callback(cb_id, "Нет доступа")
        return

    # message=None у Telegram, если сообщение с кнопкой удалено/старше 48ч — отвечать некуда.
    if not chat_id:
        await _answer_callback(cb_id, "Сообщение недоступно")
        return

    data = cq.get("data") or ""
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
    }
    handler = dispatch.get(action)
    if not handler:
        await _answer_callback(cb_id, "Неизвестное действие")
        return
    try:
        await handler(chat_id, cb_id, phone)
    except Exception:
        logger.exception("manager_bot: callback %s упал", action)
        await _answer_callback(cb_id, "Ошибка")
        await _reply(chat_id, "⚠️ Ошибка при выполнении действия (детали в логах).")


async def _cb_takeover(chat_id, cb_id, phone) -> None:
    found = await db.set_manual(phone)
    await _answer_callback(cb_id, "Взято" if found else "Не найден")
    await _reply(chat_id, f"🤝 Взял(а) себе {_digits(phone)} — бот молчит."
                 if found else f"Лид не найден: {_digits(phone)}")


async def _cb_release(chat_id, cb_id, phone) -> None:
    found = await db.set_auto(phone)
    await _answer_callback(cb_id, "Возвращён" if found else "Не найден")
    await _reply(chat_id, f"↩️ Вернул(а) {_digits(phone)} боту."
                 if found else f"Лид не найден: {_digits(phone)}")


async def _cb_block(chat_id, cb_id, phone) -> None:
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _answer_callback(cb_id, "Не найден")
        await _reply(chat_id, f"Лид не найден: {_digits(phone)}")
        return
    await db.block_lead(phone, "ручной блок (кнопка)")
    await _answer_callback(cb_id, "Заблокирован")
    await _reply(chat_id, f"⛔ Заблокирован {_digits(phone)}.")


async def _cb_card(chat_id, cb_id, phone) -> None:
    await _answer_callback(cb_id)
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _reply(chat_id, f"Лид не найден: {_digits(phone)}")
        return
    history = await db.get_conversation_history(phone, 10)
    whitelisted = await db.is_whitelisted(phone)
    text, kb = format_lead_card(lead, history, whitelisted)
    await _reply(chat_id, text, kb)


async def _cb_photo_ok(chat_id, cb_id, phone) -> None:
    """Одобрить фото вручную = путь вердикта 'ok': вернуть бота, квалифицировать, питч."""
    lead = await db.get_lead_by_phone(phone)
    if not lead:
        await _answer_callback(cb_id, "Лид не найден")
        await _reply(chat_id, f"Лид не найден: {_digits(phone)}")
        return
    # Лида могли заблокировать, пока фото ждало решения — не пишем в do_not_contact
    # (иначе бот отправит сообщение заблокированному, т.к. _run_ai минует filters.decide).
    if lead.get("do_not_contact"):
        await _answer_callback(cb_id, "Лид заблокирован")
        await _reply(chat_id, f"⚠️ {_digits(phone)} заблокирован — одобрение отменено.")
        return

    await db.set_auto(phone)                       # бот снова ведёт диалог
    await db.mark_photo_received(phone, True)
    # Возврат set_funnel_stage — идемпотентный флаг: при повторном нажатии (второй admin/
    # дабл-клик) стадия уже 'qualified' → changed=False → питч НЕ шлём второй раз.
    changed = await db.set_funnel_stage(phone, "qualified", meta={"photo": "manual_ok"})
    await _answer_callback(cb_id, "Одобрено")
    await _reply(chat_id, f"✅ Фото одобрено, {_digits(phone)} → Прошёл проверку. Бот продолжит.")
    if changed:
        # Свежий лид: стадия/поля изменились выше, AI должен видеть 'qualified' для RAG.
        lead = await db.get_lead_by_phone(phone) or lead
        import main  # ленивый импорт: избегаем цикла main↔manager_bot
        await main._run_ai(phone, lead, "[фото одобрено вручную]")


async def _cb_photo_retry(chat_id, cb_id, phone) -> None:
    """Просить другое фото: вернуть бота (следующее фото снова через Vision) + сценарий 5."""
    await db.set_auto(phone)
    await _answer_callback(cb_id, "Просим другое")
    await _reply(chat_id, f"🔄 {_digits(phone)}: попросили другое фото.")
    import main
    await main._send_scenario(phone, 5)


async def _cb_photo_reject(chat_id, cb_id, phone) -> None:
    """Отклонить фото = путь вердикта 'reject': блок навсегда + прощание (сценарий 12)."""
    title = await db.get_scenario_title(12)
    await db.block_lead(phone, f"Vision (ручной отказ): {title or 'фото неприемлемо'}")
    await _answer_callback(cb_id, "Отклонено")
    await _reply(chat_id, f"⛔ Фото отклонено, {_digits(phone)} заблокирован.")
    import main
    await main._send_scenario(phone, 12)
