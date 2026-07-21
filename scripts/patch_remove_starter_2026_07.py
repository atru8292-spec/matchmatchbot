"""Патч прод-БД: убираем Starter/подписку/$1,400 из чата. Остаются Standard/VIP (только на
видеозвонке у Ани) и ивент отдельно (единая цена).

Что делает:
- 17 сценариев (#6,14,15,16,18,19,20,21,25,26,30,31,38,39,44,48,51): вычищаем «membresía /
  $1,400 / 3 mujeres al mes», термин → «servicio / acompañamiento personal», усиливаем цель
  звонка («a partir de ahí empiezo a buscar/seleccionar»). Ивент — единая цена (токен
  [event_price_nonmember]); убраны [event_price_member] и [event_promo].
- app_settings: event_price_nonmember=5,000; event_price_member и event_price_old → пусто
  (тогда [event_promo] рендерится пустым).

trigger_es НЕ трогаем → embedding не пересчитывается.

⚠️ Guard: запуск только с флагом --force (защита от случайного прогона):
venv/bin/python -m scripts.patch_remove_starter_2026_07 --force
"""
import asyncio
import sys

import db

# ---- Литеральные (фикс/текстовые) сценарии ----

NEW_6 = (
    "Muchas gracias guapo 🤍 soy matchmaker personal: te encuentro una mujer eslava que de "
    "verdad encaje contigo, elegida a mano según tus valores y tus preferencias de físico, "
    "con acceso a una base de más de 3,000 mujeres solteras que buscan algo serio.\n\n"
    "Lo ideal es una videollamada corta: ahí te conozco a fondo y, a partir de ahí, empiezo "
    "a buscar y seleccionar personalmente a las mujeres que encajan contigo. ¿Cuándo te queda? 😊"
)

NEW_14 = (
    "Me da mucho gusto que te animes 🤍 soy matchmaker personal: te elijo a mano mujeres "
    "eslavas según tus valores y tus preferencias de físico, con acceso a nuestra base de "
    "más de 3,000 mujeres.\n\n"
    "El siguiente paso es una videollamada de 30 min: ahí te conozco mejor y, a partir de "
    "ahí, empiezo a buscar y seleccionar personalmente a las mujeres que encajan contigo. "
    "¿Qué día te queda mejor?"
)

NEW_16 = (
    "Te entiendo guapo 🤍 pero míralo así: no es un gasto, es una inversión en encontrar a "
    "tu pareja. Es un servicio premium y personal: yo misma te elijo mujeres a mano según "
    "tus valores y tus preferencias de físico, no una lotería como las apps.\n\n"
    "Los planes y precios los vemos en una videollamada según tu caso, y a partir de ahí "
    "empiezo a buscar personalmente a las mujeres que encajan contigo. ¿Te late que "
    "agendemos? Y si prefieres algo más ligero, está la opción del evento "
    "([event_price_nonmember] MXN)."
)

NEW_20 = (
    "Tenemos más de 3,000 mujeres en nuestra base 💕 todas eslavas, solteras y buscando algo "
    "serio. La mayoría vive en México, otras en sus países pero abiertas a mudarse.\n\n"
    "En una videollamada de 30 min te explico cómo funciona el servicio y, a partir de ahí, "
    "empiezo a buscar a las que encajan contigo. ¿Cuándo te queda? 😊"
)

NEW_30 = (
    "Por ahora los eventos son entre semana guapo 🤍 pero lo checo y te aviso si podemos "
    "abrir una fecha en fin de semana.\n\n"
    "En cuanto tengamos fecha te aviso sin falta. Y si quieres, mientras tanto platicamos en "
    "una videollamada y te empiezo a presentar mujeres que encajen contigo. ¿Te late?"
)

NEW_38 = (
    "Hola [имя]! 🤍 cómo estás? Soy matchmaker personal y te ayudo a conocer mujeres eslavas "
    "solteras seleccionadas a tu medida, con acompañamiento en todo el proceso ✨\n\n"
    "¿Cuándo tienes tiempo para una videollamada? Ahí te explico cómo funciona y empiezo a "
    "buscar mujeres que encajen contigo 😊"
)

NEW_39 = (
    "Te entiendo guapo 🤍 el precio refleja el trabajo personal que hago contigo: selección "
    "a mano y acompañamiento en todo el proceso.\n\n"
    "Sobre precios especiales o promociones, eso lo vemos mejor en la videollamada según tu "
    "caso. ¿Te late que agendemos y te explico todo? 😊"
)

NEW_44 = (
    "Todavía no tengo fecha exacta del próximo evento guapo, pero te aviso en cuanto la "
    "tengamos 🤍\n\n"
    "Mientras, con el servicio de matchmaking personal no tienes que esperar: te empiezo a "
    "presentar mujeres de nuestra base según lo que buscas. ¿Lo platicamos en una videollamada?"
)

NEW_48 = (
    "Ay qué lástima guapo 🤍 gracias por avisar\n\n"
    "Te aviso del próximo con tiempo. Y si quieres, mientras tanto vemos la opción del "
    "servicio de matchmaking personal para que empieces a conocer mujeres sin esperar al evento."
)

NEW_51 = (
    "El precio del evento es de [event_price_nonmember] MXN e incluye bebida de bienvenida, "
    "entrantes y lo más importante: conocer hermosas mujeres eslavas solteras que buscan una "
    "relación seria.\n\n"
    "Es un evento único en toda América Latina: comienza a las [event_start] y termina a "
    "[event_end] (puedes llegar más tarde), con mujeres y hombres solteros que van con la "
    "misma intención de algo serio. Acércate con confianza a quien te llame la atención, o si "
    "te da pena, a mí o a mis compañeras organizadoras y te presentamos. Además es un gran "
    "evento de networking, con música, baile y buena conversación.\n\n"
    "Aquí está el enlace para el boleto, ahí puedes ver fotos, videos y reviews de eventos "
    "pasados: [event_link]"
)

# ---- Гайды для AI (не сокращаем, но вычищаем Starter/$1,400/membresía) ----

NEW_15 = (
    "[Guía para AI — el lead quiere SOLO el evento, sin el servicio completo. Genera mensajes "
    "naturales en tono Anna, NO copies literal. Máximo 4 mensajes; el enlace va en el último. "
    "Escribe LITERAL los tokens [event_date], [event_time], [event_address], "
    "[event_price_nonmember], [event_link] — se rellenan solos, NO inventes.]\n\n"
    "[1] Confirma con calidez que SÍ puede venir solo al evento — es válido, no lo rechaces. "
    "Menciona el Slavic Latino Night: [event_date], [event_time], en [event_address]. Toca "
    "ligero la exclusividad (eventos pasados agotados, cupo limitado).\n\n"
    "[2] Dile claro el precio: [event_price_nonmember] MXN una sola vez, incluye bebida de "
    "bienvenida, entrantes y conocer mujeres eslavas solteras que buscan algo serio.\n\n"
    "[3] Con suavidad y sin presión, menciona que además del evento tienes un servicio de "
    "matchmaking personal (acompañamiento a tu medida, presentaciones seleccionadas a mano) "
    "por si su objetivo no es solo una noche sino de verdad encontrar pareja — los detalles y "
    "planes se ven en una videollamada. Como consejo, no como venta agresiva.\n\n"
    "[4] Pregunta directo y sin presión: ¿quiere el boleto de este evento, o prefiere que "
    "platiquemos del servicio en una videollamada? Ambas están bien. En cualquier caso pásale "
    "el token [event_link] para reservar y ver fotos, videos y reviews de eventos pasados."
)

NEW_18 = (
    "[Guía para AI — el lead escribe desde fuera de México. NO des precios de los planes de "
    "golpe.]\n\n"
    "[1] Con calidez: SÍ trabajamos internacionalmente — el servicio de matchmaking personal "
    "funciona online (te presento mujeres a tu medida, la mayoría vive en México, también en "
    "otros países), y si vienes a México entras a los eventos privados. Que se sienta "
    "bienvenido.\n\n"
    "[2] PRIMERO califica suave si no sabes lo básico (soltero / edad / qué busca).\n\n"
    "[3] Preséntalo por el VALOR: acompañamiento personal, selección a mano según valores, "
    "personalidad y físico. NO des precios de los planes en el chat — los ve Anna en la "
    "videollamada.\n\n"
    "[4] Cierra invitando a una videollamada corta: ahí lo conoces a fondo y a partir de ahí "
    "empiezas la búsqueda personalizada. Pregunta cuándo le queda."
)

NEW_19 = (
    "[Guía para AI — el lead pregunta cómo funciona / qué incluye. Explícalo TÚ completo y "
    "cálido, como Anna.]\n\n"
    "[Servicio: eres matchmaker personal; eliges a mano mujeres eslavas según valores, "
    "personalidad, estilo de vida y también preferencias de físico; acceso a una base de más "
    "de 3,000 mujeres solteras que buscan algo serio; acompañas en todo el proceso y "
    "organizas eventos privados para conocerlas en persona. No es app ni lotería: servicio "
    "confidencial y personalizado, con muchas parejas ya formadas.]\n\n"
    "[Planes y precios: NO los des en el chat — hay distintos planes y el precio lo ve Anna a "
    "detalle en la videollamada, según su caso. Si insiste, dile con calidez que justo eso se "
    "ve en la llamada.]\n\n"
    "[Cierre: invita a una videollamada corta y explícale que ahí lo conoces a fondo y, a "
    "partir de ahí, empiezas a buscar y seleccionar personalmente a las mujeres que encajan "
    "con él. Pregunta cuándo le queda. NO propongas tú un horario concreto.]"
)

NEW_21 = (
    "[Si el lead viene hablando del EVENTO: pásale el enlace de boletos, escribe literal el "
    "token [event_link], y dile que ahí reserva directo 🤍]\n\n"
    "[Si es sobre el servicio de matchmaking u otra cosa: dile que las opciones de pago y los "
    "planes los ven en la videollamada, y pregunta cuándo le queda]"
)

NEW_25 = (
    "[Guía para AI — feedback NEGATIVO del evento. Genera mensajes naturales en tono Anna, NO "
    "pitch mecánico. Dos momentos según el historial:]\n\n"
    "[SI AÚN NO SABES qué no le gustó (dijo algo vago):]\n"
    "[1] Empatía sincera y breve, sin sonar a guion.\n"
    "[2] PREGUNTA con interés real qué no le gustó o qué esperaba (pocas mujeres, no conectó, "
    "organización, ambiente…). NO asumas y NO ofrezcas nada aún: primero entender. Cierra con "
    "la pregunta, sin pitch.\n\n"
    "[SI YA DIJO qué le molestó → conéctalo directo, sin presión y sin slogan mecánico:]\n"
    "[Revisa el historial: ¿ya se le habló antes del acompañamiento personal (en la "
    "calificación inicial)? Si YA lo conoce → NO lo expliques desde cero y NO repitas lo ya "
    "dicho (regla NO REPETIR), conéctalo directo. Si NO → explícalo un poco, sin abrumar.]\n"
    "[3] Reconoce su queja concreta y liga el acompañamiento personal como respuesta "
    "ESPECÍFICA:\n"
    "   • «pocas mujeres» → en el proceso personal no dependes de cuántas haya esa noche: yo "
    "te presento mujeres elegidas para ti, una a una.\n"
    "   • «no conecté» → yo preselecciono por compatibilidad real (valores, personalidad, "
    "físico), no por azar.\n"
    "   • «organización/lugar/ambiente» → dale la razón con honestidad; el proceso 1:1 es más "
    "cuidado, privado y a tu ritmo.\n"
    "   NO des precios de los planes (los ve Anna en la videollamada). Solo si hay interés "
    "genuino, propón con calma una videollamada corta; ahí lo conoces a fondo y a partir de "
    "ahí empieza la búsqueda personalizada. Si no hay interés, respeta y deja la puerta "
    "abierta.]"
)

NEW_26 = (
    "[Guía para AI — el lead conoció a alguien en el evento y quiere su contacto. Momento "
    "emocional: NO lo monetices en seco. Dos momentos según el historial:]\n\n"
    "[SI AÚN NO SABES de quién habla:]\n"
    "[1] Alégrate con él con calidez sincera.\n"
    "[2] PREGUNTA por la persona (¿quién te gustó? ¿cómo se llama o cómo era?). Cierra con la "
    "pregunta.\n\n"
    "[SI YA SABES de quién habla:]\n"
    "[3] Explica con naturalidad que el seguimiento (encontrar el contacto, organizar una "
    "segunda cita con interés mutuo) es justo parte del acompañamiento personal — enmárcalo "
    "como beneficio real del servicio, no como cobro por pasar un número. NO sueltes el "
    "teléfono tú mismo (lo maneja Anna, con interés mutuo, no a quien no es cliente).]\n\n"
    "[4] Propón una videollamada: ahí lo conoces mejor y a partir de ahí arranca el "
    "acompañamiento para ayudarlo con ese contacto y con conocer a más mujeres compatibles. "
    "NO menciones precios de los planes en el chat. Revisa el historial y NO repitas lo ya "
    "dicho (regla NO REPETIR).]"
)

NEW_31 = (
    "[Guía para AI — el lead pregunta por eventos en otra ciudad. NO des precios de los "
    "planes de golpe.]\n\n"
    "[1] Con calidez: por ahora los eventos son en CDMX, pero el servicio de matchmaking "
    "personal funciona estés donde estés (te presento mujeres a tu medida de forma online, y "
    "cuando haya evento te aviso con tiempo por si quieres venir).]\n\n"
    "[2] Si aún no sabes lo básico (soltero / edad / qué busca), califícalo suave primero.]\n\n"
    "[3] Preséntalo por el valor (acompañamiento personal, selección a mano). NO des precios "
    "de los planes en el chat — los ve Anna en la videollamada.]\n\n"
    "[4] Pregunta si le late una videollamada corta: ahí lo conoces a fondo y a partir de ahí "
    "empieza la búsqueda personalizada.]"
)

PATCH = {
    6: NEW_6, 14: NEW_14, 15: NEW_15, 16: NEW_16, 18: NEW_18, 19: NEW_19, 20: NEW_20,
    21: NEW_21, 25: NEW_25, 26: NEW_26, 30: NEW_30, 31: NEW_31, 38: NEW_38, 39: NEW_39,
    44: NEW_44, 48: NEW_48, 51: NEW_51,
}

# event_price_member / event_price_old → пусто (member-цены больше нет, [event_promo] пуст)
PRICES = {"event_price_nonmember": "5,000", "event_price_member": "", "event_price_old": ""}

# Ничего из этого не должно остаться ни в одном пропатченном тексте
BANNED = ("1,400", "1400", "membresía", "membresia", "starter", "[event_price_member]",
          "[event_promo]", "3 mujeres al mes", "precio de miembro", "a precio preferencial")

# Гайды для AI (bracketed «[Guía para AI…]») ОБЯЗАНЫ быть ai_allowed=true — иначе _fixed_reply
# отправит лиду сырые внутренние инструкции. Фикс-тексты — ai_allowed=false.
GUIDE_IDS = {15, 18, 19, 21, 25, 26, 31}
FIXED_IDS = {39, 51}


def _check(sid: int, tmpl: str) -> None:
    low = tmpl.lower()
    for bad in BANNED:
        assert bad.lower() not in low, f"#{sid}: осталось запрещённое «{bad}»"


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    # прогоняем валидацию текстов ДО подключения (быстрый фейл при опечатке)
    for sid, tmpl in PATCH.items():
        _check(sid, tmpl)

    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, tmpl in PATCH.items():
            await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                               tmpl, sid)
            row = await db.get_scenario_row(sid)
            assert row and row["template_es"] == tmpl, f"#{sid}: template не совпал после UPDATE"
            if sid in GUIDE_IDS:
                assert row["ai_allowed"] is True, f"#{sid}: гайд должен быть ai_allowed=true"
            if sid in FIXED_IDS:
                assert row["ai_allowed"] is False, f"#{sid}: фикс-текст должен быть ai_allowed=false"
            print(f"✓ #{sid} обновлён ({len(tmpl)} символов, ai_allowed={row['ai_allowed']})")
        for k, v in PRICES.items():
            await db.set_setting(k, v)
        s = await db.get_settings(list(PRICES))
        assert s.get("event_price_nonmember") == "5,000", "event_price_nonmember != 5,000"
        assert not s.get("event_price_member"), "event_price_member не очищен"
        assert not s.get("event_price_old"), "event_price_old не очищен"
        print(f"✓ app_settings: {s}")
        print("\n✅ Патч применён: 17 сценариев + настройки ивента (единая цена 5,000).")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
