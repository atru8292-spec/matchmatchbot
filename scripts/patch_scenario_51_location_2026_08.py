"""Патч сценария #51: добавлена локация ([event_address]) в template_es и
триггер-фразы про местоположение в trigger_es (2026-08-26, живой тест). Меняем
trigger_es → пересчитываем embedding (иначе RAG-матч сломается — эмбеддинг должен
соответствовать новому тексту триггера, см. паттерн patch_age_limit_76_2026_07.py).

Запуск: venv/bin/python -m scripts.patch_scenario_51_location_2026_08
Идемпотентен: повторный прогон перезапишет теми же значениями.
"""
import asyncio

import ai
import db


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def main() -> None:
    await db.init_pool()
    pool = db._get_pool()
    try:
        row = await pool.fetchrow("SELECT trigger_es FROM scenarios WHERE id=51")
        trigger_es = row["trigger_es"]
        print(f"trigger_es actual: {trigger_es!r}")

        emb = _vector_literal(await ai._embed(trigger_es))
        await pool.execute(
            "UPDATE scenarios SET embedding=$1::vector, updated_at=now() WHERE id=51",
            emb,
        )
        print("✓ embedding сценария #51 пересчитан под текущий trigger_es.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
