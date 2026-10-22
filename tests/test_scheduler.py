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
        """Уже видел pitch (qualified, анкета не начата) → #59; next через 5 дней (1-й интервал)."""
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": "X", "name": None}  # без анкета-полей, но уже qualified → #59
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola 🤍"))
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_followup_sent", mark)

        n = await scheduler.run_followups()
        assert n == 1
        send.assert_awaited_once()
        assert db.get_scenario_template.call_args.args[0] == 59   # стадийный выбор → #59
        assert mark.call_args.args[0] == "wa_1" and mark.call_args.args[1] is not None

    async def test_early_stage_gets_36(self, monkeypatch):
        """Ранний молчун (new/qualifying, анкета не начата, ещё НЕ qualified) → #36."""
        lead = {"phone": "wa_1", "funnel_stage": "qualifying", "followup_sent_count": 0,
                "whatsapp_name": "X", "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola 🤍"))
        monkeypatch.setattr(sender, "send", AsyncMock())
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())

        n = await scheduler.run_followups()
        assert n == 1
        assert db.get_scenario_template.call_args.args[0] == 36

    async def test_stage_selects_by_anketa_state(self, monkeypatch):
        """Анкета начата → #32; анкета готова → #33; звонок назначен → пропуск."""
        started = {"phone": "wa_1", "funnel_stage": "pitched", "followup_sent_count": 0,
                   "whatsapp_name": None, "name": None, "email": "a@b.com"}  # falta name
        complete = {"phone": "wa_2", "funnel_stage": "pitched", "followup_sent_count": 0,
                    "whatsapp_name": None, "name": "Carlos", "email": "a@b.com"}
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

def _noon_cdmx(d: date) -> datetime:
    """Полдень по Мехико в день d — внутри окна отправки напоминаний."""
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=scheduler.CDMX_TZ)


def _event_settings(active="1", d="2026-08-15", t="20:30", addr="Av X"):
    return {"event_active": active, "event_date": d, "event_time": t, "event_address": addr}


class TestRunEventReminders:
    async def test_inactive_does_nothing(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings(active="0")))
        rec = AsyncMock(); monkeypatch.setattr(db, "event_recipients", rec)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 15)))
        assert n == 0
        rec.assert_not_awaited()

    async def test_bad_date_skips(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings(d="кривая")))
        rec = AsyncMock(); monkeypatch.setattr(db, "event_recipients", rec)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 15)))
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
        ph = AsyncMock(); monkeypatch.setattr(scheduler.actions, "send_event_photos", ph)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 14)))  # за день
        assert n == 1
        assert tmpl.call_args.args[0] == scheduler.REMIND_1D_SCENARIO
        send.assert_awaited_once()
        # FOMO: T-1 прикрепляет фото с датой этого ивента (дедуп вар. B)
        ph.assert_awaited_once_with("wa_1", event_date="2026-08-15")

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
        ph = AsyncMock(); monkeypatch.setattr(scheduler.actions, "send_event_photos", ph)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 15)))  # день ивента
        assert n == 1
        # неоплатившему: Шаблон B + ссылка разрешена к повтору
        args, kwargs = send.call_args
        assert kwargs.get("allow_repeat_links") is True
        assert "con" in args[1][0]  # текст из Шаблона B
        # FOMO: неоплатившему в день ивента прикрепляем фото (с датой ивента)
        ph.assert_awaited_once_with("wa_1", event_date="2026-08-15")

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
        ph = AsyncMock(); monkeypatch.setattr(scheduler.actions, "send_event_photos", ph)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 15)))  # день ивента
        assert n == 1
        args, kwargs = send.call_args
        assert kwargs.get("allow_repeat_links") is False
        assert "sin" in args[1][0]  # текст из Шаблона A
        ph.assert_not_awaited()  # оплатившему медиа НЕ шлём (без лишнего спама)

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
        ph = AsyncMock(); monkeypatch.setattr(scheduler.actions, "send_event_photos", ph)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 16)))  # утро после ивента
        assert n == 1
        assert tmpl.call_args.args[0] == scheduler.CHECKIN_MORNING_SCENARIO
        send.assert_awaited_once()
        ph.assert_not_awaited()  # ивент прошёл — FOMO неприменим, медиа не шлём

    async def test_idempotent_skips_already_sent(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="x"))
        monkeypatch.setattr(db, "event_recipients",
                            AsyncMock(return_value=[{"phone": "wa_1", "whatsapp_name": None, "name": None}]))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=True))  # уже слали
        log = AsyncMock(); monkeypatch.setattr(db, "log_event_reminder", log)
        send = AsyncMock(); monkeypatch.setattr(sender, "send", send)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 15)))
        assert n == 0
        send.assert_not_awaited()
        log.assert_not_awaited()

    async def test_other_day_no_reminder(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=_event_settings()))
        rec = AsyncMock(); monkeypatch.setattr(db, "event_recipients", rec)
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 8, 10)))  # задолго до
        assert n == 0
        rec.assert_not_awaited()


# ===== tick =====

class TestTick:
    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        """Пауза снята, авто-рассылки включены — проверяем саму логику тика."""
        monkeypatch.setattr(scheduler, "_bot_paused", AsyncMock(return_value=False))
        monkeypatch.setattr(scheduler, "_scheduler_enabled", AsyncMock(return_value=True))

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
        assert scheduler._personalize("Hola [имя]!", "Carlos") == "Hola Carlos!"

    def test_no_name_drops_greeting_placeholder_cleanly(self):
        """Sin nombre → 'Hola!' limpio, NUNCA 'guapo' ni otro apodo genérico."""
        assert scheduler._personalize("Hola [имя]! 🤍 texto", None) == "Hola! 🤍 texto"

    def test_no_name_never_contains_guapo(self):
        assert "guapo" not in scheduler._personalize("Hola [имя]! 🤍 texto", None)

    def test_name_used_regardless_of_rung(self):
        """Ya no depende de 'rung' — el nombre se usa siempre que se conoce."""
        assert scheduler._personalize("Hola [имя]! 🤍", "Carlos") == "Hola Carlos! 🤍"


class TestFollowupPersonalized:
    async def test_followup_uses_name(self, monkeypatch):
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": "Carlos", "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola [имя]! 🤍 sigues?"))
        send = AsyncMock(return_value=1); monkeypatch.setattr(sender, "send", send)
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())
        await scheduler.run_followups()
        bubbles = send.call_args.args[1]
        assert any("Carlos" in b for b in bubbles)
        assert not any("guapo" in b for b in bubbles)

    async def test_followup_no_name_drops_greeting_cleanly(self, monkeypatch):
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": None, "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola [имя]! 🤍 sigues?"))
        send = AsyncMock(return_value=1); monkeypatch.setattr(sender, "send", send)
        monkeypatch.setattr(db, "mark_followup_sent", AsyncMock())
        await scheduler.run_followups()
        bubbles = send.call_args.args[1]
        assert not any("guapo" in b for b in bubbles)
        assert any("Hola! 🤍" in b for b in bubbles)


class TestFollowupAlerts:
    async def test_per_lead_failure_alerts(self, monkeypatch):
        lead = {"phone": "wa_1", "funnel_stage": "qualified", "followup_sent_count": 0,
                "whatsapp_name": None, "name": None}
        monkeypatch.setattr(db, "due_followups", AsyncMock(return_value=[lead]))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(side_effect=RuntimeError("db")))
        err = AsyncMock(); monkeypatch.setattr(scheduler.escalation, "notify_error", err)
        await scheduler.run_followups()
        err.assert_awaited_once()


class TestTickPause:
    """Глобальная пауза (тех. режим) → tick() ничего не шлёт проактивно."""

    async def test_paused_skips_tick(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "1"}))
        f = AsyncMock(); monkeypatch.setattr(scheduler, "run_followups", f)
        e = AsyncMock(); monkeypatch.setattr(scheduler, "run_event_reminders", e)
        v = AsyncMock(); monkeypatch.setattr(scheduler, "run_videocall_reminders", v)
        await scheduler.tick()
        f.assert_not_awaited(); e.assert_not_awaited(); v.assert_not_awaited()

    async def test_not_paused_runs_tick(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(
            return_value={"bot_paused": "0", "scheduler_enabled": "1"}))
        f = AsyncMock(); monkeypatch.setattr(scheduler, "run_followups", f)
        e = AsyncMock(); monkeypatch.setattr(scheduler, "run_event_reminders", e)
        v = AsyncMock(); monkeypatch.setattr(scheduler, "run_videocall_reminders", v)
        await scheduler.tick()
        f.assert_awaited_once(); e.assert_awaited_once(); v.assert_awaited_once()


# ===== Тумблер авто-рассылок scheduler_enabled (2026-10-15) =====

class TestSchedulerEnabledFlag:
    """Пока scheduler_enabled≠1 — планировщик не шлёт НИЧЕГО (даже без паузы)."""

    def _mocks(self, monkeypatch):
        f = AsyncMock(); e = AsyncMock(); v = AsyncMock()
        monkeypatch.setattr(scheduler, "run_followups", f)
        monkeypatch.setattr(scheduler, "run_event_reminders", e)
        monkeypatch.setattr(scheduler, "run_videocall_reminders", v)
        return f, e, v

    @pytest.mark.parametrize("value", [None, "0", "", "true", "yes"])
    async def test_disabled_skips_everything(self, monkeypatch, value):
        settings_map = {"bot_paused": "0"}
        if value is not None:
            settings_map["scheduler_enabled"] = value
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=settings_map))
        f, e, v = self._mocks(monkeypatch)
        await scheduler.tick()
        f.assert_not_awaited(); e.assert_not_awaited(); v.assert_not_awaited()

    async def test_read_failure_means_disabled(self, monkeypatch):
        """Сбой чтения флага → считаем выключенным (лучше не отправить, чем залп)."""
        monkeypatch.setattr(scheduler, "_bot_paused", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_settings", AsyncMock(side_effect=RuntimeError("db")))
        f, e, v = self._mocks(monkeypatch)
        await scheduler.tick()
        f.assert_not_awaited(); e.assert_not_awaited(); v.assert_not_awaited()

    async def test_pause_wins_over_enabled(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(
            return_value={"bot_paused": "1", "scheduler_enabled": "1"}))
        f, e, v = self._mocks(monkeypatch)
        await scheduler.tick()
        f.assert_not_awaited(); e.assert_not_awaited(); v.assert_not_awaited()


class TestFollowupSilenceGuard:
    async def test_max_silence_days_passed_to_query(self, monkeypatch):
        """Защита от залпа: в выборку догонов уходит лимит давности last_inbound_at."""
        q = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "due_followups", q)
        await scheduler.run_followups()
        assert q.call_args.kwargs["max_silence_days"] == scheduler.settings.followup_max_silence_days
        assert scheduler.settings.followup_max_silence_days >= 17  # лестница 2+5+10 дн. влезает


class TestPastEventNoReminders:
    """Прошедший ивент / event_active=0 → ни напоминаний, ни FOMO-медиа."""

    def _spy(self, monkeypatch):
        send = AsyncMock(return_value=1); photos = AsyncMock()
        recips = AsyncMock(return_value=[{"phone": "wa_1", "funnel_stage": "pitched"}])
        monkeypatch.setattr(sender, "send", send)
        monkeypatch.setattr(scheduler.actions, "send_event_photos", photos)
        monkeypatch.setattr(db, "event_recipients", recips)
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola"))
        monkeypatch.setattr(db, "event_reminder_sent", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "log_event_reminder", AsyncMock())
        monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset()))
        return send, photos, recips

    @pytest.mark.parametrize("days_after", [2, 3, 30])
    async def test_past_event_sends_nothing(self, monkeypatch, days_after):
        send, photos, recips = self._spy(monkeypatch)
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "event_active": "1", "event_date": "2026-10-14"}))
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 10, 14) + timedelta(days=days_after)))
        assert n == 0
        send.assert_not_awaited(); photos.assert_not_awaited(); recips.assert_not_awaited()

    @pytest.mark.parametrize("offset", [-1, 0, 1])
    async def test_inactive_event_sends_nothing(self, monkeypatch, offset):
        send, photos, recips = self._spy(monkeypatch)
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "event_active": "0", "event_date": "2026-10-14"}))
        n = await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 10, 14) + timedelta(days=offset)))
        assert n == 0
        send.assert_not_awaited(); photos.assert_not_awaited(); recips.assert_not_awaited()

    async def test_checkin_only_paid(self, monkeypatch):
        """T+1 check-in «cómo te fue ayer?» — только оплатившим/пришедшим, без медиа."""
        send, photos, recips = self._spy(monkeypatch)
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "event_active": "1", "event_date": "2026-10-14"}))
        await scheduler.run_event_reminders(now=_noon_cdmx(date(2026, 10, 15)))
        assert sorted(recips.call_args.kwargs["only_stages"]) == sorted(scheduler.PAID_STAGES)
        photos.assert_not_awaited()


# ===== Напоминания об ивенте: «сегодня/завтра» и окно отправки по Мехико =====

class TestEventRemindersMexicoTime:
    """Ивент 18.11.2026. Мехико = UTC-6: в 18:00 17.11 по Мехико в UTC уже 18.11,
    поэтому дату и окно 10:00-20:00 считаем по America/Mexico_City."""

    def _spy(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "event_active": "1", "event_date": "2026-11-18",
            "event_time": "8:30 PM", "event_address": "Roma Norte, CDMX"}))
        monkeypatch.setattr(db, "get_scenario_template", AsyncMock(return_value="Hola"))
        recips = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "event_recipients", recips)
        monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset()))
        return recips

    @staticmethod
    def _kind(recips):
        """Какое напоминание выбрано: kind в вызове event_recipients (или None)."""
        return recips.call_args.args[1] if recips.await_count else None

    @pytest.mark.parametrize("utc, expected", [
        (datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc), None),           # 16.11 17:00 MX
        (datetime(2026, 11, 17, 15, 59, tzinfo=timezone.utc), None),          # 17.11 09:59 MX, до окна
        (datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc), "remind_1d"),    # 17.11 10:00 MX
        (datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc), "remind_1d"),    # 17.11 18:30 MX, в UTC уже 18.11
        (datetime(2026, 11, 18, 1, 59, tzinfo=timezone.utc), "remind_1d"),    # 17.11 19:59 MX
        (datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc), None),            # 17.11 20:00 MX, окно закрыто
        (datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc), None),            # 18.11 00:00 MX, ночь
        (datetime(2026, 11, 18, 15, 59, tzinfo=timezone.utc), None),          # 18.11 09:59 MX
        (datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc), "remind_day"),   # 18.11 10:00 MX
        (datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc), "remind_day"),    # 18.11 19:00 MX, в UTC 19.11
        (datetime(2026, 11, 19, 16, 0, tzinfo=timezone.utc), "checkin_morning"),  # 19.11 10:00 MX
    ])
    async def test_kind_by_mexico_date_and_window(self, monkeypatch, utc, expected):
        recips = self._spy(monkeypatch)
        await scheduler.run_event_reminders(now=utc)
        assert self._kind(recips) == expected
        if expected:
            assert recips.call_args.args[2] == "2026-11-18"

    async def test_outside_window_does_not_read_settings(self, monkeypatch):
        """Вне окна — ранний выход, даже БД не трогаем."""
        gs = AsyncMock(); monkeypatch.setattr(db, "get_settings", gs)
        n = await scheduler.run_event_reminders(
            now=datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc))  # полночь по Мехико
        assert n == 0
        gs.assert_not_awaited()


# ===== Сводка «пауза» админам =====

class TestPauseDigest:
    NOW = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)

    def _setup(self, monkeypatch, last=None, rows=None, bypass=frozenset()):
        monkeypatch.setattr(db, "get_setting", AsyncMock(return_value=last))
        inbound = AsyncMock(return_value=rows or [])
        monkeypatch.setattr(db, "inbound_since", inbound)
        monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=bypass))
        setm = AsyncMock(); monkeypatch.setattr(db, "set_setting", setm)
        notify = AsyncMock(); monkeypatch.setattr(scheduler.escalation, "notify_admins", notify)
        return inbound, setm, notify

    async def test_zero_messages_no_spam(self, monkeypatch):
        inbound, setm, notify = self._setup(monkeypatch)
        assert await scheduler.run_pause_digest(now=self.NOW) == 0
        notify.assert_not_awaited()
        setm.assert_not_awaited()
        assert inbound.call_args.args[0] == self.NOW - timedelta(hours=1)

    async def test_sends_summary_and_remembers_time(self, monkeypatch):
        rows = [{"phone": "wa_5215512345678", "name": "Carlos", "n": 2},
                {"phone": "wa_5215587654321", "name": None, "n": 1}]
        _, setm, notify = self._setup(monkeypatch, rows=rows)
        assert await scheduler.run_pause_digest(now=self.NOW) == 2
        text = notify.call_args.args[0]
        assert "С прошлой сводки написали 2 чел. (3 сообщ.)" in text
        assert "• Carlos +5215512345678 (2)" in text
        assert "• +5215587654321 (1)" in text
        setm.assert_awaited_once_with(scheduler.PAUSE_DIGEST_KEY, self.NOW.isoformat())

    async def test_not_more_than_once_per_hour(self, monkeypatch):
        last = (self.NOW - timedelta(minutes=59)).isoformat()
        inbound, _, notify = self._setup(monkeypatch, last=last,
                                         rows=[{"phone": "wa_1", "name": None, "n": 1}])
        assert await scheduler.run_pause_digest(now=self.NOW) == 0
        inbound.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_window_starts_at_last_digest(self, monkeypatch):
        """Без зазоров: окно от прошлой сводки, даже если она была больше часа назад."""
        last = self.NOW - timedelta(minutes=95)
        inbound, _, _ = self._setup(monkeypatch, last=last.isoformat())
        await scheduler.run_pause_digest(now=self.NOW)
        assert inbound.call_args.args[0] == last

    async def test_bypass_numbers_excluded(self, monkeypatch):
        rows = [{"phone": "wa_5215512345678", "name": "Test", "n": 4}]
        _, _, notify = self._setup(monkeypatch, rows=rows,
                                   bypass=frozenset({"wa_5215512345678"}))
        assert await scheduler.run_pause_digest(now=self.NOW) == 0
        notify.assert_not_awaited()

    async def test_tick_runs_digest_only_when_paused(self, monkeypatch):
        digest = AsyncMock(); monkeypatch.setattr(scheduler, "run_pause_digest", digest)
        monkeypatch.setattr(scheduler, "_bot_paused", AsyncMock(return_value=True))
        f = AsyncMock(); monkeypatch.setattr(scheduler, "run_followups", f)
        await scheduler.tick()
        digest.assert_awaited_once()
        f.assert_not_awaited()

        digest.reset_mock()
        monkeypatch.setattr(scheduler, "_bot_paused", AsyncMock(return_value=False))
        monkeypatch.setattr(scheduler, "_scheduler_enabled", AsyncMock(return_value=False))
        await scheduler.tick()
        digest.assert_not_awaited()
