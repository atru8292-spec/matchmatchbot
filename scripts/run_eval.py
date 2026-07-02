"""Quality-eval AI-ядра (РЕАЛЬНЫЙ OpenAI).

Прогоняет сообщения лидов из одного или нескольких eval-файлов через
ai.generate_reply и печатает результат для ручной оценки (без авто pass/fail).

Запуск:
  ./venv/bin/python -m scripts.run_eval                       # оба: v2 + v3
  ./venv/bin/python -m scripts.run_eval eval_cases_v2.json    # конкретный файл
"""
from __future__ import annotations

import asyncio
import json
import sys

import ai
import db

DEFAULT_FILES = ["eval_cases_v2.json", "eval_cases_v3.json"]


def _load(files: list[str]) -> list[dict]:
    cases = []
    for f in files:
        data = json.load(open(f, encoding="utf-8"))
        for c in data:
            c["_file"] = f
            cases.append(c)
    return cases


async def main() -> None:
    files = sys.argv[1:] or DEFAULT_FILES
    cases = _load(files)
    print(f"eval: {len(cases)} кейсов из {files}\n")
    await db.init_pool()
    try:
        for c in cases:
            res = await ai.generate_reply({}, [], c["msg"])
            print("=" * 78)
            print(f"#{c['id']} [{c.get('cat','?')}]  ЛИД: {c['msg']}")
            if c.get("expect"):
                print(f"ОЖИДАНИЕ: {c['expect']}")
            print(f"  scenario={res['used_scenario_id']}  funnel={res['funnel_stage']}  "
                  f"action={res['action']}  escalate={res['needs_escalation']}")
            if res["extracted"]:
                print(f"  extracted={res['extracted']}")
            print("  ОТВЕТ ANNA:")
            for m in res["messages"]:
                print(f"    • {m}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
