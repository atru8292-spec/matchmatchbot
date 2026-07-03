"""Тесты scheduler.py (блок 13): фоллоу-апы + напоминания об ивенте.

db и sender замоканы. Реальных сети/БД нет.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

import db
import funnel
import scheduler
import sender


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
    async def test_sends_scenario_by_ladder_and_marks(self, monkeypatch):
        """followup_sent_count=0 → 1-я ступень лестницы (сценарий 36), ставит next через 5 дней."""
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": "X", "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola 🤍"))
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_followup_sent", mark)

        n = await scheduler.run_followups()
        assert n == 1
        send.assert_awaited_once()
        # сценарий 1-й ступени
        scen_id = db.get_scenario_template.call_args.args[0]
        assert scen_id == funnel.FOLLOWUP_LADDER[0][0]
        # next_followup_at задан (не None для 1-й ступени)
        assert mark.call_args.args[0] == "wa_1"
        assert mark.call_args.args[1] is not None

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

    async def test_day_of_sends_scenario_47(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        tmpl = AsyncMock(return_value="hoy [hora] [dirección, lugar, parking]")
        monkeypatch.setattr(db, "get_scenario_template", tmpl)
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_1", "whatsapp_name": None, "name": None}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_event_reminder", AsyncMock())
        monkeypatch.setattr(sender, "send", AsyncMock())
        n = await scheduler.run_event_reminders(today=date(2026, 8, 15))  # день ивента
        assert tmpl.call_args.args[0] == scheduler.REMIND_DAY_SCENARIO
        assert n == 1

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
    async def test_tick_runs_both(self, monkeypatch):
        f = AsyncMock(); e = AsyncMock()
        monkeypatch.setattr(scheduler, "run_followups", f)
        monkeypatch.setattr(scheduler, "run_event_reminders", e)
        await scheduler.tick()
        f.assert_awaited_once()
        e.assert_awaited_once()

    async def test_tick_followup_failure_alerts_and_continues(self, monkeypatch):
        monkeypatch.setattr(scheduler, "run_followups", AsyncMock(side_effect=RuntimeError("x")))
        e = AsyncMock(); monkeypatch.setattr(scheduler, "run_event_reminders", e)
        err = AsyncMock(); monkeypatch.setattr(scheduler.escalation, "notify_error", err)
        await scheduler.tick()
        err.assert_awaited()             # алерт по сбою followups
        e.assert_awaited_once()          # event-часть всё равно отработала
