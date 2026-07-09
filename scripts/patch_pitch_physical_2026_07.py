"""Патч прод-БД: вернуть «пожелания по внешности» в описание персонального отбора
в питчах #6, #14, #19 (терялось при правках эскалации/фразы про фото). #16 уже полный,
#51 остаётся строго про ивент (без мостика к membership — по решению владельца).

Меняем только template_es у #6/#14/#19. mode/ai_allowed уже выставлены прошлым патчем
(#14/#19 = bot_auto, #19 ai_allowed=true). trigger_es не трогаем → embedding не пересчитывается.
Запуск: venv/bin/python -m scripts.patch_pitch_physical_2026_07
"""
import asyncio

import db

NEW_6 = (
    "Muchas gracias guapo 🤍 mira, te explico cómo te puedo ayudar. Soy matchmaker "
    "personal: mi trabajo es encontrarte una mujer eslava que de verdad encaje contigo, "
    "elegida a mano según tus valores, tu estilo de vida y también tus preferencias de "
    "físico.\n\n"
    "Cada mes te presento 3 mujeres seleccionadas especialmente para ti, ya filtradas y "
    "listas para conocerte en serio. Tienes acceso a más de 3,000 mujeres rusas, "
    "ucranianas y bielorrusas, y entras a nuestros eventos privados a precio preferencial. "
    "La membresía es de $1,400 USD al mes.\n\n"
    "Te gustaría que platiquemos en una videollamada para conocerte mejor y ver a quién te "
    "puedo presentar? 😊"
)

NEW_14 = (
    "Me da mucho gusto que te animes 🤍\n\n"
    "Te explico rápido lo que incluye: cada mes te presento 3 mujeres eslavas elegidas "
    "para ti según tus valores, tu personalidad y tus preferencias de físico; tienes "
    "acceso a toda la base de más de 3,000 mujeres y entras a nuestros eventos privados a "
    "precio de miembro. La membresía es de $1,400 USD al mes.\n\n"
    "El siguiente paso es una videollamada de 30 min para conocerte mejor y ver a quién te "
    "puedo presentar. ¿Qué día y horario te queda mejor?"
)

NEW_19 = (
    "[Guía para AI — el lead pregunta cómo funciona / qué incluye. Explícalo TÚ de forma "
    "completa y cálida, como Anna. NO difieras los detalles 'a la videollamada'.]\n\n"
    "[Explica el servicio: eres matchmaker personal; cada mes le presentas 3 mujeres "
    "eslavas elegidas a mano según sus valores, su personalidad, su estilo de vida y "
    "también sus preferencias de físico; tiene acceso a toda la base de más de 3,000 "
    "mujeres; entra a los eventos privados a precio de miembro. No es una app ni una "
    "lotería: es un servicio confidencial y personalizado, y ya han formado muchas parejas "
    "felices.]\n\n"
    "[Precio: si el lead ya está calificado o claramente interesado, dile que la membresía "
    "es de $1,400 USD/mes. Si es un lead frío (aún no sabes si es soltero o su edad), "
    "primero conócelo un poco antes de dar el precio.]\n\n"
    "[Cierre: invita a una videollamada corta y pregunta cuándo le queda. NO propongas tú "
    "un horario concreto — eso se coordina después.]"
)

PATCH = {6: NEW_6, 14: NEW_14, 19: NEW_19}


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, tmpl in PATCH.items():
            await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                               tmpl, sid)
            row = await db.get_scenario_row(sid)
            assert row["template_es"] == tmpl, f"#{sid}: template не совпал"
            has_fisico = "físico" in row["template_es"]
            assert has_fisico, f"#{sid}: 'físico' отсутствует!"
            print(f"✓ #{sid}: обновлён, 'físico' присутствует ✓  (mode={row['mode']}, ai_allowed={row['ai_allowed']})")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
