"""Патч прод-БД: цена ивента со скидкой — 5,000 MXN, старая 9,000 зачёркнута.

- app_settings: event_price_nonmember=5,000, event_price_old=9,000. Токен [event_promo]
  рендерит «(antes 9,000)» (sender._fill_event_vars) — зачёркнутая старая цена = скидка.
- В тексты #51/#16/#15 добавлен токен [event_promo] рядом с [event_price_nonmember].

Идемпотентно: replace() по подстроке уже-изменённого текста не находит цель → no-op.
trigger_es не трогаем → embedding не пересчитывается.

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_event_discount_2026_07 --force
"""
import asyncio
import sys

import db

PRICES = {"event_price_nonmember": "5,000", "event_price_old": "9,000"}

# (id, что ищем, на что меняем) — идемпотентно (после замены цель уже не совпадает)
REPLACES = [
    (51, "MXN e incluye bebida",
         "MXN[event_promo], precio especial con descuento, e incluye bebida"),
    (16, "la opción del evento ([event_price_nonmember] MXN).",
         "la opción del evento: [event_price_nonmember] MXN[event_promo]."),
    (15, "[event_price_nonmember] MXN una sola vez, incluye",
         "[event_price_nonmember] MXN[event_promo] una sola vez (precio especial con descuento), incluye"),
]


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    await db.init_pool()
    try:
        pool = db._get_pool()
        for k, v in PRICES.items():
            await db.set_setting(k, v)
        s = await db.get_settings(list(PRICES))
        assert s.get("event_price_nonmember") == "5,000" and s.get("event_price_old") == "9,000", \
            f"настройки цены не совпали: {s}"
        print(f"✓ app_settings: {s}")
        for sid, old, new in REPLACES:
            await pool.execute(
                "UPDATE scenarios SET template_es = replace(template_es, $1, $2), updated_at=now() "
                "WHERE id = $3", old, new, sid)
            row = await db.get_scenario_row(sid)
            assert row and "[event_promo]" in row["template_es"], f"#{sid}: нет [event_promo]"
            print(f"✓ #{sid}: [event_promo] на месте")
        print("\n✅ Скидка применена: 5,000 (antes 9,000).")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
