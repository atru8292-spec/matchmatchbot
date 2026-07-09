"""Патч прод-БД: (1) убрать фразу «no solo por foto sino por valores…» из питча #6;
(2) отодвинуть эскалацию на Аню для #14 и #19 — бот сам объясняет максимум, эскалация
уходит на реальный шаг назначения времени (#53/#49).

Изменения:
  #6  — template_es: убрать противопоставление «no solo por foto sino por valores…».
  #14 — mode bot_then_anna → bot_auto; template: полный питч + $1,400, спросить когда
        лиду удобно (без «yo te propongo horario» и без инструкции хвалить профессию).
  #19 — mode bot_then_anna → bot_auto; ai_allowed false → true; template: AI-гайд,
        объясняет ВСЁ сам (включая цену для тёплого лида), не откладывает «в видеозвонке».

trigger_es у всех НЕ меняется → embedding не пересчитывается. #15/#24/#26 не трогаем
(эскалация там оправдана). Запуск: venv/bin/python -m scripts.patch_escalation_pitch_2026_07
"""
import asyncio

import db

NEW_6 = (
    "Muchas gracias guapo 🤍 mira, te explico cómo te puedo ayudar. Soy matchmaker "
    "personal, mi trabajo es encontrarte una mujer eslava que de verdad encaje contigo, "
    "según tus preferencias y lo que buscas en una relación\n\n"
    "Cada mes te presento 3 mujeres seleccionadas especialmente para ti, ya filtradas y "
    "listas para conocerte en serio. Tienes acceso a más de 3,000 mujeres rusas, "
    "ucranianas y bielorrusas, y entras a nuestros eventos privados a precio preferencial. "
    "La membresía es de $1,400 USD al mes\n\n"
    "Te gustaría que platiquemos en una videollamada para conocerte mejor y ver a quién te "
    "puedo presentar? 😊"
)

NEW_14 = (
    "Me da mucho gusto que te animes 🤍\n\n"
    "Te explico rápido lo que incluye: cada mes te presento 3 mujeres eslavas "
    "seleccionadas para ti, tienes acceso a toda la base de más de 3,000 mujeres y entras "
    "a nuestros eventos privados a precio de miembro. La membresía es de $1,400 USD al mes.\n\n"
    "El siguiente paso es una videollamada de 30 min para conocerte mejor y ver a quién te "
    "puedo presentar. ¿Qué día y horario te queda mejor?"
)

NEW_19 = (
    "[Guía para AI — el lead pregunta cómo funciona / qué incluye. Explícalo TÚ de forma "
    "completa y cálida, como Anna. NO difieras los detalles 'a la videollamada'.]\n\n"
    "[Explica el servicio: eres matchmaker personal; cada mes le presentas 3 mujeres "
    "eslavas seleccionadas según sus preferencias; tiene acceso a toda la base de más de "
    "3,000 mujeres; entra a los eventos privados a precio de miembro; no es una app, es un "
    "servicio confidencial y personalizado, y ya han formado muchas parejas felices.]\n\n"
    "[Precio: si el lead ya está calificado o claramente interesado, dile que la membresía "
    "es de $1,400 USD/mes. Si es un lead frío (aún no sabes si es soltero o su edad), "
    "primero conócelo un poco antes de dar el precio.]\n\n"
    "[Cierre: invita a una videollamada corta como siguiente paso y pregunta cuándo le "
    "queda. NO propongas tú un horario concreto — eso se coordina después.]"
)


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        # #6 — только текст
        await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=6", NEW_6)
        r6 = await db.get_scenario_row(6)
        assert "no solo por foto" not in r6["template_es"], "#6: фраза про фото осталась"
        print("✓ #6: фраза про фото убрана")

        # #14 — mode + текст
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, mode='bot_auto', updated_at=now() WHERE id=14", NEW_14)
        r14 = await db.get_scenario_row(14)
        assert r14["mode"] == "bot_auto" and r14["template_es"] == NEW_14
        print(f"✓ #14: mode={r14['mode']}, ai_allowed={r14['ai_allowed']} (текст: полный питч + спросить время)")

        # #19 — mode + ai_allowed + текст
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, mode='bot_auto', ai_allowed=true, updated_at=now() WHERE id=19", NEW_19)
        r19 = await db.get_scenario_row(19)
        assert r19["mode"] == "bot_auto" and r19["ai_allowed"] is True and r19["template_es"] == NEW_19
        print(f"✓ #19: mode={r19['mode']}, ai_allowed={r19['ai_allowed']} (AI-гайд, объясняет всё сам)")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
