"""Патч прод-БД: #24/#18/#26/#31 → AI-гайды (контекст-зависимость, без голой цены/фабрикации).

- #24: убрать фабрикацию отбора + «feedback как тебя видят женщины»; сначала выяснить
  с кем/что понравилось, потом связать; учёт истории (не с нуля, не повтор цены).
- #18: не ронять $1,400 неквалифицированному; сначала квалификация, потом ценность.
- #26: не монетизировать в лоб; спросить про кого, рамка «контакт = benefit», не повтор.
- #31: как #18 — цена не сразу, сначала контекст.

Режимы/ai_allowed не меняем (все уже ai_allowed=true; #24/#26 bot_then_anna, #18/#31 bot_auto).
trigger_es не трогаем → embedding не пересчитывается.
Запуск: venv/bin/python -m scripts.patch_scenarios_24_18_26_31
"""
import asyncio

import db

NEW_24 = (
    "[Guía para AI — al lead SÍ le gustó el evento. NO uses pitch mecánico. NUNCA inventes "
    "cosas que no sabes: PROHIBIDO decir \"noté patrones\", \"seleccioné a algunos hombres y "
    "tú estás entre ellos\", o que sabes \"cómo te perciben las mujeres\" — no tienes esa "
    "información, suena a anzuelo falso. Actúa en DOS momentos según el conversation_history:]\n\n"
    "[SI AÚN NO SABES a quién conoció ni qué le gustó (solo dijo \"estuvo bien\" / \"me gustó\" "
    "/ \"conocí a alguien\", sin detalle):]\n"
    "[1] Alégrate con calidez sincera y breve.\n"
    "[2] PREGUNTA con interés real: ¿a quién conociste? / ¿qué fue lo que más te gustó? ¿hubo "
    "alguna que te llamara la atención? NO ofrezcas nada todavía — primero escúchalo. Cierra el "
    "turno con la pregunta, sin pitch.\n\n"
    "[SI EL LEAD YA CONTÓ a quién conoció o qué le gustó (en este mensaje o antes en el "
    "historial) → conéctalo directo con lo que él dijo, sin presión:]\n\n"
    "[PRIMERO revisa el conversation_history: ¿ya se le habló antes del acompañamiento personal "
    "/ la membresía?\n"
    "   • Si YA lo conoce → NO lo expliques desde cero y NO repitas el precio ya dado (regla NO "
    "REPETIR). Conéctalo directo: \"por lo que me cuentas, justo el acompañamiento personal te "
    "ayudaría a llegar a alguien así de compatible…\".\n"
    "   • Si NO se ha mencionado → explícale un poco en qué consiste, sin abrumar.]\n\n"
    "[3] Liga su experiencia concreta con el siguiente paso: si conoció a alguien pero quedó "
    "suelto, o quiere conocer a más como esa persona, el acompañamiento personal le da "
    "seguimiento real (presentaciones elegidas para él, no al azar de una noche). Solo si notas "
    "interés genuino, propón con calma una videollamada 1:1 para verlo a fondo, y pregunta "
    "cuándo le queda. Sin insistir. NO sueltes precio de golpe (menos si ya lo sabe).]"
)

NEW_18 = (
    "[Guía para AI — el lead escribe desde fuera de México (USA, Colombia, Chile, Argentina, "
    "etc.). NO sueltes el precio de golpe ni hagas el pitch completo si aún no lo conoces.]\n\n"
    "[1] Confirma con calidez que SÍ trabajamos internacionalmente: la membresía funciona online "
    "(cada mes le presento mujeres, la mayoría vive en México, también en otros países), y si "
    "viene a México entra a los eventos privados. Que se sienta bienvenido, sin importar dónde "
    "esté.]\n\n"
    "[2] PRIMERO califica suavemente si aún no sabes lo básico: ¿es soltero? ¿qué edad tiene? "
    "¿qué busca? — como con cualquier lead nuevo. NO des el precio todavía.]\n\n"
    "[3] SOLO cuando ya haya contexto (o si el lead insiste en saber el precio) → preséntalo a "
    "través del VALOR, no como cifra seca: es un acompañamiento personal, selección a mano cada "
    "mes, y entonces mencionas $1,400 USD/mes. Revisa el conversation_history: si el precio ya "
    "se dio antes, NO lo repitas (regla NO REPETIR).]\n\n"
    "[4] Cierra invitando a una videollamada corta y pregunta cuándo le queda.]"
)

NEW_26 = (
    "[Guía para AI — el lead conoció a alguien en el evento y quiere su contacto. Es un momento "
    "emocional (le interesó una persona real): NO lo monetices en seco. Actúa en DOS momentos "
    "según el conversation_history:]\n\n"
    "[SI AÚN NO SABES bien de quién habla:]\n"
    "[1] Alégrate con él con calidez sincera.\n"
    "[2] PREGUNTA por la persona: ¿quién te gustó? / ¿cómo se llama o cómo era? — muestra interés "
    "real antes de hablar de nada más. Cierra con la pregunta.\n\n"
    "[SI YA SABES de quién habla (lo dijo ahora o antes):]\n"
    "[3] Explica con naturalidad que el seguimiento (encontrar el contacto, organizar una segunda "
    "cita cuando hay interés mutuo) es justo parte del acompañamiento personal — así enmarcas el "
    "contacto como un beneficio real del servicio, no como algo que le cobras por pasar un número. "
    "NO sueltes el teléfono de la chica tú mismo (eso lo maneja Anna, con interés mutuo, no a un "
    "no-miembro).]\n\n"
    "[4] Revisa el conversation_history antes de mencionar precio:\n"
    "   • Si el precio / la membresía YA se habló → NO repitas la cifra (regla NO REPETIR); solo "
    "conéctalo (\"justo eso lo tienes como miembro\") y propón la videollamada.\n"
    "   • Si NO se ha hablado → explica breve qué es la membresía y, con calma, menciona $1,400 "
    "USD/mes SIN que suene a cobro abrupto.\n"
    "   Propón una videollamada para verlo, sin presión.]"
)

NEW_31 = (
    "[Guía para AI — el lead pregunta por eventos en otra ciudad (Monterrey, Guadalajara, no vive "
    "en CDMX). NO sueltes el precio de golpe ni fuerces el pitch de membresía sin contexto.]\n\n"
    "[1] Responde con calidez: por ahora los eventos son en CDMX, pero la membresía funciona estés "
    "donde estés (te presento mujeres cada mes online, y cuando haya evento te aviso con tiempo por "
    "si quieres venir).]\n\n"
    "[2] Si aún no sabes lo básico del lead (soltero / edad / qué busca), califícalo suave primero, "
    "como a cualquiera. NO des el precio todavía.]\n\n"
    "[3] SOLO con contexto (o si insiste en el precio) → menciona la membresía a través del valor "
    "(acompañamiento personal, selección a mano), y entonces $1,400 USD/mes. Revisa el "
    "conversation_history: si el precio ya se dio, NO lo repitas (regla NO REPETIR).]\n\n"
    "[4] Pregunta si le late platicarlo en una videollamada corta.]"
)

PATCH = {24: NEW_24, 18: NEW_18, 26: NEW_26, 31: NEW_31}


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, tmpl in PATCH.items():
            await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                               tmpl, sid)
            row = await db.get_scenario_row(sid)
            assert row["template_es"] == tmpl, f"#{sid}: template не совпал"
            print(f"✓ #{sid} обновлён (гайд). mode={row['mode']}, ai_allowed={row['ai_allowed']}")
        # контроль: в #24 не осталось фабрикации
        r24 = await db.get_scenario_row(24)
        assert "seleccioné a algunos hombres" not in r24["template_es"].lower() or "prohibido" in r24["template_es"].lower()
        print("✓ #24: фабрикация запрещена явно")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
