"""Патч: сценарии №7/№8 (возрастной фильтр) переведены на ai_allowed=True.

Найдено проактивным тестированием после поднятия лимита 65→76 (см. коммит
ac76033): жёсткий фикс-шаблон (ai_allowed=False) диспетчеризуется ЧИСТО по
семантической близости RAG, без реальной числовой логики. "tengo 76 años"
(валидный возраст) давал score=0.765 по сценарию №8 "Лиду больше 76"
(порог блокировки FIXED_BLOCK_SCORE=0.60) — т.е. ЛЮБОЙ 76-летний лид
слепо блокировался фикс-шаблоном, даже не доходя до AI, который
корректно знает границу 28-76 из промпта.

Перевод на ai_allowed=True: сценарий остаётся в контексте (RAG) как
стилевой ориентир, но решение о блокировке принимает AI, читая точный
возраст лида и правило из промпта — не смещается семантической
близостью к примерам в trigger_es. Проверено живыми вызовами: 76 лет
проходит, 77 блокируется, 27/28/90 — как ожидается (граница 28-76).

Тот же класс риска у №7 ("Лиду меньше 28") — переведён по аналогии,
хотя там граница (28) менее уязвима на практике.

НЕ трогает: trigger_es, template_es, mode, blocks_lead.

Запуск: venv/bin/python -m scripts.patch_age_scenarios_ai_allowed_2026_07
Идемпотентен.
"""
import asyncio

import db


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        rows = await pool.fetch(
            "UPDATE scenarios SET ai_allowed=true, updated_at=now() WHERE id IN (7,8) "
            "RETURNING id, title, ai_allowed, mode, blocks_lead"
        )
        for r in rows:
            print(f"✓ #{r['id']} {r['title']!r}: ai_allowed={r['ai_allowed']}, "
                  f"mode={r['mode']}, blocks_lead={r['blocks_lead']}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
