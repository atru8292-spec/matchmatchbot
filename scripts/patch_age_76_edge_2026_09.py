"""Патч: 76 лет — ПОСЛЕДНИЙ допустимый возраст (28-76 включительно), не первый
отсекаемый (владелец подтвердил 2026-09-07, живой тест: лид 76 лет блокировался
3/3 раза сценарием №8 "Лиду больше 76" — trigger_es содержал "78/80/soy mayor",
RAG-эмбеддинг матчил голое число "76" достаточно близко к "78"/"80" по
семантике "пожилой возраст", независимо от того что 76 ещё ВНУТРИ диапазона).

Промпт (anna_prompt_v5.md) обновлён отдельно — явное "76 лет ВСЕГДА проходит,
блок только с 77". Этот скрипт правит только trigger_es сценария №8 в БД:
убирает всякую двусмысленность, явно указывает границу "с 77".
template_es чисел не содержит — не трогаем (тот же паттерн, что и
scripts/patch_age_limit_76_2026_07.py, поднимавший лимит с 65 до 76).

Меняем trigger_es → пересчитываем embedding (иначе RAG-матч сломается,
эмбеддинг должен соответствовать новому тексту триггера).

Запуск: venv/bin/python -m scripts.patch_age_76_edge_2026_09
Идемпотентен: повторный прогон просто перезапишет теми же значениями.
"""
import asyncio

import ai
import db


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


NEW_TRIGGER = "tengo 77 años / tengo 80 / tengo 85 / soy mayor de 76"


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
