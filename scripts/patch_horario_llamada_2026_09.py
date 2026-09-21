"""Патч прод-БД: показывать реальный диапазон часов записи на видеозвонок ПРОАКТИВНО,
при первом же вопросе "какой день и час" (пед директной дуэньи, 2026-09-21) — новый
токен [horario_llamada] (booking.hours_text_now() + sender._fill_booking_vars).

Два разных типа находок в сценариях:

1) #53 ("Лид предлагает конкретное время") хардкодил ЛОЖНЫЙ диапазон "8am a 10pm" —
   именно то выдуманное расписание, которое привело к правилу "NUNCA inventes un
   horario" в anna_prompt_v5.md (найдено 2026-08-15), но осталось зашитым буквально
   в самом тексте сценария. Прямая дезинформация лида — исправлено первым делом.

2) #6/#14/#20/#24/#33/#38/#45/#59 — литеральные (не AI-guided) сценарии, которые
   просят день/час видеозвонка, но НЕ упоминают диапазон вообще — раньше это было
   намеренно (правило "не упоминай часы"), теперь дуэнья попросила упоминать сразу,
   чтобы не было второго хода "эта дата не подходит".

Идемпотентно: точное совпадение template_es со старым текстом на сценарий, иначе
no-op для этого id (кто-то другой уже поменял).

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_horario_llamada_2026_09 --force
"""
import asyncio
import sys

import db

PATCHES = {
    53: (
        "¡Perfecto! ¿Qué día y a qué hora exacta te queda para la videollamada? Atiendo de 8am a 10pm, hora de Ciudad de México 🤍",
        "¡Perfecto! ¿Qué día y a qué hora exacta te queda para la videollamada? Atiendo de [horario_llamada] 🤍",
    ),
    6: (
        "Muchas gracias por tu foto 🤍 Te explico un poco cómo te puedo ayudar: soy matchmaker personal y mi trabajo es encontrarte mujeres eslavas que de verdad encajen contigo, no solo por su apariencia, sino por sus valores y lo que ambos buscan en una relación.\n\nTe presento a 15 mujeres elegidas a mano especialmente para ti, todas buscando algo serio. Te acompaño en cada paso hasta que encuentres a la indicada, y ya hemos formado más de 180 parejas así. La inversión comienza desde $10,000 USD.\n\nMe encantaría que platiquemos en una videollamada para conocerte mejor y saber qué tipo de mujer estás buscando. ¿Cuándo te queda bien?",
        "Muchas gracias por tu foto 🤍 Te explico un poco cómo te puedo ayudar: soy matchmaker personal y mi trabajo es encontrarte mujeres eslavas que de verdad encajen contigo, no solo por su apariencia, sino por sus valores y lo que ambos buscan en una relación.\n\nTe presento a 15 mujeres elegidas a mano especialmente para ti, todas buscando algo serio. Te acompaño en cada paso hasta que encuentres a la indicada, y ya hemos formado más de 180 parejas así. La inversión comienza desde $10,000 USD.\n\nMe encantaría que platiquemos en una videollamada para conocerte mejor y saber qué tipo de mujer estás buscando. ¿Cuándo te queda bien? Atiendo de [horario_llamada].",
    ),
    14: (
        "Me da mucho gusto que te animes 🤍 Mi servicio es totalmente personalizado, te presento a 15 mujeres eslavas elegidas a mano según tus valores, tu personalidad y tus preferencias de físico. La inversión es desde $10,000 USD.\n\nEl siguiente paso es una videollamada de 30 minutos, ahí te conozco mejor y empiezo la búsqueda personalizada para ti. ¿Qué día te queda bien?",
        "Me da mucho gusto que te animes 🤍 Mi servicio es totalmente personalizado, te presento a 15 mujeres eslavas elegidas a mano según tus valores, tu personalidad y tus preferencias de físico. La inversión es desde $10,000 USD.\n\nEl siguiente paso es una videollamada de 30 minutos, ahí te conozco mejor y empiezo la búsqueda personalizada para ti. ¿Qué día te queda bien? Atiendo de [horario_llamada].",
    ),
    20: (
        "Conozco a más de 3,000 mujeres eslavas, todas solteras y buscando algo serio 💕 La mayoría vive en México, y otras están en sus países pero abiertas a mudarse.\n\nMi servicio es personalizado, te presento a 15 elegidas a mano para ti. En una videollamada te explico todo con calma y empiezo la búsqueda. ¿Cuándo te queda bien? 😊",
        "Conozco a más de 3,000 mujeres eslavas, todas solteras y buscando algo serio 💕 La mayoría vive en México, y otras están en sus países pero abiertas a mudarse.\n\nMi servicio es personalizado, te presento a 15 elegidas a mano para ti. En una videollamada te explico todo con calma y empiezo la búsqueda. ¿Cuándo te queda bien? Atiendo de [horario_llamada] 😊",
    ),
    24: (
        "Ay qué bueno, me da gusto 🤍\n\nMe encantaría platicar contigo 1:1 para saber cómo la pasaste, qué te pareció, y contarte qué opciones se abren para ti dentro de la agencia.\n\n¿Te late una videollamada corta? ¿Cuándo te queda? 😊",
        "Ay qué bueno, me da gusto 🤍\n\nMe encantaría platicar contigo 1:1 para saber cómo la pasaste, qué te pareció, y contarte qué opciones se abren para ti dentro de la agencia.\n\n¿Te late una videollamada corta? ¿Cuándo te queda? Atiendo de [horario_llamada] 😊",
    ),
    33: (
        "Hola [имя]! 🤍 ya tengo todo listo de tu parte. Me encantaría agendar la videollamada para presentarte a quién podría encajar contigo. ¿Cuándo te queda?",
        "Hola [имя]! 🤍 ya tengo todo listo de tu parte. Me encantaría agendar la videollamada para presentarte a quién podría encajar contigo. ¿Cuándo te queda? Atiendo de [horario_llamada].",
    ),
    38: (
        "Hola [имя]! 🤍 cómo estás? Soy matchmaker personal y te ayudo a conocer mujeres eslavas solteras seleccionadas a tu medida, con acompañamiento en todo el proceso ✨\n\n¿Cuándo tienes tiempo para una videollamada? Ahí te explico cómo funciona y empiezo a buscar mujeres que encajen contigo 😊",
        "Hola [имя]! 🤍 cómo estás? Soy matchmaker personal y te ayudo a conocer mujeres eslavas solteras seleccionadas a tu medida, con acompañamiento en todo el proceso ✨\n\n¿Cuándo tienes tiempo para una videollamada? Atiendo de [horario_llamada]. Ahí te explico cómo funciona y empiezo a buscar mujeres que encajen contigo 😊",
    ),
    45: (
        "La videollamada es importante porque cada cliente es diferente, necesito conocerte y entender qué buscas para hacer un buen match 🤍\n\nSon 30 minutos nada más. Cuándo te queda?",
        "La videollamada es importante porque cada cliente es diferente, necesito conocerte y entender qué buscas para hacer un buen match 🤍\n\nSon 30 minutos nada más. Cuándo te queda? Atiendo de [horario_llamada].",
    ),
    59: (
        "Hola [имя]! 🤍 no sé si alcanzaste a ver mi mensaje.\n\nSigo aquí si tienes dudas sobre el servicio o el evento, con gusto te cuento con calma 😊\n\nY si ya lo tienes claro, ¿te gustaría que agendemos una videollamada?",
        "Hola [имя]! 🤍 no sé si alcanzaste a ver mi mensaje.\n\nSigo aquí si tienes dudas sobre el servicio o el evento, con gusto te cuento con calma 😊\n\nY si ya lo tienes claro, ¿te gustaría que agendemos una videollamada? Atiendo de [horario_llamada].",
    ),
}


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    await db.init_pool()
    try:
        pool = db._get_pool()
        updated, skipped = 0, 0
        for scenario_id, (old, new) in PATCHES.items():
            row = await db.get_scenario_row(scenario_id)
            if not row:
                print(f"✗ #{scenario_id}: не найден в БД — пропуск")
                skipped += 1
                continue
            if row["template_es"] != old:
                print(f"⚠ #{scenario_id}: template_es не совпал с ожидаемым — "
                      f"пропуск (уже изменён кем-то другим?)")
                skipped += 1
                continue
            await pool.execute(
                "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                new, scenario_id,
            )
            print(f"✓ #{scenario_id} обновлён")
            updated += 1
        print(f"\nИтого: обновлено {updated}, пропущено {skipped} из {len(PATCHES)}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
