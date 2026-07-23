"""Патч прод-БД: сценарий №15 «Лид хочет только ивент, без подписки» — порядок шагов.

Что делает (следует за patch_scenario_15_bot_auto_2026_07.py в тот же день):
  - меняет порядок в template_es: ЦЕННОСТЬ (детали ивента, без цены) → КВАЛИФИКАЦИЯ
    (soltero+возраст) → ЦЕНА (после квалификации, ИЛИ сразу если лид сам попросил
    цену раньше). Раньше квалификация шла ПЕРЕД любыми деталями ивента — решили,
    что сперва зацепить ценностью лучше конвертит + данные лида (возраст/soltero)
    сохраняются в extracted независимо от того, купит он билет или нет.
  - mode остаётся bot_auto (не трогаем).

НЕ трогает: trigger_es (embedding/RAG-матч не меняется), ai_allowed, mode.

Запуск: venv/bin/python -m scripts.patch_scenario_15_value_first_2026_07
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
    "[1] [Confirma con calidez que SÍ puede venir solo al evento — es válido, no lo "
    "rechaces. Da el GANCHO de valor: menciona el Slavic Latino Night, [event_date], "
    "[event_time], en [event_address], toca ligero la exclusividad (eventos pasados "
    "agotados, cupo limitado). NO des el precio todavía en este paso — primero engancha "
    "con el valor.]\n\n"
    "[2] [Justo después (mismo mensaje si fluye natural, o el siguiente turno), "
    "califícalo como a cualquier lead nuevo: revisa lead_profile — si aún NO sabes si es "
    "soltero y su edad, pregúntalo con calidez antes de dar el precio (edad 28-65, "
    "soltero). Si ya lo sabes, o el lead ya lo dijo en este mismo mensaje, pasa directo "
    "al paso 3.]\n\n"
    "[3] [Dile el precio: [event_price_nonmember] MXN[event_promo] una sola vez (precio "
    "especial con descuento), incluye bebida de bienvenida, entrantes y conocer mujeres "
    "eslavas solteras que buscan algo serio. Da el precio SOLO cuando ya calificaste "
    "(soltero+edad) — EXCEPCIÓN: si el lead pregunta el precio directamente antes de que "
    "tú preguntes, dáselo de inmediato aunque aún no hayas calificado (nunca te niegues a "
    "dar el precio del evento a quien lo pide).]\n\n"
    "[4] [Con suavidad y sin presión, menciona que además del evento tienes un servicio "
    "de matchmaking personal (acompañamiento a tu medida, presentaciones seleccionadas a "
    "mano) por si su objetivo no es solo una noche sino de verdad encontrar pareja — los "
    "detalles y planes se ven en una videollamada. Como consejo, no como venta agresiva. "
    "Pregunta directo y sin presión: ¿quiere el boleto de este evento, o prefiere que "
    "platiquemos del servicio en una videollamada? Ambas están bien. En cualquier caso "
    "pásale el token [event_link] para reservar y ver fotos, videos y reviews de eventos "
    "pasados.]"
)


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        before = await db.get_scenario_row(15)
        print(f"ДО:  mode={before['mode']}  template_es[:40]={before['template_es'][:40]!r}")
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=15",
            NEW_TEMPLATE_15,
        )
        after = await db.get_scenario_row(15)
        print(f"ПОСЛЕ: mode={after['mode']}  template_es[:40]={after['template_es'][:40]!r}")
        assert after["mode"] == "bot_auto", "mode неожиданно изменился!"
        assert after["template_es"] == NEW_TEMPLATE_15, "template_es не совпал!"
        print("✓ Патч #15 применён (порядок: ценность → квалификация → цена). "
              "trigger_es/mode не тронуты.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
