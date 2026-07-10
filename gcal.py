"""Google Calendar (автозапись видеозвонков #53) + Sheets (гостевой список) через сервис-аккаунт.

Один credentials-объект на оба API (scopes calendar + spreadsheets). googleapiclient
синхронный → публичные функции async и уходят в asyncio.to_thread (не блокируем loop).
Клиенты инициализируются лениво (первый вызов) — импорт модуля не требует настройки.

Календарь: create_event с conferenceData → НАСТОЯЩАЯ Google Meet ссылка. Таймзона
America/Mexico_City. Sheets: append_guest_row + авто-создание листа/заголовков.

Любой сбой пробрасывается — вызывающий (booking/main) ловит и уходит в фолбэк (эскалация Ане).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from config import settings

logger = logging.getLogger("matchmatch.gcal")

_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]
MEET_TZ = "America/Mexico_City"
DURATION_MIN = 30

# Гостевой список: лист + заголовки (создаются при первом append, если таблица пустая).
GUEST_SHEET = "Invitados"
GUEST_HEADERS = ["Nombre", "Teléfono", "Estado de pago", "Interés", "Fecha de registro"]

# Анкеты лидов (анкета-в-чате). Гибкая схема: базовые колонки + «Extra (JSON)» под будущие
# поля анкеты, которых мы пока не знаем — новые поля не потребуют менять структуру таблицы.
ANKETA_SHEET = "Solicitudes"
ANKETA_HEADERS = ["Nombre completo", "Email", "Teléfono", "Fecha de nacimiento", "Ciudad",
                  "País de origen", "LinkedIn/Negocio", "Edad deseada pareja", "Interés",
                  "Extra (JSON)", "Fecha de registro"]

_calendar = None
_sheets = None


def is_configured() -> bool:
    """Готова ли интеграция (есть id календаря/таблицы и путь к ключу)."""
    return bool(settings.google_service_account_file
                and (settings.google_calendar_id or settings.google_sheet_id))


def _ensure_clients() -> None:
    """Лениво построить клиентов Calendar/Sheets из сервис-аккаунта (один раз на процесс)."""
    global _calendar, _sheets
    if _calendar is not None:
        return
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_file, scopes=_SCOPES)
    _calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    _sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)


# ===== Calendar =====

async def is_slot_free(start: datetime, end: datetime) -> bool:
    """Свободен ли слот [start, end) в календаре (freebusy). tz-aware datetime."""
    return await asyncio.to_thread(_is_slot_free_sync, start, end)


def _is_slot_free_sync(start: datetime, end: datetime) -> bool:
    _ensure_clients()
    body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "items": [{"id": settings.google_calendar_id}]}
    resp = _calendar.freebusy().query(body=body).execute()
    busy = resp.get("calendars", {}).get(settings.google_calendar_id, {}).get("busy", [])
    return len(busy) == 0


async def create_event(summary: str, start: datetime, description: str = "") -> dict:
    """Создать событие (БЕЗ Meet — сервис-аккаунт не может генерить конференцию; ссылку
    шлёт Аня вручную). Вернуть {event_id, html_link}."""
    return await asyncio.to_thread(_create_event_sync, summary, start, description)


def _create_event_sync(summary: str, start: datetime, description: str) -> dict:
    _ensure_clients()
    end = start + timedelta(minutes=DURATION_MIN)
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": MEET_TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": MEET_TZ},
    }
    ev = _calendar.events().insert(calendarId=settings.google_calendar_id, body=body).execute()
    return {"event_id": ev["id"], "html_link": ev.get("htmlLink")}


async def patch_event(event_id: str, start: datetime) -> dict:
    """Перенести существующее событие на новое время (кейс «передумал»). Вернуть ссылки."""
    return await asyncio.to_thread(_patch_event_sync, event_id, start)


def _patch_event_sync(event_id: str, start: datetime) -> dict:
    _ensure_clients()
    end = start + timedelta(minutes=DURATION_MIN)
    body = {"start": {"dateTime": start.isoformat(), "timeZone": MEET_TZ},
            "end": {"dateTime": end.isoformat(), "timeZone": MEET_TZ}}
    ev = _calendar.events().patch(
        calendarId=settings.google_calendar_id, eventId=event_id, body=body).execute()
    return {"event_id": ev["id"], "html_link": ev.get("htmlLink")}


async def cancel_event(event_id: str) -> None:
    """Удалить событие (при отмене). Тихо игнорирует, если уже нет."""
    await asyncio.to_thread(_cancel_event_sync, event_id)


def _cancel_event_sync(event_id: str) -> None:
    _ensure_clients()
    try:
        _calendar.events().delete(
            calendarId=settings.google_calendar_id, eventId=event_id).execute()
    except Exception:
        logger.warning("cancel_event: не удалось удалить %s (возможно уже нет)", event_id)


# ===== Sheets (гостевой список) =====

async def append_guest_row(name: str, phone: str, status: str, interest: str,
                           registered: str) -> None:
    """Добавить строку гостя в лист Invitados (создаёт лист/заголовки, если пусто)."""
    await asyncio.to_thread(_append_guest_row_sync, [name, phone, status, interest, registered])


def _col_letter(n: int) -> str:
    """Номер колонки (1-based) → буква (1→A … 26→Z, 27→AA). Хватает на любую схему."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ensure_sheet_sync(title: str, headers: list) -> None:
    """Создать лист + строку заголовков, если листа/заголовков ещё нет. Идемпотентно."""
    ss = _sheets.spreadsheets()
    meta = ss.get(spreadsheetId=settings.google_sheet_id).execute()
    if title not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
        ss.batchUpdate(spreadsheetId=settings.google_sheet_id, body={
            "requests": [{"addSheet": {"properties": {"title": title}}}]}).execute()
    rng = f"{title}!A1:{_col_letter(len(headers))}1"
    if not ss.values().get(spreadsheetId=settings.google_sheet_id, range=rng).execute().get("values"):
        ss.values().update(spreadsheetId=settings.google_sheet_id, range=rng,
                           valueInputOption="RAW", body={"values": [headers]}).execute()


def _append_row_sync(title: str, headers: list, row: list) -> None:
    _ensure_clients()
    _ensure_sheet_sync(title, headers)
    last = _col_letter(len(headers))
    _sheets.spreadsheets().values().append(
        spreadsheetId=settings.google_sheet_id, range=f"{title}!A:{last}",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]}).execute()


def _append_guest_row_sync(row: list) -> None:
    _append_row_sync(GUEST_SHEET, GUEST_HEADERS, row)


async def append_anketa_row(name: str, email: str, phone: str, dob: str, city: str,
                            country: str, business: str, desired_age: str, interest: str,
                            extra_json: str, registered: str) -> None:
    """Добавить строку анкеты лида в лист Solicitudes (создаёт лист/заголовки, если пусто).

    Гибкая схема: базовые колонки + Extra(JSON) под будущие поля (не ломает структуру)."""
    await asyncio.to_thread(_append_row_sync, ANKETA_SHEET, ANKETA_HEADERS,
                            [name, email, phone, dob, city, country, business, desired_age,
                             interest, extra_json, registered])
