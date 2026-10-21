"""Бэкап перед переделкой ответов (режим rag): все строки scenarios, все app_settings
и копия anna_prompt_v5.md в docs/backups/ с меткой времени.

Только чтение: пул открывается с default_transaction_read_only=on, любая запись упадёт.
Эмбеддинги сохраняются текстом (embedding::text), чтобы при восстановлении не
пересчитывать их через OpenAI.

Запуск: venv/bin/python -m scripts.backup_reply_mode
Восстановление сценариев: scripts/restore_scenarios.py (НЕ scripts.load_scenarios).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import asyncpg

import db

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "docs" / "backups"
PROMPT = ROOT / "anna_prompt_v5.md"

SCENARIOS_SQL = (
    "SELECT id, title, trigger_description, trigger_es, template_es, links, rules, mode, "
    "ai_allowed, blocks_lead, is_active, trigger_type, created_at, updated_at, "
    "embedding::text AS embedding FROM scenarios ORDER BY id"
)
SETTINGS_SQL = "SELECT key, value, updated_at FROM app_settings ORDER BY key"


def _jsonable(v):
    """datetime/date → ISO-строка, остальное как есть (json.dumps default)."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    raise TypeError(f"не сериализуется: {type(v)}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=_jsonable),
                    encoding="utf-8")


async def _read_only_pool_init() -> None:
    """Пул БД только на чтение (как в scripts/smoke_launch.py)."""
    orig = asyncpg.create_pool

    async def ro_pool(*a, **k):
        k.setdefault("server_settings", {})["default_transaction_read_only"] = "on"
        return await orig(*a, **k)

    asyncpg.create_pool = ro_pool
    try:
        await db.init_pool()
    finally:
        asyncpg.create_pool = orig


async def main() -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    await _read_only_pool_init()
    try:
        pool = db._get_pool()
        scenarios = [dict(r) for r in await pool.fetch(SCENARIOS_SQL)]
        app_settings = [dict(r) for r in await pool.fetch(SETTINGS_SQL)]
    finally:
        await db.close_pool()

    out_scen = BACKUP_DIR / f"scenarios_all_before_rag_{ts}.json"
    out_set = BACKUP_DIR / f"app_settings_before_rag_{ts}.json"
    out_prompt = BACKUP_DIR / f"anna_prompt_v5_before_rag_{ts}.md"
    _write_json(out_scen, scenarios)
    _write_json(out_set, app_settings)
    shutil.copy2(PROMPT, out_prompt)

    no_emb = [s["id"] for s in scenarios if not s.get("embedding")]
    print(f"scenarios: {len(scenarios)} строк (без эмбеддинга: {no_emb or 'нет'})")
    print(f"app_settings: {len(app_settings)} ключей")
    for p in (out_scen, out_set, out_prompt):
        print(f"{p.relative_to(ROOT)}  sha256={_sha256(p)}")
    if _sha256(out_prompt) != _sha256(PROMPT):
        raise SystemExit("❌ копия промпта не совпадает с оригиналом")


if __name__ == "__main__":
    asyncio.run(main())
