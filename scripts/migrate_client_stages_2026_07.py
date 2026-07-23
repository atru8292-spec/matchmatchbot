"""Миграция прод-БД: упрощаем клиентские стадии до одной общей.

client_starter / client_standard / client_vip → client_agency («Клиент агентства»).
Тарифов услуги больше нет (Standard/VIP решаются индивидуально на звонке у Ани), поэтому
все подтипы клиента сводим к одной метке. Данные о том, что человек уже клиент, не теряются.
event_attended («Гость ивента») не трогаем — это отдельный тип.

Историю в funnel_events НЕ переписываем (это лог прошлого). Меняем только текущий
leads.funnel_stage. Констрейнта на колонку нет (тип text) — простой UPDATE.

⚠️ Guard: запуск только с --force:
venv/bin/python -m scripts.migrate_client_stages_2026_07 --force
"""
import asyncio
import sys

import db

OLD_STAGES = ("client_starter", "client_standard", "client_vip")
NEW_STAGE = "client_agency"


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    await db.init_pool()
    try:
        pool = db._get_pool()
        before = await pool.fetch(
            "SELECT funnel_stage, count(*) AS n FROM leads "
            "WHERE funnel_stage = ANY($1::text[]) GROUP BY funnel_stage", list(OLD_STAGES))
        print("До миграции:", {r["funnel_stage"]: r["n"] for r in before} or "нет записей")

        res = await pool.execute(
            "UPDATE leads SET funnel_stage = $1 WHERE funnel_stage = ANY($2::text[])",
            NEW_STAGE, list(OLD_STAGES))
        print(f"UPDATE: {res}")

        # Проверка: старых стадий не осталось
        left = await pool.fetchval(
            "SELECT count(*) FROM leads WHERE funnel_stage = ANY($1::text[])", list(OLD_STAGES))
        assert left == 0, f"остались старые стадии: {left}"
        now = await pool.fetchval(
            "SELECT count(*) FROM leads WHERE funnel_stage = $1", NEW_STAGE)
        print(f"✅ Готово. Записей client_agency сейчас: {now}, старых client_* не осталось.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
