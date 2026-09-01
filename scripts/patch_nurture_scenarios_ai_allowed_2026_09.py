"""Патч: сценарии №10 (bajo ingreso) / №17 (no me interesa) переведены на ai_allowed=True.

Часть рефакторинга роутинга ai.py (ветка refactor/ai-routing-simplify, 2026-09-01).
Раньше funnel_stage='nurture' для этих двух сценариев хардкодился внутри
_fixed_reply() (_NURTURE_FIXED_SCENARIOS) — работало только для ai_allowed=false
пути. Вынесено в отдельный guardrail _enforce_nurture_stage(), применяемый
единообразно и к фикс-, и к AI-ветке (тот же паттерн, что _tag_event_interest).
В anna_prompt_v5.md для секции "bajo ingreso" добавлена явная инструкция
проставлять funnel_stage: "nurture" (аналогично уже существовавшей для #17)
— гарантия двойная (промпт + guardrail), не полагаемся только на одно.

НЕ трогает: trigger_es, template_es, mode, blocks_lead.

bot_paused=1 на момент патча — изменение не влияет на реальных лидов
(проверено перед запуском), верификация через scripts.run_eval с real AI.

Запуск: venv/bin/python -m scripts.patch_nurture_scenarios_ai_allowed_2026_09
Идемпотентен.
"""
import asyncio

import db


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        rows = await pool.fetch(
            "UPDATE scenarios SET ai_allowed=true, updated_at=now() WHERE id IN (10,17) "
            "RETURNING id, title, ai_allowed, mode, blocks_lead"
        )
        for r in rows:
            print(f"✓ #{r['id']} {r['title']!r}: ai_allowed={r['ai_allowed']}, "
                  f"mode={r['mode']}, blocks_lead={r['blocks_lead']}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
