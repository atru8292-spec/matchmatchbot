"""Тесты actions.py (блок 13): приглашение + подтверждение оплаты."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import actions
import db
import sender


class TestStageForService:
    def test_event(self):
        assert actions.stage_for_service("event") == "event_attended"

    def test_starter(self):
        assert actions.stage_for_service("starter") == "client_starter"

    def test_vip(self):
        assert actions.stage_for_service("vip") == "client_vip"

    def test_none(self):
        assert actions.stage_for_service(None) is None

    def test_unknown(self):
        assert actions.stage_for_service("random") is None


class TestMaybeSendInvitation:
    async def test_sends_when_ready(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "invitation_url": "https://x/inv.jpg", "invitation_ready": "1"}))
        send = AsyncMock(return_value=True)
        monkeypatch.setattr(sender, "send_image", send)
        ok = await actions.maybe_send_invitation("wa_1")
        assert ok is True
        send.assert_awaited_once_with("wa_1", "https://x/inv.jpg")

    async def test_skips_when_not_ready(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "invitation_url": "https://x/inv.jpg", "invitation_ready": "0"}))
        send = AsyncMock()
        monkeypatch.setattr(sender, "send_image", send)
        ok = await actions.maybe_send_invitation("wa_1")
        assert ok is False
        send.assert_not_awaited()

    async def test_skips_when_no_url(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"invitation_ready": "1"}))
        send = AsyncMock()
        monkeypatch.setattr(sender, "send_image", send)
        ok = await actions.maybe_send_invitation("wa_1")
        assert ok is False
        send.assert_not_awaited()


class TestConfirmPayment:
    async def test_sets_stage(self, monkeypatch):
        stage = AsyncMock()
        monkeypatch.setattr(db, "set_funnel_stage", stage)
        monkeypatch.setattr(actions, "maybe_send_invitation", AsyncMock())
        await actions.confirm_payment("wa_1", "client_starter", source="manual")
        stage.assert_awaited_once()
        assert stage.call_args.args[1] == "client_starter"

    async def test_event_stage_sends_invitation(self, monkeypatch):
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        inv = AsyncMock(); monkeypatch.setattr(actions, "maybe_send_invitation", inv)
        await actions.confirm_payment("wa_1", "event_attended")
        inv.assert_awaited_once_with("wa_1")

    async def test_non_event_no_invitation(self, monkeypatch):
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        inv = AsyncMock(); monkeypatch.setattr(actions, "maybe_send_invitation", inv)
        await actions.confirm_payment("wa_1", "client_starter")
        inv.assert_not_awaited()


class TestConfirmPaymentIdempotent:
    async def test_no_invitation_when_stage_unchanged(self, monkeypatch):
        """set_funnel_stage вернул False (повторный клик) → приглашение НЕ шлём второй раз."""
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock(return_value=False))
        inv = AsyncMock(); monkeypatch.setattr(actions, "maybe_send_invitation", inv)
        await actions.confirm_payment("wa_1", "event_attended")
        inv.assert_not_awaited()

    async def test_invitation_when_stage_changed(self, monkeypatch):
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock(return_value=True))
        inv = AsyncMock(); monkeypatch.setattr(actions, "maybe_send_invitation", inv)
        await actions.confirm_payment("wa_1", "event_attended")
        inv.assert_awaited_once()
