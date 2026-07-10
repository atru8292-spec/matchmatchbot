"""Тесты scheduler.py (блок 13): фоллоу-апы + напоминания об ивенте.

db и sender замоканы. Реальных сети/БД нет.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import db
import funnel
import scheduler
import sender


@pytest.fixture(autouse=True)
def _sched_antiban_stubs(monkeypatch):
    """Глобально: паузы между лидами в ноль (иначе тесты спят 15-90с) + суточный счётчик
    холодных далёк от лимита (тесты не упираются в cap, если явно не проверяют его)."""
    monkeypatch.setattr(scheduler, "_lead_pause", AsyncMock())
    monkeypatch.setattr(db, "get_daily_counter", AsyncMock(return_value=0))
    monkeypatch.setattr(db, "incr_daily_counter", AsyncMock())


# ===== _fill_event =====

class TestFillEvent:
    def test_fills_hora_and_address(self):
        out = scheduler._fill_event(
            "Te espero a las [hora] en [dirección] 🤍",
            {"event_time": "20:30", "event_address": "Av. Reforma 123"})
        assert "20:30" in out and "Av. Reforma 123" in out
        assert "[hora]" not in out and "[dirección]" not in out

    def test_fills_day_placeholder(self):
        out = scheduler._fill_event(
            "detalles: [dirección, lugar, parking]",
            {"event_address": "Club X"})
        assert "Club X" in out
        assert "[dirección" not in out


# ===== run_followups =====

class TestRunFollowups:
    async def test_stage_based_scenario_and_marks(self, monkeypatch):
        """Холодный молчун (анкета не начата) → #36; next через 5 дней (1-й интервал)."""
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": "X", "name": None}  # без анкета-полей → #36
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola 🤍"))
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_followup_sent", mark)

        n = await scheduler.run_followups()
        assert n == 1
        send.assert_awaited_once()
        assert db.get_scenario_template.call_args.args[0] == 36   # стадийный выбор → #36
        assert mark.call_args.args[0] == "wa_1" and mark.call_args.args[1] is not None

    async def test_stage_selects_by_anketa_state(self, monkeypatch):
        """Анкета начата → #32; анкета готова → #33; звонок назначен → пропуск."""
        started = {"phone": "wa_1", "funnel_stage": "pitched", "followup_sent_count": 0,
                   "whatsapp_name": None, "name": None, "email": "a@b.com"}
        complete = {"phone": "wa_2", "funnel_stage": "pitched", "followup_sent_count": 0,
                    "whatsapp_name": None, "name": None, "email": "a@b.com",
                    "date_of_birth": "x", "country": "MX", "desired_partner_age": "25-35"}
        booked = {"phone": "wa_3", "funnel_stage": "videocall_set", "followup_sent_count": 0,
                  "whatsapp_name": None, "name": None}
        picked = []
        async def tmpl(sid): picked.append(sid); return "Hola"
        monkeypatch.setattr(db, "get_scenario_template", tmpl)
        monkeypatch.setattr(sender, "send", AsyncMock(return_value=1))
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[started, complete, booked]))
        n = await scheduler.run_followups()
        assert picked == [32, 33]   # #32 (started), #33 (complete); booked → пропущен
        assert n == 2

    async def test_send_failure_does_not_mark(self, monkeypatch):
        """Wazzup не принял (send=0) → followup НЕ помечается (повтор на след. тике)."""
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": None, "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola"))
        monkeypatch.setattr(sender, "send", AsyncMock(return_value=0))  # сбой отправки
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_followup_sent", mark)
        n = await scheduler.run_followups()
        assert n == 0
        mark.assert_not_awaited()

    async def test_last_rung_sets_next_none(self, monkeypatch):
        """Последняя ступень (count=2) → next_followup_at=None (больше не догоняем)."""
        lead = {"phone": "wa_1", "funnel_stage": "pitched", "followup_sent_count": 2,
                "whatsapp_name": "X", "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola"))
        monkeypatch.setattr(sender, "send", AsyncMock())
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_followup_sent", mark)
        await scheduler.run_followups()
        assert mark.call_args.args[1] is None

    async def test_one_failure_does_not_stop_others(self, monkeypatch):
        leads = [{"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                  "whatsapp_name": None, "name": None},
                 {"phone": "wa_2", "funnel_stage": "qualified", "followup_sent_count": 0,
                  "whatsapp_name": None, "name": None}]
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=leads))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola"))
        send = AsyncMock(side_effect=[RuntimeError("boom"), 1])
        monkeypatch.setattr(sender, "send", send)
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())
        n = await scheduler.run_followups()
        assert send.await_count == 2  # второй обработан несмотря на сбой первого
        assert n == 1

    async def test_daily_cap_blocks_when_exhausted(self, monkeypatch):
        """Суточный лимит холодных исчерпан → вообще не выбираем лидов, не шлём."""
        monkeypatch.setattr(db, "get_daily_counter",
                            AsyncMock(return_value=scheduler.settings.cold_followup_daily_cap))
        due = AsyncMock(); monkeypatch.setattr(db, "due_followups", due)
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_followups()
        assert n == 0
        due.assert_not_called()   # даже не запрашиваем лидов
        send.assert_not_called()

    async def test_remaining_cap_limits_batch(self, monkeypatch):
        """Осталось 2 до лимита → limit в запросе = 2 (не весь FOLLOWUP_BATCH)."""
        cap = scheduler.settings.cold_followup_daily_cap
        monkeypatch.setattr(db, "get_daily_counter", AsyncMock(return_value=cap - 2))
        due = AsyncMock(return_value=[]); monkeypatch.setattr(db, "due_followups", due)
        await scheduler.run_followups()
        assert due.call_args.kwargs["limit"] == 2

    async def test_increments_counter_and_pauses(self, monkeypatch):
        """Успешная отправка засчитывается в суточный счётчик + пауза между лидами."""
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": "X", "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola"))
        monkeypatch.setattr(sender, "send", AsyncMock(return_value=1))
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())
        incr = AsyncMock(); monkeypatch.setattr(db, "incr_daily_counter", incr)
        pause = AsyncMock(); monkeypatch.setattr(scheduler, "_lead_pause", pause)
        await scheduler.run_followups()
        incr.assert_awaited_once_with(scheduler.COLD_COUNTER)
        pause.assert_awaited_once()


# ===== run_event_reminders =====

def _event_settings(active="1", d="2026-08-15", t="20:30", addr="Av X"):
    return {"event_active": active, "event_date": d, "event_time": t, "event_address": addr}


class TestRunEventReminders:
    async def test_inactive_does_nothing(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings(active="0")))
        rec = AsyncMock(); monkeypatch.setattr(db, "event_recipients", rec)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 15))
        assert n == 0
        rec.assert_not_awaited()

    async def test_bad_date_skips(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings(d="кривая")))
        rec = AsyncMock(); monkeypatch.setattr(db, "event_recipients", rec)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 15))
        assert n == 0
        rec.assert_not_awaited()

    async def test_t_minus_1_sends_scenario_50(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        tmpl = AsyncMock(return_value="mañana [hora] [dirección]")
        monkeypatch.setattr(db, "get_scenario_template", tmpl)
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_1", "whatsapp_name": None, "name": None}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_event_reminder", AsyncMock())
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 14))  # за день
        assert n == 1
        assert tmpl.call_args.args[0] == scheduler.REMIND_1D_SCENARIO
        send.assert_awaited_once()

    async def test_day_of_unpaid_uses_54_with_link(self, monkeypatch):
        """День ивента, неоплативший (обычная стадия) → Шаблон B (#54), ссылка разрешена."""
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        async def tmpl(sid):
            return "A sin link" if sid == scheduler.REMIND_DAY_SCENARIO else "B con [event_link]"
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(side_effect=tmpl))
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_1", "whatsapp_name": None,
                                                     "name": None, "funnel_stage": "qualified"}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_event_reminder", AsyncMock())
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 15))  # день ивента
        assert n == 1
        # неоплатившему: Шаблон B + ссылка разрешена к повтору
        args, kwargs = send.call_args
        assert kwargs.get("allow_repeat_links") is True
        assert "con" in args[1][0]  # текст из Шаблона B

    async def test_day_of_paid_uses_47_no_repeat(self, monkeypatch):
        """День ивента, оплативший (event_attended) → Шаблон A (#47), ссылку не повторяем."""
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        async def tmpl(sid):
            return "A sin link" if sid == scheduler.REMIND_DAY_SCENARIO else "B con [event_link]"
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(side_effect=tmpl))
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_2", "whatsapp_name": None,
                                                     "name": None, "funnel_stage": "event_attended"}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_event_reminder", AsyncMock())
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 15))  # день ивента
        assert n == 1
        args, kwargs = send.call_args
        assert kwargs.get("allow_repeat_links") is False
        assert "sin" in args[1][0]  # текст из Шаблона A

    async def test_morning_after_sends_scenario_23(self, monkeypatch):
        """Следующее утро после ивента (event_date+1) → check-in, сценарий 23."""
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        tmpl = AsyncMock(return_value="cómo te fue ayer? 🤍")
        monkeypatch.setattr(db, "get_scenario_template", tmpl)
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_1", "whatsapp_name": None, "name": None}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_event_reminder", AsyncMock())
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 16))  # утро после ивента
        assert n == 1
        assert tmpl.call_args.args[0] == scheduler.CHECKIN_MORNING_SCENARIO
        send.assert_awaited_once()

    async def test_idempotent_skips_already_sent(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="x"))
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_1", "whatsapp_name": None, "name": None}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=True))  # уже слали
        log = AsyncMock(); monkeypatch.setattr(db, "log_event_reminder", log)
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 15))
        assert n == 0
        send.assert_not_awaited()
        log.assert_not_awaited()

    async def test_other_day_no_reminder(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        rec = AsyncMock(); monkeypatch.setattr(db, "event_recipients", rec)
        n = await scheduler.run_event_reminders(today=date(2026, 8, 10))  # задолго до
        assert n == 0
        rec.assert_not_awaited()


# ===== tick =====

class TestTick:
    async def test_tick_runs_all(self, monkeypatch):
        f = AsyncMock(); e = AsyncMock(); v = AsyncMock()
        monkeypatch.setattr(scheduler, "run_followups", f)
        monkeypatch.setattr(scheduler, "run_event_reminders", e)
        monkeypatch.setattr(scheduler, "run_videocall_reminders", v)
        await scheduler.tick()
        f.assert_awaited_once()
        e.assert_awaited_once()
        v.assert_awaited_once()

    async def test_tick_followup_failure_alerts_and_continues(self, monkeypatch):
        monkeypatch.setattr(scheduler, "run_followups", AsyncMock(side_effect=RuntimeError("x")))
        e = AsyncMock(); monkeypatch.setattr(scheduler, "run_event_reminders", e)
        monkeypatch.setattr(scheduler, "run_videocall_reminders", AsyncMock())
        err = AsyncMock(); monkeypatch.setattr(scheduler.escalation, "notify_error", err)
        await scheduler.tick()
        err.assert_awaited()             # алерт по сбою followups
        e.assert_awaited_once()          # но event-reminders всё равно вызвались


# ===== run_videocall_reminders (сценарий 49, за ~2 часа) =====

def _lead_call(phone="wa_1", call=None):
    return {"phone": phone, "whatsapp_name": None, "name": None, "videocall_at": call}


class TestRunVideocallReminders:
    async def test_sends_and_marks_within_window(self, monkeypatch):
        """Звонок через 2 часа → попадает в окно, шлём сценарий 49, помечаем."""
        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        call = now + timedelta(hours=2)                       # ровно за 2 часа
        due = AsyncMock(return_value=[_lead_call(call=call)])
        monkeypatch.setattr(db, "due_videocall_reminders", due)
        tmpl = AsyncMock(return_value="hoy tenemos videollamada a las [hora]. Sigue en pie?")
        monkeypatch.setattr(db, "get_scenario_template", tmpl)
        send = AsyncMock(return_value=1); monkeypatch.setattr(sender, "send", send)
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_videocall_reminded", mark)

        n = await scheduler.run_videocall_reminders(now=now)

        assert n == 1
        assert tmpl.call_args.args[0] == scheduler.VIDEOCALL_SCENARIO
        # окно передано в db: [now+1.5ч, now+2.5ч]
        start, end = due.call_args.args[0], due.call_args.args[1]
        assert start == now + timedelta(minutes=90) and end == now + timedelta(minutes=150)
        # [hora] подставлено в CDMX (UTC-6): 14:00 UTC → 08:00 CDMX
        sent_bubbles = send.call_args.args[1]
        assert any("08:00" in b for b in sent_bubbles)
        assert all("[hora]" not in b for b in sent_bubbles)
        mark.assert_awaited_once_with("wa_1")

    async def test_none_due_no_send(self, monkeypatch):
        """Нет подходящих звонков → ничего не шлём, template не читаем."""
        monkeypatch.setattr(db, "due_videocall_reminders", AsyncMock(return_value=[]))
        tmpl = AsyncMock(); monkeypatch.setattr(db, "get_scenario_template", tmpl)
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_videocall_reminders(now=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc))
        assert n == 0
        send.assert_not_awaited()
        tmpl.assert_not_awaited()

    async def test_send_failure_does_not_mark(self, monkeypatch):
        """Wazzup не принял (send=0) → НЕ помечаем reminded (повтор на след. тике)."""
        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(db, "due_videocall_reminders",
                            AsyncMock(return_value=[_lead_call(call=now + timedelta(hours=2))]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="a las [hora]"))
        monkeypatch.setattr(sender, "send", AsyncMock(return_value=0))
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_videocall_reminded", mark)
        n = await scheduler.run_videocall_reminders(now=now)
        assert n == 0
        mark.assert_not_awaited()

    async def test_per_lead_failure_alerts_and_continues(self, monkeypatch):
        """Сбой на одном лиде → алерт, второй всё равно обрабатывается."""
        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        call = now + timedelta(hours=2)
        monkeypatch.setattr(db, "due_videocall_reminders",
                            AsyncMock(return_value=[_lead_call("wa_1", call), _lead_call("wa_2", call)]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="a las [hora]"))
        send = AsyncMock(side_effect=[RuntimeError("boom"), 1])
        monkeypatch.setattr(sender, "send", send)
        monkeypatch.setattr(db, "mark_videocall_reminded", AsyncMock())
        err = AsyncMock(); monkeypatch.setattr(scheduler.escalation, "notify_error", err)
        n = await scheduler.run_videocall_reminders(now=now)
        assert n == 1                    # второй лид прошёл
        err.assert_awaited()             # по первому — алерт


class TestPersonalize:
    def test_fills_imya_placeholder(self):
        assert scheduler._personalize("Hola [имя]!", "Carlos", 2) == "Hola Carlos!"

    def test_imya_fallback_guapo(self):
        assert scheduler._personalize("Hola [имя]!", None, 2) == "Hola guapo!"

    def test_guapo_to_name_on_odd_rung(self):
        # rung 0 (1-я попытка) → по имени
        assert scheduler._personalize("Hola guapo! 🤍", "Carlos", 0) == "Hola Carlos! 🤍"

    def test_guapo_kept_on_even_rung(self):
        # rung 1 (2-я попытка) → оставляем guapo
        assert scheduler._personalize("Hola guapo! 🤍", "Carlos", 1) == "Hola guapo! 🤍"

    def test_no_name_keeps_guapo(self):
        assert scheduler._personalize("Hola guapo!", None, 0) == "Hola guapo!"


class TestFollowupPersonalized:
    async def test_followup_uses_name_on_first_rung(self, monkeypatch):
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": "Carlos", "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola guapo! 🤍 sigues?"))
        send = AsyncMock(return_value=1); monkeypatch.setattr(sender, "send", send)
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())
        await scheduler.run_followups()
        bubbles = send.call_args.args[1]
        assert any("Carlos" in b for b in bubbles)
        assert not any("guapo" in b for b in bubbles)


class TestFollowupAlerts:
    async def test_per_lead_failure_alerts(self, monkeypatch):
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": None, "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(side_effect=RuntimeError("db")))
        err = AsyncMock(); monkeypatch.setattr(scheduler.escalation, "notify_error", err)
        await scheduler.run_followups()
        err.assert_awaited_once()
