"""Тесты авторизации мини-CRM (mini_auth.py) — проверка Telegram initData.

Валидный initData собираем тем же алгоритмом, что и Telegram, чтобы проверить,
что verify_init_data принимает корректную подпись и отвергает всё остальное.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

import mini_auth

BOT_TOKEN = "123456:TEST-BOT-TOKEN"
ADMIN_ID = 555
STRANGER_ID = 777


def _sign(token: str, fields: dict) -> str:
    """Собрать подписанный initData (query-string) из полей — как это делает Telegram."""
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _init_data(token: str = BOT_TOKEN, uid: int = ADMIN_ID, auth_date: int | None = None) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    user = json.dumps({"id": uid, "first_name": "Anna", "username": "anna"})
    return _sign(token, {"query_id": "AAA", "user": user, "auth_date": str(auth_date)})


class TestVerifyInitData:
    def test_valid_signature_returns_user(self):
        user = mini_auth.verify_init_data(_init_data(), BOT_TOKEN)
        assert user is not None and user["id"] == ADMIN_ID

    def test_tampered_hash_rejected(self):
        init = _init_data()[:-4] + "0000"  # портим hash
        assert mini_auth.verify_init_data(init, BOT_TOKEN) is None

    def test_wrong_token_rejected(self):
        # Подпись валидна для BOT_TOKEN, но проверяем другим токеном — не сойдётся.
        assert mini_auth.verify_init_data(_init_data(), "999:OTHER") is None

    def test_expired_auth_date_rejected(self):
        old = int(time.time()) - 100_000
        assert mini_auth.verify_init_data(_init_data(auth_date=old), BOT_TOKEN) is None

    def test_no_hash_rejected(self):
        assert mini_auth.verify_init_data("user=%7B%7D&auth_date=1", BOT_TOKEN) is None

    def test_no_user_rejected(self):
        init = _sign(BOT_TOKEN, {"auth_date": str(int(time.time()))})
        assert mini_auth.verify_init_data(init, BOT_TOKEN) is None

    def test_missing_auth_date_rejected(self):
        # auth_date обязателен — без него fail-closed, даже при валидной подписи.
        user = json.dumps({"id": ADMIN_ID, "first_name": "Anna"})
        init = _sign(BOT_TOKEN, {"query_id": "AAA", "user": user})
        assert mini_auth.verify_init_data(init, BOT_TOKEN) is None

    def test_empty_inputs(self):
        assert mini_auth.verify_init_data("", BOT_TOKEN) is None
        assert mini_auth.verify_init_data(_init_data(), "") is None


class TestRequireAdmin:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr(mini_auth.settings, "tg_manager_bot_token", BOT_TOKEN)
        monkeypatch.setattr(mini_auth.settings, "tg_manager_admin_ids", str(ADMIN_ID))
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)

    async def test_admin_ok(self):
        user = await mini_auth.require_admin(f"tma {_init_data(uid=ADMIN_ID)}")
        assert user["id"] == ADMIN_ID

    async def test_non_admin_403(self):
        with pytest.raises(HTTPException) as e:
            await mini_auth.require_admin(f"tma {_init_data(uid=STRANGER_ID)}")
        assert e.value.status_code == 403

    async def test_missing_header_401(self):
        with pytest.raises(HTTPException) as e:
            await mini_auth.require_admin(None)
        assert e.value.status_code == 401

    async def test_bad_scheme_401(self):
        with pytest.raises(HTTPException) as e:
            await mini_auth.require_admin(f"Bearer {_init_data()}")
        assert e.value.status_code == 401

    async def test_invalid_signature_401(self):
        with pytest.raises(HTTPException) as e:
            await mini_auth.require_admin("tma user=x&hash=bad")
        assert e.value.status_code == 401

    async def test_dev_mode_bypasses(self, monkeypatch):
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", True)
        user = await mini_auth.require_admin(None)  # без initData вообще
        assert user["is_dev"] is True
