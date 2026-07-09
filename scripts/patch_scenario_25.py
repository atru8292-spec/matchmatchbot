"""Патч прод-БД: #25 «Лиду не понравилось на ивенте» — почти-дословный питч → AI-гайд.

Сначала бот выясняет ЧТО именно не понравилось, потом связывает персональный подход с
конкретной жалобой (не общий «3 mujeres al mes»). Учитывает историю: если про подписку
уже говорили — не повторяет с нуля (правило NO REPETIR); если нет — объясняет чуть больше.

ai_allowed уже true, mode bot_auto, trigger_es не трогаем → embedding не пересчитывается.
Запуск: venv/bin/python -m scripts.patch_scenario_25
"""
import asyncio

import db

NEW_25 = (
    "[Guía para AI — feedback NEGATIVO del evento (al lead NO le gustó). Genera mensajes "
    "naturales en tono Anna, NO un pitch mecánico. Actúa en DOS momentos según el "
    "conversation_history:]\n\n"
    "[SI AÚN NO SABES qué fue lo que no le gustó (el lead solo dijo \"no me gustó\" / "
    "\"estuve solo\" / algo vago, sin explicar a fondo):]\n"
    "[1] Empatía SINCERA y breve, sin sonar a guion y sin disculparte de más.\n"
    "[2] PREGUNTA con interés real qué fue lo que no le gustó o qué esperaba y no encontró "
    "(pocas mujeres, no conectó con quien conoció, la organización, el ambiente…). NO asumas "
    "y NO ofrezcas nada todavía: primero entender. Cierra el turno con la pregunta, sin pitch.\n\n"
    "[SI EL LEAD YA DIJO qué le molestó (en este mensaje o antes en el historial) → conéctalo "
    "directo, sin presión y SIN el eslogan genérico \"3 mujeres al mes\":]\n\n"
    "[PRIMERO revisa el conversation_history: ¿ya se le habló antes del acompañamiento personal "
    "/ la membresía (en la calificación inicial, antes de que fuera al evento)?\n"
    "   • Si YA lo conoce → NO lo expliques desde cero como si fuera nuevo, y NO repitas el "
    "precio ni el pitch que ya se le dio (regla NO REPETIR). Conéctalo directo, apoyándote en "
    "que ya sabe de qué va: \"por eso justamente el acompañamiento personal resolvería lo que "
    "te pasó…\".\n"
    "   • Si NO se ha mencionado antes (llegó al evento sin pasar por la calificación del bot) → "
    "explícale un poco más en qué consiste el acompañamiento personal, sin abrumar.]\n\n"
    "[3] Reconoce su queja concreta con honestidad y liga el acompañamiento personal como "
    "respuesta ESPECÍFICA a eso (con la profundidad que decidiste arriba):\n"
    "   • \"pocas mujeres / no había suficientes\" → en el proceso personal no dependes de "
    "cuántas haya esa noche: yo te presento mujeres elegidas para ti, una a una.\n"
    "   • \"no conecté / sin química con quien conocí\" → justo por eso yo preselecciono por "
    "compatibilidad real (valores, personalidad, tus preferencias de físico), no por el azar de "
    "quién te tocó cerca.\n"
    "   • \"la organización / el lugar / el ambiente\" → dale la razón con honestidad, y ofrece "
    "que el proceso 1:1 es más cuidado, privado y a tu ritmo.\n"
    "   • otra cosa → adáptate a lo que dijo, sin forzar.\n"
    "   NO sueltes precio (menos aún si ya lo conoce). Solo si notas interés genuino, propón con "
    "calma una videollamada corta para platicarlo (sin insistir). Si no muestra interés, respeta "
    "y deja la puerta abierta.]"
)


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=25", NEW_25)
        row = await db.get_scenario_row(25)
        assert row["template_es"] == NEW_25, "template не совпал"
        assert "3 mujeres al mes" not in row["template_es"].split("SIN el eslogan")[0], "остался старый слоган?"
        print(f"✓ #25 обновлён (гайд). mode={row['mode']}, ai_allowed={row['ai_allowed']}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
