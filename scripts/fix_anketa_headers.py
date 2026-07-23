"""Перезаписать строку заголовков вкладки Solicitudes на актуальные (gcal.ANKETA_HEADERS).

Нужен один раз: _ensure_sheet_sync пишет заголовки только если строка пустая, поэтому
смена ANKETA_HEADERS в коде (испанский → русский) не трогает уже созданную вкладку —
там остаются старые заголовки. Этот скрипт форсит перезапись независимо от текущего
содержимого строки 1.

Запуск из /opt/matchmatch-bot:  venv/bin/python scripts/fix_anketa_headers.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gcal
from config import settings


def _force_write_headers() -> None:
    gcal._ensure_clients()
    rng = f"{gcal.ANKETA_SHEET}!A1:{gcal._col_letter(len(gcal.ANKETA_HEADERS))}1"
    gcal._sheets.spreadsheets().values().update(
        spreadsheetId=settings.google_sheet_id, range=rng,
        valueInputOption="RAW", body={"values": [gcal.ANKETA_HEADERS]}).execute()


async def main() -> None:
    print(f"→ перезаписываю заголовки вкладки «{gcal.ANKETA_SHEET}»: {gcal.ANKETA_HEADERS}")
    await asyncio.to_thread(_force_write_headers)
    print("✓ готово")


if __name__ == "__main__":
    asyncio.run(main())
