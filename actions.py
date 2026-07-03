"""Общие бизнес-действия блока 13, переиспользуемые main и manager_bot.

Вынесены сюда, чтобы не плодить циклы импортов (main ↔ manager_bot). Зависят только
от db и sender. Подтверждение оплаты — единая точка: и кнопка менеджера, и будущий
Stripe-вебхук вызывают confirm_payment (source различает источник, логика не дублируется).
"""
from __future__ import annotations

import logging

import db
import sender

logger = logging.getLogger("matchmatch.actions")

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
    logger.info("оплата подтверждена [%s] → %s (source=%s, changed=%s)",
                phone, target_stage, source, changed)
