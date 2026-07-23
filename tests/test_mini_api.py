"""Тесты API мини-CRM (mini_api.py) — эндпоинт /api/mini/leads.

БД мокаем (db.is_ready / db.list_leads_page), авторизацию обходим через
dependency_overrides (require_admin проверяется отдельно в test_mini_auth).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import base64

import ai
import db
import mini_api
import mini_auth
import media
import scheduler
import sender
import vision
from main import app
from mini_auth import require_admin


def _dt(minute: int) -> datetime:
    return datetime(2026, 7, 1, 10, minute, tzinfo=timezone.utc)


async def _async(value):
    return value

FAKE_USER = {"id": 555, "first_name": "Anna", "username": "anna"}


def _row(phone="wa_521", name="Maria", stage="qualifying", mode="auto", client=False):
    return {
        "phone": phone, "whatsapp_name": "Maria WA", "name": name,
        "funnel_stage": stage, "mode": mode, "interest": "agency",
        "age": 34, "profession": "Doctor", "city": "CDMX",
        "last_message_at": datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        "last_inbound_at": datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        "is_client": client,
        "last_message_text": "Hola!", "last_message_sender": "lead",
        "last_message_direction": "inbound",
        "last_message_created_at": datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
    }


@pytest.fixture
def client(monkeypatch):
    """TestClient с замоканной авторизацией и готовой БД."""
    app.dependency_overrides[require_admin] = lambda: FAKE_USER
    monkeypatch.setattr(db, "is_ready", lambda: True)
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestLeadsEndpoint:
    def test_returns_serialized_leads(self, client, monkeypatch):
        async def fake_page(**kw):
            return {"leads": [_row(), _row(phone="wa_522", client=True)], "total": 2}
        monkeypatch.setattr(db, "list_leads_page", fake_page)

        r = client.get("/api/mini/leads")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2 and len(data["leads"]) == 2
        assert data["hasMore"] is False
        lead = data["leads"][0]
        # camelCase + человекочитаемый лейбл стадии + ISO-дата
        assert lead["funnelStage"] == "qualifying"
        assert lead["funnelStageLabel"] == "Первичное общение"
        assert lead["lastMessagePreview"] == "Hola!"
        assert lead["lastMessageAt"].startswith("2026-07-01")
        assert data["leads"][1]["isClient"] is True

    def test_has_more_pagination(self, client, monkeypatch):
        async def fake_page(**kw):
            return {"leads": [_row()], "total": 50}
        monkeypatch.setattr(db, "list_leads_page", fake_page)

        r = client.get("/api/mini/leads?limit=1&offset=0")
        assert r.json()["hasMore"] is True

    def test_filters_passed_through(self, client, monkeypatch):
        captured = {}

        async def fake_page(**kw):
            captured.update(kw)
            return {"leads": [], "total": 0}
        monkeypatch.setattr(db, "list_leads_page", fake_page)

        r = client.get("/api/mini/leads?stage=qualifying&stage=pitched&mode=manual&search=Ana&sort=stage")
        assert r.status_code == 200
        assert captured["stages"] == ["qualifying", "pitched"]
        assert captured["mode"] == "manual"
        assert captured["search"] == "Ana"
        assert captured["sort"] == "stage"

    def test_bad_mode_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_leads_page", lambda **k: None)
        assert client.get("/api/mini/leads?mode=bogus").status_code == 422

    def test_bad_sort_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_leads_page", lambda **k: None)
        assert client.get("/api/mini/leads?sort=bogus").status_code == 422

    def test_unknown_stage_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_leads_page", lambda **k: None)
        assert client.get("/api/mini/leads?stage=nonsense").status_code == 422

    def test_bad_since_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_leads_page", lambda **k: None)
        assert client.get("/api/mini/leads?since=завтра").status_code == 422

    def test_valid_since_passes(self, client, monkeypatch):
        captured = {}

        async def fake_page(**kw):
            captured.update(kw)
            return {"leads": [], "total": 0}
        monkeypatch.setattr(db, "list_leads_page", fake_page)

        r = client.get("/api/mini/leads?since=2026-07-01T00:00:00Z")
        assert r.status_code == 200 and captured["since"] == "2026-07-01T00:00:00Z"

    def test_db_not_ready_503(self, client, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: False)
        assert client.get("/api/mini/leads").status_code == 503


class TestAuxEndpoints:
    def test_me(self, client):
        r = client.get("/api/mini/me")
        assert r.status_code == 200 and r.json()["id"] == 555

    def test_meta_has_stages(self, client):
        r = client.get("/api/mini/meta")
        assert r.status_code == 200
        codes = [s["code"] for s in r.json()["stages"]]
        assert "qualifying" in codes and "client_agency" in codes

    def test_meta_reports_paused(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "1"}))
        assert client.get("/api/mini/meta").json()["botPaused"] is True

    def test_meta_reports_not_paused(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "0"}))
        assert client.get("/api/mini/meta").json()["botPaused"] is False

    def test_bot_pause_on(self, client, monkeypatch):
        setter = AsyncMock(); monkeypatch.setattr(db, "set_setting", setter)
        r = client.post("/api/mini/bot/pause", json={"paused": True})
        assert r.status_code == 200 and r.json()["botPaused"] is True
        setter.assert_awaited_once_with("bot_paused", "1")

    def test_bot_pause_off(self, client, monkeypatch):
        setter = AsyncMock(); monkeypatch.setattr(db, "set_setting", setter)
        r = client.post("/api/mini/bot/pause", json={"paused": False})
        assert r.status_code == 200 and r.json()["botPaused"] is False
        setter.assert_awaited_once_with("bot_paused", "0")


class TestAuthEnforced:
    """Без override — реальная require_admin должна отклонять анонима."""
    def test_leads_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        c = TestClient(app)
        assert c.get("/api/mini/leads").status_code == 401

    def test_card_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        c = TestClient(app)
        assert c.get("/api/mini/lead/wa_521").status_code == 401
        assert c.post("/api/mini/lead/wa_521/takeover").status_code == 401


# ===== Карточка лида =====

def _lead_row(mode="auto", dnc=False):
    return {
        "phone": "wa_5215512345678", "whatsapp_name": "Mafer", "name": "María",
        "funnel_stage": "pitched", "mode": mode, "interest": "agency",
        "age": 41, "profession": "Dentista", "city": "CDMX",
        "last_message_at": _dt(5), "last_inbound_at": _dt(4),
        "do_not_contact": dnc, "created_at": _dt(0),
    }


class TestSerializeTimeline:
    """Чистая функция слияния таймлайна — без HTTP."""

    def test_merges_and_orders(self):
        messages = [{"id": "u1", "sender": "lead", "direction": "inbound", "text": "hola", "created_at": _dt(0)}]
        events = [{"id": 5, "from_stage": "new", "to_stage": "qualifying", "changed_at": _dt(1)}]
        actions = [
            {"id": 7, "action": "takeover", "created_at": _dt(2)},
            {"id": 8, "action": "approve_photo", "created_at": _dt(3)},  # незнакомое → skip
            {"id": 9, "action": "whitelist_add", "created_at": _dt(4)},
        ]
        notes = [{"id": 3, "text": "проверила профессию", "created_at": _dt(5)}]

        tl = mini_api._serialize_timeline(messages, events, actions, notes)
        kinds = [i["kind"] for i in tl]
        assert kinds == ["message", "stage", "action", "action", "note"]
        # approve_photo выкинут, whitelist_add смаплен в client_add
        assert [i["action"] for i in tl if i["kind"] == "action"] == ["takeover", "client_add"]
        assert tl[0]["text"] == "hola" and tl[1]["toStage"] == "qualifying"
        assert tl[-1]["text"] == "проверила профессию"

    def test_message_status_from_meta(self):
        rows = [
            {"id": "a", "sender": "anna", "direction": "outbound", "text": "x",
             "created_at": _dt(0), "meta": {"manual": True, "status": "failed"}},
            {"id": "b", "sender": "anna", "direction": "outbound", "text": "y", "created_at": _dt(1)},
        ]
        tl = mini_api._serialize_timeline(rows, [], [], [])
        assert tl[0]["status"] == "failed" and tl[0]["sender"] == "manager"
        assert tl[1]["status"] is None  # авто-ответ без статуса

    def test_sender_mapping(self):
        # БД не хранит 'manager' (CHECK lead/mila/anna): ручной ответ = anna + meta.manual.
        # Сериализатор разворачивает: lead→lead, авто anna→anna, manual→manager.
        rows = [
            {"id": "a", "sender": "lead", "direction": "inbound", "text": "x", "created_at": _dt(0)},
            {"id": "b", "sender": "anna", "direction": "outbound", "text": "y", "created_at": _dt(1)},
            {"id": "c", "sender": "anna", "direction": "outbound", "text": "z",
             "created_at": _dt(2), "meta": {"manual": True}},
        ]
        senders = [i["sender"] for i in mini_api._serialize_timeline(rows, [], [], [])]
        assert senders == ["lead", "anna", "manager"]


class TestLeadDetailEndpoint:
    def test_returns_detail(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(_lead_row()))
        monkeypatch.setattr(db, "get_whitelist_entry", lambda p: _async(None))
        monkeypatch.setattr(db, "get_lead_photos", lambda p, **k: _async([]))

        r = client.get("/api/mini/lead/wa_5215512345678")
        assert r.status_code == 200
        d = r.json()
        assert d["funnelStageLabel"] == "Показала цену"
        assert d["isClient"] is False and d["doNotContact"] is False
        assert d["firstMessageAt"].startswith("2026-07-01")

    def test_client_reason_when_whitelisted(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(_lead_row()))
        monkeypatch.setattr(db, "get_whitelist_entry",
                            lambda p: _async({"reason": "VIP", "added_by": "@arinashrr"}))
        monkeypatch.setattr(db, "get_lead_photos", lambda p, **k: _async([]))

        d = client.get("/api/mini/lead/wa_5215512345678").json()
        assert d["isClient"] is True and d["clientReason"] == "VIP"
        assert d["clientAddedBy"] == "@arinashrr"

    def test_404_when_missing(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(None))
        assert client.get("/api/mini/lead/wa_999").status_code == 404


class TestHistoryEndpoint:
    def test_merged_timeline(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(_lead_row()))
        monkeypatch.setattr(db, "get_conversation_history",
                            lambda p, **k: _async([{"id": "u1", "sender": "anna", "direction": "outbound", "text": "hola", "created_at": _dt(0)}]))
        monkeypatch.setattr(db, "get_funnel_events",
                            lambda p: _async([{"id": 1, "from_stage": "new", "to_stage": "qualifying", "changed_at": _dt(1)}]))
        monkeypatch.setattr(db, "get_manager_actions", lambda p: _async([]))
        monkeypatch.setattr(db, "get_lead_notes", lambda p: _async([]))

        tl = client.get("/api/mini/lead/wa_5215512345678/history").json()["timeline"]
        assert [i["kind"] for i in tl] == ["message", "stage"]

    def test_404_when_missing(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(None))
        assert client.get("/api/mini/lead/wa_999/history").status_code == 404


class TestNotes:
    def test_add_note_calls_db(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(_lead_row()))
        add = AsyncMock(return_value={"id": 1, "text": "ok", "created_at": _dt(0)})
        monkeypatch.setattr(db, "add_lead_note", add)

        r = client.post("/api/mini/lead/wa_5215512345678/notes", json={"text": "  ok  "})
        assert r.status_code == 200 and r.json()["kind"] == "note"
        add.assert_awaited_once_with("wa_5215512345678", "ok")  # обрезан пробел

    def test_empty_note_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "add_lead_note", AsyncMock())
        assert client.post("/api/mini/lead/wa_521/notes", json={"text": "   "}).status_code == 422

    def test_note_404_when_lead_missing(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(None))
        monkeypatch.setattr(db, "add_lead_note", AsyncMock())
        assert client.post("/api/mini/lead/wa_999/notes", json={"text": "x"}).status_code == 404


class TestActions:
    def test_takeover(self, client, monkeypatch):
        setm = AsyncMock(return_value=True)
        log = AsyncMock()
        monkeypatch.setattr(db, "set_manual", setm)
        monkeypatch.setattr(db, "log_manager_action", log)

        r = client.post("/api/mini/lead/wa_5215512345678/takeover")
        assert r.status_code == 200 and r.json()["mode"] == "manual"
        setm.assert_awaited_once_with("wa_5215512345678")
        log.assert_awaited_once_with("wa_5215512345678", "takeover", "@anna")

    def test_release(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_auto", AsyncMock(return_value=True))
        log = AsyncMock()
        monkeypatch.setattr(db, "log_manager_action", log)
        r = client.post("/api/mini/lead/wa_521/release")
        assert r.status_code == 200 and r.json()["mode"] == "auto"
        log.assert_awaited_once_with("wa_521", "release", "@anna")

    def test_resume(self, client, monkeypatch):
        monkeypatch.setattr(db, "resume_lead", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        r = client.post("/api/mini/lead/wa_521/resume")
        assert r.status_code == 200 and r.json()["doNotContact"] is False

    def test_takeover_404(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_manual", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        assert client.post("/api/mini/lead/wa_999/takeover").status_code == 404

    def test_stop(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(_lead_row()))
        block = AsyncMock()
        log = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block)
        monkeypatch.setattr(db, "log_manager_action", log)

        r = client.post("/api/mini/lead/wa_5215512345678/stop")
        assert r.status_code == 200 and r.json()["doNotContact"] is True
        block.assert_awaited_once()
        log.assert_awaited_once_with("wa_5215512345678", "stop", "@anna")

    def test_stop_404(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(None))
        monkeypatch.setattr(db, "block_lead", AsyncMock())
        assert client.post("/api/mini/lead/wa_999/stop").status_code == 404

    def test_whitelist_add(self, client, monkeypatch):
        add = AsyncMock()
        log = AsyncMock()
        monkeypatch.setattr(db, "add_to_whitelist", add)
        monkeypatch.setattr(db, "log_manager_action", log)

        r = client.post("/api/mini/lead/wa_521/whitelist", json={"reason": "VIP"})
        assert r.status_code == 200 and r.json()["isClient"] is True
        add.assert_awaited_once_with("wa_521", "VIP", "@anna")

    def test_whitelist_remove(self, client, monkeypatch):
        rm = AsyncMock()
        monkeypatch.setattr(db, "remove_from_whitelist", rm)
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        r = client.delete("/api/mini/lead/wa_521/whitelist")
        assert r.status_code == 200 and r.json()["isClient"] is False
        rm.assert_awaited_once_with("wa_521")

    def test_bad_phone_422(self, client, monkeypatch):
        # телефон без цифр не нормализуется
        assert client.post("/api/mini/lead/abc/takeover").status_code == 422


class TestSendMessage:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        # не ждём реальную антибан-задержку в тестах
        monkeypatch.setattr(sender, "compute_delay", lambda t: 0)

    def test_send_from_auto_takes_over(self, client, monkeypatch):
        setm = AsyncMock(return_value=True)
        log = AsyncMock()
        send_one = AsyncMock(return_value=True)
        save = AsyncMock(return_value={"id": 7, "text": "Hola", "created_at": _dt(0)})
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async({"mode": "auto"}))
        monkeypatch.setattr(db, "set_manual", setm)
        monkeypatch.setattr(db, "log_manager_action", log)
        monkeypatch.setattr(sender, "send_one", send_one)
        monkeypatch.setattr(db, "save_manual_message", save)

        r = client.post("/api/mini/lead/wa_5215500000001/message", json={"text": "  Hola  "})
        assert r.status_code == 200
        d = r.json()
        assert d["message"]["sender"] == "manager" and d["message"]["status"] == "sent"
        assert d["delivered"] is True and d["tookOver"] is True
        setm.assert_awaited_once()               # авто → takeover
        log.assert_awaited_once_with("wa_5215500000001", "takeover", "@anna")
        send_one.assert_awaited_once_with("5215500000001", "Hola")  # без wa_, обрезан пробел
        save.assert_awaited_once_with("wa_5215500000001", "Hola", True)

    def test_send_from_manual_no_takeover(self, client, monkeypatch):
        setm = AsyncMock()
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async({"mode": "manual"}))
        monkeypatch.setattr(db, "set_manual", setm)
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "save_manual_message",
                            AsyncMock(return_value={"id": 8, "text": "hi", "created_at": _dt(0)}))

        r = client.post("/api/mini/lead/wa_521/message", json={"text": "hi"})
        assert r.status_code == 200 and r.json()["tookOver"] is False
        setm.assert_not_awaited()  # уже manual → без takeover

    def test_send_failure_marks_failed(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async({"mode": "manual"}))
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=False))  # Wazzup упал
        save = AsyncMock(return_value={"id": 9, "text": "hi", "created_at": _dt(0)})
        monkeypatch.setattr(db, "save_manual_message", save)

        r = client.post("/api/mini/lead/wa_521/message", json={"text": "hi"})
        assert r.status_code == 200
        assert r.json()["message"]["status"] == "failed" and r.json()["delivered"] is False
        save.assert_awaited_once_with("wa_521", "hi", False)  # сохранено как failed

    def test_empty_text_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async({"mode": "manual"}))
        assert client.post("/api/mini/lead/wa_521/message", json={"text": "   "}).status_code == 422

    def test_404_when_lead_missing(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(None))
        monkeypatch.setattr(sender, "send_one", AsyncMock())
        assert client.post("/api/mini/lead/wa_999/message", json={"text": "hi"}).status_code == 404

    def test_optout_lead_requires_override_409(self, client, monkeypatch):
        """Лид с do_not_contact → ручное сообщение без override → 409 (предупреждение Ане)."""
        monkeypatch.setattr(db, "get_lead_by_phone",
                            lambda p: _async({"mode": "manual", "do_not_contact": True}))
        send = AsyncMock(); monkeypatch.setattr(sender, "send_one", send)
        r = client.post("/api/mini/lead/wa_521/message", json={"text": "hola"})
        assert r.status_code == 409
        send.assert_not_awaited()  # не отправили

    def test_optout_lead_sends_with_override(self, client, monkeypatch):
        """С override=true → Аня подтвердила, отправка проходит."""
        monkeypatch.setattr(db, "get_lead_by_phone",
                            lambda p: _async({"mode": "manual", "do_not_contact": True}))
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        monkeypatch.setattr(db, "save_manual_message",
                            AsyncMock(return_value={"id": 10, "text": "hola", "created_at": _dt(0)}))
        r = client.post("/api/mini/lead/wa_521/message", json={"text": "hola", "override": True})
        assert r.status_code == 200


class TestStats:
    def test_stats_shape(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_funnel_stats", lambda: _async([
            {"funnel_stage": "pitched", "total": 3, "last_24h": 1, "last_7d": 2},
            {"funnel_stage": "new", "total": 1, "last_24h": 1, "last_7d": 1},
        ]))
        monkeypatch.setattr(db, "get_lead_counts", lambda: _async({"total": 4, "today": 2, "week": 3}))
        monkeypatch.setattr(db, "get_pending_escalations", lambda **k: _async([
            {"phone": "wa_521", "whatsapp_name": "Carlos", "escalate_reason": "VIP",
             "minutes_left": 12, "last_inbound_at": _dt(0)},
        ]))
        monkeypatch.setattr(db, "count_pending_escalations", lambda: _async(1))

        d = client.get("/api/mini/stats").json()
        assert d["totalLeads"] == 4 and d["newToday"] == 2 and d["newWeek"] == 3
        # воронка в каноничном порядке (new раньше pitched), % от общего
        assert [f["stage"] for f in d["funnel"]] == ["new", "pitched"]
        pitched = d["funnel"][1]
        assert pitched["total"] == 3 and pitched["percent"] == 75  # 3/4
        esc = d["pendingEscalations"]
        assert esc["count"] == 1 and esc["items"][0]["name"] == "Carlos"
        assert esc["items"][0]["minutesLeft"] == 12

    def test_stats_empty(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_funnel_stats", lambda: _async([]))
        monkeypatch.setattr(db, "get_lead_counts", lambda: _async({"total": 0, "today": 0, "week": 0}))
        monkeypatch.setattr(db, "get_pending_escalations", lambda **k: _async([]))
        monkeypatch.setattr(db, "count_pending_escalations", lambda: _async(0))

        d = client.get("/api/mini/stats").json()
        assert d["totalLeads"] == 0 and d["funnel"] == []
        assert d["pendingEscalations"]["count"] == 0

    def test_stats_db_not_ready_503(self, client, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: False)
        assert client.get("/api/mini/stats").status_code == 503

    def test_stats_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        assert TestClient(app).get("/api/mini/stats").status_code == 401


class TestEvent:
    def test_get_event(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_settings", lambda keys: _async({
            "event_active": "1", "event_date": "2026-08-15", "event_time": "20:30",
            "event_address": "Av. Reforma 123", "event_link": "https://pay.example",
            "course_link": "", "invitation_url": "https://img.example/inv.jpg",
            "invitation_ready": "1",
        }))
        d = client.get("/api/mini/event").json()
        assert d["eventActive"] is True and d["eventDate"] == "2026-08-15"
        assert d["eventAddress"] == "Av. Reforma 123"
        assert d["invitationReady"] is True and d["courseLink"] == ""

    def test_put_event_saves_all_keys(self, client, monkeypatch):
        saved = {}

        async def fake_set(k, v):
            saved[k] = v
        monkeypatch.setattr(db, "set_setting", fake_set)

        body = {
            "eventActive": True, "eventDate": "2026-08-15", "eventStart": "8:30 PM",
            "eventEnd": "12:00 AM", "eventAddress": "Av. Reforma 123",
            "eventLink": "https://pay.example",
            "courseLink": "https://cursos.example", "invitationUrl": "https://img/inv.jpg",
            "invitationReady": True,
        }
        r = client.put("/api/mini/event", json=body)
        assert r.status_code == 200
        # пишет в ТЕ ЖЕ ключи, что бот
        assert saved["event_active"] == "1" and saved["event_date"] == "2026-08-15"
        assert saved["event_start"] == "8:30 PM" and saved["event_end"] == "12:00 AM"
        assert saved["event_time"] == "8:30 PM"  # зеркалит event_start
        assert saved["event_address"] == "Av. Reforma 123"
        assert saved["event_link"] == "https://pay.example"
        assert saved["invitation_ready"] == "1"
        # event_men/event_women больше не существуют — не пишем их
        assert "event_men" not in saved and "event_women" not in saved
        assert r.json()["eventActive"] is True and "eventWomen" not in r.json()

    def test_put_event_saves_prices(self, client, monkeypatch):
        saved = {}
        async def fake_set(k, v):
            saved[k] = v
        monkeypatch.setattr(db, "set_setting", fake_set)
        r = client.put("/api/mini/event", json={
            "eventActive": False,
            "eventPriceMember": "4,000", "eventPriceNonmember": "6,000", "eventPriceOld": "9,000",
        })
        assert r.status_code == 200
        assert saved["event_price_member"] == "4,000"
        assert saved["event_price_nonmember"] == "6,000"
        assert saved["event_price_old"] == "9,000"
        assert r.json()["eventPriceNonmember"] == "6,000"

    def test_put_event_bad_price_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_setting", AsyncMock())
        r = client.put("/api/mini/event", json={"eventActive": False, "eventPriceMember": "caro$$"})
        assert r.status_code == 422

    def test_put_toggles_off_to_zero(self, client, monkeypatch):
        saved = {}
        monkeypatch.setattr(db, "set_setting", lambda k, v: _async(saved.__setitem__(k, v)))
        client.put("/api/mini/event", json={"eventActive": False, "invitationReady": False})
        assert saved["event_active"] == "0" and saved["invitation_ready"] == "0"

    def test_bad_date_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_setting", AsyncMock())
        r = client.put("/api/mini/event", json={"eventActive": False, "eventDate": "15.08.2026"})
        assert r.status_code == 422

    def test_bad_link_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_setting", AsyncMock())
        r = client.put("/api/mini/event", json={"eventActive": False, "eventLink": "pay.example"})
        assert r.status_code == 422

    def test_active_requires_datetime_address_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_setting", AsyncMock())
        # active, но без времени/адреса
        r = client.put("/api/mini/event", json={"eventActive": True, "eventDate": "2026-08-15"})
        assert r.status_code == 422

    def test_ready_requires_url_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "set_setting", AsyncMock())
        r = client.put("/api/mini/event", json={"eventActive": False, "invitationReady": True})
        assert r.status_code == 422

    def test_event_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        assert TestClient(app).get("/api/mini/event").status_code == 401


class TestInvitationUpload:
    def _b64(self, data=b"fake-image-bytes"):
        return base64.b64encode(data).decode()

    def test_upload_ok(self, client, monkeypatch):
        up = AsyncMock(return_value="https://store/inv/abc.png")
        monkeypatch.setattr(vision, "upload_invitation", up)
        r = client.post("/api/mini/event/invitation",
                        json={"contentBase64": self._b64(), "contentType": "image/png"})
        assert r.status_code == 200 and r.json()["url"].endswith("abc.png")
        up.assert_awaited_once()

    def test_non_image_422(self, client, monkeypatch):
        monkeypatch.setattr(vision, "upload_invitation", AsyncMock())
        r = client.post("/api/mini/event/invitation",
                        json={"contentBase64": self._b64(), "contentType": "application/pdf"})
        assert r.status_code == 422

    def test_bad_base64_422(self, client, monkeypatch):
        monkeypatch.setattr(vision, "upload_invitation", AsyncMock())
        r = client.post("/api/mini/event/invitation",
                        json={"contentBase64": "!!!не-base64!!!", "contentType": "image/png"})
        assert r.status_code == 422

    def test_too_big_413(self, client, monkeypatch):
        monkeypatch.setattr(vision, "upload_invitation", AsyncMock())
        big = self._b64(b"x" * (5 * 1024 * 1024 + 10))
        r = client.post("/api/mini/event/invitation",
                        json={"contentBase64": big, "contentType": "image/png"})
        assert r.status_code == 413

    def test_storage_fail_502(self, client, monkeypatch):
        monkeypatch.setattr(vision, "upload_invitation", AsyncMock(return_value=None))
        r = client.post("/api/mini/event/invitation",
                        json={"contentBase64": self._b64(), "contentType": "image/png"})
        assert r.status_code == 502


class TestExport:
    def _row(self):
        return {
            "phone": "wa_5215512345678", "name": "Carlos Mendoza", "whatsapp_name": "Carlos",
            "funnel_stage": "pitched", "mode": "auto", "interest": "agency", "age": 42,
            "profession": "Abogado", "city": "CDMX", "is_client": True,
            "last_message_at": _dt(5),
            "last_message_text": "Sí, mañana con gusto señor 🤍",
        }

    def test_csv_export(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_leads_for_export", lambda **k: _async([self._row()]))
        r = client.get("/api/mini/leads/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        assert 'filename="matchmatch-leads.csv"' in r.headers["content-disposition"]
        # BOM присутствует (Excel-кириллица)
        assert r.content[:3] == b"\xef\xbb\xbf"
        text = r.content.decode("utf-8-sig")
        assert text.startswith("Имя;Телефон;Стадия")  # ';' разделитель + кириллица
        assert "Carlos Mendoza" in text
        assert "Показала цену" in text  # стадия pitched → лейбл
        assert "Агентство" in text      # interest → RU
        assert "Да" in text             # is_client
        assert "+5215512345678" in text
        assert "Sí, mañana con gusto señor" in text  # испанские ñ/í сохранены
        assert "2026-07-01 10:05" in text            # дата отформатирована

    def test_filters_passed(self, client, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return []
        monkeypatch.setattr(db, "list_leads_for_export", fake)
        client.get("/api/mini/leads/export?stage=pitched&search=Carlos&sort=stage")
        assert captured["stages"] == ["pitched"]
        assert captured["search"] == "Carlos" and captured["sort"] == "stage"

    def test_bad_mode_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_leads_for_export", AsyncMock())
        assert client.get("/api/mini/leads/export?mode=x").status_code == 422

    def test_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        assert TestClient(app).get("/api/mini/leads/export").status_code == 401


class TestClientsScreen:
    def test_list_clients(self, client, monkeypatch):
        async def fake_list(**k):
            return [
                {"phone": "wa_521", "reason": "VIP", "added_by": "@arinashrr",
                 "added_at": _dt(0), "name": "Carlos", "whatsapp_name": "Carlos"},
                {"phone": "wa_522", "reason": None, "added_by": "@dev",
                 "added_at": _dt(1), "name": None, "whatsapp_name": "Rober"},
            ]
        monkeypatch.setattr(db, "list_whitelist_with_names", fake_list)

        r = client.get("/api/mini/whitelist")
        assert r.status_code == 200
        cl = r.json()["clients"]
        assert cl[0]["name"] == "Carlos" and cl[0]["reason"] == "VIP"
        assert cl[1]["name"] == "Rober" and cl[1]["addedBy"] == "@dev"

    def test_add_client(self, client, monkeypatch):
        add = AsyncMock()
        log = AsyncMock()
        monkeypatch.setattr(db, "add_to_whitelist", add)
        monkeypatch.setattr(db, "log_manager_action", log)
        monkeypatch.setattr(db, "get_whitelist_entry",
                            lambda p: _async({"phone": "wa_521234567890", "reason": "VIP",
                                              "added_by": "@anna", "added_at": _dt(0)}))
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async({"name": "Miguel"}))

        r = client.post("/api/mini/whitelist", json={"phone": "+52 123 456 7890", "reason": "VIP"})
        assert r.status_code == 200
        d = r.json()
        assert d["phone"] == "wa_521234567890" and d["name"] == "Miguel" and d["reason"] == "VIP"
        add.assert_awaited_once_with("wa_521234567890", "VIP", "@anna")
        log.assert_awaited_once()

    def test_add_client_bad_phone_422(self, client, monkeypatch):
        monkeypatch.setattr(db, "add_to_whitelist", AsyncMock())
        assert client.post("/api/mini/whitelist", json={"phone": "нетцифр"}).status_code == 422

    def test_add_client_default_reason(self, client, monkeypatch):
        add = AsyncMock()
        monkeypatch.setattr(db, "add_to_whitelist", add)
        monkeypatch.setattr(db, "log_manager_action", AsyncMock())
        monkeypatch.setattr(db, "get_whitelist_entry", lambda p: _async(None))
        monkeypatch.setattr(db, "get_lead_by_phone", lambda p: _async(None))

        client.post("/api/mini/whitelist", json={"phone": "wa_521"})
        # пустая причина → дефолт «из мини-CRM»
        assert add.await_args.args[1] == "из мини-CRM"

    def test_remove_client(self, client, monkeypatch):
        rm = AsyncMock()
        log = AsyncMock()
        monkeypatch.setattr(db, "remove_from_whitelist", rm)
        monkeypatch.setattr(db, "log_manager_action", log)

        r = client.delete("/api/mini/whitelist/wa_521234567890")
        assert r.status_code == 200 and r.json()["phone"] == "wa_521234567890"
        rm.assert_awaited_once_with("wa_521234567890")
        log.assert_awaited_once_with("wa_521234567890", "whitelist_remove", "@anna")

    def test_clients_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        c = TestClient(app)
        assert c.get("/api/mini/whitelist").status_code == 401


class TestDayOf:
    """Напоминание дня ивента: предпросмотр + ручная отправка (mini_api day-of)."""

    _SETTINGS = {
        "event_date": "2026-07-22", "event_time": "8:30 PM", "event_start": "8:30 PM",
        "event_end": "12:00 AM", "event_address": "Roma Norte",
        "event_link": "https://tix.example/22",
        "course_link": "https://cursos.example",
    }
    _TA = "Hola! Te espero a las [event_time] en [event_address]"           # без ссылки (A/#47)
    _TB = "Hola! Todavía a tiempo, reserva aquí: [event_link]"              # со ссылкой (B/#54)

    @pytest.fixture(autouse=True)
    def _mock_common(self, monkeypatch):
        async def fake_settings(keys):
            return dict(self._SETTINGS)
        async def fake_tmpl(sid):
            return self._TA if sid == scheduler.REMIND_DAY_SCENARIO else self._TB
        monkeypatch.setattr(db, "get_settings", fake_settings)
        monkeypatch.setattr(db, "get_scenario_template", fake_tmpl)
        monkeypatch.setattr(sender, "compute_delay", lambda t: 0)  # без реальных пауз в тестах

    def test_preview_renders_both_templates(self, client):
        d = client.get("/api/mini/event/day-of/preview").json()
        a = " ".join(d["templateA"]); b = " ".join(d["templateB"])
        assert "8:30 PM" in a and "Roma Norte" in a
        assert "https://tix.example/22" not in a          # A — без ссылки
        assert "https://tix.example/22" in b              # B — со ссылкой

    def test_recipients_default_template_by_stage(self, client, monkeypatch):
        async def fake_cands(limit=200):
            return [
                {"phone": "wa_5211111111111", "whatsapp_name": None, "name": "Paid", "funnel_stage": "event_attended"},
                {"phone": "wa_5212222222222", "whatsapp_name": None, "name": "Unpaid", "funnel_stage": "qualified"},
            ]
        monkeypatch.setattr(db, "event_lead_candidates", fake_cands)
        monkeypatch.setattr(db, "event_reminder_sent_at", AsyncMock(return_value=None))
        d = client.get("/api/mini/event/day-of/recipients").json()
        by_name = {r["name"]: r for r in d["recipients"]}
        assert by_name["Paid"]["template"] == "A"       # оплативший → A
        assert by_name["Unpaid"]["template"] == "B"     # неоплативший → B
        assert by_name["Paid"]["alreadySent"] is False

    def _mock_send(self, monkeypatch, *, stage="qualified", already=False, dnc=False):
        async def fake_lead(p):
            return {"phone": p, "name": "Test", "whatsapp_name": None,
                    "funnel_stage": stage, "do_not_contact": dnc}
        monkeypatch.setattr(db, "get_lead_by_phone", fake_lead)
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=already))
        monkeypatch.setattr(db, "event_reminder_sent_at",
                            AsyncMock(return_value=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)))
        log = AsyncMock(); monkeypatch.setattr(db, "log_event_reminder", log)
        monkeypatch.setattr(db, "save_manual_message", AsyncMock(return_value={"id": 1}))
        one = AsyncMock(return_value=True); monkeypatch.setattr(sender, "send_one", one)
        return log, one

    def test_send_auto_picks_B_for_unpaid_and_logs(self, client, monkeypatch):
        log, one = self._mock_send(monkeypatch, stage="qualified")
        r = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5213333333333", "template": "auto"}]})
        d = r.json()
        assert r.status_code == 200
        assert len(d["sent"]) == 1 and d["sent"][0]["template"] == "B"
        assert d["duplicates"] == [] and d["failed"] == []
        log.assert_awaited_once()   # маркер идемпотентности записан
        # B содержит ссылку → send_one получил текст со ссылкой
        assert any("tix.example" in c.args[1] for c in one.await_args_list)

    def test_send_auto_picks_A_for_paid(self, client, monkeypatch):
        self._mock_send(monkeypatch, stage="client_agency")
        d = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5214444444444", "template": "auto"}]}).json()
        assert d["sent"][0]["template"] == "A"

    def test_send_override_template(self, client, monkeypatch):
        self._mock_send(monkeypatch, stage="qualified")  # авто был бы B
        d = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5215555555555", "template": "A"}]}).json()
        assert d["sent"][0]["template"] == "A"           # override сработал

    def test_send_duplicate_without_force_not_sent(self, client, monkeypatch):
        log, one = self._mock_send(monkeypatch, already=True)
        d = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5216666666666", "template": "auto"}], "force": False}).json()
        assert d["sent"] == [] and len(d["duplicates"]) == 1
        assert d["duplicates"][0]["sentAt"] is not None   # дата отправки для предупреждения
        one.assert_not_awaited()                          # реально НЕ слали
        log.assert_not_awaited()

    def test_send_force_resends_duplicate(self, client, monkeypatch):
        log, one = self._mock_send(monkeypatch, already=True)
        d = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5217777777777", "template": "auto"}], "force": True}).json()
        assert len(d["sent"]) == 1 and d["duplicates"] == []
        one.assert_awaited()          # слали повторно
        log.assert_not_awaited()      # маркер уже был — повторно не логируем

    def test_send_skips_do_not_contact(self, client, monkeypatch):
        _, one = self._mock_send(monkeypatch, dnc=True)
        d = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5218888888888", "template": "auto"}]}).json()
        assert d["sent"] == [] and len(d["failed"]) == 1
        assert "заблокирован" in d["failed"][0]["reason"]
        one.assert_not_awaited()

    def test_send_lead_not_found_failed(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        d = client.post("/api/mini/event/day-of/send",
                        json={"recipients": [{"phone": "wa_5219999999999", "template": "auto"}]}).json()
        assert len(d["failed"]) == 1 and "не найден" in d["failed"][0]["reason"]


# ===== Тест переписки (/api/mini/test-chat) — песочница =====

class TestTestChat:
    """POST /test-chat: реальный ai.generate_reply без записи в БД."""

    _REPLY = {
        "messages": ["Hola Test! Soy Anna 🤍", "Eres soltero?"],
        "extracted": {"interest": "agency"},
        "funnel_stage": "qualifying",
        "action": "respond",
        "needs_escalation": False,
        "used_scenario_id": 1,
    }

    def _mock_pipeline(self, monkeypatch, reply=None):
        """Замокать read-only пайплайн (без OpenAI/БД)."""
        monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value=reply or self._REPLY))
        monkeypatch.setattr(ai, "search_scenarios",
                            AsyncMock(return_value=[{"id": 1, "score": 0.742}, {"id": 3, "score": 0.61}]))
        monkeypatch.setattr(db, "get_scenario_title",
                            AsyncMock(side_effect=lambda sid: f"Сценарий {sid}"))
        # render_bubbles обычно ходит в БД за настройками — мокаем на «как есть»
        monkeypatch.setattr(sender, "render_bubbles",
                            AsyncMock(side_effect=lambda msgs, phone=None, **kw: list(msgs)))

    def test_returns_reply_and_debug(self, client, monkeypatch):
        self._mock_pipeline(monkeypatch)
        r = client.post("/api/mini/test-chat", json={
            "leadProfile": {"whatsappName": "Test"},
            "history": [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "Hola!"}],
            "message": "sí, soltero",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["messages"] == ["Hola Test! Soy Anna 🤍", "Eres soltero?"]
        assert d["usedScenarioId"] == 1 and d["usedScenarioTitle"] == "Сценарий 1"
        assert d["action"] == "respond" and d["needsEscalation"] is False
        assert d["funnelStage"] == "qualifying" and d["extracted"] == {"interest": "agency"}
        assert d["ragCandidates"] == [
            {"id": 1, "score": 0.742, "title": "Сценарий 1"},
            {"id": 3, "score": 0.61, "title": "Сценарий 3"},
        ]

    def test_passes_history_and_profile_to_generate_reply(self, client, monkeypatch):
        """Память диалога: history + профиль реально уходят в generate_reply."""
        gen = AsyncMock(return_value=self._REPLY)
        monkeypatch.setattr(ai, "generate_reply", gen)
        monkeypatch.setattr(ai, "search_scenarios", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "get_scenario_title", AsyncMock(return_value="X"))
        monkeypatch.setattr(sender, "render_bubbles",
                            AsyncMock(side_effect=lambda msgs, phone=None, **kw: list(msgs)))
        client.post("/api/mini/test-chat", json={
            "leadProfile": {"isSingle": True, "age": 40, "profession": "abogado"},
            "history": [{"sender": "lead", "text": "hola"}],
            "message": "cuánto cuesta?",
        })
        lead_arg, history_arg, text_arg = gen.call_args.args
        assert lead_arg["is_single"] is True and lead_arg["age"] == 40
        assert lead_arg["profession"] == "abogado" and lead_arg["phone"] is None
        assert history_arg == [{"sender": "lead", "text": "hola"}]
        assert text_arg == "cuánto cuesta?"

    def test_does_not_write_to_db(self, client, monkeypatch):
        """Изоляция: НИ insert_message, НИ upsert_lead, НИ save_photo не вызываются."""
        self._mock_pipeline(monkeypatch)
        ins = AsyncMock(); ups = AsyncMock(); sph = AsyncMock()
        monkeypatch.setattr(db, "insert_message", ins)
        monkeypatch.setattr(db, "upsert_lead", ups)
        monkeypatch.setattr(db, "save_photo", sph)
        r = client.post("/api/mini/test-chat", json={"message": "hola"})
        assert r.status_code == 200
        ins.assert_not_called()
        ups.assert_not_called()
        sph.assert_not_called()

    def test_empty_message_422(self, client, monkeypatch):
        self._mock_pipeline(monkeypatch)
        r = client.post("/api/mini/test-chat", json={"message": "   "})
        assert r.status_code == 422

    def test_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        assert TestClient(app).post("/api/mini/test-chat", json={"message": "hola"}).status_code == 401


# ===== Медиа с ивентов (/api/mini/event/media) =====

class TestEventMedia:
    def test_list(self, client, monkeypatch):
        monkeypatch.setattr(db, "list_event_media", AsyncMock(return_value=[
            {"id": 1, "storage_url": "https://s/x.jpg", "storage_path": "event-media/x.jpg",
             "media_type": "image", "size_bytes": 1024, "is_active": True, "created_at": _dt(0)}]))
        r = client.get("/api/mini/event/media")
        assert r.status_code == 200
        m = r.json()["media"][0]
        assert m["id"] == 1 and m["mediaType"] == "image" and m["url"] == "https://s/x.jpg"

    def test_upload_image(self, client, monkeypatch):
        monkeypatch.setattr(vision, "upload_event_media",
                            AsyncMock(return_value=("https://s/a.jpg", "event-media/a.jpg")))
        monkeypatch.setattr(db, "add_event_media", AsyncMock(return_value={
            "id": 2, "storage_url": "https://s/a.jpg", "storage_path": "event-media/a.jpg",
            "media_type": "image", "size_bytes": 9, "is_active": True, "created_at": _dt(0)}))
        r = client.post("/api/mini/event/media",
                        files={"file": ("a.jpg", b"fakeimage", "image/jpeg")})
        assert r.status_code == 200 and r.json()["mediaType"] == "image"

    def test_upload_unsupported_type_422(self, client):
        r = client.post("/api/mini/event/media",
                        files={"file": ("a.pdf", b"x", "application/pdf")})
        assert r.status_code == 422

    def test_upload_video_transcodes(self, client, monkeypatch):
        # видео → media.transcode_video (мокаем, ffmpeg не зовём), затем upload
        monkeypatch.setattr(media, "transcode_video", lambda raw: b"small-mp4")
        monkeypatch.setattr(vision, "upload_event_media",
                            AsyncMock(return_value=("https://s/v.mp4", "event-media/v.mp4")))
        monkeypatch.setattr(db, "add_event_media", AsyncMock(return_value={
            "id": 3, "storage_url": "https://s/v.mp4", "storage_path": "event-media/v.mp4",
            "media_type": "video", "size_bytes": 9, "is_active": True, "created_at": _dt(0)}))
        r = client.post("/api/mini/event/media",
                        files={"file": ("v.mov", b"bigvideo", "video/quicktime")})
        assert r.status_code == 200 and r.json()["mediaType"] == "video"

    def test_upload_video_too_large_422_with_message(self, client, monkeypatch):
        def boom(raw):
            raise media.VideoTooLargeError("El video sigue pesando más de 16 MB tras comprimir. Recórtalo…")
        monkeypatch.setattr(media, "transcode_video", boom)
        r = client.post("/api/mini/event/media",
                        files={"file": ("v.mp4", b"huge", "video/mp4")})
        assert r.status_code == 422 and "16 MB" in r.json()["detail"]

    def test_delete(self, client, monkeypatch):
        monkeypatch.setattr(db, "delete_event_media", AsyncMock(return_value=True))
        r = client.delete("/api/mini/event/media/5")
        assert r.status_code == 200 and r.json()["deleted"] == 5

    def test_delete_missing_404(self, client, monkeypatch):
        monkeypatch.setattr(db, "delete_event_media", AsyncMock(return_value=False))
        assert client.delete("/api/mini/event/media/99").status_code == 404

    def test_requires_auth(self, monkeypatch):
        monkeypatch.setattr(db, "is_ready", lambda: True)
        monkeypatch.setattr(mini_auth.settings, "mini_dev_mode", False)
        assert TestClient(app).get("/api/mini/event/media").status_code == 401
