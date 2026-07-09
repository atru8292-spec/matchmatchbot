"""Живая проверка Google-интеграции — ЗАПУСКАТЬ НА СЕРВЕРЕ (где ключ + .env).

Делает реальные вызовы API:
  1) создаёт тестовое событие «завтра 17:00 CDMX» с настоящей Google Meet ссылкой
     в тестовом календаре;
  2) добавляет тестовую строку в лист «Invitados» тестовой таблицы.
Печатает результат обоих действий. Тестовые календарь/таблица — прод не трогается.
Событие потом можно удалить в календаре вручную.

Запуск из /opt/matchmatch-bot:  venv/bin/python scripts/verify_google.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Корень репо в sys.path — чтобы import gcal/config работал при запуске файлом
# (python scripts/verify_google.py кладёт на path папку скрипта, не корень репо).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gcal
from config import settings

CDMX = ZoneInfo("America/Mexico_City")


async def main() -> None:
    print("=" * 60)
    print("ПРОВЕРКА GOOGLE-ИНТЕГРАЦИИ")
    print("=" * 60)
    print("key file    :", settings.google_service_account_file)
    print("calendar_id :", settings.google_calendar_id or "⚠ ПУСТО")
    print("sheet_id    :", settings.google_sheet_id or "⚠ ПУСТО")
    print()

    # 1) Calendar — тестовое событие завтра в 17:00 CDMX
    start = (datetime.now(CDMX) + timedelta(days=1)).replace(
        hour=17, minute=0, second=0, microsecond=0)
    print(f"→ CALENDAR: создаю тестовое событие (БЕЗ Meet) на {start.strftime('%A %d.%m.%Y %H:%M')} CDMX")
    try:
        free = await gcal.is_slot_free(start, start + timedelta(minutes=30))
        print("  слот свободен (freebusy):", free)
        ev = await gcal.create_event(
            "TEST · Videollamada MatchMatch (verify)", start,
            description="Проверка интеграции бота. Можно удалить.")
        print("  ✓ event_id  :", ev["event_id"])
        print("  ✓ event link:", ev.get("html_link"), "(Meet-ссылку лиду отправляет Аня вручную)")
    except Exception as e:
        print("  ✗ CALENDAR ОШИБКА:", repr(e))

    print()

    # 2) Sheets — тестовая строка в Invitados
    print("→ SHEETS: добавляю тестовую строку в лист «Invitados»")
    try:
        now = datetime.now(CDMX).strftime("%Y-%m-%d %H:%M")
        await gcal.append_guest_row("TEST Diego (verify)", "+5215500000004", "Pagado", "event", now)
        print("  ✓ строка добавлена (лист/заголовки создаются автоматически, если было пусто)")
    except Exception as e:
        print("  ✗ SHEETS ОШИБКА:", repr(e))

    print()
    print("ГОТОВО. Проверь: событие с Meet-ссылкой в календаре + строка в таблице.")
    print("Тестовое событие можно удалить вручную.")


if __name__ == "__main__":
    asyncio.run(main())
