"""Патч прод-БД: цена ивента (член/не-член/старая) — из app_settings, а не хардкод.

- Токены [event_price_member]/[event_price_nonmember] в #15/#16/#51 (+ условный
  [event_promo] «(antes X)» в #51). Подстановка — sender._fill_event_vars.
- app_settings: event_price_member=4,000, event_price_nonmember=6,000, event_price_old=9,000.

trigger_es не трогаем → embedding не пересчитывается. Запуск:
venv/bin/python -m scripts.patch_event_prices_2026_07
"""
import asyncio

import db

NEW_51 = (
    "El precio para no miembros es de [event_price_nonmember] MXN[event_promo], y para "
    "miembros [event_price_member] MXN. Incluye: bebida de bienvenida, entrantes y lo más "
    "importante: conocer hermosas mujeres eslavas solteras que buscan una relación seria.\n\n"
    "Es un evento único como este en toda América Latina. Comienza a las [event_start] y "
    "termina a [event_end], puedes venir más tarde. Habrá mujeres guapas e inteligentes "
    "solteras y hombres exitosos solteros.\n\n"
    "Todos van con la misma intención de algo serio, así que puedes acercarte con confianza "
    "a la que te llame la atención, platicar y, si hay química, intercambiar contactos para "
    "seguir viéndose. Si te da pena, acércate a mí o a mis compañeras organizadoras, te "
    "entendemos y te presentamos con quien te guste. Además es un gran evento de networking: "
    "muchos encuentran no solo pareja sino también nuevos amigos y contactos de negocios. "
    "Habrá música, baile y buena conversación, y todas son hermosas mujeres eslavas que "
    "viven en CDMX.\n\n"
    "Aquí está el enlace para obtener el boleto, ahí puedes ver fotos, videos y reviews de "
    "eventos pasados: [event_link]"
)

NEW_16 = (
    "Te entiendo guapo 🤍 pero míralo así: no es un gasto, es una inversión en algo tan "
    "importante como encontrar a tu pareja.\n\n"
    "Con la membresía de $1,400 USD al mes, cada mes yo misma te presento 3 mujeres eslavas "
    "verificadas, elegidas según lo que buscas en físico, valores y personalidad. Además "
    "tienes acceso a toda nuestra base de más de 3,000 mujeres y entras a los eventos a "
    "precio de miembro, [event_price_member] en vez de [event_price_nonmember] pesos.\n\n"
    "Es un servicio premium y personal, no una lotería como las apps. Piensa cuánto tiempo y "
    "dinero se va en citas al azar que no llevan a nada, aquí todo el proceso va dirigido a "
    "tu objetivo.\n\n"
    "¿Te late que lo platiquemos en una videollamada y te enseño cómo funciona? Y si "
    "prefieres, puedes empezar solo con un evento."
)

NEW_15 = (
    "[Guía para AI — el lead quiere SOLO el evento, sin membresía. Genera mensajes naturales "
    "en tono Anna siguiendo estos 4 puntos, NO copies literal. Máximo 4 mensajes; el enlace "
    "va en el último. Escribe LITERAL los tokens [event_date], [event_time], [event_address], "
    "[event_price_nonmember], [event_price_member], [event_link] — se rellenan solos, NO "
    "inventes fecha/hora/dirección/precios.]\n\n"
    "[1] [Confirma con calidez que SÍ puede venir solo al evento, sin membresía — es válido, "
    "no lo rechaces. Menciona el evento Slavic Latino Night: fecha [event_date], hora "
    "[event_time], en [event_address]. Toca ligero la exclusividad (los eventos pasados se "
    "agotaron, cupo limitado).]\n\n"
    "[2] [Compara los precios claro y honesto, sin esconder nada: solo el evento son "
    "[event_price_nonmember] MXN una sola vez. Con la membresía ($1,400 USD/mes) ese mismo "
    "evento te sale en [event_price_member] MXN, bastante más barato, y además cada mes te "
    "presento 3 mujeres eslavas elegidas según tus valores, tu personalidad y tus "
    "preferencias de físico, acceso a la base de más de 3,000 mujeres, y todos los eventos "
    "siguientes también a precio de miembro. Deja claro que la membresía no es 'pagar "
    "[event_price_member]': ese es el precio de miembro para el evento DENTRO de la membresía "
    "de $1,400/mes.]\n\n"
    "[3] [Inclina con suavidad hacia la membresía, sin presión: si tu objetivo no es solo "
    "salir una noche sino de verdad encontrar pareja, la membresía es más efectiva — no "
    "dependes de un solo evento, cada mes hay presentaciones personales y no esperas a la "
    "siguiente fecha. Como un buen consejo, no como venta agresiva.]\n\n"
    "[4] [Pregunta directo y sin presión: ¿te gustaría que platiquemos de la membresía, o "
    "prefieres por ahora solo el boleto de este evento? Ambas opciones están bien. En "
    "cualquier caso pásale el token literal [event_link] para reservar y ver fotos, videos y "
    "reviews de eventos pasados.]"
)

PATCH = {15: NEW_15, 16: NEW_16, 51: NEW_51}
PRICES = {"event_price_member": "4,000", "event_price_nonmember": "6,000", "event_price_old": "9,000"}


async def main() -> None:
    await db.init_pool()
    try:
        pool = db._get_pool()
        for sid, tmpl in PATCH.items():
            await pool.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                               tmpl, sid)
            row = await db.get_scenario_row(sid)
            assert row["template_es"] == tmpl, f"#{sid}: template не совпал"
            # старые голые цены не должны остаться
            bad = ("9,000" in tmpl) or ("4,000" in tmpl and "[event_price_member]" not in tmpl)
            assert not bad, f"#{sid}: остались голые цены"
            print(f"✓ #{sid}: токены цены проставлены")
        for k, v in PRICES.items():
            await db.set_setting(k, v)
        s = await db.get_settings(list(PRICES))
        print(f"✓ app_settings: {s}")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
