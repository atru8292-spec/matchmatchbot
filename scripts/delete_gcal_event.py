"""Удалить событие из Google Calendar по event_id — ЗАПУСКАТЬ НА СЕРВЕРЕ (где ключ + .env).

Нужен для чистки тестовых событий (например после scripts/verify_google.py), когда
владелец календаря (Аня) не может зайти и удалить вручную сам, но сервис-аккаунт
имеет право "Make changes to events".

Запуск из /opt/matchmatch-bot:  venv/bin/python scripts/delete_gcal_event.py <event_id>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gcal
from config import settings


def _check_still_exists(event_id: str) -> bool:
    """True si el evento SIGUE existiendo (delete no confirmado por gcal.cancel_event,
    que traga cualquier excepción — aquí verificamos de verdad)."""
    gcal._ensure_clients()
    try:
        ev = gcal._calendar.events().get(
            calendarId=settings.google_calendar_id, eventId=event_id).execute()
        return ev.get("status") != "cancelled"
    except Exception:
        return False  # 404/410 у Google API → ya no existe


async def main(event_id: str) -> None:
    print(f"→ удаляю событие {event_id} из календаря {settings.google_calendar_id}...")
    await gcal.cancel_event(event_id)
    if _check_still_exists(event_id):
        print("✗ ВНИМАНИЕ: событие всё ещё существует — удаление НЕ прошло, проверь доступ вручную")
        sys.exit(1)
    print("✓ подтверждено: события больше нет в календаре")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: venv/bin/python scripts/delete_gcal_event.py <event_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
