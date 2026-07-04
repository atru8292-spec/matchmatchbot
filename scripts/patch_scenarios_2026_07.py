"""Точечный патч живых сценариев (БЕЗ полного reseed) — раунд 2026-07.

Обновляет только изменённые строки, НЕ делает DELETE (системный сценарий 50 остаётся):
  - №1/№2/№3 — крючок про суть MatchMatch в первом сообщении (template_es).
  - №26 — явная формулировка про передачу контакта при взаимном интересе (template_es + rules).
  - №19 — расширенный trigger_es (+ пере-генерация embedding, т.к. триггер изменился).

Остальные сценарии и №50 не трогаются. Источник текста — scenarios_49_final.json.
Запуск: ./venv/bin/python -m scripts.patch_scenarios_2026_07
"""
from __future__ import annotations

import asyncio
import json

import asyncpg
import httpx

from config import settings

SCENARIOS_FILE = "scenarios_49_final.json"
# id → нужна ли пере-генерация embedding (только если менялся trigger_es).
PATCH_IDS = {1: False, 2: False, 3: False, 19: True, 26: False}


async def _embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.openai_embedding_model, "input": text},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def main() -> None:
    scenarios = {s["id"]: s for s in json.load(open(SCENARIOS_FILE, encoding="utf-8"))}
    conn = await asyncpg.connect(dsn=settings.supabase_db_dsn, timeout=30)
    try:
        # Безопасность: убеждаемся, что системный сценарий 50 на месте ДО и ПОСЛЕ.
        has_50_before = await conn.fetchval("SELECT count(*) FROM scenarios WHERE id = 50")
        total_before = await conn.fetchval("SELECT count(*) FROM scenarios")
        print(f"до патча: всего сценариев={total_before}, сценарий 50 присутствует={bool(has_50_before)}")

        async with conn.transaction():
            for sid, reembed in PATCH_IDS.items():
                s = scenarios[sid]
                template_es = "\n\n".join(s["messages"])
                rules = s.get("note") or ""
                trigger_es = s["trigger_es"]
                if reembed:
                    emb = _vector_literal(await _embed(trigger_es))
                    await conn.execute(
                        "UPDATE scenarios SET template_es=$1, rules=$2, trigger_es=$3, "
                        "embedding=$4::vector WHERE id=$5",
                        template_es, rules, trigger_es, emb, sid,
                    )
                    print(f"  №{sid}: template+rules+trigger_es+embedding обновлены")
                else:
                    await conn.execute(
                        "UPDATE scenarios SET template_es=$1, rules=$2, trigger_es=$3 WHERE id=$4",
                        template_es, rules, trigger_es, sid,
                    )
                    print(f"  №{sid}: template+rules обновлены (trigger_es без изменений)")

        total_after = await conn.fetchval("SELECT count(*) FROM scenarios")
        has_50_after = await conn.fetchval("SELECT count(*) FROM scenarios WHERE id = 50")
        print(f"после патча: всего сценариев={total_after}, сценарий 50 присутствует={bool(has_50_after)}")

        # Показать первые строки обновлённых template для глазами.
        for sid in PATCH_IDS:
            tmpl = await conn.fetchval("SELECT template_es FROM scenarios WHERE id=$1", sid)
            first = (tmpl or "").split("\n\n")[0]
            print(f"  проверка №{sid} 1-й баббл: {first[:70]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
