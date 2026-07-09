"""Патч прод-БД: сценарий №15 «Лид хочет только ивент, без подписки».

Что делает:
  - ai_allowed: False → True (был фикс-шаблон, стал адаптивным через AI);
  - template_es: старый фикс-текст → AI-гайд (явное честное сравнение
    «только ивент 9,000 vs подписка $1,400/мес c ивентом за 4,000», уклон в подписку,
    без давления, билет даётся всегда).

НЕ трогает: trigger_es (значит embedding НЕ пересчитывается — RAG-матч не меняется),
mode (остаётся bot_then_anna), другие сценарии.

Запуск: venv/bin/python -m scripts.patch_scenario_15_ai
Идемпотентен: повторный прогон просто перезапишет теми же значениями.
"""
import asyncio

import db

NEW_TEMPLATE_15 = (
    "[Guía para AI — el lead quiere SOLO el evento, sin membresía. Genera mensajes "
    "naturales en tono Anna siguiendo estos 4 puntos, NO copies literal. Máximo 4 "
    "mensajes; el enlace va en el último. Escribe LITERAL los tokens [event_date], "
    "[event_time], [event_address], [event_link] — se rellenan solos, NO inventes "
    "fecha/hora/dirección.]\n\n"
    "[1] [Confirma con calidez que SÍ puede venir solo al evento, sin membresía — es "
    "válido, no lo rechaces. Menciona el evento Slavic Latino Night: fecha [event_date], "
    "hora [event_time], en [event_address]. Toca ligero la exclusividad (los eventos "
    "pasados se agotaron, cupo limitado).]\n\n"
    "[2] [Compara los precios claro y honesto, sin esconder nada: solo el evento son "
    "9,000 MXN una sola vez. Con la membresía ($1,400 USD/mes) ese mismo evento te sale "
    "en 4,000 MXN — 5,000 menos — y además cada mes te presento 3 mujeres eslavas según "
    "tus preferencias, acceso a la base de 3,000+, y todos los eventos siguientes también "
    "a precio de miembro. Deja claro que la membresía no es 'pagar 4,000': 4,000 es el "
    "precio de miembro para el evento DENTRO de la membresía de $1,400/mes.]\n\n"
    "[3] [Inclina con suavidad hacia la membresía, sin presión: si tu objetivo no es solo "
    "salir una noche sino de verdad encontrar pareja, la membresía es más efectiva — no "
    "dependes de un solo evento, cada mes hay presentaciones personales y no esperas a la "
    "siguiente fecha. Como un buen consejo, no como venta agresiva.]\n\n"
    "[4] [Pregunta directo y sin presión: ¿te gustaría que platiquemos de la membresía, o "
    "prefieres por ahora solo el boleto de este evento? Ambas opciones están bien. En "
    "cualquier caso pásale el token literal [event_link] para reservar y ver fotos, videos "
    "y reviews de eventos pasados.]"
)


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        before = await db.get_scenario_row(15)
        print(f"ДО:  ai_allowed={before['ai_allowed']}  "
              f"template_es[:40]={before['template_es'][:40]!r}")
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, ai_allowed=true, updated_at=now() "
            "WHERE id=15",
            NEW_TEMPLATE_15,
        )
        after = await db.get_scenario_row(15)
        print(f"ПОСЛЕ: ai_allowed={after['ai_allowed']}  "
              f"template_es[:40]={after['template_es'][:40]!r}")
        assert after["ai_allowed"] is True, "ai_allowed не переключился!"
        assert after["template_es"] == NEW_TEMPLATE_15, "template_es не совпал!"
        print("✓ Патч #15 применён (template_es + ai_allowed=True). trigger_es/embedding не тронуты.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
