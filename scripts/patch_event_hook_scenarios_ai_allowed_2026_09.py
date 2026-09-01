"""Патч: сценарии №2/№51/№52/№39 переведены на ai_allowed=True.

Часть рефакторинга роутинга ai.py (ветка refactor/ai-routing-simplify,
2026-09-01): раньше эти сценарии были ai_allowed=False (дословный template,
OpenAI не вызывается) и диспетчеризовались через хардкод-форсы в generate_reply
(cold/warm-lead price routers, "novia rusa"), которые продавливали конкретный
id в обход контекста переписки — источник целой цепочки регрессов этой сессии
(см. историю коммитов fadd022, 0563c54, 2665060, 153e9cc, 5313e1d). Форсы
заменены на augment-кандидата + пост-генерационные guardrail'ы
(_enforce_link_presence, _enforce_service_price_gate, _tag_event_interest) —
теперь можно безопасно дать AI решать с полным контекстом, не теряя
business-critical детали (ссылка на билет, цена сервиса недоступна холодным).

№2 (крючок ивента), №51 (цена+детали+ссылка), №52 (детали без цены), №39
(защита цены от скидки) — вся "event hook" семья сценариев, RAG-контекст для
которой AI теперь читает как референс, а не жёсткий сценарий-приказ.

НЕ трогает: trigger_es, template_es, mode, blocks_lead.

bot_paused=1 на момент патча — изменение не влияет на реальных лидов
(проверено перед запуском), верификация через scripts.run_eval с real AI.

Запуск: venv/bin/python -m scripts.patch_event_hook_scenarios_ai_allowed_2026_09
Идемпотентен.
"""
import asyncio

import db


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        rows = await pool.fetch(
            "UPDATE scenarios SET ai_allowed=true, updated_at=now() WHERE id IN (2,51,52,39) "
            "RETURNING id, title, ai_allowed, mode, blocks_lead"
        )
        for r in rows:
            print(f"✓ #{r['id']} {r['title']!r}: ai_allowed={r['ai_allowed']}, "
                  f"mode={r['mode']}, blocks_lead={r['blocks_lead']}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
