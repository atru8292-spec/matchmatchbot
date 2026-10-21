"""Smoke-тест перед запуском (2026-10-21): три базовых разговора об ивенте 18.11.

Реальный OpenAI (боевой промпт + сценарии из прод-БД), всё остальное — в памяти:
  - пул БД открывается ТОЛЬКО на чтение (default_transaction_read_only=on): любая
    запись упадёт, а не попадёт в прод;
  - настройки ивента подменяются в памяти (event_active=1 и т.д.), флаги в базе не трогаем;
  - отправка в Wazzup и Telegram — заглушки, наружу ничего не уходит.

Путь реплики как в main (упрощённо): filters.decide → (needs_ai) ai.generate_reply →
sender.render_bubbles + sender.service_text_reason. Фото, склейка залпа и запись
профиля не участвуют: профиль лида обновляется в памяти из extracted.

Запуск (тратит ключ OpenAI): venv/bin/python -m scripts.smoke_launch
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import asyncpg

import ai
import db
import escalation
import filters
import main
import sender

EVENT_SETTINGS = {
    "event_active": "1",
    "event_date": "2026-11-18",
    "event_time": "8:30 PM",
    "event_start": "8:30 PM",
    "event_end": "12:00 AM",
    "event_address": "Polanco, CDMX",
    "event_price_member": "",
    "event_price_nonmember": "6000",
    "event_price_old": "8,000",
    "event_price_next": "7000",
    "event_link": "https://www.rusaencdmx.com/18-11-26-slavic-latino-night-halloween",
    "bot_paused": "0",
}

CONVERSATIONS = [
    ("1. Обычный новый лид", "Carlos", [
        "hola, vi el evento",
        "¿de qué se trata el evento?",
        "¿cuánto cuesta?",
        "vi que es de halloween, ¿hay que ir disfrazado?",
        "¿qué incluye el precio?",
        "ok me interesa, ¿cómo compro el boleto?",
    ]),
    ("2. Сомневающийся лид", "Javier", [
        "hola, me interesa el evento de las chicas eslavas",
        "¿cuánto cuesta?",
        "uf, se me hace caro la verdad",
        "lo voy a pensar",
        "¿hay precio si voy con un amigo?",
        "no me escribas por aquí, mejor llámame",
    ]),
    ("3. Недоверчивый лид", "Ricardo", [
        "hola, quiero info del evento",
        "¿eres un bot?",
        "¿dónde es exactamente?",
        "¿a qué hora termina?",
        "¿cuántas personas van?",
        "no me llames ni me escribas",
    ]),
]

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⭐❤]")
_END_RE = re.compile(r"termina|12:00|medianoche|12 ?am|hasta las|cierra", re.I)


def _flags(text: str) -> list[str]:
    out = []
    reason = sender.service_text_reason(text)
    if reason:
        out.append(f"СЛУЖЕБНОЕ({reason})")
    if _END_RE.search(text):
        out.append("ВРЕМЯ ОКОНЧАНИЯ?")
    if "5,500" in text or "5500" in text:
        out.append("5,500")
    if re.search(r"\b(6,?000|8,?000)\b", text):
        out.append("цена")
    return out


async def _run(title: str, name: str, lines: list[str], idx: int) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    phone = f"wa_52155000099{idx:02d}"
    lead = {"phone": phone, "whatsapp_name": name, "name": None, "mode": "auto",
            "funnel_stage": "new", "do_not_contact": False, "manual_until": None}
    history: list[dict] = []
    now = datetime.now(timezone.utc) - timedelta(minutes=30)
    for n, text in enumerate(lines, 1):
        now += timedelta(minutes=2)
        print(f"\n[{n}] ЛИД: {text}")
        d = filters.decide(lead, False, text, phone, frozenset(), bot_paused=False)
        history.append({"sender": "lead", "direction": "inbound", "text": text, "created_at": now})
        if d.action != "needs_ai":
            print(f"    РЕШЕНИЕ: {d.action} (alert={d.alert_manager}, quiet={d.quiet}) — {d.reason}")
            if d.action == "optout" and not d.quiet:
                print(f"    ↳ main отправил бы лиду: {main._OPTOUT_CONFIRM!r}")
            elif d.action == "call_request":
                print("    ↳ лиду ничего; ручной режим + алерт Ане (notify_call_request)")
            elif d.action == "companion_question":
                print("    ↳ лиду ничего; ручной режим + алерт Ане (notify_companion_question)")
            break
        r = await ai.generate_reply(lead, history[:-1], text)
        bubbles = await sender.render_bubbles(r.get("messages") or [], None, True)
        emojis = sum(len(_EMOJI_RE.findall(b)) for b in bubbles)
        meta = {k: r.get(k) for k in ("action", "funnel_stage", "used_scenario_id")}
        extra = [k for k in ("send_event_photo", "send_event_video", "send_invitation",
                             "needs_escalation") if r.get(k)]
        print(f"    meta: {meta}{' +' + ','.join(extra) if extra else ''} | эмодзи: {emojis}")
        if len(bubbles) != len(r.get("messages") or []):
            print(f"    ⚠ бабблов от AI {len(r.get('messages') or [])}, после подстановки {len(bubbles)}")
        for b in bubbles:
            f = _flags(b)
            print(f"    БОТ: {b}" + (f"   ⟵ {', '.join(f)}" if f else ""))
            history.append({"sender": "anna", "direction": "outbound", "text": b,
                            "created_at": now + timedelta(seconds=30)})
        for k, v in (r.get("extracted") or {}).items():
            lead[k] = v
        if r.get("funnel_stage"):
            lead["funnel_stage"] = r["funnel_stage"]


async def amain() -> None:
    # Пул только на чтение: запись в прод физически невозможна.
    orig_create_pool = asyncpg.create_pool

    async def ro_pool(*a, **k):
        k.setdefault("server_settings", {})["default_transaction_read_only"] = "on"
        return await orig_create_pool(*a, **k)

    asyncpg.create_pool = ro_pool

    real_get_settings = db.get_settings

    async def get_settings(keys):
        real = await real_get_settings([k for k in keys if k not in EVENT_SETTINGS])
        return {**real, **{k: EVENT_SETTINGS[k] for k in keys if k in EVENT_SETTINGS}}

    async def no_send(*a, **k):
        raise RuntimeError("smoke: отправка запрещена")

    async def no_tg(*a, **k):
        return None

    db.get_settings = get_settings
    sender.send_one = no_send
    escalation._send_telegram = no_tg

    await db.init_pool()
    try:
        for i, (title, name, lines) in enumerate(CONVERSATIONS, 1):
            await _run(title, name, lines, i)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(amain())
