"""Патч прод-БД: ребрендинг "MatchMatch" → "Match Match Agency" + ссылка на сайт.

Источник: референс-документ владельца (Google Docs, вставлен в чат 2026-07-25) —
официальное название теперь "Match Match Agency" (два слова), сайт
www.matchmatchagency.com/es добавлен в приветствие (сценарий №1).

Затрагивает: #1 (Лид написал первый раз), #2 (спрашивает про ивент),
#3 (хочет знакомиться со славянками), #22 (это безопасно?).
Только эти 4 сценария содержали "MatchMatch" в БД — проверено запросом
ILIKE '%MatchMatch%' перед патчем.

НЕ трогает: trigger_es (embedding не пересчитывается), mode, ai_allowed.

Запуск: venv/bin/python -m scripts.patch_brand_rename_2026_07
Идемпотентен: повторный прогон просто ничего не найдёт для замены.
"""
import asyncio

import db

# id → (старый template_es, новый template_es)
PATCHES = {
    1: (
        "Hola! Soy Anna, fundadora de MatchMatch 🤍\n\nSomos una agencia matrimonial premium: yo personalmente te busco una mujer eslava (rusa, ucraniana o bielorrusa) que de verdad encaje contigo para algo serio, pareja y familia ✨\n\nAntes de contarte más, déjame conocerte un poquito, eres soltero?",
        "Hola! Soy Anna, fundadora de Match Match Agency www.matchmatchagency.com/es 🤍\n\nYo personalmente me encargo de presentarte a mujeres eslavas —rusas, ucranianas o bielorrusas— que realmente sean compatibles contigo y que también estén buscando una relación seria, formar una pareja y construir una familia. ✨\n\nAntes de contarte más, déjame conocerte un poquito, eres soltero?",
    ),
    2: (
        "Hola! 🤍 Soy Anna, la fundadora de MatchMatch. Con gusto te cuento del evento, es nuestro Slavic Latino Night, una noche para conocer mujeres eslavas solteras que buscan algo serio.\n\nAntes de darte todos los detalles, déjame conocerte un poquito. ¿Eres soltero? Si te late, con gusto te cuento cómo funciona todo.",
        "Hola! 🤍 Soy Anna, fundadora de Match Match Agency. Con mucho gusto te cuento sobre nuestro evento, Slavic Latino Night: una noche en la que podrás conocer a 30 mujeres eslavas solteras que están buscando una relación seria.\n\nAntes de compartirte todos los detalles, me gustaría conocerte un poquito. ¿Estás soltero? Si te late, con gusto te explico cómo funciona todo.",
    ),
    3: (
        "Hola! Qué bueno que te interesa 🤍 soy Anna de MatchMatch\n\nJusto eso hago: como matchmaker personal te busco una mujer eslava (rusa, ucraniana o bielorrusa) que quiera algo serio, pareja y familia, y te acompaño en todo el proceso ✨\n\nAntes de platicarte más, eres soltero?",
        "Hola! Gracias por tu interés! 🤍 Soy Anna, fundadora de Match Match Agency www.matchmatchagency.com/es\n\nJusto eso hago: como matchmaker personal, te presento a mujeres eslavas (rusas, ucranianas o bielorrusas) que también están buscando una relación seria, y te acompaño durante todo el proceso para que encuentres y construyas una relación feliz con la mujer que más te guste. ✨\n\nAntes de platicarte más, eres soltero?",
    ),
    22: (
        "Te entiendo 🤍 MatchMatch es una agencia matrimonial registrada formalmente. Hemos organizado varios eventos en CDMX, tenemos clientes activos y todo está documentado\n\nCheca mi Instagram: @rusaencdmx, ahí ves fotos de eventos pasados y la dinámica\n\nEn la videollamada te puedo enseñar testimonios. Cuándo te queda? 😊",
        "Nuestra agencia, Match Match Agency (www.matchmatchagency.com/es), es una agencia de matchmaking. Hemos organizado más de 26 eventos en la Ciudad de México y contamos con clientes activos en México, Estados Unidos, Chile, Argentina, Francia y Emiratos Árabes Unidos.\n\nTengo casi un millón de seguidores en Instagram.com/rusaencdmx y casi dos millones en TikTok.com/@rusaencdmx.\n\nEn la videollamada también puedo enseñarte testimonios de nuestros clientes y contarte con más detalle cómo funciona todo. Cuándo tienes 30 minutos para platicar conmigo? 😊",
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

        remaining = await pool.fetch("SELECT id FROM scenarios WHERE template_es ILIKE '%MatchMatch%'")
        if remaining:
            print(f"⚠ ОСТАЛИСЬ с 'MatchMatch': {[r['id'] for r in remaining]}")
        else:
            print("✓ старое имя бренда больше нигде в template_es не осталось")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
