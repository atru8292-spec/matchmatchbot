"""Автозапись видеозвонка (#53): валидация времени + free/busy + создание события.

Полностью автоматическая логика (бот действует без Ани). Машина состояний с обработкой
всех рискованных сценариев: прошлое, вне рабочих часов, занятый слот, перенос («передумал»),
гонка двух лидов (advisory-lock), сбой Google → фолбэк на эскалацию Ане.

Чистые хелперы (parse/validate/fmt/message_for) тестируются без сети; resolve_and_book
делает I/O (Google + БД) и ловит сбои → Outcome.ERROR.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

import db
import gcal

logger = logging.getLogger("matchmatch.booking")

CDMX = ZoneInfo("America/Mexico_City")
WORK_START_MIN = 8 * 60    # 08:00
WORK_END_MIN = 22 * 60     # 22:00 (последний старт 21:30 при 30-мин слоте)
DURATION = timedelta(minutes=gcal.DURATION_MIN)

_ES_DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_ES_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre")


class Outcome(str, Enum):
    BOOKED = "booked"            # создано новое событие
    RESCHEDULED = "rescheduled"  # перенесено существующее (передумал)
    SAME = "same"               # то же время уже забронировано → переподтверждаем
    VAGUE = "vague"             # время не распознано → просим конкретное
    PAST = "past"               # время в прошлом
    OUT_OF_HOURS = "out_of_hours"  # вне 08:00–22:00 CDMX
    BUSY = "busy"               # слот занят
    ERROR = "error"             # сбой Google / не настроено → фолбэк на Аню


@dataclass
class Result:
    outcome: Outcome
    when: datetime | None = None
    link: str | None = None        # ссылка на событие в календаре (для Ани, лиду НЕ шлём)
    alt_when: datetime | None = None


# ===== чистые хелперы =====

def parse_proposed(iso) -> datetime | None:
    """ISO-строка от AI → tz-aware datetime в CDMX. Кривая/пустая → None."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=CDMX) if dt.tzinfo is None else dt.astimezone(CDMX)


def validate(dt: datetime, now: datetime) -> Outcome | None:
    """Проверить будущее + рабочие часы. Вернуть Outcome-ошибку или None если ок."""
    if dt <= now:
        return Outcome.PAST
    mod = dt.hour * 60 + dt.minute
    if mod < WORK_START_MIN or mod > WORK_END_MIN - gcal.DURATION_MIN:
        return Outcome.OUT_OF_HOURS
    return None


def fmt_es(dt: datetime) -> str:
    """'jueves 10 de julio a las 5:00 PM (hora de CDMX)'."""
    d = dt.astimezone(CDMX)
    hour = d.strftime("%I:%M %p").lstrip("0")
    return f"{_ES_DAYS[d.weekday()]} {d.day} de {_ES_MONTHS[d.month - 1]} a las {hour} (hora de CDMX)"


def message_for(res: Result) -> str:
    """Сообщение лиду по исходу (испанский, скупо на эмодзи, без тире)."""
    # Ссылку на звонок лиду НЕ шлём (её отправит Аня вручную) — только подтверждаем время.
    if res.outcome in (Outcome.BOOKED, Outcome.SAME):
        return (f"¡Perfecto! Te confirmo la videollamada: {fmt_es(res.when)}. "
                f"En un ratito te comparto el link para conectarte 🤍")
    if res.outcome == Outcome.RESCHEDULED:
        return (f"¡Listo, lo cambié! Tu videollamada queda para {fmt_es(res.when)}. "
                f"Te paso el link actualizado en un momento 🤍")
    if res.outcome == Outcome.VAGUE:
        return ("¡Claro! ¿Qué día y a qué hora exacta te queda para la videollamada? "
                "Atiendo de 8am a 10pm, hora de Ciudad de México 🤍")
    if res.outcome == Outcome.PAST:
        return "Esa fecha ya pasó guapo 🤍 ¿me das un día y hora a futuro para la videollamada?"
    if res.outcome == Outcome.OUT_OF_HOURS:
        return ("Atiendo videollamadas de 8am a 10pm, hora de Ciudad de México 🤍 "
                "¿qué hora dentro de ese rango te queda?")
    if res.outcome == Outcome.BUSY:
        if res.alt_when:
            return (f"Uy, justo esa hora ya está ocupada 🤍 ¿te queda {fmt_es(res.alt_when)}? "
                    "O dime otra hora que te acomode.")
        return ("Uy, esa hora ya está ocupada 🤍 ¿me das otra hora que te acomode? "
                "(de 8am a 10pm, hora de CDMX)")
    # ERROR — тёплый фолбэк (main дополнительно эскалирует Ане)
    return "Déjame confirmar el horario y te escribo en un ratito 🤍"


# ===== оркестратор (I/O) =====

async def _find_next_free(after: datetime, now: datetime) -> datetime | None:
    """Ближайший свободный 30-мин слот того же рабочего дня после `after` (или None)."""
    cur = after + DURATION
    for _ in range(12):
        mod = cur.hour * 60 + cur.minute
        if mod < WORK_START_MIN or mod > WORK_END_MIN - gcal.DURATION_MIN:
            return None  # вышли за рабочий день — проще попросить другой день
        if cur > now and await gcal.is_slot_free(cur, cur + DURATION):
            return cur
        cur += DURATION
    return None


async def resolve_and_book(lead: dict, proposed_iso, now: datetime) -> Result:
    """Полный цикл автозаписи. Никогда не бросает — сбой → Outcome.ERROR (фолбэк на Аню)."""
    dt = parse_proposed(proposed_iso)
    if dt is None:
        return Result(Outcome.VAGUE)
    bad = validate(dt, now)
    if bad:
        return Result(bad)
    if not gcal.is_configured():
        logger.warning("booking: google не настроен → ERROR (фолбэк)")
        return Result(Outcome.ERROR)

    try:
        pool = db._get_pool()
        slot_key = int(dt.timestamp()) // (gcal.DURATION_MIN * 60)  # 30-мин бакет, детерминир.
        async with pool.acquire() as conn:
            async with conn.transaction():
                # advisory-lock по слоту: сериализует одновременные брони на одно время (гонка)
                await conn.execute("SELECT pg_advisory_xact_lock($1)", slot_key)

                existing_id = lead.get("videocall_event_id")
                existing_at = lead.get("videocall_at")
                # идемпотентность: лид назвал ровно то же время, что уже забронировано
                if existing_id and existing_at and abs((existing_at - dt).total_seconds()) < 60:
                    return Result(Outcome.SAME, when=dt, link=lead.get("calendar_link"))

                if not await gcal.is_slot_free(dt, dt + DURATION):
                    alt = await _find_next_free(dt, now)
                    return Result(Outcome.BUSY, alt_when=alt)

                nombre = lead.get("name") or lead.get("whatsapp_name") or "Cliente"
                if existing_id:  # передумал → переносим существующее (без дубля)
                    ev = await gcal.patch_event(existing_id, dt)
                    outcome = Outcome.RESCHEDULED
                else:
                    ev = await gcal.create_event(
                        f"Videollamada MatchMatch — {nombre}", dt,
                        description="Videollamada de 30 min con MatchMatch.")
                    outcome = Outcome.BOOKED

                link = ev.get("html_link") or ""  # ссылка на событие в календаре (для Ани)
                await db.set_videocall_booking(lead["phone"], dt, ev["event_id"], link, conn=conn)
                return Result(outcome, when=dt, link=link)
    except Exception:
        logger.exception("resolve_and_book упал → ERROR (фолбэк на Аню)")
        return Result(Outcome.ERROR)
