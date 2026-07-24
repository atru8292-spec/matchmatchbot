"""Патч прод-БД: сценарий №39 «Дай скидку» — добавлена скидка «приведи друга»
для ивента (5,000 MXN/чел), по референс-документу владельца. ПОДТВЕРЖДЕНО
владельцем в чате 2026-07-25 ("если Мила так написала ок наверно").

Это ЕДИНСТВЕННОЕ исключение из жёсткого правила "NO HAY DESCUENTOS" в
anna_prompt_v5.md — касается ТОЛЬКО билета на ивент при паре друзей, не
услуги агентства. Промпт обновлён отдельно (см. коммит), чтобы AI не
противоречил сам себе в других ответах про скидки.

НЕ трогает: trigger_es (embedding не пересчитывается), mode, ai_allowed.

Запуск: venv/bin/python -m scripts.patch_event_friend_discount_2026_07
Идемпотентен: повторный прогон просто ничего не найдёт для замены.
"""
import asyncio

import db

OLD = (
    "Te entiendo 🤍 el precio refleja el trabajo personal que hago contigo: "
    "selección a mano y acompañamiento en todo el proceso.\n\n"
    "Sobre precios especiales o promociones, eso lo vemos mejor en la videollamada "
    "según tu caso. ¿Te late que agendemos y te explico todo? 😊"
)
NEW = (
    "Te entiendo 🤍 el precio refleja el trabajo personal que hago contigo: "
    "selección a mano y acompañamiento en todo el proceso.\n\n"
    "Sobre precios especiales o promociones, eso lo vemos mejor en la videollamada "
    "según tu caso. ¿Te late que agendemos y te explico todo? 😊\n\n"
    "Si es sobre el evento: puedo hacerte un descuento si vienes con un amigo — en "
    "ese caso el precio sería de 5,000 pesos por persona. Puedes hacer la "
    "transferencia a nuestra cuenta bancaria."
)


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        row = await db.get_scenario_row(39)
        if not row:
            print("✗ #39 не найден в БД")
            return
        if row["template_es"] != OLD:
            print("⚠ #39: template_es не совпал с ожидаемым — пропуск")
            return
        await pool.execute(
            "UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=39", NEW,
        )
        print("✓ #39 обновлён (добавлена скидка за друга на ивент)")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
