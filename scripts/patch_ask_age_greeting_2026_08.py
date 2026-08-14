"""Патч прод-БД: сценарии #1, #2, #3 (приветствие) — добавлен вопрос про возраст.

Баг найден в живом тесте (Аня, 2026-08-14, лид "Pedro"): бот спросил только "eres
soltero?", получил "Si" и сразу перешёл к профессии, ни разу не спросив возраст явно.
Возраст всплыл только через ~18 минут разговора, при сборе анкеты (fecha de nacimiento),
уже ПОСЛЕ фото, питча и согласия на видеозвонок.

Корень: сценарии #1/#2/#3 — approved-текст приветствия, который AI обязан использовать
"casi literal" (anna_prompt_v5.md) — спрашивали ТОЛЬКО "eres soltero?", без возраста.
Явного шага "спроси возраст" после этого нигде в сценариях не было, так что для лида
вне 28-76 фильтр (filters.py MIN_AGE/MAX_AGE) не мог сработать рано — лид проходил
весь funnel (фото, питч, договорённость о звонке) и только потом мог быть отбит.

Фикс: возвращаем вопрос "y qué edad tienes?" рядом с "eres soltero?" — как это было в
старых логах (4 августа) и как описывает anna_prompt_v5.md строка 116 ("calificas
(soltero? edad? a qué te dedicas?)").

trigger_es не трогаем → embedding не пересчитывается.
Запуск: venv/bin/python -m scripts.patch_ask_age_greeting_2026_08
"""
import asyncio

import db

PATCHES = {
    1: (
        "déjame conocerte un poquito, eres soltero?",
        "déjame conocerte un poquito, ¿eres soltero y qué edad tienes?",
    ),
    2: (
        "me gustaría conocerte un poquito. ¿Estás soltero? Si te late, con gusto te explico cómo funciona todo.",
        "me gustaría conocerte un poquito. ¿Estás soltero y qué edad tienes? Si te late, con gusto te explico cómo funciona todo.",
    ),
    3: (
        "Antes de platicarte más, eres soltero?",
        "Antes de platicarte más, ¿eres soltero y qué edad tienes?",
    ),
}


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        for scenario_id, (old, new) in PATCHES.items():
            row = await db.get_scenario_row(scenario_id)
            assert row is not None, f"#{scenario_id} не найден"
            assert old in row["template_es"], f"#{scenario_id}: старый текст не найден, проверь руками"
            updated = row["template_es"].replace(old, new)
            await pool.execute(
                "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                updated, scenario_id,
            )
            check = await db.get_scenario_row(scenario_id)
            assert check["template_es"] == updated, f"#{scenario_id}: не применилось"
            assert "edad" in check["template_es"].lower(), f"#{scenario_id}: edad не появился"
            print(f"✓ #{scenario_id} обновлён")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
