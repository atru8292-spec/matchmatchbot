"""Разово: отправить лиду продолжение вручную (после escalate) + вернуть mode='auto'.

Нужен когда бот застрял на переходной фразе (bot_then_anna escalate) из-за ложного
срабатывания сценария, и лид не получил реальную информацию — шлём нормальное
продолжение и снимаем ручной режим, чтобы бот снова отвечал сам.

Запуск (на сервере, где реальный WAZZUP_CHANNEL_ID):
  venv/bin/python -m scripts.resume_lead_manual <phone_digits> "msg1" ["msg2" ...]

Пример:
  venv/bin/python -m scripts.resume_lead_manual 79150981866 \
    "Perdón la tardanza guapo! Justo esto es lo que quería contarte del evento 🤍" \
    "Es nuestro Slavic Latino Night, este [event_date] a las [event_time], en [event_address]. El boleto cuesta [event_price_nonmember] MXN[event_promo], incluye bebida de bienvenida, entrantes y la oportunidad de conocer mujeres eslavas solteras que buscan algo serio." \
    "¿Te gustaría el boleto para este evento, o prefieres que platiquemos primero de mi servicio personalizado en una videollamada?"
"""
from __future__ import annotations

import asyncio
import sys

import db
import sender


async def main() -> None:
    if len(sys.argv) < 3:
        print("Использование: venv/bin/python -m scripts.resume_lead_manual <phone_digits> \"msg1\" [\"msg2\" ...]")
        sys.exit(1)
    phone = "wa_" + "".join(c for c in sys.argv[1] if c.isdigit())
    messages = sys.argv[2:]

    await db.init_pool()
    try:
        lead = await db.get_lead_by_phone(phone)
        if not lead:
            print(f"✗ лид {phone} не найден — ничего не отправляю")
            sys.exit(1)
        print(f"→ отправляю {len(messages)} сообщение(й) лиду {phone} (mode было: {lead.get('mode')})")
        sent = await sender.send(phone, messages)
        print(f"  отправлено {sent}/{len(messages)}")
        await db.update_lead_fields(phone, mode="auto")
        after = await db.get_lead_by_phone(phone)
        print(f"✓ mode восстановлен: {after.get('mode')}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
