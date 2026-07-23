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


async def main(event_id: str) -> None:
    print(f"→ удаляю событие {event_id} из календаря {os.environ.get('GOOGLE_CALENDAR_ID', '')}...")
    await gcal.cancel_event(event_id)
    print("✓ готово (или события уже не было — cancel_event тихо игнорирует это)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: venv/bin/python scripts/delete_gcal_event.py <event_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
