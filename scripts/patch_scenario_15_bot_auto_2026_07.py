"""Патч прод-БД: сценарий №15 «Лид хочет только ивент, без подписки».

Что делает:
  - mode: bot_then_anna → bot_auto (владелец пересмотрел решение — бот ведёт лида
    сам, без передачи Ане; квалификация должна происходить внутри самого бота);
  - template_es: добавлен шаг [0] — квалификация (soltero + возраст) ПЕРЕД тем как
    давать детали/цену ивента, как у любого нового лида. Раньше гайд сразу давал
    цену/ссылку, полагаясь на то, что дальше Аня сама разберётся при эскалации —
    без эскалации это означало вообще без единой проверки.

НЕ трогает: trigger_es (embedding не пересчитывается — RAG-матч не меняется),
ai_allowed (остаётся True), другие сценарии.

Запуск: venv/bin/python -m scripts.patch_scenario_15_bot_auto_2026_07
Идемпотентен: повторный прогон просто перезапишет теми же значениями.
"""
import asyncio

import db

NEW_TEMPLATE_15 = (
    "[Guía para AI — el lead quiere SOLO el evento, sin el servicio completo. Genera "
    "mensajes naturales en tono Anna, NO copies literal. Máximo 4 mensajes; el enlace "
    "va en el último. Escribe LITERAL los tokens [event_date], [event_time], "
    "[event_address], [event_price_nonmember], [event_link] — se rellenan solos, NO "
    "inventes.]\n\n"
    "[0] [ANTES de dar precio/detalles del evento: revisa lead_profile. Si aún NO sabes "
    "si es soltero y su edad, califícalo primero — igual que a cualquier lead nuevo, no "
    "te saltes el filtro solo porque pide el evento (edad 28-65, soltero). Pregúntalo con "
    "calidez, sin dar precio todavía, y espera su respuesta antes de seguir. Si ya lo "
    "sabes (o el lead ya lo dijo en este mismo mensaje), pasa directo al paso 1.]\n\n"
    "[1] [Confirma con calidez que SÍ puede venir solo al evento — es válido, no lo "
    "rechaces. Menciona el Slavic Latino Night: [event_date], [event_time], en "
    "[event_address]. Toca ligero la exclusividad (eventos pasados agotados, cupo "
    "limitado).]\n\n"
    "[2] [Dile claro el precio: [event_price_nonmember] MXN[event_promo] una sola vez "
    "(precio especial con descuento), incluye bebida de bienvenida, entrantes y conocer "
    "mujeres eslavas solteras que buscan algo serio.]\n\n"
    "[3] [Con suavidad y sin presión, menciona que además del evento tienes un servicio "
    "de matchmaking personal (acompañamiento a tu medida, presentaciones seleccionadas a "
    "mano) por si su objetivo no es solo una noche sino de verdad encontrar pareja — los "
    "detalles y planes se ven en una videollamada. Como consejo, no como venta agresiva.]"
    "\n\n"
    "[4] [Pregunta directo y sin presión: ¿quiere el boleto de este evento, o prefiere "
    "que platiquemos del servicio en una videollamada? Ambas están bien. En cualquier "
    "caso pásale el token [event_link] para reservar y ver fotos, videos y reviews de "
    "eventos pasados.]"
)


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        before = await db.get_scenario_row(15)
        print(f"ДО:  mode={before['mode']}  template_es[:40]={before['template_es'][:40]!r}")
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, mode='bot_auto', updated_at=now() "
            "WHERE id=15",
            NEW_TEMPLATE_15,
        )
        after = await db.get_scenario_row(15)
        print(f"ПОСЛЕ: mode={after['mode']}  template_es[:40]={after['template_es'][:40]!r}")
        assert after["mode"] == "bot_auto", "mode не переключился!"
        assert after["template_es"] == NEW_TEMPLATE_15, "template_es не совпал!"
        print("✓ Патч #15 применён (mode=bot_auto + гайд с квалификацией). "
              "trigger_es/embedding не тронуты.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
