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

import db
import mini_api
import mini_auth
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
        assert "qualifying" in codes and "client_vip" in codes


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
            "eventActive": True, "eventDate": "2026-08-15", "eventTime": "20:30",
            "eventAddress": "Av. Reforma 123", "eventLink": "https://pay.example",
            "courseLink": "https://cursos.example", "invitationUrl": "https://img/inv.jpg",
            "invitationReady": True,
        }
        r = client.put("/api/mini/event", json=body)
        assert r.status_code == 200
        # пишет в ТЕ ЖЕ ключи, что бот
        assert saved["event_active"] == "1" and saved["event_date"] == "2026-08-15"
        assert saved["event_time"] == "20:30" and saved["event_address"] == "Av. Reforma 123"
        assert saved["event_link"] == "https://pay.example"
        assert saved["invitation_ready"] == "1"
        assert r.json()["eventActive"] is True

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
