"""Тесты автозаписи звонка (#53): чистые хелперы + машина состояний booking.resolve_and_book.

Google (gcal) и пул БД замоканы — сеть не трогаем. Проверяем КАЖДЫЙ рискованный исход.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import actions
import ai
import booking
import db
import gcal

CDMX = ZoneInfo("America/Mexico_City")
NOW = datetime(2026, 7, 8, 14, 0, tzinfo=CDMX)  # среда 8 июля 2026, 14:00


# ===== фейковый пул БД (acquire → conn → transaction) =====
class _Conn:
    def __init__(self):
        self.executed = []
    async def execute(self, q, *a):
        self.executed.append((q, a))
    def transaction(self):
        return _Ctx()

class _Ctx:
    async def __aenter__(self): return None
    async def __aexit__(self, *a): return False

class _Acquire:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False

class _Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return _Acquire(self.conn)


@pytest.fixture
def gpatch(monkeypatch):
    """Замокать gcal + пул + запись брони; вернуть управляемые моки."""
    conn = _Conn()
    monkeypatch.setattr(db, "_get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(db, "set_videocall_booking", AsyncMock())
    monkeypatch.setattr(gcal, "is_configured", lambda: True)
    monkeypatch.setattr(gcal, "is_slot_free", AsyncMock(return_value=True))
    monkeypatch.setattr(gcal, "create_event", AsyncMock(return_value={
        "event_id": "ev123", "html_link": "https://calendar.google.com/event?eid=abc"}))
    monkeypatch.setattr(gcal, "patch_event", AsyncMock(return_value={
        "event_id": "ev123", "html_link": "https://calendar.google.com/event?eid=abc"}))
    return {"conn": conn}


def _lead(**kw):
    base = {"phone": "wa_52155", "name": "Diego", "whatsapp_name": "Diego",
            "videocall_event_id": None, "videocall_at": None, "calendar_link": None}
    base.update(kw)
    return base


# ===== чистые хелперы =====

class TestPureHelpers:
    def test_parse_naive_iso_to_cdmx(self):
        dt = booking.parse_proposed("2026-07-10T17:00:00")
        assert dt.hour == 17 and dt.tzinfo is not None

    def test_parse_invalid_returns_none(self):
        assert booking.parse_proposed("mañana tarde") is None
        assert booking.parse_proposed("") is None
        assert booking.parse_proposed(None) is None

    def test_validate_past(self):
        assert booking.validate(datetime(2026, 7, 7, 10, 0, tzinfo=CDMX), NOW) == booking.Outcome.PAST

    def test_validate_out_of_hours(self):
        assert booking.validate(datetime(2026, 7, 10, 3, 0, tzinfo=CDMX), NOW) == booking.Outcome.OUT_OF_HOURS
        # 21:45 старт → конец 22:15 > 22:00 → вне часов
        assert booking.validate(datetime(2026, 7, 10, 21, 45, tzinfo=CDMX), NOW) == booking.Outcome.OUT_OF_HOURS

    def test_validate_ok(self):
        assert booking.validate(datetime(2026, 7, 10, 17, 0, tzinfo=CDMX), NOW) is None
        assert booking.validate(datetime(2026, 7, 10, 8, 0, tzinfo=CDMX), NOW) is None
        assert booking.validate(datetime(2026, 7, 10, 21, 30, tzinfo=CDMX), NOW) is None

    def test_fmt_es_full(self):
        s = booking.fmt_es(datetime(2026, 7, 10, 17, 0, tzinfo=CDMX))
        assert "10 de julio" in s and "5:00 PM" in s and "CDMX" in s

    def test_message_booked_confirms_time_without_link(self):
        r = booking.Result(booking.Outcome.BOOKED, when=datetime(2026, 7, 10, 17, 0, tzinfo=CDMX),
                           link="https://calendar.google.com/event?eid=abc")
        m = booking.message_for(r)
        assert "confirmo" in m and "10 de julio" in m
        # ссылку лиду НЕ шлём — её отправит Аня вручную
        assert "http" not in m and "calendar.google.com" not in m

    def test_message_each_outcome_nonempty(self):
        for o in booking.Outcome:
            r = booking.Result(o, when=datetime(2026, 7, 10, 17, 0, tzinfo=CDMX),
                               link="l", alt_when=datetime(2026, 7, 10, 18, 0, tzinfo=CDMX))
            assert booking.message_for(r).strip()


# ===== машина состояний (resolve_and_book) =====

class TestResolveAndBook:
    async def test_vague_when_unparseable(self, gpatch):
        r = await booking.resolve_and_book(_lead(), None, NOW)
        assert r.outcome == booking.Outcome.VAGUE
        gcal.create_event.assert_not_called()

    async def test_past_rejected_no_google(self, gpatch):
        r = await booking.resolve_and_book(_lead(), "2026-07-07T10:00:00", NOW)
        assert r.outcome == booking.Outcome.PAST
        gcal.is_slot_free.assert_not_called()

    async def test_out_of_hours_rejected(self, gpatch):
        r = await booking.resolve_and_book(_lead(), "2026-07-10T03:00:00", NOW)
        assert r.outcome == booking.Outcome.OUT_OF_HOURS
        gcal.is_slot_free.assert_not_called()

    async def test_not_configured_error(self, gpatch, monkeypatch):
        monkeypatch.setattr(gcal, "is_configured", lambda: False)
        r = await booking.resolve_and_book(_lead(), "2026-07-10T17:00:00", NOW)
        assert r.outcome == booking.Outcome.ERROR

    async def test_booked_creates_event_and_saves(self, gpatch):
        r = await booking.resolve_and_book(_lead(), "2026-07-10T17:00:00", NOW)
        assert r.outcome == booking.Outcome.BOOKED
        assert r.link and "calendar.google.com" in r.link  # ссылка на событие (для Ани)
        gcal.create_event.assert_awaited_once()
        db.set_videocall_booking.assert_awaited_once()
        # advisory-lock реально взят
        assert any("pg_advisory_xact_lock" in q for q, _ in gpatch["conn"].executed)

    async def test_busy_suggests_alt_no_booking(self, gpatch):
        # слот занят, следующий свободен
        gcal.is_slot_free.side_effect = [False, True]
        r = await booking.resolve_and_book(_lead(), "2026-07-10T17:00:00", NOW)
        assert r.outcome == booking.Outcome.BUSY
        assert r.alt_when is not None
        gcal.create_event.assert_not_called()

    async def test_reschedule_patches_existing(self, gpatch):
        lead = _lead(videocall_event_id="ev123",
                     videocall_at=datetime(2026, 7, 10, 17, 0, tzinfo=CDMX))
        r = await booking.resolve_and_book(lead, "2026-07-11T18:00:00", NOW)
        assert r.outcome == booking.Outcome.RESCHEDULED
        gcal.patch_event.assert_awaited_once()
        gcal.create_event.assert_not_called()

    async def test_same_time_idempotent(self, gpatch):
        when = datetime(2026, 7, 10, 17, 0, tzinfo=CDMX)
        lead = _lead(videocall_event_id="ev123", videocall_at=when, calendar_link="https://meet/x")
        r = await booking.resolve_and_book(lead, "2026-07-10T17:00:00", NOW)
        assert r.outcome == booking.Outcome.SAME
        gcal.create_event.assert_not_called()
        gcal.patch_event.assert_not_called()

    async def test_google_failure_returns_error(self, gpatch):
        gcal.create_event.side_effect = RuntimeError("Google 503")
        r = await booking.resolve_and_book(_lead(), "2026-07-10T17:00:00", NOW)
        assert r.outcome == booking.Outcome.ERROR


# ===== контракт AI + гостевой список =====

class TestContractAndSheets:
    def test_ai_extracts_proposed_videocall_at(self):
        out = ai._validate_output({"messages": ["ok"], "proposed_videocall_at": "2026-07-10T17:00:00"})
        assert out["proposed_videocall_at"] == "2026-07-10T17:00:00"
        out2 = ai._validate_output({"messages": ["ok"], "proposed_videocall_at": ""})
        assert out2["proposed_videocall_at"] is None
        out3 = ai._validate_output({"messages": ["ok"]})
        assert out3["proposed_videocall_at"] is None

    async def test_guest_list_writes_to_event_tab(self, monkeypatch):
        """Оплата ивента → писатель зовётся с вкладкой и данными анкеты; written → без алерта."""
        monkeypatch.setattr(actions.settings, "google_guest_sheet_id", "guestbook123")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={
            "name": "Diego", "last_name": "Herrera", "email": "d@h.com",
            "profession": "arquitecto", "age": 38}))
        monkeypatch.setattr(db, "get_settings",
                            AsyncMock(return_value={"event_guest_tab": "22 de Julio "}))
        wr = AsyncMock(return_value="written")
        monkeypatch.setattr(gcal, "append_guest_to_event_tab", wr)
        alert = AsyncMock(); monkeypatch.setattr(actions.escalation, "notify_guest_list_issue", alert)
        await actions._add_to_guest_list("wa_5215500000004")
        wr.assert_awaited_once()
        args = wr.call_args.args
        assert args[0] == "22 de Julio"                  # tab застриплен при чтении настройки
        assert args[1] == "Diego Herrera"                # name = name + last_name
        assert args[2] == "d@h.com" and args[3] == "+5215500000004"  # email, phone
        assert args[4] == "38" and args[5] == "arquitecto"          # age, profession
        alert.assert_not_awaited()

    async def test_guest_list_no_tab_alerts_anna(self, monkeypatch):
        """Вкладки нет (no_tab) → алерт Ане, оплата не роняется."""
        monkeypatch.setattr(actions.settings, "google_guest_sheet_id", "guestbook123")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"name": "Diego"}))
        monkeypatch.setattr(db, "get_settings",
                            AsyncMock(return_value={"event_guest_tab": "22 de Julio"}))
        monkeypatch.setattr(gcal, "append_guest_to_event_tab", AsyncMock(return_value="no_tab"))
        alert = AsyncMock(); monkeypatch.setattr(actions.escalation, "notify_guest_list_issue", alert)
        await actions._add_to_guest_list("wa_5215500000004")
        alert.assert_awaited_once()
        assert alert.call_args.args[2] == "no_tab"

    async def test_guest_list_no_setting_alerts(self, monkeypatch):
        """Вкладка не задана в CRM → алерт no_setting, писатель не зовётся."""
        monkeypatch.setattr(actions.settings, "google_guest_sheet_id", "guestbook123")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"name": "Diego"}))
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"event_guest_tab": ""}))
        wr = AsyncMock(); monkeypatch.setattr(gcal, "append_guest_to_event_tab", wr)
        alert = AsyncMock(); monkeypatch.setattr(actions.escalation, "notify_guest_list_issue", alert)
        await actions._add_to_guest_list("wa_5215500000004")
        wr.assert_not_awaited()
        assert alert.call_args.args[2] == "no_setting"

    async def test_guest_list_skipped_when_book_not_configured(self, monkeypatch):
        """google_guest_sheet_id пуст → фича выключена, ничего не зовём."""
        monkeypatch.setattr(actions.settings, "google_guest_sheet_id", "")
        wr = AsyncMock(); monkeypatch.setattr(gcal, "append_guest_to_event_tab", wr)
        alert = AsyncMock(); monkeypatch.setattr(actions.escalation, "notify_guest_list_issue", alert)
        await actions._add_to_guest_list("wa_5215500000004")
        wr.assert_not_awaited(); alert.assert_not_awaited()

    async def test_guest_list_db_error_alerts_not_raises(self, monkeypatch):
        """Сбой Supabase (оплата уже подтверждена!) → алерт error, исключение НЕ всплывает."""
        monkeypatch.setattr(actions.settings, "google_guest_sheet_id", "guestbook123")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(side_effect=RuntimeError("db down")))
        alert = AsyncMock(); monkeypatch.setattr(actions.escalation, "notify_guest_list_issue", alert)
        await actions._add_to_guest_list("wa_5215500000004")  # не должно бросить
        alert.assert_awaited_once()
        assert alert.call_args.args[2] == "error"
        assert alert.call_args.args[0]["phone"] == "wa_5215500000004"  # алерт с номером


class TestMainBookingFlow:
    """main._handle_videocall_booking: подтверждение лиду + алерт Ане / фолбэк на ERROR."""

    async def test_booked_sends_lead_confirm_and_anna_alert(self, monkeypatch):
        import main
        import escalation
        import sender
        when = datetime(2026, 7, 10, 17, 0, tzinfo=CDMX)
        monkeypatch.setattr(booking, "resolve_and_book", AsyncMock(
            return_value=booking.Result(booking.Outcome.BOOKED, when=when, link="L")))
        lead_msg = AsyncMock(); monkeypatch.setattr(sender, "send", lead_msg)
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_videocall_booked", alert)

        await main._handle_videocall_booking(
            "wa_52155", {"phone": "wa_52155", "name": "Diego"}, "combined", "2026-07-10T17:00:00")

        lead_msg.assert_awaited_once()          # подтверждение времени лиду (без ссылки)
        assert "http" not in lead_msg.call_args.args[1][0]
        alert.assert_awaited_once()             # алерт Ане с датой/временем
        assert "10 de julio" in alert.call_args.args[1]

    async def test_error_falls_back_to_escalation(self, monkeypatch):
        import main
        import escalation
        import sender
        monkeypatch.setattr(booking, "resolve_and_book",
                            AsyncMock(return_value=booking.Result(booking.Outcome.ERROR)))
        monkeypatch.setattr(sender, "send", AsyncMock())
        monkeypatch.setattr(db, "update_lead_fields", AsyncMock())
        esc = AsyncMock(); monkeypatch.setattr(escalation, "notify_escalation", esc)
        booked = AsyncMock(); monkeypatch.setattr(escalation, "notify_videocall_booked", booked)

        await main._handle_videocall_booking("wa_52155", {"phone": "wa_52155"}, "c", "bad")

        esc.assert_awaited_once()               # фолбэк-эскалация Ане
        booked.assert_not_called()              # «забронировано» НЕ шлём при сбое


# ===== Медиа ивентов: два инструмента (send_event_photos / send_event_video) =====

class TestEventMediaActions:
    async def test_photos_sends_up_to_3_if_not_sent(self, monkeypatch):
        import actions, sender
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"event_date": "2026-08-15"}))
        ems = AsyncMock(return_value=False); monkeypatch.setattr(db, "event_media_sent", ems)
        monkeypatch.setattr(db, "random_event_media", AsyncMock(return_value=[
            {"storage_url": "https://s/1.jpg", "media_type": "image"},
            {"storage_url": "https://s/2.jpg", "media_type": "image"}]))
        sm = AsyncMock(return_value=True); monkeypatch.setattr(sender, "send_media", sm)
        n = await actions.send_event_photos("wa_1")
        assert n == 2 and sm.await_count == 2
        # запрошен именно тип image, count = EVENT_PHOTO_COUNT
        assert db.random_event_media.call_args.args == ("image", actions.EVENT_PHOTO_COUNT)
        # дедуп и отправка получили активную дату ивента (вар. B, резолв из настроек)
        assert ems.call_args.args == ("wa_1", "image", "2026-08-15")
        assert sm.call_args.args[3] == "2026-08-15"  # event_date проброшен в send_media

    async def test_video_sends_one_if_not_sent(self, monkeypatch):
        import actions, sender
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"event_date": "2026-08-15"}))
        monkeypatch.setattr(db, "event_media_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "random_event_media", AsyncMock(return_value=[
            {"storage_url": "https://s/v.mp4", "media_type": "video"}]))
        sm = AsyncMock(return_value=True); monkeypatch.setattr(sender, "send_media", sm)
        n = await actions.send_event_video("wa_1")
        assert n == 1
        assert db.random_event_media.call_args.args == ("video", actions.EVENT_VIDEO_COUNT)

    async def test_explicit_event_date_skips_settings_lookup(self, monkeypatch):
        """Явный event_date (путь планировщика) → в настройки не ходим, дату пробрасываем."""
        import actions, sender
        gs = AsyncMock(); monkeypatch.setattr(db, "get_settings", gs)
        ems = AsyncMock(return_value=False); monkeypatch.setattr(db, "event_media_sent", ems)
        monkeypatch.setattr(db, "random_event_media", AsyncMock(return_value=[
            {"storage_url": "https://s/1.jpg", "media_type": "image"}]))
        sm = AsyncMock(return_value=True); monkeypatch.setattr(sender, "send_media", sm)
        n = await actions.send_event_photos("wa_1", event_date="2026-09-20")
        assert n == 1
        gs.assert_not_called()  # дату передали явно — settings не трогаем
        assert ems.call_args.args == ("wa_1", "image", "2026-09-20")
        assert sm.call_args.args[3] == "2026-09-20"

    async def test_dedup_per_type_skips(self, monkeypatch):
        """Тип уже слали на этот ивент → пропуск, в пул даже не ходим."""
        import actions, sender
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"event_date": "2026-08-15"}))
        monkeypatch.setattr(db, "event_media_sent", AsyncMock(return_value=True))
        rnd = AsyncMock(); monkeypatch.setattr(db, "random_event_media", rnd)
        sm = AsyncMock(); monkeypatch.setattr(sender, "send_media", sm)
        assert await actions.send_event_photos("wa_1") == 0
        assert await actions.send_event_video("wa_1") == 0
        rnd.assert_not_called(); sm.assert_not_called()

    async def test_no_media_of_type_sends_nothing(self, monkeypatch):
        import actions, sender
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"event_date": "2026-08-15"}))
        monkeypatch.setattr(db, "event_media_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "random_event_media", AsyncMock(return_value=[]))
        sm = AsyncMock(); monkeypatch.setattr(sender, "send_media", sm)
        assert await actions.send_event_video("wa_1") == 0
        sm.assert_not_called()

    def test_media_marker_dated_vs_legacy(self):
        """Вар. B: с датой — маркер привязан к ивенту; без даты — легаси-глобальный."""
        assert db.media_marker("image", "2026-08-15") == "[foto ивента отправлено 2026-08-15]"
        assert db.media_marker("video", "2026-08-15") == "[video ивента отправлено 2026-08-15]"
        assert db.media_marker("image") == "[foto ивента отправлено]"  # легаси
        assert db.media_marker("image", None) == "[foto ивента отправлено]"
        assert db.media_marker("bogus", "2026-08-15") is None

    def test_ai_contract_two_tools(self):
        import ai
        out = ai._validate_output({"messages": ["x"], "send_event_photo": True, "send_event_video": True})
        assert out["send_event_photo"] is True and out["send_event_video"] is True
        d = ai._validate_output({"messages": ["x"]})
        assert d["send_event_photo"] is False and d["send_event_video"] is False

    def test_fixed_event_detail_attaches_video(self):
        """#51/#52 (ai_allowed=false, в обход OpenAI) — код сам прикладывает видео атмосферы."""
        import ai
        r51 = ai._fixed_reply({"id": 51, "template_es": "precio…", "mode": "bot_auto"})
        assert r51["send_event_video"] is True and r51["send_event_photo"] is False
        r39 = ai._fixed_reply({"id": 39, "template_es": "no descuentos", "mode": "bot_auto"})
        assert r39["send_event_video"] is False   # прочие фикс-сценарии — без видео


# ===== Анкета-в-чате → Google Sheet =====

class TestAnketa:
    async def test_saves_when_complete_and_not_yet_saved(self, monkeypatch):
        import actions, funnel
        lead = {"phone": "wa_52155", "name": "Diego", "last_name": "Herrera",
                "email": "d@x.com", "date_of_birth": "1988-05-12", "city": "CDMX",
                "country": "México", "business_link": "linkedin.com/in/d",
                "desired_partner_age": "25-35", "is_single": True, "profession": "arquitecto",
                "interest": "agency"}
        monkeypatch.setattr(actions.settings, "google_sheet_id", "sheet123")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=lead))
        monkeypatch.setattr(db, "anketa_saved", AsyncMock(return_value=False))
        append = AsyncMock(); monkeypatch.setattr(actions.gcal, "append_anketa_row", append)
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_anketa_saved", mark)
        ok = await actions.save_anketa_if_complete("wa_52155")
        assert ok is True
        append.assert_awaited_once()
        # имя = name+last_name; email проброшен
        args = append.call_args.args
        assert args[0] == "Diego Herrera" and args[1] == "d@x.com"
        mark.assert_awaited_once_with("wa_52155")

    async def test_skips_when_incomplete(self, monkeypatch):
        import actions
        lead = {"phone": "wa_1", "email": "d@x.com"}  # не хватает полей
        monkeypatch.setattr(actions.settings, "google_sheet_id", "s")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=lead))
        append = AsyncMock(); monkeypatch.setattr(actions.gcal, "append_anketa_row", append)
        assert await actions.save_anketa_if_complete("wa_1") is False
        append.assert_not_called()

    async def test_dedup_skips_if_already_saved(self, monkeypatch):
        import actions
        lead = {"phone": "wa_1", "name": "D", "email": "d@x.com", "date_of_birth": "1988-05-12",
                "country": "MX", "desired_partner_age": "25-35"}
        monkeypatch.setattr(actions.settings, "google_sheet_id", "s")
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=lead))
        monkeypatch.setattr(db, "anketa_saved", AsyncMock(return_value=True))  # уже писали
        append = AsyncMock(); monkeypatch.setattr(actions.gcal, "append_anketa_row", append)
        assert await actions.save_anketa_if_complete("wa_1") is False
        append.assert_not_called()

    def test_anketa_complete_helper(self):
        import funnel
        assert funnel.anketa_complete({"email": "a", "date_of_birth": "b", "country": "c",
                                       "desired_partner_age": "d"}) is True
        assert funnel.anketa_complete({"email": "a"}) is False

    def test_parse_dob(self):
        import main
        from datetime import date
        assert main._parse_dob("1988-05-12") == date(1988, 5, 12)
        assert main._parse_dob("12/05/1988") == date(1988, 5, 12)
        assert main._parse_dob("no soy fecha") is None

    def test_ai_extracts_anketa_fields(self):
        import ai
        out = ai._validate_output({"messages": ["ok"], "extracted": {
            "email": "d@x.com", "date_of_birth": "1988-05-12", "country": "México",
            "desired_partner_age": "25-35", "last_name": "Herrera"}})
        ex = out["extracted"]
        assert ex["email"] == "d@x.com" and ex["date_of_birth"] == "1988-05-12"
        assert ex["country"] == "México" and ex["last_name"] == "Herrera"


# ===== Гостевой список ивента: маппинг колонок по заголовку + запись в свободный слот =====

# Заголовок «нового» макета (Men в C, Women в L; у колонки «#» пустой заголовок).
_HDR_NEW = ["Pago", "", "Men", "Emails", "Number", "Photos", "Age", "Profession", "Notes",
            "", "", "Women", "Numbers", "", "Number given at event", "Name", "Number"]


class _FakeSS:
    """Мок googleapiclient spreadsheets(): get/values().get()/values().batchUpdate()."""
    def __init__(self, titles, values):
        self._titles = titles
        self._values = values
        self.batched = None

    def get(self, spreadsheetId=None):
        sheets = [{"properties": {"title": t}} for t in self._titles]
        return _Exec({"sheets": sheets})

    def values(self):
        outer = self
        class _V:
            def get(self, spreadsheetId=None, range=None):
                return _Exec({"values": outer._values})
            def batchUpdate(self, spreadsheetId=None, body=None):
                outer.batched = body
                return _Exec({})
        return _V()


class _Exec:
    def __init__(self, res): self._res = res
    def execute(self): return self._res


def _patch_sheets(monkeypatch, titles, values):
    ss = _FakeSS(titles, values)
    monkeypatch.setattr(gcal, "_ensure_clients", lambda: None)
    monkeypatch.setattr(gcal, "_sheets", type("S", (), {"spreadsheets": lambda self: ss})())
    monkeypatch.setattr(gcal.settings, "google_guest_sheet_id", "book")
    return ss


class TestGuestColumns:
    def test_new_layout_men_block_only(self):
        cols = gcal._guest_columns(_HDR_NEW)
        assert cols == {"men": 2, "emails": 3, "number": 4, "age": 6, "profession": 7}
        # НЕ подхватил женские Number(16)/Numbers(12) — граница по Women(11)
        assert cols["number"] == 4

    def test_old_layout_men_at_col_a(self):
        """Старый макет: Men в колонке A, Women в I — маппинг по имени всё равно верный."""
        hdr = ["Men", "Emails", "Number", "Age", "Profession", "", "", "", "Women", "Numbers"]
        cols = gcal._guest_columns(hdr)
        assert cols == {"men": 0, "emails": 1, "number": 2, "age": 3, "profession": 4}

    def test_no_men_header_returns_none(self):
        assert gcal._guest_columns(["Foo", "Bar", "Women"]) is None

    def test_first_free_men_row(self):
        vals = [_HDR_NEW,
                ["Klar", "1", "Ricardo", "", "55 1", "", "", "", "", "", "", "Анна", "7 9", "", "", "", ""],
                ["", "2", "", "", "", "", "", "", "", "", "", "Mila", "998", "", "", "", ""]]
        assert gcal._first_free_men_row(vals, 0, 2) == 2   # первая строка с пустым Men
        # все заняты → None
        full = [_HDR_NEW, ["Klar", "1", "Ricardo", "", "55 1", "", "", "", "", "", "", "", "", "", "", "", ""]]
        assert gcal._first_free_men_row(full, 0, 2) is None


class TestWriteGuestSync:
    def _values(self):
        return [_HDR_NEW,
                ["Klar", "1", "Ricardo", "", "55 1", "", "", "", "", "", "", "Анна", "7 9", "", "", "", ""],
                ["", "2", "", "", "", "", "", "", "", "", "", "Mila", "998", "", "", "", ""]]

    def test_writes_men_block_to_first_free_slot(self, monkeypatch):
        ss = _patch_sheets(monkeypatch, ["22 de Julio "], self._values())
        st = gcal._write_guest_sync("22 de Julio", {
            "men": "Diego Herrera", "emails": "d@h.com", "number": "+52155",
            "age": "38", "profession": "arquitecto"})
        assert st == "written"
        ranges = {d["range"]: d["values"][0][0] for d in ss.batched["data"]}
        # первый свободный слот = строка 3 листа; колонки C/D/E/G/H
        assert ranges == {"'22 de Julio '!C3": "Diego Herrera", "'22 de Julio '!D3": "d@h.com",
                          "'22 de Julio '!E3": "+52155", "'22 de Julio '!G3": "38",
                          "'22 de Julio '!H3": "arquitecto"}
        # женский блок (L+) и Pago(A)/Photos(F)/Notes(I) не тронуты
        assert not any(r.split("!")[1][0] in ("A", "F", "I", "L", "M") for r in ranges)

    def test_tab_matched_with_strip(self, monkeypatch):
        """В книге вкладка с хвостовым пробелом, в настройке — без; strip → совпало."""
        ss = _patch_sheets(monkeypatch, ["22 de Julio "], self._values())
        st = gcal._write_guest_sync("22 de Julio", {"men": "X"})
        assert st == "written"

    def test_no_tab_when_missing(self, monkeypatch):
        _patch_sheets(monkeypatch, ["27 de Mayo"], self._values())
        assert gcal._write_guest_sync("22 de Julio", {"men": "X"}) == "no_tab"

    def test_no_slot_when_full(self, monkeypatch):
        vals = [_HDR_NEW, ["Klar", "1", "Ricardo", "", "55", "", "", "", "", "", "", "", "", "", "", "", ""]]
        _patch_sheets(monkeypatch, ["22 de Julio"], vals)
        assert gcal._write_guest_sync("22 de Julio", {"men": "X"}) == "no_slot"

    def test_bad_layout_no_men_header(self, monkeypatch):
        _patch_sheets(monkeypatch, ["22 de Julio"], [["Foo", "Bar"], ["", ""]])
        assert gcal._write_guest_sync("22 de Julio", {"men": "X"}) == "bad_layout"
