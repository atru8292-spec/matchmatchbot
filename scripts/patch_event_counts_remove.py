"""Патч прод-БД: убрать числа гостей ([event_men]/[event_women]) из ивент-сценариев.

Контекст: количества мужчин/женщин на ивенте убраны из CRM (miniapp EventScreen) и
из бэка (mini_api). Эти токены оставались только в #51/#52 — правим текст, чтобы бот
не называл конкретные числа, а говорил обобщённо. #15 уже без них (AI-гайд).

Меняем ТОЛЬКО template_es у #51/#52 (trigger_es не трогаем → embedding не пересчитывается).
Плюс удаляем осиротевшие app_settings event_men/event_women (больше нигде не используются:
ни фронт, ни бэк, ни sender._EVENT_VAR_KEYS, ни один шаблон).

Запуск: venv/bin/python -m scripts.patch_event_counts_remove
Идемпотентен.
"""
import asyncio

import db

NEW_51 = (
    "El precio es de 9,000 MXN para no miembros y 4,000 MXN para miembros, e incluye: "
    "bebida de bienvenida, entrantes y lo más importante: conocer hermosas mujeres "
    "eslavas solteras que buscan una relación seria.\n\n"
    "Es un evento único como este en toda América Latina. Comienza a las [event_start] "
    "y termina a [event_end], puedes venir más tarde. Habrá mujeres guapas e inteligentes "
    "solteras y hombres exitosos solteros.\n\n"
    "Todos van con la misma intención de algo serio, así que puedes acercarte con "
    "confianza a la que te llame la atención, platicar y, si hay química, intercambiar "
    "contactos para seguir viéndose. Si te da pena, acércate a mí o a mis compañeras "
    "organizadoras, te entendemos y te presentamos con quien te guste. Además es un gran "
    "evento de networking: muchos encuentran no solo pareja sino también nuevos amigos y "
    "contactos de negocios. Habrá música, baile y buena conversación, y todas son "
    "hermosas mujeres eslavas que viven en CDMX.\n\n"
    "Aquí está el enlace para obtener el boleto, ahí puedes ver fotos, videos y reviews "
    "de eventos pasados: [event_link]"
)

NEW_52 = (
    "El evento incluye: bebida de bienvenida, entrantes y lo más importante: conocer "
    "hermosas mujeres eslavas solteras que buscan una relación seria.\n\n"
    "Es un evento único como este en toda América Latina. Comienza a las [event_start] "
    "y termina a [event_end], y puedes venir más tarde. Habrá mujeres guapas e "
    "inteligentes solteras y hombres exitosos solteros.\n\n"
    "Todos van con la misma intención de algo serio, así que puedes acercarte con "
    "confianza a la que te llame la atención, platicar y, si hay química, intercambiar "
    "contactos para seguir viéndose. Si te da pena, acércate a mí o a mis compañeras "
    "organizadoras, te presentamos con quien te guste. Además es un gran evento de "
    "networking: muchos encuentran no solo pareja sino también nuevos amigos y contactos "
    "de negocios. Habrá música, baile y buena conversación, y todas son hermosas mujeres "
    "eslavas que viven en CDMX.\n\n"
    "Si quieres, te paso el enlace con fotos y videos de eventos pasados. Y cuando gustes "
    "te cuento los precios y cómo reservar."
)

PATCH = {51: NEW_51, 52: NEW_52}


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, new_tmpl in PATCH.items():
            before = await db.get_scenario_row(sid)
            had = "[event_men]" in before["template_es"] or "[event_women]" in before["template_es"]
            await pool.execute(
                "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                new_tmpl, sid,
            )
            after = await db.get_scenario_row(sid)
            leftover = "[event_men]" in after["template_es"] or "[event_women]" in after["template_es"]
            assert after["template_es"] == new_tmpl, f"#{sid}: template не совпал"
            assert not leftover, f"#{sid}: остались токены men/women"
            print(f"✓ #{sid}: было men/women={had} → после={leftover} (токены убраны)")

        # осиротевшие настройки
        res = await pool.execute(
            "DELETE FROM app_settings WHERE key IN ('event_men','event_women')")
        print(f"✓ app_settings: удалены event_men/event_women ({res})")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
