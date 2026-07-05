"""Заливка 49 сценариев в scenarios + эмбеддинги по trigger_es (RAG).

Одноразовый скрипт (можно перезапускать — очищает и заливает заново).
- DELETE старых записей (Mila).
- INSERT 49 из scenarios_49_final.json.
- embeddings по trigger_es (испанский — лиды пишут на испанском!) через text-embedding-3-small.

Запуск: ./venv/bin/python -m scripts.load_scenarios
"""
from __future__ import annotations

import asyncio
import json
import sys

import asyncpg
import httpx

from config import settings

# ⛔ GUARD: JSON устарел (49 + старый WIP), прод — источник правды (51 сценарий + патчи:
# separado не блок, №50 T-1, №51 цена/детали, event, estafa, клуб). Reseed сотрёт №50/№51
# и все патчи. Запуск разрешён ТОЛЬКО с явным --force после пересборки JSON из прода.
if __name__ == "__main__" and "--force" not in sys.argv:
    sys.exit(
        "⛔ load_scenarios заблокирован: JSON устарел, reseed сотрёт прод-сценарии №50/№51 "
        "и все патчи. Прод — источник правды. См. CLAUDE.md / память load-scenarios-danger.\n"
        "Если точно уверен (JSON пересобран из прода) — запусти с --force."
    )

SCENARIOS_FILE = "scenarios_49_final.json"
# Фиксированные ответы (без AI-вариаций): блокировки + скидка + "ты бот".
AI_DISALLOWED_IDS = {7, 8, 9, 10, 12, 15, 27, 28, 29, 39, 40}  # 15 — приглашение на ивент дословно


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Получить эмбеддинги для списка текстов одним батч-запросом OpenAI."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.openai_embedding_model, "input": texts},
        )
        r.raise_for_status()
        data = r.json()["data"]
    # OpenAI возвращает в порядке входа, но сортируем по index для надёжности.
    data.sort(key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def _vector_literal(vec: list[float]) -> str:
    """pgvector-литерал '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def main() -> None:
    scenarios = json.load(open(SCENARIOS_FILE, encoding="utf-8"))
    assert len(scenarios) == 49, f"ожидалось 49 сценариев, получено {len(scenarios)}"

    # Эмбеддинги строго по trigger_es (испанские фразы лидов).
    trigger_es_list = [s["trigger_es"] for s in scenarios]
    print(f"генерю эмбеддинги для {len(trigger_es_list)} trigger_es ...")
    embeddings = await _embed_batch(trigger_es_list)
    assert len(embeddings) == 49 and len(embeddings[0]) == 1536, "неверная размерность эмбеддингов"

    conn = await asyncpg.connect(dsn=settings.supabase_db_dsn, timeout=30)
    try:
        async with conn.transaction():
            old = await conn.fetchval("SELECT count(*) FROM scenarios")
            await conn.execute("DELETE FROM scenarios")
            print(f"удалено старых записей: {old}")

            for s, emb in zip(scenarios, embeddings):
                template_es = "\n\n".join(s["messages"])
                await conn.execute(
                    "INSERT INTO scenarios "
                    "(id, title, trigger_description, trigger_es, template_es, mode, "
                    " blocks_lead, rules, ai_allowed, is_active, embedding) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,true,$10::vector)",
                    s["id"], s["title"], s["trigger"], s["trigger_es"], template_es,
                    s["mode"], bool(s["blocks_lead"]), s.get("note") or "",
                    s["id"] not in AI_DISALLOWED_IDS,
                    _vector_literal(emb),
                )
        print("залито 49 сценариев")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
