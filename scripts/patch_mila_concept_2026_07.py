"""Патч прод-БД: концепт Милы — «servicio personalizado» (15 mujeres/~6 meses, hasta 20,
inversión desde $10,000 USD). Цену/цифры бот называет ЗАИНТЕРЕСОВАННОМУ или на прямой вопрос;
холодному — сначала ценность + звонок. Ивент — единая цена 6,000 MXN (как у Милы).

Затрагивает сценарии #6,14,16,19,20,21,26,30,31,44,48,18 + настройку event_price_nonmember=6,000.
Ивент-тексты (#51/#15/#52) и цену member/old не трогаем (member/old уже пусты).
trigger_es не трогаем → embedding не пересчитывается.

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_mila_concept_2026_07 --force
"""
import asyncio
import sys

import db

NEW_6 = (
    "Muchas gracias guapo 🤍 soy matchmaker personal y mi servicio es 100% personalizado: a lo "
    "largo de unos 6 meses te presento a 15 mujeres eslavas (hasta 20 según el caso), elegidas "
    "a mano por tus valores, tu personalidad y tus preferencias de físico, de una base de más "
    "de 3,000 solteras que buscan algo serio.\n\n"
    "No es una app ni un evento suelto: es un acompañamiento serio hasta que encuentres a la "
    "indicada, y ya se han formado más de 80 parejas ✨ La inversión es desde $10,000 USD.\n\n"
    "Lo ideal es una videollamada corta: ahí te conozco a fondo, te explico todo a detalle y "
    "empiezo la búsqueda personalizada para ti. ¿Cuándo te queda? 😊"
)

NEW_14 = (
    "Me da mucho gusto que te animes 🤍 mi servicio es totalmente personalizado: a lo largo de "
    "~6 meses te presento a 15 mujeres eslavas (hasta 20), elegidas a mano por tus valores, tu "
    "personalidad y tus preferencias de físico, con acceso a una base de más de 3,000 solteras. "
    "La inversión es desde $10,000 USD.\n\n"
    "El siguiente paso es una videollamada de 30 min: ahí te conozco mejor y empiezo la búsqueda "
    "personalizada para ti. ¿Qué día te queda?"
)

NEW_16 = (
    "Te entiendo guapo 🤍 pero míralo así: no es un gasto, es una inversión en encontrar a tu "
    "pareja. Es un servicio premium y 100% personal: a lo largo de 6 meses te presento a 15 "
    "mujeres eslavas (hasta 20) elegidas a mano para ti, con acompañamiento en todo el proceso "
    "— no una lotería como las apps. La inversión es desde $10,000 USD.\n\n"
    "¿Te late que lo veamos en una videollamada y te explico a detalle qué incluye? Y si "
    "prefieres algo más ligero, está la opción del evento ([event_price_nonmember] MXN)."
)

NEW_20 = (
    "Tenemos más de 3,000 mujeres en nuestra base 💕 todas eslavas, solteras y buscando algo "
    "serio. La mayoría vive en México, otras en sus países pero abiertas a mudarse.\n\n"
    "Mi servicio es personalizado: a lo largo de ~6 meses te presento a 15 (hasta 20) elegidas "
    "a mano para ti. En una videollamada te explico todo a detalle y empiezo la búsqueda. "
    "¿Cuándo te queda? 😊"
)

NEW_30 = (
    "Por ahora los eventos son entre semana guapo 🤍 pero lo checo y te aviso si podemos abrir "
    "una fecha en fin de semana.\n\n"
    "En cuanto tengamos fecha te aviso sin falta. Y si quieres, mientras tanto platicamos en "
    "una videollamada de mi servicio personalizado y te empiezo a presentar mujeres compatibles "
    "contigo. ¿Te late?"
)

NEW_44 = (
    "Todavía no tengo fecha exacta del próximo evento guapo, pero te aviso en cuanto la "
    "tengamos 🤍\n\n"
    "Mientras, con mi servicio personalizado no tienes que esperar al evento: a lo largo del "
    "proceso te voy presentando mujeres compatibles de nuestra base. ¿Lo vemos en una "
    "videollamada?"
)

NEW_48 = (
    "Ay qué lástima guapo 🤍 gracias por avisar\n\n"
    "Te aviso del próximo con tiempo. Y si quieres, mientras tanto vemos mi servicio "
    "personalizado para que empieces a conocer mujeres compatibles sin esperar al evento."
)

NEW_19 = (
    "[Guía para AI — el lead pregunta cómo funciona / qué incluye. Explícalo TÚ completo y "
    "cálido, como Anna.]\n\n"
    "[Servicio: eres matchmaker personal; es un servicio 100% personalizado — a lo largo de ~6 "
    "meses le presentas a 15 mujeres eslavas (hasta 20 según el caso), elegidas a mano por "
    "valores, personalidad y físico, de una base de más de 3,000 solteras; lo acompañas en todo "
    "el proceso hasta que encuentre a la indicada. Ya se han formado más de 80 parejas. También "
    "hay eventos privados. No es app ni lotería.]\n\n"
    "[Precio: si el lead ya está calificado o claramente interesado, dile que la inversión es "
    "desde $10,000 USD. Si es un lead frío (aún no sabes si es soltero o su edad), primero "
    "conócelo un poco antes de dar el precio.]\n\n"
    "[Cierre: invita a una videollamada corta y explícale que ahí lo conoces a fondo, le "
    "detallas todo y, a partir de ahí, empiezas la búsqueda personalizada. Pregunta cuándo le "
    "queda. NO propongas tú un horario concreto.]"
)

NEW_18 = (
    "[Guía para AI — el lead escribe desde fuera de México.]\n\n"
    "[1] Con calidez: SÍ trabajamos internacionalmente — mi servicio personalizado funciona "
    "online (te presento mujeres a tu medida, la mayoría vive en México, también en otros "
    "países), y si vienes a México entras a los eventos privados. Que se sienta bienvenido.\n\n"
    "[2] PRIMERO califica suave si no sabes lo básico (soltero / edad / qué busca).\n\n"
    "[3] Preséntalo por el VALOR: servicio 100% personalizado, a lo largo de ~6 meses te "
    "presento a 15 mujeres (hasta 20) elegidas a mano. Da el precio (inversión desde $10,000 "
    "USD) SOLO si ya está calificado o lo pide; a un lead frío, primero contexto.\n\n"
    "[4] Cierra invitando a una videollamada corta: ahí lo conoces a fondo y empiezas la "
    "búsqueda personalizada. Pregunta cuándo le queda.]"
)

NEW_21 = (
    "[Si el lead viene hablando del EVENTO: pásale el enlace de boletos, escribe literal el "
    "token [event_link], y dile que ahí reserva directo 🤍]\n\n"
    "[Si es sobre el servicio personalizado u otra cosa: dile que las opciones de pago y los "
    "detalles de la inversión los ven en la videollamada, y pregunta cuándo le queda]"
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
    "segunda cita con interés mutuo) es justo parte de mi servicio personalizado — enmárcalo "
    "como beneficio real, no como cobro por pasar un número. NO sueltes el teléfono tú mismo "
    "(lo maneja Anna, con interés mutuo, no a quien no es cliente).]\n\n"
    "[4] Propón una videollamada: ahí lo conoces mejor y arranca el acompañamiento para "
    "ayudarlo con ese contacto y con conocer a más mujeres compatibles. Si lo pide o está "
    "claramente interesado, la inversión es desde $10,000 USD; si no lo pide, no sueltes cifra. "
    "Revisa el historial y NO repitas lo ya dicho (regla NO REPETIR).]"
)

NEW_31 = (
    "[Guía para AI — el lead pregunta por eventos en otra ciudad.]\n\n"
    "[1] Con calidez: por ahora los eventos son en CDMX, pero mi servicio personalizado "
    "funciona estés donde estés (te presento mujeres a tu medida online, y cuando haya evento "
    "te aviso con tiempo por si quieres venir).]\n\n"
    "[2] Si aún no sabes lo básico (soltero / edad / qué busca), califícalo suave primero.]\n\n"
    "[3] Preséntalo por el valor (servicio personalizado, 15 mujeres en ~6 meses, hasta 20, a "
    "mano). Da el precio (desde $10,000 USD) solo si está calificado o lo pide.]\n\n"
    "[4] Pregunta si le late una videollamada corta: ahí lo conoces a fondo y empieza la "
    "búsqueda personalizada.]"
)

PATCH = {
    6: NEW_6, 14: NEW_14, 16: NEW_16, 18: NEW_18, 19: NEW_19, 20: NEW_20, 21: NEW_21,
    26: NEW_26, 30: NEW_30, 31: NEW_31, 44: NEW_44, 48: NEW_48,
}

# ивент: единая цена 6,000 (как у Милы). member/old остаются пустыми.
PRICES = {"event_price_nonmember": "6,000"}

# Не должно остаться в пропатченных текстах (старая модель). $10,000 — ЛЕГАЛЕН (новый концепт).
BANNED = ("1,400", "1400", "membresía", "membresia", "starter", "3 mujeres al mes",
          "[event_price_member]", "[event_promo]", "precio de miembro", "a precio preferencial")
GUIDE_IDS = {18, 19, 21, 26, 31}


def _check(sid: int, tmpl: str) -> None:
    low = tmpl.lower()
    for bad in BANNED:
        assert bad.lower() not in low, f"#{sid}: осталось запрещённое «{bad}»"


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    for sid, tmpl in PATCH.items():
        _check(sid, tmpl)

    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, tmpl in PATCH.items():
            await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                               tmpl, sid)
            row = await db.get_scenario_row(sid)
            assert row and row["template_es"] == tmpl, f"#{sid}: template не совпал"
            if sid in GUIDE_IDS:
                assert row["ai_allowed"] is True, f"#{sid}: гайд должен быть ai_allowed=true"
            print(f"✓ #{sid} обновлён ({len(tmpl)} символов, ai_allowed={row['ai_allowed']})")
        for k, v in PRICES.items():
            await db.set_setting(k, v)
        s = await db.get_settings(["event_price_nonmember", "event_price_member", "event_price_old"])
        assert s.get("event_price_nonmember") == "6,000", "event_price_nonmember != 6,000"
        print(f"✓ app_settings ивент: {s}")
        print("\n✅ Патч применён: концепт Милы (12 сценариев) + ивент 6,000.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
