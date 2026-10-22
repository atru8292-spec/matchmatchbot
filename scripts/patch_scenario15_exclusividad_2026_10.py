"""Сценарий №15: убрать «toca ligero la exclusividad (eventos pasados agotados, cupo limitado)»
— давление и неподтверждённые факты (решение Арины 2026-10-22). Остальной текст не меняется.

⛔ Запись только после «деплой ок». Перед записью — бэкап строки в docs/backups/ и
~/matchmatch-backups/2026-10-22/. Замена — точной подстрокой (ровно одно вхождение), иначе
ничего не пишем. Эмбеддинг не пересчитывается: trigger_es не меняется.
Откат: scripts/restore_scenarios.py --file <бэкап> --ids 15 --force.

venv/bin/python -m scripts.patch_scenario15_exclusividad_2026_10           # сухой прогон
venv/bin/python -m scripts.patch_scenario15_exclusividad_2026_10 --force   # запись
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import db
from scripts.patch_rag_scenarios_2026_10 import plan

ROOT = Path(__file__).resolve().parent.parent
HOME_BACKUPS = Path.home() / "matchmatch-backups" / "2026-10-22"
PATCHES = [
    (15, ", toca ligero la exclusividad (eventos pasados agotados, cupo limitado)", ""),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    await db.init_pool()
    try:
        pool = db._get_pool()
        rows = await pool.fetch(
            "SELECT id, title, trigger_description, trigger_es, template_es, links, rules, mode, "
            "ai_allowed, blocks_lead, is_active, trigger_type, embedding::text AS embedding "
            "FROM scenarios WHERE id = 15")
        current = {r["id"]: r["template_es"] for r in rows}
        changes, errors = plan(current, PATCHES)
        for sid, text in changes.items():
            print(f"── #{sid} diff:")
            for line in difflib.unified_diff(current[sid].split("\n"), text.split("\n"), lineterm="", n=0):
                print("  ", line)
        if errors:
            print("❌ Ничего не пишу:\n  " + "\n  ".join(errors))
            return 1
        if not args.force:
            print("Сухой прогон. Для записи: --force (только после «деплой ок»)")
            return 0
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = ROOT / "docs" / "backups" / f"scenario_15_before_exclusividad_patch_{ts}.json"
        backup.write_text(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1, default=str),
                          encoding="utf-8")
        HOME_BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, HOME_BACKUPS / backup.name)
        print(f"Бэкап: {backup.relative_to(ROOT)} (+ копия в {HOME_BACKUPS})")
        async with pool.acquire() as conn:
            async with conn.transaction():
                for sid, text in changes.items():
                    await conn.execute("UPDATE scenarios SET template_es = $2, updated_at = now() "
                                       "WHERE id = $1", sid, text)
        print(f"Записано: {sorted(changes)}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
