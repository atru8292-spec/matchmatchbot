"""Смоук режимов ответа: legacy и rag рядом на одних и тех же сообщениях (этап 4).

Реальный OpenAI (тратит деньги!), всё остальное изолировано:
  - БД только на чтение: каждый запрос проверяется в Python (ReadOnlyPool), запись или
    транзакция останавливают прогон (server_settings пулер Supabase игнорирует);
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
import reply_mode
import sender

NOW = datetime.now(timezone.utc)
LINK = "https://www.rusaencdmx.com/18-11-26-slavic-latino-night-halloween"
EVENT_SETTINGS = {
    "event_active": "1", "event_date": "2026-11-18", "event_time": "8:30 PM",
    "event_start": "8:30 PM", "event_end": "12:00 AM", "event_address": "Roma Norte, CDMX",
    "event_price_member": "", "event_price_nonmember": "6000", "event_price_next": "",
    # автоответ владелицы 2026-10-22: preventa 6,000, зачёркнутая 9,000, без лестницы цен
    "event_price_old": "9,000", "event_link": LINK, "bot_paused": "0",
    "event_theme_phrase": "Esta vez es temática de Halloween 🎃",
}
ANNOUNCED = "2026-10-20T17:45:22+00:00"
# Настройки конкретного диалога поверх EVENT_SETTINGS (диалог 15: без зачёркнутой цены).
OVERRIDE: dict = {}
# Сбой OpenAI посреди диалога (диалог 14): сколько следующих вызовов чата rag уронить.
FAIL_NEXT = {"n": 0}
FAIL_MARK = "[сбой OpenAI] "


def ev() -> dict:
    return {**EVENT_SETTINGS, **OVERRIDE}

GREETING_EVENT = [b for b in knowledge.load_seed()
                  if b["id"] == "greeting_event"][0]["contenido_es"].split("\n\n")


def M(sender_, text, minutes_ago, manual=False, meta=None):
    return {"sender": sender_, "direction": "inbound" if sender_ == "lead" else "outbound",
            "text": text, "created_at": NOW - timedelta(minutes=minutes_ago),
            "meta": meta or ({"manual": True} if manual else {})}


def _rendered(bubbles):
    return [b.replace("[event_date]", "18 de noviembre").replace("[event_address]", "Roma Norte, CDMX")
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
# Как в main.py: фото прошло проверку → photo_received=True и маркер в тексте для AI.
PHOTO_OK = "[фото одобрено]"
# Фото с подписью: main.py сначала прогоняет подпись через filters.decide и темы «молча Ане».
PHOTO_CAPTION = "[фото с подписью] "
AUTO_GREETING = ("👉 ¡Bienvenido a Match Match Agency! ✨\n\nEstamos organizando un evento exclusivo "
                 "para solteros: SLAVIC LATINO CONNECT NIGHT, Miércoles 18 de Noviembre, $6,000 MXN "
                 "PREVENTA, Roma Norte, CDMX.")
DIALOGS = [
    # Ссылка на ивент — только после одобренного фото (решение владелицы 2026-10-22).
    ("1. Ивент с нуля: покупка до фото, потом фото", [], {}, [
        "hola, vi el evento", "me llamo Javier, sí soltero", "¿qué incluye?",
        "ok, ¿cómo compro el boleto?", PHOTO_OK]),
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
    ("11. Фото прислал сам, потом спросил, как купить", BASE_EVENT, BASE_LEAD,
     [PHOTO_OK, "¿cómo compro el boleto?"]),
    # Решения владелицы 2026-10-22: риски из первого живого прогона.
    ("12. Старая переписка с выдумками старого бота (OXXO, 30 и 30)",
     BASE_EVENT + [M("lead", "¿cuántas personas van?", 2 * DAY),
                   M("anna", "Van 30 mujeres eslavas solteras y 30 hombres.", 2 * DAY - 1),
                   M("lead", "¿y cómo pago?", 2 * DAY - 2),
                   M("anna", "Puedes pagar con tarjeta o en efectivo en OXXO.", 2 * DAY - 3)],
     BASE_LEAD, ["¿al final cuántas mujeres van?", "¿y cómo se paga el boleto, como me dijiste?"]),
    ("13. Без фото после двух просьб, хочет купить",
     BASE_EVENT + [M("anna", "¿Me mandas una foto tuya?", 20), M("lead", "¿dónde es?", 15),
                   M("anna", "Es en Roma Norte, CDMX. ¿Me mandas una foto tuya?", 14)],
     {**BASE_LEAD, "age": 38, "profession": "ingeniero"}, ["¿cómo compro el boleto?"]),
    ("14. Сбой OpenAI посреди диалога", BASE_EVENT, BASE_LEAD,
     ["¿qué incluye?", FAIL_MARK + "¿a qué hora empieza?", "¿hay que ir disfrazado?"]),
    ("15. Без зачёркнутой старой цены", BASE_EVENT, BASE_LEAD, ["¿cuánto cuesta?"],
     {"event_price_old": ""}),
    ("16. Игнорирует вопрос о возрасте, три вопроса подряд",
     BASE_EVENT + [M("anna", "Es en Roma Norte, CDMX.", 10),
                   M("anna", "¿Cuántos años tienes y a qué te dedicas?", 10)],
     BASE_LEAD, ["¿qué incluye?", "¿hay que ir disfrazado?", "¿a qué hora empieza?"]),
    # Решения владелицы 2026-10-22: новое приветствие, автоприветствие, факты из автоответа.
    ("17. Голое «Hola» → ивент → имя", [], {}, ["Hola", "el evento", "me llamo Pedro, sí soltero"]),
    ("18. «Hola» после автоприветствия WhatsApp",
     [M("anna", AUTO_GREETING, 1, meta={"source": "auto_greeting"})], {}, ["Hola", "el evento"]),
    ("19. Сколько людей и сколько стоит", BASE_EVENT, BASE_LEAD,
     ["¿cuántas personas van?", "¿cuánto cuesta?"]),
    ("20. Фото с подписью «voy con mi novia»", BASE_EVENT, BASE_LEAD,
     [PHOTO_CAPTION + "voy con mi novia"]),
    ("21. Оплата картой", BASE_EVENT, BASE_LEAD, ["¿Aceptan tarjeta?"]),
    ("22. Хочет пойти, но дорого", BASE_EVENT, BASE_LEAD, ["Me gustaría ir pero está muy caro"]),
    # Сценарий Арины 2026-10-22: весь путь лида с нуля, 15 сообщений.
    ("23. Сценарий Арины целиком", [], {}, [
        "Hola", "Me interesa el evento", "Me llamo Luis, soy soltero", PHOTO_OK, "35, soy abogado",
        "¿cuánto cuesta?", "¿cuántas personas van?", "¿dónde es?", "¿a qué hora termina?",
        "está muy caro", "¿cómo compro?", "¿eres un bot?", "voy con mi novia", "¿aceptan tarjeta?",
        "¿viste el partido del América ayer?"]),
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
        if FAIL_NEXT["n"] and "chat/completions" in url:
            FAIL_NEXT["n"] -= 1
            raise RuntimeError("429 Too Many Requests (смоук: имитация сбоя)")
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


def judge(bubbles: list[str], lead_text: str, presented_before: bool,
          buying: bool | None = None, photo: bool = True, prev_reply: str | None = None,
          dio_algo: bool = True) -> list[str]:
    """buying — лид спрашивал, как купить (сейчас или раньше, а ссылку ещё не получил);
    photo — фото лида одобрено (без него ссылки на ивент нет)."""
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
    if buying is None:
        buying = bool(_PURCHASE.search(lead_text))
    if (LINK in text or "rusaencdmx.com" in text) and not buying:
        out.append("ссылка без вопроса о покупке")
    # с 2026-10-22 на прямой вопрос о покупке ссылка уходит и без фото (нужны имя и «холост ли»)
    if LINK in text and not photo and not buying:
        out.append("ссылка до фото")
    if presented_before and re.search(r"soy anna", text, re.I):
        out.append("повторная презентация")
    if re.search(r"medianoche|12:00|único", text, re.I):
        out.append("неподтверждённый факт")
    # лестницы цен нет; зачёркнутая — только «~9,000 MXN~» (автоответ владелицы 2026-10-22)
    if re.search(r"(?<!\d)[78],?000(?!\d)", text):
        out.append("старая лестница цен")
    if "~" in text.replace(f"~{ev().get('event_price_old')} MXN~", ""):
        out.append("зачёркнутая не та")
    for slot in memory.SLOT_QUESTION_RE:
        if memory.pregunta_slot(slot, prev_reply) and memory.pregunta_slot(slot, text):
            out.append(f"вопрос слота подряд: {slot}")
    if re.search(r"\[[a-z_]+\]", text):
        out.append("неподставленный токен")
    # Факт про ивент без вопроса лида (кроме приветствия, хода с фото и ответа со ссылкой).
    if presented_before and lead_text != PHOTO_OK and LINK not in text \
            and not rag._BROAD_ASK_RE.search(lead_text) \
            and not memory._PRICE_OBJECTION_RE.search(lead_text):
        asked = rag.temas_preguntados(lead_text)
        for b in bubbles:
            for sent in rag._sentences(b):
                temas = memory.hechos_en(sent, ev())
                if "?" not in sent and temas and not temas & asked and not rag._RECAP_RE.match(sent):
                    out.append(f"факт без вопроса: {sorted(temas)}")
    if any(re.search(r"[,:;]\s*$", b) for b in bubbles):
        out.append("оборванная фраза")
    if bubbles and re.match(r"^\W*(?:muchas\s+)?gracias\b", bubbles[0], re.I) and not dio_algo:
        out.append("«Gracias» без повода")
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
    if text.startswith(FAIL_MARK):
        text = text[len(FAIL_MARK):]
        FAIL_NEXT["n"] = 1 if mode == "rag" else 0
    prev_reply = memory.ultima_respuesta(
        [dict(m, direction="inbound" if m["sender"] == "lead" else "outbound") for m in L.history])
    link_before = any(LINK in (m["text"] or "") for m in L.history if m["sender"] == "anna")
    buying = bool(_PURCHASE.search(text)) or (not link_before and any(
        _PURCHASE.search(m["text"] or "") for m in L.history if m["sender"] == "lead"))
    if text.startswith(PHOTO_CAPTION):
        caption = text[len(PHOTO_CAPTION):]
        d = filters.decide(L.lead, False, caption, L.phone, frozenset(), bot_paused=False)
        if d.action != "needs_ai" or rag._SILENT_TOPIC_RE.search(caption):
            L.history.append(M("lead", caption, 0))
            return {"decision": d.action if d.action != "needs_ai" else "silent_topic",
                    "bubbles": [], "violations": [], "usd": 0.0}
        text = f"{caption}\n\n{PHOTO_OK}"
    if text.endswith(PHOTO_OK):
        L.lead["photo_received"] = True          # main.py ставит флаг до вызова AI
        # фото одобрено, имя и «холост ли» есть — ход сразу ведёт к покупке (решение 2026-10-22)
        buying = buying or bool(L.lead.get("name") and L.lead.get("is_single") is True)
    else:
        d = filters.decide(L.lead, False, text, L.phone, frozenset(), bot_paused=False)
        if d.action != "needs_ai":
            L.history.append(M("lead", text, 0))
            return {"decision": d.action, "bubbles": [], "violations": [], "usd": 0.0}
    L.history.append(M("lead", text, 0))
    lead_before = dict(L.lead)
    before = spend.usd
    if mode == "legacy":
        r = await ai._generate_reply_legacy(L.lead, L.history[-21:-1], text)
    else:
        # через общий диспетчер, как в проде: сбой режима rag → молчание, а не legacy
        r = await ai.generate_reply(L.lead, [], text)
    FAIL_NEXT["n"] = 0
    bubbles = await sender.render_bubbles(r.get("messages") or [], None, True)
    after = await sender.render_bubbles(r.get("after_media") or [], None, True)
    presented = any(m["sender"] == "anna" and (m.get("meta") or {}).get("source") != "auto_greeting"
                    for m in L.history[:-1])
    for b in bubbles:
        L.history.append(M("anna", b, 0))
    # Порядок, как шлёт main.py: rag — текст, видео, фото, вопрос; legacy — текст, фото, видео.
    media = []
    if r.get("send_event_video"):
        media.append(("video", r.get("video_caption")))
    if r.get("send_event_photo"):
        media.append(("image", r.get("photo_caption")))
    if mode == "legacy":
        media.reverse()
    order = [f"💬 {b}" for b in bubbles]
    for kind, caption in media:
        if caption:
            order.append(f"💬 {caption}")
        order.append("🎬 [видео ивента]" if kind == "video" else "🖼 [фото прошлых ивентов]")
        L.history.append(M("anna", db.media_marker(kind, ev().get("event_date")), 0))
    order += [f"💬 {b}" for b in after]
    for b in after:
        L.history.append(M("anna", b, 0))
    for k, v in (r.get("extracted") or {}).items():
        L.lead[k] = v
    if r.get("rag_estado") is not None:
        L.lead["rag_estado"] = r["rag_estado"]
    if r.get("funnel_stage"):
        L.lead["funnel_stage"] = r["funnel_stage"]
    said = bubbles + [c for _, c in media if c] + after
    viol = judge(said, text, presented, buying, bool(L.lead.get("photo_received")), prev_reply,
                 rag.lead_dio_algo(text, r.get("extracted") or {}, lead_before))
    texts = [o for o in order if o.startswith("💬")]
    if any("?" in t for t in texts[:-1]):
        viol.append("вопрос не последним сообщением")
    return {"decision": r.get("action"), "bubbles": bubbles, "order": order,
            "silent_reason": r.get("silent_reason"),
            "meta": r.get("rag_meta"), "media": [k for k in ("send_event_photo", "send_event_video")
                                                   if r.get(k)],
            "violations": viol, "usd": spend.usd - before}


def install_db(leads_by_phone: dict) -> None:
    real_get_settings = db.get_settings

    async def get_settings(keys):
        real = await real_get_settings([k for k in keys if k not in ev()])
        return {**real, **{k: ev()[k] for k in keys if k in ev()}}

    real_get_setting = db.get_setting

    async def get_setting(key):
        if key == "event_announced_at":
            return ANNOUNCED
        if key in ev():
            return ev()[key]
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

    async def resolve(phone):
        return reply_mode.RAG

    reply_mode.resolve = resolve        # rag-ход идёт через ai.generate_reply, как в проде
    db.get_settings = get_settings
    db.get_setting = get_setting
    db.get_conversation_history = history
    db.get_lead_notes = notes
    db.add_daily_amount = no_write
    db.update_memory_summary = no_write
    sender.send_one = no_send
    escalation._send_telegram = no_write


class WriteBlocked(BaseException):
    """Попытка записи в прод-БД из смоука. BaseException: не глотается try/except в коде бота."""


_READ_SQL = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(?:select|with|show)\b", re.IGNORECASE)
_WRITE_SQL = re.compile(r"\b(?:insert|update|delete|alter|create|drop|truncate|grant|merge)\b",
                        re.IGNORECASE)


class ReadOnlyPool:
    """Защита записи на уровне Python. Supabase pooler (транзакционный режим) игнорирует
    server_settings, поэтому default_transaction_read_only=on сам по себе НЕ работает
    (проверено 2026-10-22: SHOW default_transaction_read_only → off)."""

    def __init__(self, pool):
        self._pool = pool
        self.reads = 0

    def _check(self, sql: str) -> None:
        if not _READ_SQL.match(sql or "") or _WRITE_SQL.search(sql or ""):
            raise WriteBlocked(f"смоук: запрос на запись заблокирован: {(sql or '')[:120]!r}")
        self.reads += 1

    async def fetch(self, sql, *a, **k):
        self._check(sql)
        return await self._pool.fetch(sql, *a, **k)

    async def fetchrow(self, sql, *a, **k):
        self._check(sql)
        return await self._pool.fetchrow(sql, *a, **k)

    async def fetchval(self, sql, *a, **k):
        self._check(sql)
        return await self._pool.fetchval(sql, *a, **k)

    async def execute(self, sql, *a, **k):
        self._check(sql)
        return await self._pool.execute(sql, *a, **k)

    def acquire(self, *a, **k):
        raise WriteBlocked("смоук: pool.acquire (транзакции) заблокирован")

    async def close(self):
        await self._pool.close()


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
    db._pool = ReadOnlyPool(db._pool)
    lines = [f"# Смоук reply_mode ({'DRY' if args.dry else 'OpenAI'}) {NOW:%Y-%m-%d %H:%M} UTC", ""]
    rows = []
    modes = ("legacy", "rag") if args.modes == "both" else ("rag",)
    try:
        idx = 0
        if args.part in ("messages", "all"):
            lines.append("## 13 сообщений\n")
            for text, hist, prof in MESSAGES:
                idx += 1
                res = {}
                for mode in modes:
                    L = Lead(mode, idx, hist, prof)
                    leads[L.phone] = L
                    res[mode] = await one_turn(mode, L, text, spend)
                rows.append(("сообщение", text, res))
                lines += _fmt(text, res, modes)
        if args.part in ("dialogs", "all"):
            for title, hist, prof, turns, *over in DIALOGS:
                idx += 1
                if args.only and title.split(".")[0] not in args.only.split(","):
                    continue
                OVERRIDE.clear()
                OVERRIDE.update(over[0] if over else {})
                knowledge.reset_cache()
                lines.append(f"\n## {title}\n")
                Ls = {mode: Lead(mode, idx, hist, prof) for mode in modes}
                for L in Ls.values():
                    leads[L.phone] = L
                for text in turns:
                    res = {mode: await one_turn(mode, Ls[mode], text, spend) for mode in Ls}
                    rows.append((title, text, res))
                    lines += _fmt(text, res, modes)
    except Budget as e:
        lines.append(f"\n⛔ ОСТАНОВЛЕНО: {e}")
        print(f"⛔ ОСТАНОВЛЕНО: {e}")
    finally:
        await db.close_pool()
    silent = sum(1 for _, _, r in rows if r.get("rag", {}).get("decision") == "silent")
    viol = {m: sum(1 for _, _, r in rows if r.get(m, {}).get("violations")) for m in ("legacy", "rag")}
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


def _fmt(text: str, res: dict, modes=("legacy", "rag")) -> list[str]:
    out = [f"**Лид:** {text}"]
    for mode in modes:
        r = res[mode]
        head = f"- **{mode}** ({r['decision']}, ${r['usd']:.4f})"
        if r.get("silent_reason"):
            head += f" — {r['silent_reason']}"
        if r.get("media"):
            head += f" + {','.join(r['media'])}"
        out.append(head)
        out += [f"    - {b}" for b in (r.get("order") or r["bubbles"])]
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
    # повторный прогон только нового режима (ответы legacy не меняются) — вдвое дешевле
    ap.add_argument("--modes", choices=["both", "rag"], default="both")
    # только эти диалоги по номеру: --part dialogs --only 1,3,11
    ap.add_argument("--only", default="")
    asyncio.run(amain(ap.parse_args()))
