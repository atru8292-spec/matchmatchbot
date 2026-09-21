"""Патч прод-БД: сценарий #44 («Когда следующий ивент?») хардкодил «Todavía no tengo
fecha exacta del próximo evento» — ложное утверждение, когда event_active=1 и дата
реально назначена (сейчас 14.10.26). Триггер #44 («cuando es el proximo evento»)
почти дословно совпадает с тем, как лид спросит про дату, и RAG мог подобрать именно
#44 вместо токен-сценариев (#2/#51) — бот врал бы лиду, что даты нет.

Фикс: #44 теперь литерально использует токены [event_date]/[event_time]/[event_address]
(тот же механизм sender._fill_event_vars, что и #2/#47/#50/#51/#54) — правда всегда,
раз дата назначена. Если event_date когда-нибудь очистят (реальный разрыв между
ивентами) — токен подставится пустой строкой, фраза станет неполной, но не ЛОЖНОЙ
(в отличие от текущего текста). Отдельная защита от пустой даты — не в рамках этого
патча (при следующем реальном разрыве между ивентами оценить отдельно).

Идемпотентно: точное совпадение template_es со старым текстом, иначе no-op (кто-то
другой уже поменял).

⚠️ Guard: только с --force:
venv/bin/python -m scripts.patch_scenario44_event_date_2026_09 --force
"""
import asyncio
import sys

import db

SCENARIO_ID = 44
OLD = ("Todavía no tengo fecha exacta del próximo evento, pero te aviso en cuanto la "
       "tengamos 🤍\n\nMientras tanto, como miembro de la agencia, tienes acceso a "
       "conocer a 15 mujeres guapas y compatibles contigo de nuestra base de datos, "
       "sin tener que esperar al evento. ¿Lo vemos en una videollamada?")
NEW = ("¡Con gusto! 🤍 El próximo evento es el [event_date], a las [event_time], en "
       "[event_address]. Si quieres, te cuento el precio y te paso el link para que "
       "apartes tu lugar 😊")


async def main() -> None:
    if "--force" not in sys.argv:
        print("⚠️  Guard: запуск только с --force. Прод-БД не тронута.")
        sys.exit(1)
    await db.init_pool()
    try:
        pool = db._get_pool()
        row = await db.get_scenario_row(SCENARIO_ID)
        if not row:
            print(f"✗ #{SCENARIO_ID}: не найден в БД")
            return
        if row["template_es"] != OLD:
            print(f"⚠ #{SCENARIO_ID}: template_es не совпал с ожидаемым — пропуск "
                  f"(уже изменён кем-то другим?)")
            return
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
            NEW, SCENARIO_ID,
        )
        row2 = await db.get_scenario_row(SCENARIO_ID)
        assert row2 and row2["template_es"] == NEW, f"#{SCENARIO_ID}: замена не применилась"
        print(f"✓ #{SCENARIO_ID} обновлён: теперь на токенах [event_date]/[event_time]/[event_address]")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
