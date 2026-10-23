"""Убрать цену услуги из сценариев прод-БД (решение Арины 2026-10-24: бот цену услуги не
называет никогда). Точечные замены фраз в template_es, без reseed (load_scenarios НЕ трогаем).

По умолчанию сухой прогон: показывает, что изменится. --commit — запись, перед ней бэкап
изменяемых строк в docs/backups и ~/matchmatch-backups/<дата>/.

    venv/bin/python -m scripts.patch_scenarios_no_service_price            # сухой прогон
    venv/bin/python -m scripts.patch_scenarios_no_service_price --commit   # запись
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
import servicio

NO_PRICE = "El precio del servicio no se da por chat: depende de cada caso y se platica en una llamada corta."

# id сценария → [(старая фраза, новая фраза)]. Старая должна найтись ровно один раз.
REPLACEMENTS: dict[int, list[tuple[str, str]]] = {
    6: [(" La inversión comienza desde $10,000 USD.", "")],
    14: [(" La inversión es desde $10,000 USD.", "")],
    15: [("dale TAMBIÉN el precio del servicio (desde $10,000 USD) en el MISMO mensaje",
          "del servicio NO das precio (depende de cada caso, se platica en una llamada corta)")],
    16: [("Con la membresía de 10,000 USD te presento a 15 mujeres eslavas",
          "Te presento a 15 mujeres eslavas")],
    18: [("Da el precio (inversión desde $10,000 USD) SOLO si lo pide directamente o muestra interés "
          "claro en avanzar — pasar la calificación NO es esa señal, es apenas el primer paso.", NO_PRICE)],
    19: [("[Precio: da la inversión (desde $10,000 USD) SOLO si el lead lo pregunta directamente o "
          "muestra un interés claro en avanzar.", "[Precio: " + NO_PRICE)],
    26: [("Da el precio (desde $10,000 USD) SOLO si lo pide directamente o muestra interés claro en "
          "avanzar — no por defecto.", NO_PRICE)],
    31: [("Da el precio (desde $10,000 USD) SOLO si lo pide directamente o muestra interés claro en "
          "avanzar — no automáticamente por estar calificado.", NO_PRICE)],
}


def apply(text: str, pairs: list[tuple[str, str]]) -> tuple[str, list[str]]:
    problems = []
    for old, new in pairs:
        n = text.count(old)
        if n != 1:
            problems.append(f"фраза найдена {n} раз: {old[:70]!r}")
            continue
        text = text.replace(old, new)
    return text, problems


async def main(commit: bool) -> int:
    await db.init_pool()
    try:
        rows = await db._get_pool().fetch(
            "SELECT id, template_es FROM scenarios WHERE id = ANY($1::int[]) ORDER BY id",
            list(REPLACEMENTS))
        changes, failed = [], False
        missing = set(REPLACEMENTS) - {r["id"] for r in rows}
        for i in sorted(missing):
            print(f"№{i}: нет в БД")
            failed = True
        for r in rows:
            new, problems = apply(r["template_es"] or "", REPLACEMENTS[r["id"]])
            for p in problems:
                print(f"№{r['id']}: {p}")
                failed = True
            if new != r["template_es"]:
                changes.append((r["id"], r["template_es"], new))
                print(f"№{r['id']}: будет изменён")
        # Все активные сценарии: после замен не должно остаться сумм услуги (не только список выше).
        new_by_id = {i: new for i, _, new in changes}
        for r in await db._get_pool().fetch("SELECT id, template_es FROM scenarios WHERE is_active"):
            text = new_by_id.get(r["id"], r["template_es"] or "")
            if servicio.MONEY_RE.search(text.replace("[event_price_nonmember] MXN", "")) and \
                    any(w in text.lower() for w in ("usd", "dólar", "dolar", "membres", "inversi")):
                print(f"№{r['id']}: после патча похоже на сумму услуги — проверь вручную")
        if failed:
            print("Есть фразы, которые не нашлись ровно один раз — ничего не пишу.")
            return 1
        if not commit:
            print(f"Сухой прогон: изменится {len(changes)} сценариев. Для записи: --commit")
            return 0
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = [{"id": i, "template_es": old} for i, old, _ in changes]
        day = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        for d in (Path("docs/backups"), Path.home() / "matchmatch-backups" / day):
            d.mkdir(parents=True, exist_ok=True)
            (d / f"scenarios_before_no_service_price_{ts}.json").write_text(
                json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
        async with db._get_pool().acquire() as c:
            async with c.transaction():
                for i, _, new in changes:
                    await c.execute("UPDATE scenarios SET template_es = $1, updated_at = now() WHERE id = $2",
                                    new, i)
        print(f"Записано: {len(changes)} сценариев (эмбеддинги не меняются: триггеры те же). Бэкап: {ts}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--commit" in sys.argv)))
