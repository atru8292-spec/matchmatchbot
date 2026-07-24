"""Патч прод-БД: точные формулировки из референс-документа владельца (Mila's doc,
вставлен в чат 2026-07-25) — 7 сценариев, где текст доступен как окончательный
("blue"/финальный) и не конфликтует с уже принятыми в этой сессии решениями.

Затрагивает: #6, #16, #24, #44, #51, #52, #56.

Для #51/#52 (детали ивента) содержание документа перенесено, НО с сохранением
рабочих токенов [event_price_nonmember]/[event_start]/[event_end]/[event_link]/
[event_promo] вместо зашитых чисел из документа (6000 MXN, 8:30pm и т.д.) —
иначе Аня не сможет менять цену/дату/время ивента из CRM без правки текста
сценария. Количество мужчин/женщин (30/30) оставлено буквально — под них ещё
нет токенов в системе (см. sender.py _EVENT_VAR_KEYS).

НЕ применены (сознательно пропущены — см. отчёт в чате, требуют
подтверждения владельца):
  #38 — новая цена $3,000/3 женщины не входит в текущую модель (10,000 USD/15 женщин)
  #39 — добавляет скидку «приведи друга» 5,000/чел, противоречит жёсткому
        правилу "NO HAY DESCUENTOS" в промпте
  #53 — текст документа сломает автозапись звонка (booking.py ждёт от лида
        точный день+час, а не "жди, я гляну календарь")
  #55 — содержит реальные банковские реквизиты, не трогать без явного ОК

НЕ трогает: trigger_es (embedding не пересчитывается), mode, ai_allowed.

Запуск: venv/bin/python -m scripts.patch_mila_wording_2026_07
Идемпотентен: повторный прогон просто ничего не найдёт для замены.
"""
import asyncio

import db

PATCHES = {
    6: (
        "Muchas gracias 🤍 soy matchmaker personal y mi servicio es 100% personalizado: a lo largo de unos 6 meses te presento a 15 mujeres eslavas (hasta 20 según el caso), elegidas a mano por tus valores, tu personalidad y tus preferencias de físico, de una base de más de 3,000 solteras que buscan algo serio.\n\nNo es una app ni un evento suelto: es un acompañamiento serio hasta que encuentres a la indicada, y ya se han formado más de 80 parejas ✨\n\n¿Te gustaría que platiquemos un poco más y te cuente cómo te puedo ayudar? 🤍",
        "Muchas gracias. 🤍 Mira, te explico cómo te puedo ayudar. Soy matchmaker personal y mi trabajo es encontrar y presentarte a mujeres eslavas que de verdad encajen contigo, no solo por su apariencia, sino también por sus valores y por lo que ambos buscan en una relación.\n\nDurante seis meses te presentamos a 15 mujeres increíbles que cumplen con tus requisitos y para quienes tú también cumples con los suyos. Todas están interesadas en construir una relación seria. Es un servicio altamente personalizado, enfocado al 100% en ayudarte a encontrar y construir una relación feliz con una mujer extraordinaria. La inversión en nuestros servicios comienza desde 10,000 USD.\n\n¿Cuándo tienes tiempo para una videollamada conmigo? Me gustaría conocer mejor qué tipo de mujer estás buscando y mostrarte nuestra base de datos de mujeres que podrían ser compatibles contigo.",
    ),
    16: (
        "Te entiendo 🤍 pero míralo así: no es un gasto, es una inversión en encontrar a tu pareja. Es un servicio premium y 100% personal: a lo largo de 6 meses te presento a 15 mujeres eslavas (hasta 20) elegidas a mano para ti, con acompañamiento en todo el proceso — no una lotería como las apps. La inversión es desde $10,000 USD.\n\n¿Te late que lo veamos en una videollamada y te explico a detalle qué incluye? Y si prefieres algo más ligero, está la opción del evento: [event_price_nonmember] MXN[event_promo].",
        "Es una inversión en algo tan importante como encontrar a tu pareja.\n\nCon la membresía de 10,000 USD te presento a 15 mujeres eslavas seleccionadas de acuerdo con tus preferencias físicas, tus valores y tu personalidad. Contamos con una base de datos de más de 3,000 mujeres eslavas solteras que están buscando una relación seria.\n\nEs un servicio premium y completamente personalizado, no una lotería como las apps de citas. Piensa en cuánto tiempo y dinero se va en citas al azar que no llevan a nada. Aquí todo el proceso está diseñado para ayudarte a encontrar y construir una relación feliz y duradera.\n\n¿Te late que lo platiquemos en una videollamada? Con gusto te explicaré a detalle cómo funcionan nuestros servicios personalizados de matchmaking, nuestro evento de matchmaking y te mostraré los perfiles de mujeres que podrían ser compatibles contigo.",
    ),
    24: (
        "Ay [имя], qué gusto 🤍 me dio mucho que vinieras. Me encantó verte por ahí.\n\nMe encantaría platicar contigo 1:1 para saber cómo la pasaste, qué te pareció, y contarte qué opciones se abren para ti dentro de la agencia.\n\n¿Te late una videollamada corta? ¿Cuándo te queda? 😊",
        "Ay qué bueno, me da gusto 🤍\n\nMe encantaría platicar contigo 1:1 para saber cómo la pasaste, qué te pareció, y contarte qué opciones se abren para ti dentro de la agencia.\n\n¿Te late una videollamada corta? ¿Cuándo te queda? 😊",
    ),
    44: (
        "Todavía no tengo fecha exacta del próximo evento, pero te aviso en cuanto la tengamos 🤍\n\nMientras, con mi servicio personalizado no tienes que esperar al evento: a lo largo del proceso te voy presentando mujeres compatibles de nuestra base. ¿Lo vemos en una videollamada?",
        "Todavía no tengo fecha exacta del próximo evento, pero te aviso en cuanto la tengamos 🤍\n\nMientras tanto, como miembro de la agencia, tienes acceso a conocer a 15 mujeres guapas y compatibles contigo de nuestra base de datos, sin tener que esperar al evento. ¿Lo vemos en una videollamada?",
    ),
    51: (
        "El precio del evento es de [event_price_nonmember] MXN[event_promo], precio especial con descuento, e incluye bebida de bienvenida, entrantes y lo más importante: conocer hermosas mujeres eslavas solteras que buscan una relación seria.\n\nEs un evento único en toda América Latina: comienza a las [event_start] y termina a [event_end] (puedes llegar más tarde), con mujeres y hombres solteros que van con la misma intención de algo serio. Acércate con confianza a quien te llame la atención, o si te da pena, a mí o a mis compañeras organizadoras y te presentamos. Además es un gran evento de networking, con música, baile y buena conversación.\n\nAquí está el enlace para el boleto, ahí puedes ver fotos, videos y reviews de eventos pasados: [event_link]",
        "El precio es de [event_price_nonmember] MXN[event_promo], e incluye: bebida de bienvenida, entrantes y lo más importante: conocer hermosas mujeres eslavas solteras que buscan una relación seria. También es un gran evento de networking porque muchos hombres encuentran no solo novias sino también nuevos amigos y socios comerciales.\n\nEs un evento único como este en toda América Latina. Comienza a las [event_start] y termina a [event_end], puedes venir más tarde. Habrá 30 mujeres guapas e inteligentes solteras y 30 hombres exitosos solteros.\n\nTodos van con la misma intención de encontrar una relación seria, así que puedes acercarte con confianza a la que te llame la atención, platicar y, si hay química, intercambiar contactos para seguir viéndose. Si te da pena, acércate a mí o a mis compañeras organizadoras, te presentamos con quien te guste. Además es un gran evento de networking: muchos encuentran no solo pareja sino también nuevos amigos y contactos de negocios. Habrá música, baile y buena conversación, y todas son hermosas mujeres eslavas que viven en CDMX.\n\nAquí está el enlace para obtener el boleto. Allí podrás ver fotos, vídeos y reviews de los eventos pasados: [event_link]",
    ),
    52: (
        "El evento incluye: bebida de bienvenida, entrantes y lo más importante: conocer hermosas mujeres eslavas solteras que buscan una relación seria.\n\nEs un evento único como este en toda América Latina. Comienza a las [event_start] y termina a [event_end], y puedes venir más tarde. Habrá mujeres guapas e inteligentes solteras y hombres exitosos solteros.\n\nTodos van con la misma intención de algo serio, así que puedes acercarte con confianza a la que te llame la atención, platicar y, si hay química, intercambiar contactos para seguir viéndose. Si te da pena, acércate a mí o a mis compañeras organizadoras, te presentamos con quien te guste. Además es un gran evento de networking: muchos encuentran no solo pareja sino también nuevos amigos y contactos de negocios. Habrá música, baile y buena conversación, y todas son hermosas mujeres eslavas que viven en CDMX.\n\nSi quieres, te paso el enlace con fotos y videos de eventos pasados. Y cuando gustes te cuento los precios y cómo reservar.",
        "El evento incluye: bebida de bienvenida, entrantes y lo más importante: conocer hermosas mujeres eslavas solteras que buscan una relación seria, también es un gran evento de networking porque muchos hombres encuentran no solo novias sino también nuevos amigos y socios comerciales.\n\nEs un evento único como este en toda América Latina. Comienza a las [event_start] y termina a [event_end], y puedes venir más tarde. Habrá 30 mujeres guapas e inteligentes solteras y 30 hombres exitosos solteros.\n\nTodos van con la misma intención de encontrar una relación seria, así que puedes acercarte con confianza a la que te llame la atención, platicar y, si hay química, intercambiar contactos para seguir viéndose. Si te da pena, acércate a mí o a mis compañeras organizadoras, te presentamos con quien te guste. Además es un gran evento de networking: muchos encuentran no solo pareja sino también nuevos amigos y contactos de negocios. Habrá música, baile y buena conversación, y todas son hermosas mujeres eslavas que viven en CDMX.\n\nSi quieres, te paso el enlace con fotos y videos de eventos pasados. Y cuando gustes te cuento los precios y cómo reservar.",
    ),
    56: (
        "Muy buena pregunta 🤍 subimos un poco el precio porque ahora tenemos mejor lugar, mejor organización, una lista de espera larga y muchísimas historias de éxito, muchas parejas felices.\n\nAun así, por todo lo que recibes, sigue siendo una inversión que de verdad vale la pena ✨",
        "Muy buena pregunta 🤍 subimos un poco el precio porque ahora tenemos mejor lugar, mejor organización, una lista de espera larga y muchísimas historias de éxito, muchas parejas felices.\n\nYa hemos formado muchas parejas y hacemos todo lo posible para ayudar a todos a encontrar a su media naranja en nuestros eventos. ❤️ Aun así, por todo lo que recibes, sigue siendo una inversión que de verdad vale la pena ✨",
    ),
}


async def main() -> None:
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
