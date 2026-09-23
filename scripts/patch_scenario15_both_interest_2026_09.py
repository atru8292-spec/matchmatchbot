"""Патч прод-БД: сценарий #15 ("Лид хочет только ивент, без подписки") матчился RAG'ом
даже когда лид явно писал про ОБА направления ("me interesa tanto el evento como el
servicio, cuánto cuesta cada uno?") — найдено 2026-09-23 живым тестом. Собственная
формулировка сценария ("el lead quiere SOLO el evento") вводила модель в заблуждение:
вместо цены она отвечала "¿cómo te llamas? ¿Eres soltero?" — тот же паттерн, что правило
GATE даёт как ПРИМЕР для другого случая (первое сообщение, все поля пустые), скопированный
буквально не по адресу. Правка правила в anna_prompt_v5.md (línea "Ambigüedad AMBOS") этого
одного не хватило — модель весила формулировку САМОГО сценария выше общего правила.

Фикс: добавлен явный блок в начале #15, разруливающий случай "лид упомянул ОБА" —
не считать это "solo evento", и явно разрешить дать обе цифры сразу, без калификации,
если лид попросил обе.

Идемпотентно: точное совпадение template_es со старым текстом, иначе no-op.

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_scenario15_both_interest_2026_09 --force
"""
import asyncio
import sys

import db

SCENARIO_ID = 15
OLD = ("[Guía para AI — el lead quiere SOLO el evento, sin el servicio completo. Genera "
       "mensajes naturales en tono Anna, NO copies literal. Máximo 4 mensajes; el enlace "
       "va en el último. Escribe LITERAL los tokens [event_date], [event_time], "
       "[event_address], [event_price_nonmember], [event_link] — se rellenan solos, NO "
       "inventes.]\n\n[1] [Confirma con calidez que SÍ puede venir solo al evento — es "
       "válido, no lo rechaces. Da el GANCHO de valor: menciona el Slavic Latino Night, "
       "[event_date], [event_time], en [event_address], toca ligero la exclusividad "
       "(eventos pasados agotados, cupo limitado).]\n\n[2] [Dile el precio de inmediato, "
       "sin pedirle antes que se identifique como soltero ni su edad — el precio del "
       "evento no necesita calificación previa (regla confirmada por la dueña): "
       "[event_price_nonmember] MXN[event_promo], incluye bebida de bienvenida, entrantes "
       "y conocer mujeres eslavas solteras que buscan algo serio.]\n\n[3] [Con suavidad y "
       "sin presión, menciona que además del evento tienes un servicio de matchmaking "
       "personal (acompañamiento a tu medida, presentaciones seleccionadas a mano) por si "
       "su objetivo no es solo una noche sino de verdad encontrar pareja — los detalles y "
       "planes se ven en una videollamada. Como consejo, no como venta agresiva. Pregunta "
       "directo y sin presión: ¿quiere el boleto de este evento, o prefiere que "
       "platiquemos del servicio en una videollamada? Ambas están bien. En cualquier caso "
       "pásale el token [event_link] para reservar y ver fotos, videos y reviews de "
       "eventos pasados.]")
NEW = ("[Guía para AI — este escenario es la referencia de TONO para el evento; puede "
       "matchear aunque el lead haya mencionado TAMBIÉN el servicio (no solo el evento) — "
       "en ese caso NO lo trates como \"solo evento\": sigue dando el precio del evento en "
       "el paso [2] igual, pero si el lead pidió las dos cifras (\"cuánto cuesta cada "
       "uno\", precio genérico con ambos intereses ya mostrados), dale TAMBIÉN el precio "
       "del servicio (desde $10,000 USD) en el MISMO mensaje — NUNCA pidas nombre, "
       "soltero, edad ni profesión antes de responder una pregunta de precio ya hecha, "
       "sea del evento, del servicio, o de ambos. Genera mensajes naturales en tono Anna, "
       "NO copies literal. Máximo 4 mensajes; el enlace va en el último. Escribe LITERAL "
       "los tokens [event_date], [event_time], [event_address], [event_price_nonmember], "
       "[event_link] — se rellenan solos, NO inventes.]\n\n[1] [Confirma con calidez que "
       "SÍ puede venir solo al evento — es válido, no lo rechaces. Da el GANCHO de valor: "
       "menciona el Slavic Latino Night, [event_date], [event_time], en [event_address], "
       "toca ligero la exclusividad (eventos pasados agotados, cupo limitado).]\n\n[2] "
       "[Dile el precio de inmediato, sin pedirle antes que se identifique como soltero "
       "ni su edad — el precio del evento no necesita calificación previa (regla "
       "confirmada por la dueña): [event_price_nonmember] MXN[event_promo], incluye "
       "bebida de bienvenida, entrantes y conocer mujeres eslavas solteras que buscan "
       "algo serio.]\n\n[3] [Con suavidad y sin presión, menciona que además del evento "
       "tienes un servicio de matchmaking personal (acompañamiento a tu medida, "
       "presentaciones seleccionadas a mano) por si su objetivo no es solo una noche "
       "sino de verdad encontrar pareja — los detalles y planes se ven en una "
       "videollamada. Como consejo, no como venta agresiva. Pregunta directo y sin "
       "presión: ¿quiere el boleto de este evento, o prefiere que platiquemos del "
       "servicio en una videollamada? Ambas están bien. En cualquier caso pásale el "
       "token [event_link] para reservar y ver fotos, videos y reviews de eventos "
       "pasados.]")


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    await db.init_pool()
    try:
        pool = db._get_pool()
        row = await db.get_scenario_row(SCENARIO_ID)
        if not row:
            print(f"✗ #{SCENARIO_ID}: не найден в БД")
            return
        if row["template_es"] != OLD:
            print(f"⚠ #{SCENARIO_ID}: template_es не совпал с ожидаемым — пропуск "
                  f"(уже изменён кем-то другим?)")
            return
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
            NEW, SCENARIO_ID,
        )
        row2 = await db.get_scenario_row(SCENARIO_ID)
        assert row2 and row2["template_es"] == NEW, f"#{SCENARIO_ID}: замена не применилась"
        print(f"✓ #{SCENARIO_ID} обновлён: обрабатывает случай 'ambos intereses'")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
