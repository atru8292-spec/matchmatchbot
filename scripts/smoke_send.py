"""Smoke-тест отправки в Wazzup (Тест 1): напрямую sender.send, без AI и без входящего потока.

Проверяет что POST /v3/message реально доставляет сообщение в WhatsApp.
Номер и текст можно передать аргументами.

Запуск:
  ./venv/bin/python -m scripts.smoke_send
  ./venv/bin/python -m scripts.smoke_send wa_79635378880 "Hola prueba"
"""
from __future__ import annotations

import asyncio
import sys

import db
import sender


async def main() -> None:
    phone = sys.argv[1] if len(sys.argv) > 1 else "wa_79635378880"
    text = sys.argv[2] if len(sys.argv) > 2 else "Спасибо дура"
    await db.init_pool()
    try:
        # лид должен существовать (FK для save_outbound); создаём если нет
        await db.upsert_lead(phone, whatsapp_name="Smoke Test")
        print(f"отправляю лиду {phone}: {text!r}")
        sent = await sender.send(phone, [text])
        print(f"результат: отправлено {sent}/1", "✅" if sent == 1 else "❌ провал")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
