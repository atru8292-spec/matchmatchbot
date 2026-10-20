"""Патч прод-БД под ивент 18.11.2026 (Slavic-Latino Connect Night, Хеллоуин):
  #2  — одна фраза про тематику Хеллоуин: костюм или маска от организаторов;
  #15 — то же в подсказке для AI, шаг [1];
  #51 — убрано время окончания ([event_end]): лидам его не называем, только начало.
«30 mujeres y 30 hombres solteros» и цены не трогаем.

После 18.11 Хеллоуин убрать (обратный патч), иначе бот скажет его про следующий ивент.

Бэкап трёх строк — в docs/backups/ до записи. Замены — точным совпадением подстроки
(ровно одно вхождение), иначе ничего не пишем. Одна транзакция. Сценарии кэшируются
в памяти процесса — после патча нужен рестарт сервиса.

⚠️ Guard: без --force только показывает, что поменяется:
venv/bin/python -m scripts.patch_event_halloween_2026_11           # сухой прогон
venv/bin/python -m scripts.patch_event_halloween_2026_11 --force   # запись
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import db

# (id сценария, было, станет) — подстроки template_es
PATCHES = [
    (2,
     "conocer a 30 mujeres eslavas solteras que buscan una relación seria.\n\n",
     "conocer a 30 mujeres eslavas solteras que buscan una relación seria. Esta vez es "
     "temática de Halloween: puedes venir disfrazado y, si no tienes disfraz, te damos "
     "una máscara.\n\n"),
    (15,
     "toca ligero la exclusividad (eventos pasados agotados, cupo limitado).]",
     "toca ligero la exclusividad (eventos pasados agotados, cupo limitado). Menciona "
     "también que esta vez es temática de Halloween: puede venir disfrazado y, si no "
     "tiene disfraz, le damos una máscara.]"),
    (51,
     "Comienza a las [event_start] y termina a [event_end] (puedes venir más tarde), "
     "en [event_address].",
     "Comienza a las [event_start] (puedes venir más tarde), en [event_address]."),
]


async def main() -> None:
    force = "--force" in sys.argv
    await db.init_pool()
    try:
        pool = db._get_pool()
        ids = [p[0] for p in PATCHES]
        rows = {r["id"]: dict(r) for r in await pool.fetch(
            "SELECT id, title, template_es, updated_at FROM scenarios WHERE id = ANY($1)", ids)}
        new_texts = {}
        for sid, old, new in PATCHES:
            tmpl = (rows.get(sid) or {}).get("template_es") or ""
            n = tmpl.count(old)
            if n != 1:
                print(f"✗ #{sid}: ожидаемый фрагмент найден {n} раз — ничего не пишу")
                return
            new_texts[sid] = tmpl.replace(old, new)
            print(f"#{sid} {rows[sid]['title']!r}\n  было:   {old!r}\n  станет: {new!r}")
        if not force:
            print("\nСухой прогон: прод-БД не тронута. Запись — с --force.")
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = Path("docs/backups") / f"scenarios_2_15_51_before_halloween_{stamp}.json"
        backup.write_text(json.dumps(
            [{**r, "updated_at": r["updated_at"].isoformat()} for r in rows.values()],
            ensure_ascii=False, indent=1))
        print(f"бэкап: {backup}")

        async with pool.acquire() as conn:
            async with conn.transaction():
                for sid, text in new_texts.items():
                    await conn.execute(
                        "UPDATE scenarios SET template_es = $1, updated_at = now() WHERE id = $2",
                        text, sid)
        after = {r["id"]: r["template_es"] for r in await pool.fetch(
            "SELECT id, template_es FROM scenarios WHERE id = ANY($1)", ids)}
        for sid, text in new_texts.items():
            assert after[sid] == text, f"#{sid}: замена не применилась"
        print(f"✓ обновлены #{', #'.join(map(str, ids))}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
