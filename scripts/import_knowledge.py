"""Импорт knowledge/seed.json в таблицу knowledge (миграция 008) с эмбеддингами.

⛔ НЕ запускать до «деплой ок». Upsert по id: существующие записи обновляются, записи,
которых нет в seed, НЕ удаляются (выключаются вручную activo=false). Эмбеддинг по
cuando_usar (text-embedding-3-small), один запрос на весь seed (< $0.001).

Перед любой записью — бэкап всей таблицы knowledge в docs/backups/. Если запись в БД правили
вручную (updated_by не пустой — мини-CRM), её activo, confirmado и contenido_es seed не
перезаписывает; остальные поля обновляются. --force перезаписывает и их (тоже с бэкапом).

venv/bin/python -m scripts.import_knowledge            # сухой прогон: что вставится/обновится
venv/bin/python -m scripts.import_knowledge --write    # запись, ручные правки целы
venv/bin/python -m scripts.import_knowledge --force    # запись поверх ручных правок
venv/bin/python -m scripts.import_knowledge --write --no-embeddings   # без OpenAI: новые записи
        без вектора (бот посчитает сам при поиске), у существующих векторы не трогаются
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import ai_conversation
import db
import knowledge

COLS = ["id", "tipo", "titulo", "contenido_es", "cuando_usar", "como_responder", "notas_tono",
        "frases_ejemplo", "malo", "bueno", "por_que", "situacion", "literal", "accion",
        "escalar_despues", "escenario_literal", "confirmado", "evento_id", "valid_from",
        "valid_to", "activo", "fuente", "ru"]
_DATE_COLS = {"valid_from", "valid_to"}
# Поля, которые владелица правит в мини-CRM: при ручной правке (updated_by) seed их не трогает.
PROTECTED = ("activo", "confirmado", "contenido_es")
ROOT = Path(__file__).resolve().parent.parent


def build_sql(force: bool = False, embeddings: bool = True) -> str:
    """Upsert одной записи: колонки COLS + эмбеддинг (::vector). jsonb-поля передаются
    python-объектами — их кодирует кодек пула (db._init_connection). Без force поля
    PROTECTED у записи с непустым updated_by остаются как в БД. embeddings=False — вектор
    не пишется (NULL у новой записи, у существующей остаётся прежний)."""
    if not embeddings:
        vals = ", ".join(f"${i + 1}" for i in range(len(COLS)))

        def upd_n(c):
            if c in PROTECTED and not force:
                return (f"{c} = CASE WHEN COALESCE(knowledge.updated_by, '') = '' "
                        f"THEN EXCLUDED.{c} ELSE knowledge.{c} END")
            return f"{c} = EXCLUDED.{c}"
        return (f"INSERT INTO knowledge ({', '.join(COLS)}) VALUES ({vals}) "
                f"ON CONFLICT (id) DO UPDATE SET {', '.join(upd_n(c) for c in COLS[1:])}, "
                f"updated_at = now()")
    vals = ", ".join(f"${i + 1}" for i in range(len(COLS))) + f", ${len(COLS) + 1}::vector"

    def upd(c):
        if c in PROTECTED and not force:
            return (f"{c} = CASE WHEN COALESCE(knowledge.updated_by, '') = '' "
                    f"THEN EXCLUDED.{c} ELSE knowledge.{c} END")
        return f"{c} = EXCLUDED.{c}"
    updates = ", ".join(upd(c) for c in COLS[1:] + ["embedding"])
    return (f"INSERT INTO knowledge ({', '.join(COLS)}, embedding) VALUES ({vals}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = now()")


async def backup_table(pool) -> Path:
    """Вся таблица knowledge (без эмбеддингов) в docs/backups/ перед записью."""
    rows = await pool.fetch("SELECT * FROM knowledge ORDER BY id")
    data = [{k: v for k, v in dict(r).items() if k != "embedding"} for r in rows]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "docs" / "backups" / f"knowledge_before_import_{ts}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def row_params(rec: dict) -> list:
    out = []
    for c in COLS:
        v = rec.get(c)
        out.append(date.fromisoformat(v) if c in _DATE_COLS and v else v)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="записать, ручные правки целы")
    ap.add_argument("--force", action="store_true", help="записать поверх ручных правок")
    ap.add_argument("--no-embeddings", action="store_true", help="без OpenAI: векторы не считать")
    args = ap.parse_args()
    recs = knowledge.load_seed()
    await db.init_pool()
    try:
        pool = db._get_pool()
        rows = await pool.fetch("SELECT id, COALESCE(updated_by, '') AS updated_by FROM knowledge")
        existing = {r["id"] for r in rows}
        manual = sorted(r["id"] for r in rows if r["updated_by"])
        new = [r["id"] for r in recs if r["id"] not in existing]
        upd = [r["id"] for r in recs if r["id"] in existing]
        extra = sorted(existing - {r["id"] for r in recs})
        print(f"Вставить: {len(new)}, обновить: {len(upd)}, в таблице есть, в seed нет "
              f"(не трогаю): {extra or 'нет'}")
        print(f"Правлены вручную ({', '.join(PROTECTED)} "
              f"{'ПЕРЕЗАПИШУ (--force)' if args.force else 'не трогаю'}): {manual or 'нет'}")
        if not (args.write or args.force):
            print("Сухой прогон. Для записи: --write (или --force поверх ручных правок)")
            return 0
        backup = await backup_table(pool)
        print(f"Бэкап: {backup.relative_to(ROOT)}")
        if args.no_embeddings:
            sql = build_sql(force=args.force, embeddings=False)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for r in recs:
                        await conn.execute(sql, *row_params(r))
            print(f"Записано {len(recs)} записей без эмбеддингов (OpenAI не вызывался)")
            return 0
        cost = ai_conversation.Cost()
        vecs = await ai_conversation._embed_batch([r["cuando_usar"] or r["titulo"] for r in recs], cost)
        sql = build_sql(force=args.force)
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
