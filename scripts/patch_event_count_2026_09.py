"""Патч прод-БД: актуальная дата ивента 14.10.26 (rusaencdmx.com/14-10-26-slavic-latino-night)
подтвердила «¡Este es nuestro evento número 29!» — значит проведено 28 предыдущих, а не 27.

- Сценарий #22 («Это безопасно? Не развод?»): «más de 27 eventos» → «más de 28 eventos».
  Остальные факты (цена 6,000, дата, адрес, ссылка) уже актуальны в app_settings —
  проверено 2026-09-21, обновлены кем-то ранее в тот же день, совпадают с сайтом 1:1.

Идемпотентно: replace() по подстроке уже-изменённого текста не находит цель → no-op.
trigger_es не трогаем → embedding не пересчитывается.

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_event_count_2026_09 --force
"""
import asyncio
import sys

import db

REPLACES = [
    (22, "más de 27 eventos", "más de 28 eventos"),
]


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, old, new in REPLACES:
            row_before = await db.get_scenario_row(sid)
            if not row_before or old not in row_before["template_es"]:
                print(f"· #{sid}: подстрока не найдена (уже применено?) — пропуск")
                continue
            await pool.execute(
                "UPDATE scenarios SET template_es = replace(template_es, $1, $2), updated_at=now() "
                "WHERE id = $3", old, new, sid)
            row = await db.get_scenario_row(sid)
            assert row and new in row["template_es"], f"#{sid}: замена не применилась"
            print(f"✓ #{sid}: '{old}' → '{new}'")
        print("\n✅ Счётчик ивентов обновлён.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
