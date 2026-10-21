"""Правки прод-сценариев под решения владелицы 2026-10-21 (оба режима ответов).

  №39 — убрать предложение про 5,500 с другом (бот 5,500 не называет никогда);
  №2, №15 — название «Slavic-Latino Connect Night» (как на сайте);
  №2, №15, №51 — «entrantes» → «botanas»; без зачёркнутой «было» ([event_promo] убрать:
         8000 на сайте — будущая ступень цены, а не старая);
  №40 не меняется (решение владелицы 2026-10-21: ни текст, ни эмодзи).

⛔ НЕ запускать до «деплой ок». Перед записью — бэкап затронутых строк в docs/backups/.
Замена — точной подстрокой (ровно одно вхождение), иначе ничего не пишем. Одна транзакция.
Эмбеддинги не пересчитываются: trigger_es не меняется. Откат: scripts/restore_scenarios.py.

venv/bin/python -m scripts.patch_rag_scenarios_2026_10                      # сухой прогон
venv/bin/python -m scripts.patch_rag_scenarios_2026_10 --force              # запись
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import db

ROOT = Path(__file__).resolve().parent.parent

# (id, было, станет) — точные подстроки template_es (сверено с бэкапом 2026-10-21).
PATCHES = [
    (39, "\n\nSi tu pregunta era sobre el EVENTO: ahí sí hay una excepción — si vienes con un "
         "amigo, el precio es de 5,500 MXN por persona (en vez de 6,000).", ""),
    (2, "Slavic Latino Night", "Slavic-Latino Connect Night"),
    (2, "[event_price_nonmember] MXN[event_promo] e incluye bebida de bienvenida y entrantes",
     "[event_price_nonmember] MXN e incluye bebida de bienvenida y botanas"),
    (15, "Slavic Latino Night", "Slavic-Latino Connect Night"),
    (15, "[event_price_nonmember] MXN[event_promo], incluye bebida de bienvenida, entrantes",
     "[event_price_nonmember] MXN, incluye bebida de bienvenida, botanas"),
    (51, "[event_price_nonmember] MXN[event_promo] e incluye bebida de bienvenida, entrantes",
     "[event_price_nonmember] MXN e incluye bebida de bienvenida, botanas"),
]


def plan(rows: dict[int, str], patches: list[tuple]) -> tuple[dict[int, str], list[str]]:
    """Чистая функция: новые тексты по id и список ошибок (вхождение не ровно одно)."""
    new = dict(rows)
    errors = []
    for sid, old, repl in patches:
        text = new.get(sid)
        if text is None:
            errors.append(f"#{sid}: строки нет")
            continue
        n = text.count(old)
        if n != 1:
            errors.append(f"#{sid}: вхождений {n} вместо 1: {old[:60]!r}")
            continue
        new[sid] = text.replace(old, repl)
    return {k: v for k, v in new.items() if v != rows.get(k)}, errors


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    patches = PATCHES
    ids = sorted({p[0] for p in patches})

    await db.init_pool()
    try:
        pool = db._get_pool()
        rows = await pool.fetch(
            "SELECT id, title, trigger_description, trigger_es, template_es, links, rules, mode, "
            "ai_allowed, blocks_lead, is_active, trigger_type, embedding::text AS embedding "
            "FROM scenarios WHERE id = ANY($1::int[]) ORDER BY id", ids)
        current = {r["id"]: r["template_es"] for r in rows}
        changes, errors = plan(current, patches)
        for sid, text in changes.items():
            print(f"── #{sid}\nБЫЛО:\n{current[sid]}\nСТАНЕТ:\n{text}\n")
        if errors:
            print("❌ Ничего не пишу:\n  " + "\n  ".join(errors))
            return 1
        if not args.force:
            print("Сухой прогон. Для записи: --force")
            return 0
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = ROOT / "docs" / "backups" / f"scenarios_{'_'.join(map(str, ids))}_before_rag_patch_{ts}.json"
        backup.write_text(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1),
                          encoding="utf-8")
        print(f"Бэкап: {backup.relative_to(ROOT)}")
        async with pool.acquire() as conn:
            async with conn.transaction():
                for sid, text in changes.items():
                    await conn.execute("UPDATE scenarios SET template_es = $2, updated_at = now() "
                                       "WHERE id = $1", sid, text)
        print(f"Записано: {sorted(changes)}. Откат: scripts/restore_scenarios.py --file {backup.relative_to(ROOT)}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
