"""Патч прод-БД (Этап 1 — тексты): пересмотр воронки под реальный процесс Ани.

Обновляет тексты #24/#39/#46/#32/#33/#36 + вставляет новые #55-58 (с эмбеддингами по
trigger_es). #38 НЕ трогаем (сегмент old_base пуст — активация отдельно в будущем).

ВАЖНО: #32/#33/#36 — scheduled, шлются планировщиком дословно. Тексты меняем сейчас, но
СТАДИЙНАЯ логика выбора (какой из них слать по funnel_stage) — Этап 2 (код). Пока лидов
с таймерами 0 и Wazzup off, так что рассинхрон текст↔лестница ни на ком не проявится.

Запуск: venv/bin/python -m scripts.patch_funnel_texts_2026_07
Идемпотентен: ON CONFLICT для inserts, UPDATE перезаписывает.
"""
import asyncio

import asyncpg

from config import settings

# ===== обновления существующих текстов =====
UPDATES = {
    24: (  # после ивента — тёплая интонация Ани, без ложной конкретики
        "Ay [имя], qué gusto 🤍 me dio mucho que vinieras. Me encantó verte por ahí.\n\n"
        "Me encantaría platicar contigo 1:1 para saber cómo la pasaste, qué te pareció, y "
        "contarte qué opciones se abren para ti dentro de la agencia.\n\n"
        "¿Te late una videollamada corta? ¿Cuándo te queda? 😊"
    ),
    39: (  # скидки — смягчение (дверь на звонок вместо «jamás»)
        "Te entiendo guapo 🤍 el precio refleja el trabajo personal que hago contigo: "
        "selección a mano, 3 mujeres al mes y acompañamiento en todo el proceso.\n\n"
        "Sobre precios especiales o alguna promoción, eso lo vemos mejor en la videollamada "
        "según tu caso 😊\n\n¿Te late que agendemos y te explico todo?"
    ),
    46: (  # единый диапазон возраста девушек
        "Son chicas eslavas, rusas, ucranianas, bielorrusas. La mayoría entre 24 y 40 años, "
        "y también hay mujeres hermosas de 40 a 47, profesionales: medicina, marketing, IT, "
        "negocios.\n\n"
        "La mayoría vive aquí en México, y algunas todavía en sus países pero abiertas a "
        "mudarse por la persona correcta.\n\n"
        "Todas buscan relación seria, pareja, familia 🤍"
    ),
    32: (  # догон: согласился на звонок, анкету (чат) не дозаполнил
        "Hola guapo! 🤍 me faltan solo un par de datos para completar tu perfil antes de la "
        "videollamada. Cuando tengas un momento me los pasas y agendamos ✨"
    ),
    33: (  # догон: анкета собрана, звонок не назначен
        "Hola [имя]! 🤍 ya tengo tu perfil listo. Me encantaría agendar la videollamada para "
        "presentarte a quién podría encajar contigo. ¿Cuándo te queda?"
    ),
    36: (  # догон: замолчал холодным — мягкий опт-аут
        "Hola [имя]! 🤍 ¿sigues soltero y buscando algo serio? Si por ahora ya no te interesa, "
        "dime y te saco de la lista con gusto, sin problema 🤍"
    ),
}

# ===== новые сценарии #55-58 =====
NEW = [
    {
        "id": 55, "title": "Оплата Oxxo/Walmart/перевод",
        "mode": "bot_then_anna", "ai_allowed": True, "blocks_lead": False, "trigger_type": "reply",
        "trigger_description": "Как оплатить наличными/переводом (Oxxo, Walmart, transferencia)",
        "trigger_es": ("puedo pagar en efectivo / cómo puedo pagar en efectivo / aceptan oxxo / "
                       "puedo pagar en oxxo / pago en walmart / puedo hacer transferencia / "
                       "transferencia bancaria / puedo depositar / aceptan deposito / otras formas de pago"),
        "template_es": ("Claro guapo 🤍 además de tarjeta, puedes pagar en efectivo en Oxxo o "
                        "Walmart, o por transferencia bancaria.\n\nPara asegurar tu lugar te paso "
                        "los datos exactos, cualquier cosa me avisas y te ayudo con el proceso ✨"),
    },
    {
        "id": 56, "title": "Почему подорожало",
        "mode": "bot_auto", "ai_allowed": True, "blocks_lead": False, "trigger_type": "reply",
        "trigger_description": "Почему цена выше, чем раньше",
        "trigger_es": ("por qué subió el precio / por qué aumentó el precio / antes era más barato / "
                       "por qué es más caro ahora / subieron el precio / por qué cambió el precio"),
        "template_es": ("Muy buena pregunta 🤍 subimos un poco el precio porque ahora tenemos mejor "
                        "lugar, mejor organización, una lista de espera larga y muchísimas historias "
                        "de éxito, muchas parejas felices.\n\nAun así, por todo lo que recibes, sigue "
                        "siendo una inversión que de verdad vale la pena ✨"),
    },
    {
        "id": 57, "title": "Те же девушки, что в прошлый раз?",
        "mode": "bot_auto", "ai_allowed": True, "blocks_lead": False, "trigger_type": "reply",
        "trigger_description": "Будут ли те же девушки, что на прошлом ивенте",
        "trigger_es": ("serán las mismas chicas / las mismas mujeres que la vez pasada / que antes / "
                       "que el evento pasado / mismas de siempre / van a estar las mismas"),
        "template_es": ("Cada evento es diferente, con nuevas invitadas 🤍 a veces algunas regresan, "
                        "pero lo que te garantizo es que siempre serán mujeres hermosas, inteligentes "
                        "y que buscan algo serio."),
    },
    {
        "id": 58, "title": "Подтверждение купленного билета",
        "mode": "bot_auto", "ai_allowed": True, "blocks_lead": False, "trigger_type": "reply",
        "trigger_description": "Лид сообщил что купил/забронировал билет ивента (не подписку)",
        "trigger_es": ("ya compré el boleto / ya reservé mi lugar / compré mi ticket / ya aparté mi "
                       "lugar / ya tengo mi boleto / reservé el boleto / ya adquirí el boleto"),
        "template_es": ("¡Gracias por reservar tu lugar guapo! 🤍 Te agrego a la lista de invitados.\n\n"
                        "¿Ya te llegó el boleto por correo? Si no lo ves, revisa spam y promociones, "
                        "y cualquier cosa me avisas ✨"),
    },
]


async def _embed(text: str) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.openai_embedding_model, "input": text},
        )
        r.raise_for_status()
        vec = r.json()["data"][0]["embedding"]
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def main() -> None:
    conn = await asyncpg.connect(dsn=settings.supabase_db_dsn, timeout=30)
    try:
        # 1) обновления текстов
        for sid, tmpl in UPDATES.items():
            await conn.execute("UPDATE scenarios SET template_es=$1, updated_at=now() WHERE id=$2",
                               tmpl, sid)
            print(f"✓ #{sid} текст обновлён")
        # 2) новые сценарии + эмбеддинги
        for s in NEW:
            emb = await _embed(s["trigger_es"])
            await conn.execute(
                "INSERT INTO scenarios (id, title, trigger_description, trigger_es, template_es, "
                "  mode, blocks_lead, ai_allowed, is_active, trigger_type, embedding) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,true,$9,$10::vector) "
                "ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, "
                "  trigger_description=EXCLUDED.trigger_description, trigger_es=EXCLUDED.trigger_es, "
                "  template_es=EXCLUDED.template_es, mode=EXCLUDED.mode, ai_allowed=EXCLUDED.ai_allowed, "
                "  trigger_type=EXCLUDED.trigger_type, embedding=EXCLUDED.embedding, updated_at=now()",
                s["id"], s["title"], s["trigger_description"], s["trigger_es"], s["template_es"],
                s["mode"], s["blocks_lead"], s["ai_allowed"], s["trigger_type"], emb,
            )
            print(f"✓ #{s['id']} «{s['title']}» вставлен (mode={s['mode']}, ai_allowed={s['ai_allowed']})")
        # 3) контроль
        rows = await conn.fetch("SELECT id, embedding IS NOT NULL AS has_emb FROM scenarios "
                                "WHERE id IN (55,56,57,58) ORDER BY id")
        print("контроль эмбеддингов:", [(r["id"], r["has_emb"]) for r in rows])
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
