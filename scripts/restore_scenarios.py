"""Восстановление строк scenarios из бэкапа scripts/backup_reply_mode.py — строка за строкой.

⛔ Это НЕ scripts.load_scenarios: ничего не удаляет. Для каждой строки бэкапа:
  - строка есть в БД и отличается → UPDATE полей из RESTORE_FIELDS;
  - строки нет в БД → INSERT (с id из бэкапа);
  - строки в БД, которых нет в бэкапе, только показываются, НЕ удаляются.
Эмбеддинг берётся из бэкапа текстом (OpenAI не вызывается).

Режимы:
  venv/bin/python -m scripts.restore_scenarios --file <бэкап.json>            # сухой прогон: расхождения
  venv/bin/python -m scripts.restore_scenarios --file <бэкап.json> --verify   # только сверка, код выхода 1 при расхождениях
  venv/bin/python -m scripts.restore_scenarios --file <бэкап.json> --force    # запись, одна транзакция
  --ids 2,39,51  — ограничить набором id.
Сценарии читаются из БД на каждый запрос (db.search_scenarios_by_vector), рестарт не нужен.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

import db

RESTORE_FIELDS = ("title", "trigger_description", "trigger_es", "template_es", "links", "rules",
                  "mode", "ai_allowed", "blocks_lead", "is_active", "trigger_type", "embedding")

CURRENT_SQL = (
    "SELECT id, title, trigger_description, trigger_es, template_es, links, rules, mode, "
    "ai_allowed, blocks_lead, is_active, trigger_type, embedding::text AS embedding "
    "FROM scenarios ORDER BY id"
)


def _norm(v):
    """Сравнение без ложных расхождений: jsonb из asyncpg приходит строкой, из бэкапа — тоже,
    но с другим форматированием; приводим к python-объекту, если это JSON."""
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "[{":
            try:
                return json.loads(s)
            except ValueError:
                return v
    return v


def diff_rows(backup: list[dict], current: list[dict], ids: set[int] | None = None) -> dict:
    """Чистая функция: что поменяется. {'update': [(id, [поля])], 'insert': [id], 'extra': [id]}."""
    cur = {r["id"]: r for r in current}
    bak = {r["id"]: r for r in backup}
    if ids:
        cur = {k: v for k, v in cur.items() if k in ids}
        bak = {k: v for k, v in bak.items() if k in ids}
    update, insert = [], []
    for sid in sorted(bak):
        b = bak[sid]
        if sid not in cur:
            insert.append(sid)
            continue
        fields = [f for f in RESTORE_FIELDS if _norm(b.get(f)) != _norm(cur[sid].get(f))]
        if fields:
            update.append((sid, fields))
    extra = sorted(set(cur) - set(bak))
    return {"update": update, "insert": insert, "extra": extra}


def _param(field: str, value):
    """Значение для параметра SQL: links/rules — jsonb (строка JSON), остальное как есть."""
    if field in ("links", "rules") and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


def _cast(field: str) -> str:
    return {"embedding": "::vector", "links": "::jsonb", "rules": "::jsonb"}.get(field, "")


async def apply(pool, backup: list[dict], plan: dict) -> None:
    bak = {r["id"]: r for r in backup}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for sid, fields in plan["update"]:
                sets = ", ".join(f"{f} = ${i + 2}{_cast(f)}" for i, f in enumerate(fields))
                await conn.execute(
                    f"UPDATE scenarios SET {sets}, updated_at = now() WHERE id = $1",
                    sid, *[_param(f, bak[sid].get(f)) for f in fields])
            for sid in plan["insert"]:
                cols = ("id",) + RESTORE_FIELDS
                vals = ", ".join(f"${i + 1}{_cast(c)}" for i, c in enumerate(cols))
                await conn.execute(
                    f"INSERT INTO scenarios ({', '.join(cols)}) VALUES ({vals})",
                    sid, *[_param(f, bak[sid].get(f)) for f in RESTORE_FIELDS])


def _print_plan(plan: dict) -> None:
    for sid, fields in plan["update"]:
        print(f"  UPDATE #{sid}: {', '.join(fields)}")
    for sid in plan["insert"]:
        print(f"  INSERT #{sid} (нет в БД)")
    if plan["extra"]:
        print(f"  в БД есть, в бэкапе нет (НЕ трогаю): {plan['extra']}")
    n = len(plan["update"]) + len(plan["insert"])
    print(f"Расхождений: {n}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--ids", default="")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    backup = json.loads(Path(args.file).read_text(encoding="utf-8"))
    ids = {int(x) for x in args.ids.split(",") if x.strip()} or None
    await db.init_pool()
    try:
        pool = db._get_pool()
        current = [dict(r) for r in await pool.fetch(CURRENT_SQL)]
        plan = diff_rows(backup, current, ids)
        _print_plan(plan)
        if args.verify:
            return 1 if (plan["update"] or plan["insert"]) else 0
        if not args.force:
            print("Сухой прогон. Для записи: --force")
            return 0
        if plan["update"] or plan["insert"]:
            await apply(pool, backup, plan)
            after = diff_rows(backup, [dict(r) for r in await pool.fetch(CURRENT_SQL)], ids)
            print("После записи:")
            _print_plan(after)
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
