"""Патч прод-БД: убрать «guapo» из всех шаблонов сценариев (владелец решил — бот
больше НИКОГДА так не обращается к лиду).

21 сценарий содержат «guapo» — во всех случаях это обращение-вокатив («Oye guapo,
te entiendo…», «Hola guapo! 🤍…»), убирается без потери смысла грамматически чисто.
Проверяет ДО каждого апдейта, что текущий template_es совпадает с ожидаемым (защита
от гонки/уже применённого патча) — если нет, ПРОПУСКАЕТ строку и предупреждает,
не перезаписывает вслепую.

НЕ трогает: trigger_es (embedding не пересчитывается), mode, ai_allowed.

Запуск: venv/bin/python -m scripts.patch_remove_guapo_2026_07
Идемпотентен: повторный прогон просто ничего не найдёт для замены (текст уже новый).
"""
import asyncio

import db

# id → (старый template_es, новый template_es без "guapo")
PATCHES = {
    6: (
        "Muchas gracias guapo 🤍 soy matchmaker personal y mi servicio es 100% personalizado: a lo largo de unos 6 meses te presento a 15 mujeres eslavas (hasta 20 según el caso), elegidas a mano por tus valores, tu personalidad y tus preferencias de físico, de una base de más de 3,000 solteras que buscan algo serio.\n\nNo es una app ni un evento suelto: es un acompañamiento serio hasta que encuentres a la indicada, y ya se han formado más de 80 parejas ✨\n\n¿Te gustaría que platiquemos un poco más y te cuente cómo te puedo ayudar? 🤍",
        "Muchas gracias 🤍 soy matchmaker personal y mi servicio es 100% personalizado: a lo largo de unos 6 meses te presento a 15 mujeres eslavas (hasta 20 según el caso), elegidas a mano por tus valores, tu personalidad y tus preferencias de físico, de una base de más de 3,000 solteras que buscan algo serio.\n\nNo es una app ni un evento suelto: es un acompañamiento serio hasta que encuentres a la indicada, y ya se han formado más de 80 parejas ✨\n\n¿Te gustaría que platiquemos un poco más y te cuente cómo te puedo ayudar? 🤍",
    ),
    9: (
        "Oye guapo, te entiendo pero solo trabajamos con hombres solteros, las chicas buscan algo serio 🤍\n\nCuando cambie tu situación me avisas, aquí estoy",
        "Oye, te entiendo pero solo trabajamos con hombres solteros, las chicas buscan algo serio 🤍\n\nCuando cambie tu situación me avisas, aquí estoy",
    ),
    10: (
        "Gracias por el info guapo 🤍\n\nMira, ahorita tengo una lista de espera que puede tardar entre 6 y 12 meses.\n\nMientras, te invito a tomar nuestros cursos en línea sobre cómo conocer, conectar y tener una relación con mujeres eslavas, aquí: [course_link]. Cuando se desocupan lugares para la agencia, primero consideramos a quienes ya tomaron los cursos, y de ahí los demás",
        "Gracias por el info 🤍\n\nMira, ahorita tengo una lista de espera que puede tardar entre 6 y 12 meses.\n\nMientras, te invito a tomar nuestros cursos en línea sobre cómo conocer, conectar y tener una relación con mujeres eslavas, aquí: [course_link]. Cuando se desocupan lugares para la agencia, primero consideramos a quienes ya tomaron los cursos, y de ahí los demás",
    ),
    12: (
        "Esa foto no es la adecuada guapo. Te deseo lo mejor 🤍",
        "Esa foto no es la adecuada. Te deseo lo mejor 🤍",
    ),
    13: (
        "Te entiendo guapo, pero sin foto no podemos seguir 🤍\n\nEs parte del proceso para todos, así sé con quién estoy hablando antes de avanzar. Es solo para mí, no la comparto con nadie\n\nSi prefieres me pasas tu Instagram",
        "Te entiendo, pero sin foto no podemos seguir 🤍\n\nEs parte del proceso para todos, así sé con quién estoy hablando antes de avanzar. Es solo para mí, no la comparto con nadie\n\nSi prefieres me pasas tu Instagram",
    ),
    16: (
        "Te entiendo guapo 🤍 pero míralo así: no es un gasto, es una inversión en encontrar a tu pareja. Es un servicio premium y 100% personal: a lo largo de 6 meses te presento a 15 mujeres eslavas (hasta 20) elegidas a mano para ti, con acompañamiento en todo el proceso — no una lotería como las apps. La inversión es desde $10,000 USD.\n\n¿Te late que lo veamos en una videollamada y te explico a detalle qué incluye? Y si prefieres algo más ligero, está la opción del evento: [event_price_nonmember] MXN[event_promo].",
        "Te entiendo 🤍 pero míralo así: no es un gasto, es una inversión en encontrar a tu pareja. Es un servicio premium y 100% personal: a lo largo de 6 meses te presento a 15 mujeres eslavas (hasta 20) elegidas a mano para ti, con acompañamiento en todo el proceso — no una lotería como las apps. La inversión es desde $10,000 USD.\n\n¿Te late que lo veamos en una videollamada y te explico a detalle qué incluye? Y si prefieres algo más ligero, está la opción del evento: [event_price_nonmember] MXN[event_promo].",
    ),
    22: (
        "Te entiendo guapo 🤍 MatchMatch es una agencia matrimonial registrada formalmente. Hemos organizado varios eventos en CDMX, tenemos clientes activos y todo está documentado\n\nCheca mi Instagram: @rusaencdmx, ahí ves fotos de eventos pasados y la dinámica\n\nEn la videollamada te puedo enseñar testimonios. Cuándo te queda? 😊",
        "Te entiendo 🤍 MatchMatch es una agencia matrimonial registrada formalmente. Hemos organizado varios eventos en CDMX, tenemos clientes activos y todo está documentado\n\nCheca mi Instagram: @rusaencdmx, ahí ves fotos de eventos pasados y la dinámica\n\nEn la videollamada te puedo enseñar testimonios. Cuándo te queda? 😊",
    ),
    27: (
        "Te agradezco la honestidad guapo 🤍\n\nPero nosotros trabajamos solo con personas que buscan relación seria. Las chicas en nuestra base buscan matrimonio, familia, pareja a largo plazo\n\nSi cambias tu enfoque, aquí estaremos ✨\n\nMientras, te invito a tomar nuestros cursos en línea sobre cómo conocer y conectar con mujeres eslavas, aquí: [course_link]",
        "Te agradezco la honestidad 🤍\n\nPero nosotros trabajamos solo con personas que buscan relación seria. Las chicas en nuestra base buscan matrimonio, familia, pareja a largo plazo\n\nSi cambias tu enfoque, aquí estaremos ✨\n\nMientras, te invito a tomar nuestros cursos en línea sobre cómo conocer y conectar con mujeres eslavas, aquí: [course_link]",
    ),
    30: (
        "Por ahora los eventos son entre semana guapo 🤍 pero lo checo y te aviso si podemos abrir una fecha en fin de semana.\n\nEn cuanto tengamos fecha te aviso sin falta. Y si quieres, mientras tanto platicamos en una videollamada de mi servicio personalizado y te empiezo a presentar mujeres compatibles contigo. ¿Te late?",
        "Por ahora los eventos son entre semana 🤍 pero lo checo y te aviso si podemos abrir una fecha en fin de semana.\n\nEn cuanto tengamos fecha te aviso sin falta. Y si quieres, mientras tanto platicamos en una videollamada de mi servicio personalizado y te empiezo a presentar mujeres compatibles contigo. ¿Te late?",
    ),
    32: (
        "Hola guapo! 🤍 me faltan solo un par de datos para completar tu perfil antes de la videollamada. Cuando tengas un momento me los pasas y agendamos ✨",
        "Hola! 🤍 me faltan solo un par de datos para completar tu perfil antes de la videollamada. Cuando tengas un momento me los pasas y agendamos ✨",
    ),
    35: (
        "Prefiero leer porfa guapo 🤍 me lo escribes?",
        "Prefiero leer porfa 🤍 me lo escribes?",
    ),
    39: (
        "Te entiendo guapo 🤍 el precio refleja el trabajo personal que hago contigo: selección a mano y acompañamiento en todo el proceso.\n\nSobre precios especiales o promociones, eso lo vemos mejor en la videollamada según tu caso. ¿Te late que agendemos y te explico todo? 😊",
        "Te entiendo 🤍 el precio refleja el trabajo personal que hago contigo: selección a mano y acompañamiento en todo el proceso.\n\nSobre precios especiales o promociones, eso lo vemos mejor en la videollamada según tu caso. ¿Te late que agendemos y te explico todo? 😊",
    ),
    40: (
        "Jajaja para nada guapo, soy Anna, la fundadora 🤍 checa mi Instagram @rusaencdmx si quieres ✨",
        "Jajaja para nada, soy Anna, la fundadora 🤍 checa mi Instagram @rusaencdmx si quieres ✨",
    ),
    41: (
        "Claro guapo! Mándame su foto, edad y profesión porfa. Es soltero? Solo solteros pueden asistir 🤍\n\nO si prefiere, él me puede escribir directo por este número",
        "Claro! Mándame su foto, edad y profesión porfa. Es soltero? Solo solteros pueden asistir 🤍\n\nO si prefiere, él me puede escribir directo por este número",
    ),
    42: (
        "Déjame revisar tu caso guapo, te respondo pronto 🤍",
        "Déjame revisar tu caso, te respondo pronto 🤍",
    ),
    44: (
        "Todavía no tengo fecha exacta del próximo evento guapo, pero te aviso en cuanto la tengamos 🤍\n\nMientras, con mi servicio personalizado no tienes que esperar al evento: a lo largo del proceso te voy presentando mujeres compatibles de nuestra base. ¿Lo vemos en una videollamada?",
        "Todavía no tengo fecha exacta del próximo evento, pero te aviso en cuanto la tengamos 🤍\n\nMientras, con mi servicio personalizado no tienes que esperar al evento: a lo largo del proceso te voy presentando mujeres compatibles de nuestra base. ¿Lo vemos en una videollamada?",
    ),
    48: (
        "Ay qué lástima guapo 🤍 gracias por avisar\n\nTe aviso del próximo con tiempo. Y si quieres, mientras tanto vemos mi servicio personalizado para que empieces a conocer mujeres compatibles sin esperar al evento.",
        "Ay qué lástima 🤍 gracias por avisar\n\nTe aviso del próximo con tiempo. Y si quieres, mientras tanto vemos mi servicio personalizado para que empieces a conocer mujeres compatibles sin esperar al evento.",
    ),
    49: (
        "Hola guapo! 🤍 te recuerdo que hoy tenemos nuestra videollamada a las [hora]. Sigue en pie para ti?",
        "Hola! 🤍 te recuerdo que hoy tenemos nuestra videollamada a las [hora]. Sigue en pie para ti?",
    ),
    50: (
        "Hola guapo! 🤍 te recuerdo que mañana es el evento ✨ Te espero a las [event_time] en [event_address]. Cualquier duda me avisas 🤍",
        "Hola! 🤍 te recuerdo que mañana es el evento ✨ Te espero a las [event_time] en [event_address]. Cualquier duda me avisas 🤍",
    ),
    55: (
        "Claro guapo 🤍 además de tarjeta, puedes pagar en efectivo en Oxxo o Walmart, o por transferencia bancaria.\n\nPara asegurar tu lugar te paso los datos exactos, cualquier cosa me avisas y te ayudo con el proceso ✨",
        "Claro 🤍 además de tarjeta, puedes pagar en efectivo en Oxxo o Walmart, o por transferencia bancaria.\n\nPara asegurar tu lugar te paso los datos exactos, cualquier cosa me avisas y te ayudo con el proceso ✨",
    ),
    58: (
        "¡Gracias por reservar tu lugar guapo! 🤍 Te agrego a la lista de invitados.\n\n¿Ya te llegó el boleto por correo? Si no lo ves, revisa spam y promociones, y cualquier cosa me avisas ✨",
        "¡Gracias por reservar tu lugar! 🤍 Te agrego a la lista de invitados.\n\n¿Ya te llegó el boleto por correo? Si no lo ves, revisa spam y promociones, y cualquier cosa me avisas ✨",
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

        remaining = await pool.fetch("SELECT id FROM scenarios WHERE template_es ILIKE '%guapo%'")
        if remaining:
            print(f"⚠ ОСТАЛИСЬ с 'guapo': {[r['id'] for r in remaining]}")
        else:
            print("✓ 'guapo' больше нигде в template_es не осталось")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
