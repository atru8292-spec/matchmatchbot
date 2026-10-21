"""Смоук режимов ответа: legacy и rag рядом на одних и тех же сообщениях (этап 4).

Реальный OpenAI (тратит деньги!), всё остальное изолировано:
  - пул БД только на чтение (default_transaction_read_only=on): запись в прод невозможна;
  - настройки ивента — в памяти (event_active=1, 6000 → 7000, event_price_old пусто);
  - история и заметки лида — в памяти скрипта, со сдвинутыми датами;
  - Wazzup и Telegram — заглушки, наружу ничего не уходит;
  - бюджет: перед каждым вызовом OpenAI проверяется потраченное, при превышении --budget
    прогон останавливается (по умолчанию $2).

  --part messages   13 одиночных сообщений
  --part dialogs    10 диалогов
  --part all        всё (по умолчанию)
  --dry             без OpenAI: фиктивные ответы, проверка самого скрипта (бесплатно)

Ключ OpenAI — только из окружения процесса (с сервера по SSH, без записи на диск):
  OPENAI_API_KEY="$(ssh -o BatchMode=yes root@2.26.71.254 'grep ^OPENAI_API_KEY= /opt/matchmatch-bot/.env | cut -d= -f2-')" \
    venv/bin/python -m scripts.smoke_reply_mode --part all --out <файл.md>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import asyncpg

import ai
import ai_conversation as rag
import conversation_rules
import db
import escalation
import filters
import knowledge
import memory
import sender

NOW = datetime.now(timezone.utc)
LINK = "https://www.rusaencdmx.com/18-11-26-slavic-latino-night-halloween"
EVENT_SETTINGS = {
    "event_active": "1", "event_date": "2026-11-18", "event_time": "8:30 PM",
    "event_start": "8:30 PM", "event_end": "12:00 AM", "event_address": "Polanco, CDMX",
    "event_price_member": "", "event_price_nonmember": "6000", "event_price_next": "7000",
    "event_price_old": "", "event_link": LINK, "bot_paused": "0",
}
ANNOUNCED = "2026-10-20T17:45:22+00:00"

GREETING_EVENT = [b for b in knowledge.load_seed()
                  if b["id"] == "greeting_event"][0]["contenido_es"].split("\n\n")


def M(sender_, text, minutes_ago, manual=False):
    return {"sender": sender_, "direction": "inbound" if sender_ == "lead" else "outbound",
            "text": text, "created_at": NOW - timedelta(minutes=minutes_ago),
            "meta": {"manual": True} if manual else {}}


def _rendered(bubbles):
    return [b.replace("[event_date]", "18 de noviembre de 2026").replace("[event_address]", "Polanco, CDMX")
            for b in bubbles]


# База для одиночных сообщений 3–13: лид пришёл на ивент, назвал имя и что холост.
BASE_EVENT = ([M("lead", "hola, vi el evento", 30)]
              + [M("anna", b, 29) for b in _rendered(GREETING_EVENT)]
              + [M("lead", "Me llamo Carlos y sí, estoy soltero", 25),
                 M("anna", "¡Qué bien, Carlos! ¿Tienes alguna duda sobre el evento?", 24)])
BASE_LEAD = {"name": "Carlos", "is_single": True, "interest": "event", "funnel_stage": "qualifying"}

MESSAGES = [
    ("hola", [], {}),
    ("hola, vi el evento", [], {}),
] + [(t, BASE_EVENT, BASE_LEAD) for t in [
    "¿de qué se trata el evento?", "¿cuánto cuesta?", "¿cuántas personas van?",
    "¿dónde es exactamente?", "¿a qué hora termina?", "¿hay que ir disfrazado?",
    "¿qué incluye el precio?", "se me hace caro", "lo voy a pensar",
    "¿cómo compro el boleto?", "¿eres un bot?"]]

DAY = 24 * 60
DIALOGS = [
    ("1. Ивент с нуля", [], {}, [
        "hola, vi el evento", "me llamo Javier, sí soltero", "¿qué incluye?",
        "¿a qué hora empieza?", "ok, ¿cómo compro el boleto?"]),
    ("2. Услуга", [], {}, [
        "hola, quiero conocer mujeres rusas", "Luis, soltero", "tengo 41, soy abogado",
        "¿cómo funciona el servicio?", "¿cuánto cuesta?"]),
    ("3. Уход от темы", BASE_EVENT, BASE_LEAD, [
        "¿viste el partido del América ayer?", "jaja sí, estuvo buenísimo, ¿tú le vas a alguien?",
        "¿y qué opinas de la política?"]),
    ("4. Вопрос про друга", BASE_EVENT, BASE_LEAD, ["¿puedo ir con un amigo?"]),
    ("5. Наличные", BASE_EVENT, BASE_LEAD, ["¿puedo pagar en efectivo en OXXO?"]),
    ("6. Агрессия", BASE_EVENT, BASE_LEAD, ["son unos pinches rateros"]),
    ("7. Возвращение через 3 дня посреди квалификации",
     [M("lead", "hola, vi el evento", 3 * DAY + 40)]
     + [M("anna", b, 3 * DAY + 39) for b in _rendered(GREETING_EVENT)],
     {"interest": "event"},
     ["hola, perdón, me llamo Diego y sí estoy soltero", "¿cuánto cuesta?"]),
    ("8. Квалифицированный лид через неделю, вопрос про ивент",
     BASE_EVENT[:1] + [dict(m, created_at=m["created_at"] - timedelta(days=8)) for m in BASE_EVENT[1:]]
     + [M("lead", "¿cuánto cuesta?", 8 * DAY), M("anna", "Ahora cuesta 6000 MXN e incluye bebida de bienvenida y botanas.", 8 * DAY - 1),
        M("lead", "ok, lo pienso", 8 * DAY - 5)],
     {"name": "Andrés", "is_single": True, "interest": "event", "funnel_stage": "qualifying"},
     ["hola", "¿dónde es?"]),
    ("9. Лид говорил про ивент 16 октября",
     [M("lead", "info del evento", 12 * DAY),
      M("anna", "El evento es el 16 de octubre a las 8:00 PM en la Roma Norte y cuesta 5000 MXN.", 12 * DAY - 1),
      M("lead", "ok gracias", 12 * DAY - 10)],
     {"name": "Jorge", "is_single": True, "interest": "event"},
     ["hola, ¿cuándo es el evento?", "¿y cuánto cuesta?"]),
    ("10. Аня вела вручную, потом вернула боту",
     BASE_EVENT + [M("lead", "¿me apartas un lugar?", 20),
                   M("anna", "Hola Carlos, soy Anna. Te guardo un lugar hasta el viernes si me confirmas hoy.", 19, manual=True),
                   M("lead", "ok, déjame ver", 18)],
     BASE_LEAD, ["¿entonces me guardas el lugar hasta el viernes?", "¿qué incluye el boleto?"]),
]

# ───────────────────────── бюджет и заглушки ─────────────────────────


class Budget(BaseException):
    """BaseException: конвейеры ловят Exception и отвечали бы заглушкой вместо остановки."""


RESERVE_USD = 0.05


class Spend:
    def __init__(self, limit: float):
        self.limit = limit
        self.usd = 0.0
        self.calls = 0
        self.by_mode = {"legacy": 0.0, "rag": 0.0}
        self.mode = "legacy"


def install(spend: Spend, dry: bool) -> None:
    orig_post = ai._openai_post

    async def post(url, payload, timeout):
        if spend.usd + RESERVE_USD >= spend.limit:   # запас на один вызов
            raise Budget(f"бюджет ${spend.limit:.2f} исчерпан (${spend.usd:.3f})")
        r = await (_fake_post(url, payload) if dry else orig_post(url, payload, timeout))
        usage = r.json().get("usage") or {}
        p = rag.PRICES.get(payload.get("model"), rag.PRICES["gpt-4.1"])
        tin = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        usd = ((tin - cached) * p["in"] + cached * p["cached"] + tout * p["out"]) / 1e6
        spend.usd += usd
        spend.calls += 1
        spend.by_mode[spend.mode] += usd
        return r

    ai._openai_post = post


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


async def _fake_post(url, payload):
    """--dry: правдоподобные ответы без OpenAI (проверка самого скрипта)."""
    if "embeddings" in url:
        inp = payload["input"]
        inp = inp if isinstance(inp, list) else [inp]
        data = [{"index": i, "embedding": [1.0, (len(t) % 13) / 13.0] + [0.0] * 1534}
                for i, t in enumerate(inp)]
        return _Resp({"data": data, "usage": {"prompt_tokens": 10 * len(inp)}})
    system = payload["messages"][0]["content"]
    if system == rag.VERIFY_SYSTEM:
        content = {"sin_fuente": []}
    elif system == rag.SUMMARY_SYSTEM:
        content = {"resumen": "- resumen de prueba"}
    elif "bubbles" in system:
        content = {"bubbles": ["Es en [event_address]."], "accion": "respond", "fuentes": [],
                   "falta_dato": False, "en_tema": True, "extracted": {}}
    else:
        content = {"messages": ["Respuesta de prueba"], "action": "respond", "extracted": {}}
    return _Resp({"choices": [{"message": {"content": json.dumps(content)}}],
                  "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})


# ───────────────────────── оценка ответа ─────────────────────────

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⏰]")
_PURCHASE = re.compile(r"compr|reserv|apart|quiero ir|link|enlace|\bpag(?:o|ar)\b", re.I)


def judge(bubbles: list[str], lead_text: str, presented_before: bool) -> list[str]:
    out = []
    text = "\n\n".join(bubbles)
    if text.count("?") > 1:
        out.append(f"вопросов {text.count('?')}")
    e = len(_EMOJI.findall(text))
    if e > 2 or any(len(_EMOJI.findall(b)) > 1 for b in bubbles):
        out.append(f"эмодзи {e}")
    v = conversation_rules.violations(text)
    if v:
        out.append("; ".join(v))
    if re.search(r"(?<!\d)5[\s.,]?500(?!\d)", text):
        out.append("5,500")
    if (LINK in text or "rusaencdmx.com" in text) and not _PURCHASE.search(lead_text):
        out.append("ссылка без вопроса о покупке")
    if presented_before and re.search(r"soy anna", text, re.I):
        out.append("повторная презентация")
    if re.search(r"medianoche|12:00|único", text, re.I):
        out.append("неподтверждённый факт")
    if re.search(r"~|(?<!\d)8,?000(?!\d)", text):
        out.append("зачёркнутая/8000")
    return out


# ───────────────────────── прогон ─────────────────────────

class Lead:
    def __init__(self, mode: str, idx: int, history: list[dict], profile: dict, notes=None):
        self.phone = f"wa_52155999{idx:02d}{0 if mode == 'legacy' else 1}"
        self.history = [dict(m, lead_phone=self.phone) for m in history]
        self.lead = {"phone": self.phone, "whatsapp_name": "Lead", "mode": "auto",
                     "do_not_contact": False, "manual_until": None, **profile}
        self.notes = notes or []


async def one_turn(mode: str, L: Lead, text: str, spend: Spend) -> dict:
    spend.mode = mode
    d = filters.decide(L.lead, False, text, L.phone, frozenset(), bot_paused=False)
    L.history.append(M("lead", text, 0))
    if d.action != "needs_ai":
        return {"decision": d.action, "bubbles": [], "violations": [], "usd": 0.0}
    before = spend.usd
    if mode == "legacy":
        r = await ai._generate_reply_legacy(L.lead, L.history[-21:-1], text)
    else:
        r = await rag.generate_reply(L.lead, [], text)
    bubbles = await sender.render_bubbles(r.get("messages") or [], None, True)
    presented = any(m["sender"] == "anna" for m in L.history[:-1])
    for b in bubbles:
        L.history.append(M("anna", b, 0))
    for k, v in (r.get("extracted") or {}).items():
        L.lead[k] = v
    if r.get("rag_estado") is not None:
        L.lead["rag_estado"] = r["rag_estado"]
    if r.get("funnel_stage"):
        L.lead["funnel_stage"] = r["funnel_stage"]
    return {"decision": r.get("action"), "bubbles": bubbles,
            "silent_reason": r.get("silent_reason"),
            "meta": r.get("rag_meta"), "media": [k for k in ("send_event_photo", "send_event_video")
                                                   if r.get(k)],
            "violations": judge(bubbles, text, presented), "usd": spend.usd - before}


def install_db(leads_by_phone: dict) -> None:
    real_get_settings = db.get_settings

    async def get_settings(keys):
        real = await real_get_settings([k for k in keys if k not in EVENT_SETTINGS])
        return {**real, **{k: EVENT_SETTINGS[k] for k in keys if k in EVENT_SETTINGS}}

    real_get_setting = db.get_setting

    async def get_setting(key):
        if key == "event_announced_at":
            return ANNOUNCED
        if key in EVENT_SETTINGS:
            return EVENT_SETTINGS[key]
        return await real_get_setting(key)

    async def history(phone, limit=30):
        L = leads_by_phone.get(phone)
        return [] if L is None else L.history[-limit:]

    async def notes(phone):
        L = leads_by_phone.get(phone)
        return [{"text": n} for n in (L.notes if L else [])]

    async def no_write(*a, **k):
        return 0.0

    async def no_send(*a, **k):
        raise RuntimeError("smoke: отправка запрещена")

    db.get_settings = get_settings
    db.get_setting = get_setting
    db.get_conversation_history = history
    db.get_lead_notes = notes
    db.add_daily_amount = no_write
    db.update_memory_summary = no_write
    sender.send_one = no_send
    escalation._send_telegram = no_write


async def amain(args) -> None:
    orig = asyncpg.create_pool

    async def ro_pool(*a, **k):
        k.setdefault("server_settings", {})["default_transaction_read_only"] = "on"
        return await orig(*a, **k)
    asyncpg.create_pool = ro_pool

    from config import settings
    if not args.dry and not (settings.openai_api_key or "").startswith("sk-"):
        raise SystemExit("нет OPENAI_API_KEY в окружении процесса")
    spend = Spend(args.budget)
    install(spend, args.dry)
    leads: dict[str, Lead] = {}
    install_db(leads)
    await db.init_pool()
    lines = [f"# Смоук reply_mode ({'DRY' if args.dry else 'OpenAI'}) {NOW:%Y-%m-%d %H:%M} UTC", ""]
    rows = []
    try:
        idx = 0
        if args.part in ("messages", "all"):
            lines.append("## 13 сообщений\n")
            for text, hist, prof in MESSAGES:
                idx += 1
                res = {}
                for mode in ("legacy", "rag"):
                    L = Lead(mode, idx, hist, prof)
                    leads[L.phone] = L
                    res[mode] = await one_turn(mode, L, text, spend)
                rows.append(("сообщение", text, res))
                lines += _fmt(text, res)
        if args.part in ("dialogs", "all"):
            for title, hist, prof, turns in DIALOGS:
                idx += 1
                lines.append(f"\n## {title}\n")
                Ls = {mode: Lead(mode, idx, hist, prof) for mode in ("legacy", "rag")}
                for L in Ls.values():
                    leads[L.phone] = L
                for text in turns:
                    res = {mode: await one_turn(mode, Ls[mode], text, spend) for mode in Ls}
                    rows.append((title, text, res))
                    lines += _fmt(text, res)
    except Budget as e:
        lines.append(f"\n⛔ ОСТАНОВЛЕНО: {e}")
        print(f"⛔ ОСТАНОВЛЕНО: {e}")
    finally:
        await db.close_pool()
    silent = sum(1 for _, _, r in rows if r["rag"]["decision"] == "silent")
    viol = {m: sum(1 for _, _, r in rows if r[m]["violations"]) for m in ("legacy", "rag")}
    lines += ["", "## Итог",
              f"- ходов: {len(rows)}; вызовов OpenAI: {spend.calls}; потрачено ${spend.usd:.3f} "
              f"(legacy ${spend.by_mode['legacy']:.3f}, rag ${spend.by_mode['rag']:.3f})",
              f"- rag silent: {silent} из {len(rows)}",
              f"- ответов с нарушениями: legacy {viol['legacy']}, rag {viol['rag']}"]
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        with open(args.out + ".json", "w", encoding="utf-8") as f:
            json.dump([{"bloque": b, "lead": t, **{m: r[m] for m in r}} for b, t, r in rows], f,
                      ensure_ascii=False, indent=1, default=str)


def _fmt(text: str, res: dict) -> list[str]:
    out = [f"**Лид:** {text}"]
    for mode in ("legacy", "rag"):
        r = res[mode]
        head = f"- **{mode}** ({r['decision']}, ${r['usd']:.4f})"
        if r.get("silent_reason"):
            head += f" — {r['silent_reason']}"
        if r.get("media"):
            head += f" + {','.join(r['media'])}"
        out.append(head)
        out += [f"    - {b}" for b in r["bubbles"]]
        if r.get("meta") and mode == "rag":
            m = r["meta"]
            out.append(f"    - _fuentes={m.get('fuentes')} etapa={m.get('etapa')} slot={m.get('slot')} "
                       f"notas={m.get('notas')}_")
        if r["violations"]:
            out.append(f"    - ⚠️ {'; '.join(r['violations'])}")
    out.append("")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["messages", "dialogs", "all"], default="all")
    ap.add_argument("--budget", type=float, default=2.0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--out", default="")
    asyncio.run(amain(ap.parse_args()))
