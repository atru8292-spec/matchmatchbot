"""Патч прод-БД: цена услуги — только по явному интересу, не по факту прохождения фильтра.

Живой демо-диалог (wa_5215500000001, «Carlos») показал эталонное поведение: после фото
бот НЕ давал цену сразу — сказал «te cuento cómo funciona», и только когда лид САМ
спросил «cuánto cuesta la membresía?», дал цену. Текущий #6 (после Mila-концепта) давал
$10,000 сразу в первом питче, что оказалось слишком рано — «прошёл фильтр» ≠ «заинтересован».

- #6 (главный питч после фото): убрана цена, заканчивается приглашением рассказать больше/
  на звонок. Цена остаётся ТОЛЬКО в #14 (интерес) и #16 (вопрос о цене/дорого) — уже так было.
- #18/#19/#26/#31 (AI-гайды): убрано «calificado» как самостоятельное основание для цены —
  оставлено только «lo pide o interés claro».
- anna_prompt_v5.md правится отдельно (не в этом скрипте) — тоже нужен деплой+рестарт
  (промпт кэшируется в памяти процесса).

trigger_es не трогаем → embedding не пересчитывается.

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_price_after_interest_2026_07 --force
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
    "indicada, y ya se han formado más de 80 parejas ✨\n\n"
    "¿Te gustaría que platiquemos un poco más y te cuente cómo te puedo ayudar? 🤍"
)

NEW_18 = (
    "[Guía para AI — el lead escribe desde fuera de México.]\n\n"
    "[1] Con calidez: SÍ trabajamos internacionalmente — mi servicio personalizado funciona "
    "online (te presento mujeres a tu medida, la mayoría vive en México, también en otros "
    "países), y si vienes a México entras a los eventos privados. Que se sienta bienvenido.\n\n"
    "[2] PRIMERO califica suave si no sabes lo básico (soltero / edad / qué busca).\n\n"
    "[3] Preséntalo por el VALOR: servicio 100% personalizado, a lo largo de ~6 meses te "
    "presento a 15 mujeres (hasta 20) elegidas a mano. Da el precio (inversión desde $10,000 "
    "USD) SOLO si lo pide directamente o muestra interés claro en avanzar — pasar la "
    "calificación NO es esa señal, es apenas el primer paso.\n\n"
    "[4] Cierra invitando a una videollamada corta: ahí lo conoces a fondo y empiezas la "
    "búsqueda personalizada. Pregunta cuándo le queda.]"
)

NEW_19 = (
    "[Guía para AI — el lead pregunta cómo funciona / qué incluye. Explícalo TÚ completo y "
    "cálido, como Anna.]\n\n"
    "[Servicio: eres matchmaker personal; es un servicio 100% personalizado — a lo largo de ~6 "
    "meses le presentas a 15 mujeres eslavas (hasta 20 según el caso), elegidas a mano por "
    "valores, personalidad y físico, de una base de más de 3,000 solteras; lo acompañas en "
    "todo el proceso hasta que encuentre a la indicada. Ya se han formado más de 80 parejas. "
    "También hay eventos privados. No es app ni lotería.]\n\n"
    "[Precio: da la inversión (desde $10,000 USD) SOLO si el lead lo pregunta directamente o "
    "muestra un interés claro en avanzar. Que haya preguntado «cómo funciona» no es esa señal "
    "por sí sola — explícale el servicio primero, y si insiste en el precio o dice que le "
    "interesa, entonces sí dáselo.]\n\n"
    "[Cierre: invita a una videollamada corta y explícale que ahí lo conoces a fondo, le "
    "detallas todo y, a partir de ahí, empiezas la búsqueda personalizada. Pregunta cuándo le "
    "queda. NO propongas tú un horario concreto.]"
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
    "ayudarlo con ese contacto y con conocer a más mujeres compatibles. Da el precio (desde "
    "$10,000 USD) SOLO si lo pide directamente o muestra interés claro en avanzar — no por "
    "defecto. Revisa el historial y NO repitas lo ya dicho (regla NO REPETIR).]"
)

NEW_31 = (
    "[Guía para AI — el lead pregunta por eventos en otra ciudad.]\n\n"
    "[1] Con calidez: por ahora los eventos son en CDMX, pero mi servicio personalizado "
    "funciona estés donde estés (te presento mujeres a tu medida online, y cuando haya evento "
    "te aviso con tiempo por si quieres venir).]\n\n"
    "[2] Si aún no sabes lo básico (soltero / edad / qué busca), califícalo suave primero.]\n\n"
    "[3] Preséntalo por el valor (servicio personalizado, 15 mujeres en ~6 meses, hasta 20, a "
    "mano). Da el precio (desde $10,000 USD) SOLO si lo pide directamente o muestra interés "
    "claro en avanzar — no automáticamente por estar calificado.]\n\n"
    "[4] Pregunta si le late una videollamada corta: ahí lo conoces a fondo y empieza la "
    "búsqueda personalizada.]"
)

PATCH = {6: NEW_6, 18: NEW_18, 19: NEW_19, 26: NEW_26, 31: NEW_31}
GUIDE_IDS = {18, 19, 26, 31}

# #6 больше НЕ должен содержать цену; гайды не должны опираться на "calificado" как триггер
BANNED_IN_6 = ("$10,000", "10,000 usd", "inversión")
BANNED_STANDALONE = ("está calificado o lo pide", "calificado o lo pide", "si ya está calificado")


def _check() -> None:
    low6 = NEW_6.lower()
    for bad in BANNED_IN_6:
        assert bad.lower() not in low6, f"#6: осталась цена/якорь «{bad}»"
    for sid in GUIDE_IDS:
        low = PATCH[sid].lower()
        for bad in BANNED_STANDALONE:
            assert bad.lower() not in low, f"#{sid}: осталось «calificado» как триггер цены («{bad}»)"


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    _check()
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
        print("\n✅ Цена услуги теперь только по явному интересу (#14/#16), не по факту фильтра.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
