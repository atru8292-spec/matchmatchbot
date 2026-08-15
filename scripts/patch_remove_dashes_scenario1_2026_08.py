"""Патч прод-БД: сценарий #1 (приветствие) — убраны длинные тире вокруг "rusas, ucranianas o bielorrusas".

Баг найден в живом тесте (2026-08-15): "Yo personalmente me encargo de presentarte a
mujeres eslavas —rusas, ucranianas o bielorrusas— que..." — тире-скобки читаются
странно в чате. Промпт (anna_prompt_v5.md, "Evita: Guiones largos en el texto")
уже это запрещает, но конкретно в сценарии #1 (approved-текст, AI обязан использовать
"casi literal") тире остались — правило не спасло, т.к. это буквальный текст сценария.

Запуск: venv/bin/python -m scripts.patch_remove_dashes_scenario1_2026_08
"""
import asyncio

import db

OLD = "mujeres eslavas —rusas, ucranianas o bielorrusas— que realmente"
NEW = "mujeres eslavas, rusas, ucranianas o bielorrusas, que realmente"


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        row = await db.get_scenario_row(1)
        assert OLD in row["template_es"], "старый текст не найден, проверь руками"
        updated = row["template_es"].replace(OLD, NEW)
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=1", updated,
        )
        check = await db.get_scenario_row(1)
        assert check["template_es"] == updated, "не применилось"
        assert "—" not in check["template_es"], "тире всё ещё есть"
        print("✓ #1 обновлён, тире убраны")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
