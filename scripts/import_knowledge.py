"""Импорт knowledge/seed.json в таблицу knowledge (миграция 008) с эмбеддингами.

⛔ НЕ запускать до «деплой ок». Upsert по id: существующие записи обновляются, записи,
которых нет в seed, НЕ удаляются (выключаются вручную activo=false). Эмбеддинг по
cuando_usar (text-embedding-3-small), один запрос на весь seed (~93 строки, < $0.001).

venv/bin/python -m scripts.import_knowledge            # сухой прогон: что вставится/обновится
venv/bin/python -m scripts.import_knowledge --force    # запись
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date

sys.path.insert(0, ".")

import ai_conversation
import db
import knowledge

COLS = ["id", "tipo", "titulo", "contenido_es", "cuando_usar", "como_responder", "notas_tono",
        "frases_ejemplo", "malo", "bueno", "por_que", "situacion", "literal", "accion",
        "escalar_despues", "escenario_literal", "confirmado", "evento_id", "valid_from",
        "valid_to", "activo", "fuente", "ru"]
_DATE_COLS = {"valid_from", "valid_to"}


def build_sql() -> str:
    """Upsert одной записи: колонки COLS + эмбеддинг (::vector). jsonb-поля передаются
    python-объектами — их кодирует кодек пула (db._init_connection)."""
    vals = ", ".join(f"${i + 1}" for i in range(len(COLS))) + f", ${len(COLS) + 1}::vector"
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS[1:] + ["embedding"])
    return (f"INSERT INTO knowledge ({', '.join(COLS)}, embedding) VALUES ({vals}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = now()")


def row_params(rec: dict) -> list:
    out = []
    for c in COLS:
        v = rec.get(c)
        out.append(date.fromisoformat(v) if c in _DATE_COLS and v else v)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    recs = knowledge.load_seed()
    await db.init_pool()
    try:
        pool = db._get_pool()
        existing = {r["id"] for r in await pool.fetch("SELECT id FROM knowledge")}
        new = [r["id"] for r in recs if r["id"] not in existing]
        upd = [r["id"] for r in recs if r["id"] in existing]
        extra = sorted(existing - {r["id"] for r in recs})
        print(f"Вставить: {len(new)}, обновить: {len(upd)}, в таблице есть, в seed нет "
              f"(не трогаю): {extra or 'нет'}")
        if not args.force:
            print("Сухой прогон. Для записи: --force")
            return 0
        cost = ai_conversation.Cost()
        vecs = await ai_conversation._embed_batch([r["cuando_usar"] or r["titulo"] for r in recs], cost)
        sql = build_sql()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for r, v in zip(recs, vecs):
                    await conn.execute(sql, *row_params(r), json.dumps(v))
        print(f"Записано {len(recs)} записей, эмбеддинги ${cost.usd:.5f}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
