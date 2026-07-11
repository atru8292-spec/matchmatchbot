"""Общие бизнес-действия блока 13, переиспользуемые main и manager_bot.

Вынесены сюда, чтобы не плодить циклы импортов (main ↔ manager_bot). Зависят только
от db и sender. Подтверждение оплаты — единая точка: и кнопка менеджера, и будущий
Stripe-вебхук вызывают confirm_payment (source различает источник, логика не дублируется).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import funnel
import gcal
import sender
from config import settings

logger = logging.getLogger("matchmatch.actions")
_CDMX = ZoneInfo("America/Mexico_City")

INVITATION_URL_KEY = "invitation_url"
INVITATION_READY_KEY = "invitation_ready"

# selected_service → стадия при подтверждении оплаты. Неоднозначное (None/пусто) →
# manager_bot спросит Аню кнопками (payment_event / payment_sub).
SERVICE_TO_STAGE = {
    "event": "event_attended",
    "starter": "client_starter",
    "standard": "client_standard",
    "vip": "client_vip",
}


async def maybe_send_invitation(phone: str) -> bool:
    """Отправить картинку-приглашение, ЕСЛИ Аня отметила её готовой и задала URL.

    Пока invitation_ready != '1' или нет URL — не шлём (картинка под каждый ивент своя,
    отправляем только после подтверждения готовности). Вернуть True если отправлено.
    """
    s = await db.get_settings([INVITATION_URL_KEY, INVITATION_READY_KEY])
    if s.get(INVITATION_READY_KEY) != "1" or not s.get(INVITATION_URL_KEY):
        logger.info("приглашение не готово (ready=%s, url=%s) — не шлём %s",
                    s.get(INVITATION_READY_KEY), bool(s.get(INVITATION_URL_KEY)), phone)
        return False
    return await sender.send_image(phone, s[INVITATION_URL_KEY])


EVENT_PHOTO_COUNT = 3  # фото за раз (антибан)
EVENT_VIDEO_COUNT = 1  # видео за раз (тяжёлое — одного достаточно)


async def send_event_photos(phone: str, event_date: str | None = None) -> int:
    """Прислать лиду до 3 случайных ФОТО с ивентов (если фото ещё не слали). Вернуть число."""
    return await _send_event_media(phone, "image", EVENT_PHOTO_COUNT, event_date)


async def send_event_video(phone: str, event_date: str | None = None) -> int:
    """Прислать лиду 1 случайное ВИДЕО с ивента (если видео ещё не слали). Вернуть число."""
    return await _send_event_media(phone, "video", EVENT_VIDEO_COUNT, event_date)


async def _send_event_media(phone: str, media_type: str, count: int,
                            event_date: str | None = None) -> int:
    """Отправить медиа заданного типа с дедупом в рамках ивента (не повторяем тип лиду).

    event_date задаёт ивент (вар. B): на новый ивент шлём заново. Если не передан —
    берём активный event_date из настроек (одинаковый ключ у chat-reply и планировщика).
    Уже слали этот тип на этот ивент → 0. Нет медиа типа → 0. Отдельными сообщениями с
    антибан-паузой (внутри send_media). Сбой не роняет основной ответ (ловит вызывающий).
    """
    if event_date is None:
        s = await db.get_settings(["event_date"])
        event_date = s.get("event_date") or None
    if await db.event_media_sent(phone, media_type, event_date):
        logger.info("медиа ивента (%s) уже слалось %s (ивент %s) — пропуск (дедуп)",
                    media_type, phone, event_date)
        return 0
    items = await db.random_event_media(media_type, count)
    if not items:
        logger.info("медиа ивента (%s) нет в пуле — нечего слать %s", media_type, phone)
        return 0
    sent = 0
    for m in items:
        if await sender.send_media(phone, m["storage_url"], m.get("media_type", media_type),
                                   event_date):
            sent += 1
    logger.info("медиа ивента (%s): отправлено %d/%d %s", media_type, sent, len(items), phone)
    return sent


def stage_for_service(selected_service: str | None) -> str | None:
    """Стадия по selected_service лида (или None, если неоднозначно — спросить Аню)."""
    return SERVICE_TO_STAGE.get((selected_service or "").strip().lower())


async def confirm_payment(phone: str, target_stage: str, source: str = "manual") -> None:
    """Подтвердить оплату: сменить стадию + (для ивента) отправить приглашение.

    ЕДИНАЯ точка: кнопка менеджера и будущий Stripe-вебхук зовут её же. source —
    'manual' | 'stripe' | ... (для лога/аудита).
    """
    # set_funnel_stage возвращает True только если стадия РЕАЛЬНО сменилась. Приглашение
    # шлём лишь на первом подтверждении — защита от двойного клика/повторного вебхука Stripe.
    changed = await db.set_funnel_stage(phone, target_stage, meta={"payment": source})
    if changed and target_stage == "event_attended":
        await maybe_send_invitation(phone)
        await _add_to_guest_list(phone)
    logger.info("оплата подтверждена [%s] → %s (source=%s, changed=%s)",
                phone, target_stage, source, changed)


async def save_anketa_if_complete(phone: str) -> bool:
    """Если анкета лида собрана и ещё не записана — добавить строку в Sheet «Solicitudes».

    Гибрид чат→Sheet: базовые колонки + Extra(JSON) под будущие поля. Дедуп: extra_data.
    anketa_saved (одна строка на лида). Не критично: сбой не роняет ответ (ловит вызывающий).
    Вернуть True если записали.
    """
    if not settings.google_sheet_id:
        return False
    lead = await db.get_lead_by_phone(phone)
    if not lead or not funnel.anketa_complete(lead):
        return False
    if await db.anketa_saved(phone):
        return False
    full_name = " ".join(x for x in [lead.get("name"), lead.get("last_name")] if x)
    digits = "".join(c for c in (phone or "") if c.isdigit())
    dob = lead.get("date_of_birth")
    dob_s = dob.isoformat() if hasattr(dob, "isoformat") else (dob or "")
    marital = "Soltero" if lead.get("is_single") else (lead.get("marital_status") or "")
    # Extra(JSON) — поля из чат-квалификации + запас под будущие поля анкеты
    extra = json.dumps({"marital_status": marital, "profession": lead.get("profession") or ""},
                       ensure_ascii=False)
    registered = datetime.now(_CDMX).strftime("%Y-%m-%d %H:%M")
    try:
        await gcal.append_anketa_row(
            full_name, lead.get("email") or "", ("+" + digits) if digits else "",
            dob_s, lead.get("city") or "", lead.get("country") or "",
            lead.get("business_link") or "", str(lead.get("desired_partner_age") or ""),
            lead.get("interest") or "", extra, registered)
        await db.mark_anketa_saved(phone)
        logger.info("анкета записана в Sheet Solicitudes: %s", phone)
        return True
    except Exception:
        logger.exception("save_anketa_if_complete упал [%s]", phone)
        return False


async def _add_to_guest_list(phone: str) -> None:
    """Добавить оплатившего ивент в гостевой список (Google Sheets 'Invitados').

    Не критично: сбой Sheets (или не настроено) НЕ ломает подтверждение оплаты.
    """
    if not settings.google_sheet_id:
        return
    try:
        lead = await db.get_lead_by_phone(phone)
        if not lead:
            return
        name = lead.get("name") or lead.get("whatsapp_name") or ""
        digits = "".join(c for c in (phone or "") if c.isdigit())
        registered = datetime.now(_CDMX).strftime("%Y-%m-%d %H:%M")
        await gcal.append_guest_row(name, ("+" + digits) if digits else "", "Pagado",
                                    lead.get("interest") or "", registered)
        logger.info("гостевой список: добавлен %s", phone)
    except Exception:
        logger.exception("guest list append failed [%s] (оплата уже подтверждена)", phone)
