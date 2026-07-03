"""Планировщик (блок 13): почасовой тик.

A) Follow-up молчунов — по next_followup_at, лестница funnel.FOLLOWUP_LADDER (2→5→10 дней,
   макс 3 попытки), тексты из сценариев 36/33/38. Не трогает whitelist/do_not_contact/
   финальные стадии (фильтрует db.due_followups).
B) Напоминания об ивенте — за день (сценарий 50) и в день (сценарий 47), адрес/время из
   app_settings. Идемпотентно (маркер в events по дате+типу) — не дублируем при рестарте.

Один in-process asyncio-таск (run_loop) стартует в lifespan, спит TICK_INTERVAL_SEC.
Каждый тик и каждый лид обёрнуты — сбой одного не рушит остальных; сбой тика → notify_error.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import db
import escalation
import funnel
import sender

logger = logging.getLogger("matchmatch.scheduler")

TICK_INTERVAL_SEC = 3600  # раз в час
FOLLOWUP_BATCH = 50       # лидов за тик (антибан: не всё разом)
EVENT_REMINDER_BATCH = 30 # получателей напоминаний за тик (антибан порциями)

# Ключи app_settings ивента.
EVENT_KEYS = ["event_active", "event_date", "event_time", "event_address"]
REMIND_1D_SCENARIO = 50   # «завтра ивент»
REMIND_DAY_SCENARIO = 47  # «сегодня ивент»


def _bubbles(template: str) -> list[str]:
    return [p.strip() for p in (template or "").split("\n\n") if p.strip()]


def _personalize(text: str, name: str | None, rung: int) -> str:
    """Подстановка имени в follow-up: [имя] → имя/guapo всегда; 'Hola guapo' → 'Hola {имя}'
    на 1-й и 3-й попытке (rung 0 и 2), если имя известно — чтобы не долбить 'guapo' каждый раз."""
    display = (name or "").strip()
    text = text.replace("[имя]", display or "guapo")
    if display and rung in (0, 2):
        text = text.replace("Hola guapo", f"Hola {display}")
    return text


def _fill_event(template: str, settings_map: dict) -> str:
    """Подставить время/адрес из app_settings в плейсхолдеры сценария."""
    addr = settings_map.get("event_address") or ""
    hora = settings_map.get("event_time") or ""
    return (template
            .replace("[hora]", hora)
            .replace("[dirección, lugar, parking]", addr)
            .replace("[dirección]", addr))


# ===== A) Follow-up =====

async def run_followups() -> int:
    """Разослать очередные фоллоу-апы. Вернуть число обработанных лидов."""
    leads = await db.due_followups(list(funnel.NO_FOLLOWUP_STAGES),
                                   funnel.MAX_FOLLOWUPS, limit=FOLLOWUP_BATCH)
    done = 0
    for ld in leads:
        phone = ld["phone"]
        try:
            count = ld["followup_sent_count"]
            if count >= len(funnel.FOLLOWUP_LADDER):
                continue  # подстраховка (query и так фильтрует < MAX)
            scenario_id, next_days = funnel.FOLLOWUP_LADDER[count]
            tmpl = await db.get_scenario_template(scenario_id)
            if not tmpl:
                logger.error("followup: нет template сценария %s", scenario_id)
                await escalation.notify_error("scheduler.followup", f"нет сценария {scenario_id}")
                continue
            name = ld.get("whatsapp_name") or ld.get("name")
            bubbles = [_personalize(b, name, count) for b in _bubbles(tmpl)]
            sent = await sender.send(phone, bubbles)
            if sent == 0:
                # Wazzup не принял (send не бросает, вернул 0) — НЕ жжём ступень лестницы,
                # оставляем next_followup_at как есть, повторим на следующем тике.
                logger.warning("followup: send=0 для %s — не помечаю, повтор позже", phone)
                continue
            next_at = (None if next_days is None
                       else datetime.now(timezone.utc) + timedelta(days=next_days))
            await db.mark_followup_sent(phone, next_at)
            done += 1
        except Exception as e:
            logger.exception("followup упал для %s", phone)
            await escalation.notify_error("scheduler.followup", repr(e), phone)
    if done:
        logger.info("followups: отправлено %d", done)
    return done


# ===== B) Напоминания об ивенте =====

async def run_event_reminders(today=None) -> int:
    """Разослать напоминания об ивенте, если сегодня T-1 или день ивента. Вернуть число."""
    s = await db.get_settings(EVENT_KEYS)
    if s.get("event_active") != "1":
        return 0
    date_str = s.get("event_date")
    if not date_str:
        return 0
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        logger.warning("event_date некорректен: %r — пропуск", date_str)
        return 0

    today = today or datetime.now(timezone.utc).date()
    if today == event_date - timedelta(days=1):
        return await _send_event_batch("remind_1d", REMIND_1D_SCENARIO, s, date_str)
    if today == event_date:
        return await _send_event_batch("remind_day", REMIND_DAY_SCENARIO, s, date_str)
    return 0


async def _send_event_batch(kind: str, scenario_id: int, settings_map: dict,
                            date_str: str) -> int:
    tmpl = await db.get_scenario_template(scenario_id)
    if not tmpl:
        logger.error("event reminder: нет template сценария %s", scenario_id)
        await escalation.notify_error("scheduler.event", f"нет сценария {scenario_id}")
        return 0
    text = _fill_event(tmpl, settings_map)
    bubbles = _bubbles(text)
    # LIMIT + батч (антибан: не всё разом). Остаток уйдёт на следующих тиках —
    # идемпотентность (маркер в events) не даст дублей уже отправленным.
    recipients = await db.event_recipients(list(funnel.EVENT_REMINDER_EXCLUDE_STAGES),
                                           limit=EVENT_REMINDER_BATCH)
    sent = 0
    for ld in recipients:
        phone = ld["phone"]
        try:
            if await db.event_reminder_sent(phone, kind, date_str):
                continue  # уже слали это напоминание на эту дату — идемпотентность
            ok = await sender.send(phone, bubbles)
            if ok == 0:
                continue  # Wazzup не принял — не логируем маркер, повторим на след. тике
            await db.log_event_reminder(phone, kind, date_str)
            sent += 1
        except Exception as e:
            logger.exception("event reminder %s упал для %s", kind, phone)
            await escalation.notify_error("scheduler.event", repr(e), phone)
    if sent:
        logger.info("event reminder %s: отправлено %d (дата %s)", kind, sent, date_str)
    return sent


# ===== Тик и цикл =====

async def tick() -> None:
    """Один проход планировщика. Сбой любой части → лог + technical-алерт, не роняет цикл."""
    try:
        await run_followups()
    except Exception as e:
        logger.exception("run_followups упал")
        await escalation.notify_error("scheduler.followups", repr(e))
    try:
        await run_event_reminders()
    except Exception as e:
        logger.exception("run_event_reminders упал")
        await escalation.notify_error("scheduler.events", repr(e))


async def run_loop() -> None:
    """Вечный цикл: тик сразу на старте (добьёт пропущенное за простой), затем каждый час.
    Останавливается отменой таска (CancelledError) в lifespan.shutdown."""
    while True:
        await tick()
        await asyncio.sleep(TICK_INTERVAL_SEC)
