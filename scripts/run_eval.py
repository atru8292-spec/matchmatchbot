"""Quality-eval AI-ядра по eval_cases_v2.json (РЕАЛЬНЫЙ OpenAI).

Прогоняет каждое сообщение лида через ai.generate_reply и печатает результат
для ручной оценки (без авто pass/fail — качество смотрит человек).

Запуск: ./venv/bin/python -m scripts.run_eval
"""
from __future__ import annotations

import asyncio
import json

import ai
import db

CASES_FILE = "eval_cases_v2.json"


async def main() -> None:
    cases = json.load(open(CASES_FILE, encoding="utf-8"))
    await db.init_pool()
    try:
        for c in cases:
            # каждый кейс — как новый лид (без истории), чистый первый контакт
            res = await ai.generate_reply({}, [], c["msg"])
            print("=" * 78)
            print(f"#{c['id']} [{c.get('cat','?')}]  ЛИД: {c['msg']}")
            print(f"ОЖИДАНИЕ: {c.get('expect','')}")
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
