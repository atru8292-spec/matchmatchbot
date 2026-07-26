"""Патч: возрастной лимит поднят с 65 до 76 лет (владелец, 2026-07-26).

Промпт (anna_prompt_v5.md) обновлён отдельно (4 места: "28-65" → "28-76").
Этот скрипт правит только сценарий №8 «Лиду больше 65» в БД — его
trigger_es содержал примеры возрастов (68, 70), которые раньше были ЗА
пределами лимита, а теперь попадают ВНУТРЬ нового диапазона (28-76).
template_es чисел не содержит — не трогаем.

Меняем trigger_es → пересчитываем embedding (иначе RAG-матч сломается,
эмбеддинг должен соответствовать новому тексту триггера).

Запуск: venv/bin/python -m scripts.patch_age_limit_76_2026_07
Идемпотентен: повторный прогон просто перезапишет теми же значениями.
"""
import asyncio

import ai
import db


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


NEW_TRIGGER = "tengo 78 años / tengo 80 / soy mayor"


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        before = await pool.fetchval("SELECT trigger_es FROM scenarios WHERE id=8")
        print(f"ДО:  trigger_es={before!r}")

        emb = _vector_literal(await ai._embed(NEW_TRIGGER))
        await pool.execute(
            "UPDATE scenarios SET trigger_es=$1, embedding=$2::vector, updated_at=now() "
            "WHERE id=8",
            NEW_TRIGGER, emb,
        )

        after = await pool.fetchval("SELECT trigger_es FROM scenarios WHERE id=8")
        print(f"ПОСЛЕ: trigger_es={after!r}")
        assert after == NEW_TRIGGER, "trigger_es не совпал!"
        print("✓ Патч #8 применён (trigger_es + embedding). template_es/mode не тронуты.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
