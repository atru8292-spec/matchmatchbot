"""Авторизация мини-CRM (Telegram Mini App) — проверка initData.

Мини-апп открывается из менеджер-бота «Лиды». Telegram при открытии Web App
передаёт подписанный initData (query-string). Здесь мы:
1. Проверяем HMAC-подпись initData токеном менеджер-бота (данные реально от Telegram).
2. Проверяем свежесть auth_date (не старый replay).
3. Достаём user.id и сверяем с whitelisted admin_ids (та же логика, что в manager_bot).

Dev-режим (settings.mini_dev_mode=True) полностью обходит проверку — только для
локальной разработки. На проде обязан быть выключен (иначе API открыт всем).

Алгоритм подписи — стандартный для Telegram Web Apps:
    secret_key   = HMAC_SHA256(key="WebAppData", msg=bot_token)
    expected_hash = HMAC_SHA256(key=secret_key, msg=data_check_string)
где data_check_string — пары "key=value" (кроме hash), отсортированные по ключу и
склеенные через '\\n'.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from config import settings

logger = logging.getLogger("mini_auth")

# Синтетический пользователь для dev-режима (реального Telegram нет).
DEV_USER: dict = {"id": 0, "first_name": "Dev", "username": "dev", "is_dev": True}


def _data_check_string(pairs: list[tuple[str, str]]) -> str:
    """Собрать data-check-string: пары key=value (кроме hash), отсортированные по ключу."""
    filtered = [(k, v) for k, v in pairs if k != "hash"]
    filtered.sort(key=lambda kv: kv[0])
    return "\n".join(f"{k}={v}" for k, v in filtered)


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int | None = None,
) -> dict | None:
    """Проверить подпись initData и вернуть user-словарь, либо None если невалидно.

    None возвращается на любой проблеме: нет токена/hash, подпись не сошлась,
    auth_date просрочен, user не распарсился. Никогда не бросает — вызывающий сам
    решает, что делать с None (обычно 401).
    """
    if not init_data or not bot_token:
        return None

    # keep_blank_values — чтобы пустые поля не потерялись и не сломали подпись.
    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    received_hash = data.get("hash")
    if not received_hash:
        return None

    data_check_string = _data_check_string(pairs)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    # constant-time сравнение — не даём таймингом подобрать hash.
    if not hmac.compare_digest(expected_hash, received_hash):
        logger.warning("initData: подпись не сошлась")
        return None

    # Свежесть auth_date — защита от повторного использования старого initData.
    # auth_date обязателен по спецификации Telegram: нет/не число → fail-closed.
    if max_age_seconds is None:
        max_age_seconds = settings.mini_init_data_max_age
    auth_date_raw = data.get("auth_date")
    if not auth_date_raw or not auth_date_raw.isdigit():
        logger.warning("initData: нет валидного auth_date")
        return None
    age = time.time() - int(auth_date_raw)
    if age > max_age_seconds:
        logger.warning("initData: просрочен (age=%.0fs > %ds)", age, max_age_seconds)
        return None

    # user — JSON-строка внутри initData.
    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError):
        logger.warning("initData: user не распарсился")
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user


async def require_admin(authorization: Optional[str] = Header(default=None)) -> dict:
    """FastAPI-зависимость: пускает только авторизованных админов мини-CRM.

    Ожидает заголовок `Authorization: tma <initData>` (конвенция @telegram-apps/sdk).
    Dev-режим отдаёт DEV_USER без проверки. Иначе: валидируем подпись и сверяем
    user.id с manager_admin_ids. Бросает 401 (нет/битый initData) или 403 (не админ).
    """
    if settings.mini_dev_mode:
        return DEV_USER

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Missing initData")

    init_data = authorization[len("tma "):]
    user = verify_init_data(init_data, settings.tg_manager_bot_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid initData")

    try:
        user_id = int(user.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid initData")

    if user_id not in settings.manager_admin_ids:
        raise HTTPException(status_code=403, detail="Not authorized")

    return user
