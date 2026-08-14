"""Автозапись видеозвонка (#53): валидация времени + расписание троих + создание события.

Полностью автоматическая логика (бот действует без Ани). Машина состояний с обработкой
всех рискованных сценариев: прошлое, час не покрыт ничьим расписанием, слот занят,
перенос («передумал»), гонка двух лидов (advisory-lock), сбой Google → фолбэк на эскалацию.

Расписание троих (Аня/Мила/Рита) — свой часовой пояс + часы «от-до» на брата, настраивается
в мини-CRM (app_settings, ключи assignee_<slug>_{tz,start,end}), без миграции схемы. Дефолт
(пока никто не настроил) — старое поведение: все трое CDMX 07:00–14:00.

Чистые хелперы (parse/fmt/message_for/_in_window/_hours_text) тестируются без сети;
resolve_and_book делает I/O (Google + БД) и ловит сбои → Outcome.ERROR.
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
DURATION = timedelta(minutes=gcal.DURATION_MIN)

_ES_DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_ES_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre")

# Расписание por defecto — hasta que se configure en el mini-CRM (mismo comportamiento que
# antes de tener las 3 agendas independientes: CDMX 07:00–14:00 para las 3).
_DEFAULT_TZ = "America/Mexico_City"
_DEFAULT_START = "07:00"
_DEFAULT_END = "14:00"

# nombre → slug de las claves en app_settings (ver mini_api.py /assignees)
_SLUGS = {"Аня": "anya", "Мила": "mila", "Рита": "rita"}
SCHEDULE_KEYS = [
    key
    for name in _SLUGS.values()
    for key in (f"assignee_{name}_tz", f"assignee_{name}_start", f"assignee_{name}_end")
]


class Outcome(str, Enum):
    BOOKED = "booked"            # создано новое событие
    RESCHEDULED = "rescheduled"  # перенесено существующее (передумал)
    SAME = "same"               # то же время уже забронировано → переподтверждаем
    VAGUE = "vague"             # время не распознано → просим конкретное
    PAST = "past"               # время в прошлом
    OUT_OF_HOURS = "out_of_hours"  # час не покрыт ничьим расписанием
    BUSY = "busy"               # слот занят (все, кто покрывает этот час, уже заняты)
    ERROR = "error"             # сбой Google / не настроено → фолбэк на Аню


@dataclass
class Result:
    outcome: Outcome
    when: datetime | None = None
    link: str | None = None        # ссылка на событие в календаре (для Ани, лиду НЕ шлём)
    alt_when: datetime | None = None
    assignee: str | None = None    # кто ведёт (Аня/Мила/Рита) — только при BOOKED/RESCHEDULED
    hours_text: str | None = None  # «7am a 2pm» реальное окно (из расписаний), для VAGUE/OUT_OF_HOURS


# ===== расписание троих =====

def _parse_hhmm(s: str) -> tuple[int, int]:
    """'7:30' → (7, 30). Кривое значение в БД (ручной ввод) → дефолт 07:00, не падаем."""
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except Exception:
        return 7, 0


def _zone(tz_str: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return CDMX  # битый/пустой tz из БД → не падаем, считаем как CDMX


async def _load_schedules() -> dict[str, tuple[str, str, str]]:
    """{'Аня': (tz, start, end), ...}. Сбой БД / незаполненное → дефолты (старое поведение)."""
    try:
        s = await db.get_settings(SCHEDULE_KEYS)
    except Exception:
        logger.exception("booking: не смог прочитать расписания, использую дефолты")
        s = {}
    out = {}
    for name, slug in _SLUGS.items():
        out[name] = (
            s.get(f"assignee_{slug}_tz") or _DEFAULT_TZ,
            s.get(f"assignee_{slug}_start") or _DEFAULT_START,
            s.get(f"assignee_{slug}_end") or _DEFAULT_END,
        )
    return out


def _in_window(dt: datetime, tz_str: str, start_hhmm: str, end_hhmm: str) -> bool:
    """dt (tz-aware, cualquier zona) cae dentro de [start, end) en SU horario local — la
    conversión usa la fecha real de dt, así que el horario de verano se resuelve solo."""
    local = dt.astimezone(_zone(tz_str))
    mod = local.hour * 60 + local.minute
    sh, sm = _parse_hhmm(start_hhmm)
    eh, em = _parse_hhmm(end_hhmm)
    return sh * 60 + sm <= mod < eh * 60 + em


def _covering_names(dt: datetime, schedules: dict[str, tuple[str, str, str]]) -> list[str]:
    """Quiénes (de las 3) tienen ese horario dentro de SU disponibilidad configurada."""
    return [name for name, (tz, s, e) in schedules.items() if _in_window(dt, tz, s, e)]


def _fmt_hour12(h: int, m: int) -> str:
    ampm = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{ampm}" if m else f"{h12}{ampm}"


def _hours_text(schedules: dict[str, tuple[str, str, str]], ref: datetime) -> str:
    """Ventana global (unión) para el mensaje al lead — min inicio / max fin de las 3,
    convertido a CDMX en la fecha `ref` (aproximado: no repite día si cruza medianoche,
    suficiente para el texto informativo; la validación real usa datetime completo)."""
    starts_cdmx: list[int] = []
    ends_cdmx: list[int] = []
    for tz_str, start_hhmm, end_hhmm in schedules.values():
        tz = _zone(tz_str)
        sh, sm = _parse_hhmm(start_hhmm)
        eh, em = _parse_hhmm(end_hhmm)
        local_start = ref.astimezone(tz).replace(hour=sh, minute=sm, second=0, microsecond=0)
        local_end = ref.astimezone(tz).replace(hour=eh, minute=em, second=0, microsecond=0)
        start_cdmx = local_start.astimezone(CDMX)
        end_cdmx = local_end.astimezone(CDMX)
        starts_cdmx.append(start_cdmx.hour * 60 + start_cdmx.minute)
        ends_cdmx.append(end_cdmx.hour * 60 + end_cdmx.minute)
    lo, hi = min(starts_cdmx), max(ends_cdmx)
    return f"{_fmt_hour12(lo // 60, lo % 60)} a {_fmt_hour12(hi // 60, hi % 60)}"


# ===== otros hexlpers puros =====

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
    """Единственная быстрая синхронная проверка (без БД): время в будущем. Покрытие
    расписаниями троих проверяется отдельно (_covering_names — нужна БД)."""
    if dt <= now:
        return Outcome.PAST
    return None


def fmt_es(dt: datetime) -> str:
    """'jueves 10 de julio a las 5:00 PM (hora de CDMX)'."""
    d = dt.astimezone(CDMX)
    hour = d.strftime("%I:%M %p").lstrip("0")
    return f"{_ES_DAYS[d.weekday()]} {d.day} de {_ES_MONTHS[d.month - 1]} a las {hour} (hora de CDMX)"


def message_for(res: Result) -> str:
    """Сообщение лиду по исходу (испанский, скупо на эмодзи, без тире)."""
    hours = res.hours_text or "7am a 2pm"
    # Ссылку на звонок лиду НЕ шлём (её отправит la responsable вручную) — только подтверждаем время.
    if res.outcome in (Outcome.BOOKED, Outcome.SAME):
        return (f"¡Perfecto! Te confirmo la videollamada: {fmt_es(res.when)}. "
                f"En un ratito te comparto el link para conectarte 🤍")
    if res.outcome == Outcome.RESCHEDULED:
        return (f"¡Listo, lo cambié! Tu videollamada queda para {fmt_es(res.when)}. "
                f"Te paso el link actualizado en un momento 🤍")
    if res.outcome == Outcome.VAGUE:
        return (f"¡Claro! ¿Qué día y a qué hora exacta te queda para la videollamada? "
                f"Atiendo de {hours}, hora de Ciudad de México")
    if res.outcome == Outcome.PAST:
        return "Esa fecha ya pasó, ¿me das un día y hora a futuro para la videollamada?"
    if res.outcome == Outcome.OUT_OF_HOURS:
        return (f"Atiendo videollamadas de {hours}, hora de Ciudad de México. "
                f"¿qué hora dentro de ese rango te queda?")
    if res.outcome == Outcome.BUSY:
        if res.alt_when:
            return (f"Uy, justo esa hora ya está ocupada, ¿te queda {fmt_es(res.alt_when)}? "
                    "O dime otra hora que te acomode.")
        return (f"Uy, esa hora ya está ocupada, ¿me das otra hora que te acomode? "
                f"(de {hours}, hora de CDMX)")
    # ERROR — тёплый фолбэк (main дополнительно эскалирует Ане)
    return "Déjame confirmar el horario y te escribo en un ratito 🤍"


# ===== оркестратор (I/O) =====

async def _pick_assignee(dt: datetime, schedules: dict[str, tuple[str, str, str]]
                         ) -> tuple[str, str] | None:
    """Primer responsable (orden Аня→Мила→Рита) que CUBRE ese horario (según su propia
    agenda) y AÚN está libre en el calendario para ese slot exacto. None si nadie aplica."""
    covering = set(_covering_names(dt, schedules))
    if not covering:
        return None
    taken = await gcal.slot_taken_names(dt, dt + DURATION)
    for name, color_id in gcal.ASSIGNEES:
        if name in covering and name not in taken:
            return name, color_id
    return None


async def _find_next_free(after: datetime, now: datetime,
                          schedules: dict[str, tuple[str, str, str]]) -> datetime | None:
    """Ближайший слот (hasta 10h adelante) con alguien que lo cubra y esté libre — o None."""
    cur = after + DURATION
    for _ in range(20):
        if cur > now and await _pick_assignee(cur, schedules) is not None:
            return cur
        cur += DURATION
    return None


async def resolve_and_book(lead: dict, proposed_iso, now: datetime) -> Result:
    """Полный цикл автозаписи. Никогда не бросает — сбой → Outcome.ERROR (фолбэк)."""
    dt = parse_proposed(proposed_iso)
    if dt is None:
        schedules = await _load_schedules()
        return Result(Outcome.VAGUE, hours_text=_hours_text(schedules, now))
    bad = validate(dt, now)
    if bad:
        return Result(bad)

    schedules = await _load_schedules()
    if not _covering_names(dt, schedules):
        return Result(Outcome.OUT_OF_HOURS, hours_text=_hours_text(schedules, now))

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

                assignee = await _pick_assignee(dt, schedules)
                if assignee is None:
                    alt = await _find_next_free(dt, now, schedules)
                    return Result(Outcome.BUSY, alt_when=alt,
                                 hours_text=_hours_text(schedules, now))
                assignee_name, color_id = assignee

                nombre = lead.get("name") or lead.get("whatsapp_name") or "Cliente"
                summary = f"Videollamada MatchMatch — {nombre} [{assignee_name}]"
                if existing_id:  # передумал → переносим существующее (без дубля)
                    ev = await gcal.patch_event(existing_id, dt, summary=summary, color_id=color_id)
                    outcome = Outcome.RESCHEDULED
                else:
                    ev = await gcal.create_event(
                        summary, dt, description="Videollamada de 30 min con MatchMatch.",
                        color_id=color_id)
                    outcome = Outcome.BOOKED

                link = ev.get("html_link") or ""  # ссылка на событие в календаре (для Ани)
                await db.set_videocall_booking(lead["phone"], dt, ev["event_id"], link,
                                               assignee=assignee_name, conn=conn)
                return Result(outcome, when=dt, link=link, assignee=assignee_name)
    except Exception:
        logger.exception("resolve_and_book упал → ERROR (фолбэк на Аню)")
        return Result(Outcome.ERROR)
