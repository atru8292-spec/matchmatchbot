"""Режим ответов rag: разговор по базе знаний с памятью лида (app_settings.reply_mode).

Управляемый конвейер, модель сама в базу не ходит:
  1. память лида (memory.build): окно 40 сообщений, сводка, ya_hecho, заметки, regreso, слоты;
  2. дословно без модели: сценарии VERBATIM_IDS (бот?, блоки) и base_script при совпадении
     ситуации (приветствия, pitch услуги после фото);
  3. поиск по knowledge (эмбеддинг последнего сообщения, top 5) → контекст → gpt-4.1, JSON;
  4. falta_dato + query_extra → один повторный поиск и повторный вызов; данных нет → silent;
  5. проверки кодом: 5,500, числа только из источников, ссылки, эмодзи, один вопрос, имя в
     начале, повторная презентация, зачины/фразы/сленг (conversation_rules), этап venta;
  6. сверка утверждений с источниками второй моделью (gpt-4.1-mini); без источника → silent;
  7. логи: режим, этап, слот, источники, длина памяти, токены, стоимость; расход за сутки
     копится в app_settings (counter:rag_cost_usd:<дата Мехико>) для /status.
silent = лиду ничего, ручной режим и алерт Ане (main._run_ai, ветка action='silent').
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import ai
import conversation_rules
import db
import knowledge
import memory
import sender
from filters import MAX_AGE, MIN_AGE

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent / "anna_prompt_conversation.md"
MODEL = "gpt-4.1"
CHECK_MODEL = "gpt-4.1-mini"
SUMMARY_MODEL = "gpt-4.1-mini"
EMBED_MODEL = "text-embedding-3-small"
TEMPERATURE = 0.3
MAX_BUBBLES = 4
TOP_K = 5
COST_COUNTER = "rag_cost_usd"
DAILY_COST_WARN_USD = 5.0

# Дословно без модели (решение владелицы): «ты бот?» и блоки.
VERBATIM_IDS = {40, 9, 12, 27, 28, 29, 7, 8}
# base_script, которые код отправляет сам при совпадении ситуации.
CODE_LITERAL_IDS = ("greeting_hola", "greeting_event", "greeting_service", "service_pitch")

# Цена за 1M токенов, USD (OpenAI, 2026-10). cached — часть prompt из кэша.
PRICES = {
    "gpt-4.1": {"in": 2.00, "cached": 0.50, "out": 8.00},
    "gpt-4.1-mini": {"in": 0.40, "cached": 0.10, "out": 1.60},
    "text-embedding-3-small": {"in": 0.02, "cached": 0.02, "out": 0.0},
}

VIDEO_CAPTION = "Te dejo también un video con los detalles del evento."
PHOTO_CAPTION = "Así se ven nuestros eventos."

_prompt_cache: str | None = None


# ───────────────────────── стоимость ─────────────────────────

class Cost:
    """Токены и стоимость одного ответа."""

    def __init__(self) -> None:
        self.usd = 0.0
        self.tokens = {"in": 0, "cached": 0, "out": 0}
        self.calls: list[str] = []

    def add(self, model: str, usage: dict | None) -> None:
        usage = usage or {}
        p = PRICES.get(model, PRICES["gpt-4.1"])
        tin = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        self.usd += ((tin - cached) * p["in"] + cached * p["cached"] + tout * p["out"]) / 1e6
        self.tokens["in"] += tin
        self.tokens["cached"] += cached
        self.tokens["out"] += tout
        self.calls.append(model)


async def _record_daily_cost(cost: Cost) -> None:
    if cost.usd <= 0:
        return
    try:
        total = await db.add_daily_amount(COST_COUNTER, cost.usd)
        if total >= DAILY_COST_WARN_USD and total - cost.usd < DAILY_COST_WARN_USD:
            import escalation
            await escalation.notify_error(
                "ai_conversation", f"расход OpenAI режима rag за сутки ${total:.2f} > "
                f"${DAILY_COST_WARN_USD:.0f}", None)
    except Exception:
        logger.exception("rag: не записала расход за сутки")


# ───────────────────────── OpenAI ─────────────────────────

def load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        if not PROMPT_PATH.exists():
            raise NotImplementedError("нет anna_prompt_conversation.md (этап 3)")
        _prompt_cache = PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


async def _chat(model: str, system: str, user: dict | str, cost: Cost,
                max_tokens: int = 700, temperature: float = TEMPERATURE) -> dict:
    content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False, default=str)
    r = await ai._openai_post(
        "https://api.openai.com/v1/chat/completions",
        {"model": model, "temperature": temperature, "max_tokens": max_tokens,
         "response_format": {"type": "json_object"},
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": content}]},
        timeout=60,
    )
    data = r.json()
    cost.add(model, data.get("usage"))
    return json.loads(data["choices"][0]["message"]["content"])


async def _embed_batch(texts: list[str], cost: Cost | None = None) -> list[list[float]]:
    r = await ai._openai_post("https://api.openai.com/v1/embeddings",
                              {"model": EMBED_MODEL, "input": texts}, timeout=30)
    data = r.json()
    if cost is not None:
        cost.add(EMBED_MODEL, data.get("usage"))
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


# ───────────────────────── распознавание сообщения ─────────────────────────

_QUESTION_WORD_RE = re.compile(
    r"^\W*(?:qu[eé]|cu[aá]nto|cu[aá]ntos|cu[aá]ntas|cu[aá]ndo|d[oó]nde|c[oó]mo|cu[aá]l|"
    r"qui[eé]n|por\s*qu[eé]|hay|puedo|se\s+puede|tienen|tienes|aceptan|incluye|es\s+)\b",
    re.IGNORECASE)
_EVENT_INTEREST_RE = re.compile(r"evento|boleto|halloween|slavic|novia\s+rusa", re.IGNORECASE)
_AGENCY_INTEREST_RE = re.compile(r"rus[ao]s?|eslav[ao]s?|ucranian|bielorrus|matchmak|agencia|"
                                 r"servicio|conocer\s+mujeres|busco\s+(?:pareja|esposa|novia)",
                                 re.IGNORECASE)
_PURCHASE_RE = re.compile(r"compr|boleto|pag[oa]r?\b|pago|link|enlace|reserv|apart|"
                          r"quiero\s+ir|me\s+apunto|inscrib|registr|d[oó]nde\s+(?:compro|pago)",
                          re.IGNORECASE)
# Прямая просьба ссылки: повторяем, даже если ya_hecho.enlace_enviado.
_LINK_REQUEST_RE = re.compile(r"link|enlace|p[aá]same|m[aá]ndame\s+el|otra\s+vez|de\s+nuevo|"
                              r"c[oó]mo\s+(?:compro|pago|reservo)|d[oó]nde\s+(?:compro|pago|reservo)",
                              re.IGNORECASE)


def has_question(text: str) -> bool:
    text = text or ""
    return "?" in text or "¿" in text or bool(_QUESTION_WORD_RE.search(text))


def interes_de(text: str) -> str:
    if _EVENT_INTEREST_RE.search(text or ""):
        return "event"
    if _AGENCY_INTEREST_RE.search(text or ""):
        return "agency"
    return "sin_definir"


def situacion_ok(sit: dict, mem: dict, user_text: str, lead: dict) -> bool:
    """Машинное условие base_script (knowledge.situacion)."""
    ya, cal = mem["ya_hecho"], mem["calificacion"]
    for key, want in (sit or {}).items():
        if key == "primer_mensaje_del_lead":
            have = mem["primer_mensaje_del_lead"]
        elif key == "pregunta_concreta":
            have = has_question(user_text)
        elif key == "presentacion_hecha":
            have = ya["presentacion"]
        elif key == "interes":
            if interes_de(user_text) not in want:
                return False
            continue
        elif key == "ruta":
            have = cal["ruta"] == "servicio" or lead.get("interest") in ("agency", "both")
            want = True
        elif key == "foto_aprobada":
            have = "[фото одобрено]" in (user_text or "")
        elif key == "slots_completos":
            have = all(lead.get(s) if s != "is_single" else lead.get("is_single") is True
                       for s in ("name", "is_single", "age", "profession"))
        elif key == "precio_servicio_dado":
            have = bool(ya["precio_servicio_dado"])
        else:
            return False          # неизвестное условие — не рискуем дословной отправкой
        if bool(have) != bool(want):
            return False
    return True


# ───────────────────────── проверки ответа кодом ─────────────────────────

_TOKEN_RE = re.compile(r"\[[a-z_]+\]")
_NUM_RE = re.compile(r"\d+(?:[.,:]\d+)*")
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_5500_RE = re.compile(r"5[\s.,]?500")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_QUESTION_SENT_RE = re.compile(r"¿[^?]*\?|[^.!¿?]*\?")
_INTRO_RE = re.compile(r"\bsoy\s+anna\b[^.!?]*[.!?]?\s*", re.IGNORECASE)
_HOLA_START_RE = re.compile(r"^\s*¡?\s*hola\b[^.!?,]*[.!?,]?\s*", re.IGNORECASE)
_SERVICE_PRICE_RE = re.compile(r"10[.,]?000\s*(?:usd|d[oó]lares)?|\$\s*10", re.IGNORECASE)


def _norm_num(s: str) -> str:
    return s if ":" in s else s.replace(",", "").replace(".", "")


def numbers_in(text: str) -> set[str]:
    return {_norm_num(n) for n in _NUM_RE.findall(_TOKEN_RE.sub(" ", text or ""))}


def render_tokens(text: str, settings: dict) -> str:
    """Подставить токены ивента для сверки (как сделает sender)."""
    def val(m):
        k = m.group(0)[1:-1]
        v = (settings.get(k) or "").strip()
        return sender._fmt_date_es(v) if k == "event_date" and v else v
    return _TOKEN_RE.sub(val, text or "")


def allowed_numbers(fichas: list[dict], settings: dict, lead: dict, lead_texts: list[str]) -> set[str]:
    nums: set[str] = set()
    for f in fichas:
        nums |= numbers_in(render_tokens(f.get("contenido_es") or "", settings))
    for k in ("event_date", "event_start", "event_price_nonmember", "event_price_next"):
        v = settings.get(k) or ""
        nums |= numbers_in(sender._fmt_date_es(v) if k == "event_date" and v else v)
    for t in lead_texts:
        nums |= numbers_in(t)
    for k in ("age", "desired_partner_age"):
        if lead.get(k):
            nums |= numbers_in(str(lead[k]))
    return nums


def _sentences(text: str) -> list[str]:
    """Предложения по . ! ? (знак остаётся у предложения)."""
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]


def _drop_sentences(text: str, pred) -> str:
    return " ".join(s for s in _sentences(text) if not pred(s)).strip()


def keep_one_emoji(bubbles: list[str]) -> list[str]:
    seen = False
    out = []
    for b in bubbles:
        def repl(m):
            nonlocal seen
            if seen:
                return ""
            seen = True
            return m.group(0)
        out.append(re.sub(r"[ \t]{2,}", " ", _EMOJI_RE.sub(repl, b)).strip())
    return out


def keep_last_question(bubbles: list[str]) -> list[str]:
    """Не больше одного вопроса на ответ: оставляем последний вопрос, прочие убираем."""
    found = [(i, m.group(0)) for i, b in enumerate(bubbles) for m in _QUESTION_SENT_RE.finditer(b)]
    if len(found) <= 1:
        return bubbles
    keep_i, keep_q = found[-1]
    out = []
    for i, b in enumerate(bubbles):
        for j, q in found:
            if j == i and not (j == keep_i and q == keep_q):
                b = b.replace(q, "", 1)
        out.append(re.sub(r"\s{2,}", " ", b).strip())
    return out


def strip_name_start(bubbles: list[str], name: str | None) -> list[str]:
    if not name:
        return bubbles
    rx = re.compile(rf"^\s*[¡¿]?\s*{re.escape(name)}\s*[,!.:]\s*", re.IGNORECASE)
    return [rx.sub("", b, count=1) for b in bubbles]


def clean_style(bubbles: list[str]) -> list[str]:
    """Зачин в начале пузыря срезаем; предложения с запрещённой фразой или сленгом убираем."""
    out = []
    for b in bubbles:
        m = conversation_rules.BANNED_OPENER_RE.search(b)
        if m:
            b = re.sub(r"^[\s,.!:;]+", "", b[m.end():])
            b = b[:1].upper() + b[1:]
        b = _drop_sentences(b, lambda s: bool(conversation_rules.BANNED_PHRASE_RE.search(s)
                                              or conversation_rules.SLANG_RE.search(s)))
        out.append(b)
    return out


def check_reply(bubbles: list[str], *, mem: dict, fichas: list[dict], lead: dict,
                user_text: str) -> tuple[list[str], str | None, list[str]]:
    """Проверки кодом. Возвращает (пузыри, причина silent или None, список правок)."""
    notes: list[str] = []
    settings, ya, cal = mem["settings"], mem["ya_hecho"], mem["calificacion"]
    text = "\n\n".join(bubbles)
    if _5500_RE.search(_TOKEN_RE.sub(" ", text)):
        return [], "в ответе 5,500", ["5500"]
    link = (settings.get("event_link") or "").strip()
    for url in _URL_RE.findall(text):
        if not (link and url.rstrip(".,)") == link):
            return [], f"ссылка не из базы: {url}", ["url"]
    extra = set(numbers_in(text)) - allowed_numbers(fichas, settings, lead, mem["lead_texts"])
    if extra:
        return [], f"числа без источника: {sorted(extra)}", ["numbers"]

    # Ссылка на билет: только на вопрос о покупке, только в этапе venta, без повтора.
    def has_link(b):
        return "[event_link]" in b or bool(link and link in b)
    if any(has_link(b) for b in bubbles):
        reason = None
        if not _PURCHASE_RE.search(user_text or ""):
            reason = "лид не спрашивал, как купить"
        elif cal["etapa"] == "calificacion":
            reason = "этап calificacion"
        elif ya.get("enlace_enviado") and not _LINK_REQUEST_RE.search(user_text or ""):
            reason = "ссылка уже отправлена"
        if reason:
            bubbles = [b for b in bubbles if not has_link(b)]
            notes.append(f"убрала ссылку ({reason})")
    # Цена услуги — только в этапе venta.
    if cal["etapa"] == "calificacion" and cal["ruta"] == "servicio":
        new = [_drop_sentences(b, lambda s: bool(_SERVICE_PRICE_RE.search(s))) for b in bubbles]
        if new != bubbles:
            notes.append("убрала цену услуги (этап calificacion)")
            bubbles = new
    # Повторная презентация и приветствие посреди разговора.
    if ya.get("presentacion"):
        new = [_INTRO_RE.sub("", b) for b in bubbles]
        if mem.get("regreso") is None:
            new = [_HOLA_START_RE.sub("", b, count=1) for b in new]
        if new != bubbles:
            notes.append("убрала повторную презентацию")
            bubbles = new
    name = lead.get("name") or ai._plausible_name(lead.get("whatsapp_name"))
    bubbles = strip_name_start(bubbles, name)
    styled = clean_style(bubbles)
    if styled != bubbles:
        notes.append("убрала зачин/фразу/сленг")
        bubbles = styled
    bubbles = keep_last_question(keep_one_emoji(bubbles))
    bubbles = [b[:1].upper() + b[1:] for b in (b.strip() for b in bubbles) if b]
    if len(bubbles) > MAX_BUBBLES:
        bubbles = bubbles[:MAX_BUBBLES]
    if not bubbles:
        return [], "после проверок ответ пустой", notes
    return bubbles, None, notes


VERIFY_SYSTEM = (
    "Eres un verificador. Recibes la RESPUESTA de una asesora a un cliente y las FUENTES "
    "permitidas. Lista las afirmaciones de hecho de la RESPUESTA (sobre el evento, la "
    "agencia, precios, fechas, lugares, personas, cantidades, condiciones, procesos) que NO "
    "estén respaldadas por las FUENTES o por lo que el propio cliente dijo. Preguntas, "
    "saludos, empatía y frases de cortesía no son afirmaciones. Los tokens entre corchetes "
    "como [event_price_nonmember] son valores de las fuentes. Responde SOLO JSON: "
    '{"sin_fuente": ["afirmación", ...]} (lista vacía si todo está respaldado).'
)


async def verify_claims(bubbles: list[str], fichas: list[dict], card: dict | None,
                        lead_texts: list[str], cost: Cost) -> list[str]:
    payload = {
        "RESPUESTA": bubbles,
        "FUENTES": {"evento_actual": card,
                    "fichas": [f.get("contenido_es") for f in fichas if f.get("contenido_es")]},
        "LO_QUE_DIJO_EL_CLIENTE": lead_texts[-15:],
    }
    out = await _chat(CHECK_MODEL, VERIFY_SYSTEM, payload, cost, max_tokens=300, temperature=0)
    claims = out.get("sin_fuente") or []
    return [str(c) for c in claims if str(c).strip()]


# ───────────────────────── сводка памяти ─────────────────────────

SUMMARY_SYSTEM = (
    "Resume la parte antigua de una conversación de WhatsApp entre un cliente (lead) y Anna "
    "(asesora de una agencia de matchmaking). Escribe en español, máximo 120 palabras, en "
    "viñetas: qué contó el cliente de sí mismo; qué preguntó; qué le prometieron (el bot o "
    "Anna a mano); sus objeciones; en qué quedaron. SOLO lo que está en los mensajes, sin "
    "suposiciones. Cifras solo citadas tal cual. Responde JSON: {\"resumen\": \"...\"}."
)
SUMMARY_LAG = 20


async def maybe_refresh_summary(lead: dict, mem: dict, cost: Cost, force: bool = False) -> None:
    """Сводка всего, что старше окна в 40 сообщений, если её нет или она отстала на 20+."""
    prior = mem["prior"]
    older = prior[:-memory.WINDOW]
    if not older or not lead.get("phone"):
        return
    covered = int(lead.get("memoria_mensajes") or 0)
    if not force and lead.get("memoria_resumen") and len(older) - covered < SUMMARY_LAG:
        return
    lines = [{"fecha": memory.fecha(m["created_at"]) if isinstance(m.get("created_at"), datetime)
              else None, "autor": memory.autor(m), "texto": m.get("text") or ""}
             for m in older[-400:]]
    out = await _chat(SUMMARY_MODEL, SUMMARY_SYSTEM, {"mensajes": lines}, cost,
                      max_tokens=400, temperature=0)
    resumen = (out.get("resumen") or "").strip()
    if not resumen:
        return
    last_id = older[-1].get("id")
    try:
        await db.update_memory_summary(lead["phone"], resumen, last_id, len(older))
    except Exception:
        logger.exception("rag: не сохранила сводку памяти [%s]", lead.get("phone"))
    lead["memoria_resumen"] = resumen
    lead["memoria_mensajes"] = len(older)
    mem["resumen"] = resumen


async def refresh_stale_summaries(limit: int = 5, quiet_hours: int = 6) -> int:
    """Планировщик: лиды, которые молчат > quiet_hours и чья сводка отстала. Возвращает число."""
    try:
        phones = await db.leads_needing_summary(memory.WINDOW + SUMMARY_LAG, quiet_hours, limit)
    except Exception:
        logger.exception("rag: не нашла лидов для сводки (миграция 008 применена?)")
        return 0
    done = 0
    for phone in phones:
        cost = Cost()
        try:
            lead = await db.get_lead_by_phone(phone) or {"phone": phone}
            mem = await memory.build(lead, "")
            await maybe_refresh_summary(lead, mem, cost)
            done += 1
        except Exception:
            logger.exception("rag: сводка памяти упала [%s]", phone)
        finally:
            await _record_daily_cost(cost)
    return done


# ───────────────────────── ответ ─────────────────────────

def _result(messages: list[str], action: str = "respond", **extra) -> dict:
    base = {"messages": messages, "funnel_stage": None, "action": action, "extracted": {},
            "needs_escalation": action in ("escalate", "silent"), "used_scenario_id": None,
            "send_event_photo": False, "send_event_video": False,
            "proposed_videocall_at": None}
    base.update(extra)
    return base


def _silent(reason: str, **extra) -> dict:
    return _result([], "silent", silent_reason=f"Режим rag: {reason}", **extra)


def _parse_model(raw: dict) -> dict:
    bubbles = raw.get("bubbles") or raw.get("messages") or []
    if not isinstance(bubbles, list):
        bubbles = [str(bubbles)]
    raw_ex = raw.get("extracted") or {}
    extracted = {k: raw_ex.get(k) for k in ai._EXTRACTED_KEYS if raw_ex.get(k) is not None}
    stage = raw.get("funnel_stage")
    return {
        "bubbles": [str(b).strip() for b in bubbles if str(b).strip()],
        "accion": "silent" if raw.get("accion") == "silent" else "respond",
        "fuentes": [str(f) for f in (raw.get("fuentes") or [])],
        "falta_dato": bool(raw.get("falta_dato")),
        "query_extra": (raw.get("query_extra") or "").strip() or None,
        "en_tema": raw.get("en_tema") is not False,
        "extracted": extracted,
        "send_event_photo": bool(raw.get("send_event_photo")),
        "funnel_stage": stage if stage in ai.funnel.FUNNEL_STAGES else None,
        "proposed_videocall_at": (raw.get("proposed_videocall_at")
                                  if isinstance(raw.get("proposed_videocall_at"), str)
                                  and raw.get("proposed_videocall_at").strip() else None),
    }


def build_context(lead: dict, mem: dict, fichas: list[dict], always: dict, user_text: str,
                  card: dict | None) -> dict:
    cal = {k: v for k, v in mem["calificacion"].items()}
    return {
        "ahora_cdmx": ai._ahora_cdmx(),
        "evento_actual": card,
        "calificacion": cal,
        "memoria": {
            "resumen": mem["resumen"],
            "aviso_resumen": "El resumen describe al lead y la conversación; no es fuente de "
                             "datos sobre la agencia ni el evento.",
            "ya_hecho": mem["ya_hecho"],
            "notas_internas": {"aviso": "Interno: no citar ni parafrasear al lead.",
                               "notas": mem["notas_internas"]},
        },
        "regreso": mem["regreso"],
        "historial": mem["historial"],
        "fichas": [knowledge.ficha_for_model(f) for f in fichas],
        "estilo": always["estilo"],
        "ejemplos_buenos": always["ejemplos_buenos"],
        "lead": {"nombre": lead.get("name") or ai._plausible_name(lead.get("whatsapp_name"))},
        "lead_message": user_text,
    }


async def _verbatim_scenario(vec: list[float]) -> dict | None:
    """Сценарий из VERBATIM_IDS с уверенным совпадением → дословный ответ (как legacy)."""
    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    rows = [dict(r) for r in await db.search_scenarios_by_vector(literal, 3)]
    if not rows or rows[0]["id"] not in VERBATIM_IDS:
        return None
    top = rows[0]
    ambiguous = len(rows) >= 2 and rows[0]["score"] - rows[1]["score"] < 0.05
    block = top.get("mode") == "bot_then_block" or top.get("blocks_lead")
    if ambiguous or top["score"] < (ai.FIXED_BLOCK_SCORE if block else ai.FIXED_SCORE):
        return None
    return top


async def _scenario_reply(sid: int) -> dict | None:
    row = await db.get_scenario_row(sid)
    return ai._fixed_reply(row) if row else None


async def generate_reply(lead: dict, history: list[dict], user_text: str) -> dict:
    """Ответ в режиме rag. Контракт результата тот же, что у ai.generate_reply, плюс
    silent_reason (текст алерта Ане) и rag_estado (сохраняет main)."""
    prompt = load_prompt()          # нет промпта → NotImplementedError → legacy
    cost = Cost()
    lead = dict(lead or {})
    try:
        result = await _generate(lead, user_text, prompt, cost)
    finally:
        await _record_daily_cost(cost)
    meta = result.setdefault("rag_meta", {})
    meta["usd"] = round(cost.usd, 5)
    meta["tokens"] = dict(cost.tokens)
    logger.info("rag [%s]: action=%s etapa=%s slot=%s fuentes=%s memoria≈%s tok "
                "tokens=%s usd=%.4f notas=%s", lead.get("phone"), result["action"],
                meta.get("etapa"), meta.get("slot"), meta.get("fuentes"),
                meta.get("memoria_tokens"), cost.tokens, cost.usd, meta.get("notas"))
    return result


async def _generate(lead: dict, user_text: str, prompt: str, cost: Cost) -> dict:
    mem = await memory.build(lead, user_text)
    settings = mem["settings"]
    cal = mem["calificacion"]
    meta = {"etapa": cal["etapa"], "slot": cal["siguiente"]}

    if len(mem["prior"]) > memory.WINDOW:
        try:
            await maybe_refresh_summary(lead, mem, cost)
        except Exception:
            logger.exception("rag: сводка памяти перед ответом упала")

    query = user_text
    if len((user_text or "").split()) < 4 and mem["ultimo_mensaje_anna"]:
        query = f"{mem['ultimo_mensaje_anna']} {user_text}"
    vec = (await _embed_batch([query], cost))[0]

    # 1. Дословные сценарии (бот?, блоки) — как в legacy, без модели.
    try:
        top = await _verbatim_scenario(vec)
    except Exception:
        logger.exception("rag: поиск дословных сценариев упал")
        top = None
    if top:
        reply = ai._fixed_reply(top)
        reply["rag_meta"] = {**meta, "fuentes": [f"scenario:{top['id']}"]}
        return reply

    # 2. base_script при точном совпадении ситуации.
    recs = knowledge.usable(await knowledge.records(), settings, knowledge.today_cdmx())
    by_id = {r["id"]: r for r in recs}
    for rid in CODE_LITERAL_IDS:
        rec = by_id.get(rid)
        if rec and rec.get("literal") and situacion_ok(rec.get("situacion") or {}, mem,
                                                        user_text, lead):
            stage = "pitched" if rid == "service_pitch" else "qualifying"
            interest = {"greeting_event": "event", "greeting_service": "agency"}.get(rid)
            return _result([b.strip() for b in rec["contenido_es"].split("\n\n") if b.strip()],
                           funnel_stage=stage, extracted={"interest": interest} if interest else {},
                           rag_meta={**meta, "fuentes": [rid], "literal": True})

    # 3. Поиск по базе знаний и вызов модели.
    async def embed_records(texts):
        return await _embed_batch(texts, cost)
    fichas = await knowledge.search(vec, recs, embed_records, top_k=TOP_K)
    always = knowledge.always_in_context(recs)
    name_rec = by_id.get("event_name")
    card = memory.evento_actual(settings)
    if card and name_rec:
        card["nombre"] = name_rec["contenido_es"]
    context = build_context(lead, mem, fichas, always, user_text, card)
    meta["memoria_tokens"] = len(json.dumps(
        {k: context[k] for k in ("memoria", "historial", "calificacion")},
        ensure_ascii=False, default=str)) // 4

    try:
        out = _parse_model(await _chat(MODEL, prompt, context, cost))
        if out["falta_dato"] and out["query_extra"]:
            vec2 = (await _embed_batch([out["query_extra"]], cost))[0]
            more = await knowledge.search(vec2, recs, embed_records, top_k=TOP_K)
            seen = {f["id"] for f in fichas}
            fichas = fichas + [f for f in more if f["id"] not in seen]
            context["fichas"] = [knowledge.ficha_for_model(f) for f in fichas]
            context["reintento"] = "Segunda búsqueda con query_extra. Si aún falta el dato: falta_dato=true."
            out = _parse_model(await _chat(MODEL, prompt, context, cost))
    except Exception as e:
        logger.exception("rag: OpenAI/разбор ответа упал")
        import escalation
        try:
            await escalation.notify_error("ai_conversation.generate_reply", repr(e), lead.get("phone"))
        except Exception:
            logger.exception("notify_error упал")
        return ai._fallback_reply()

    meta["fuentes"] = out["fuentes"]
    estado = dict(lead.get("rag_estado") or {})
    if out["en_tema"]:
        estado["fuera_de_tema_seguidos"] = 0
    else:
        estado["fuera_de_tema_seguidos"] = int(estado.get("fuera_de_tema_seguidos") or 0) + 1
    extra = {"rag_estado": estado, "extracted": out["extracted"], "rag_meta": meta}

    if out["falta_dato"]:
        return _silent("нет данных в базе знаний", **extra)
    if out["accion"] == "silent":
        return _silent("модель решила передать Ане", **extra)
    silent_ids = {f["id"] for f in fichas if f.get("accion") == "silent_escalate"}
    if silent_ids & set(out["fuentes"]):
        return _silent(f"политика молчания ({', '.join(sorted(silent_ids & set(out['fuentes'])))})",
                       **extra)
    if not out["en_tema"]:
        if estado["fuera_de_tema_seguidos"] >= 3 or int(estado.get("regresos") or 0) >= 2:
            return _silent("лид третий раз подряд не по теме", **extra)
        estado["regresos"] = int(estado.get("regresos") or 0) + 1

    # Фильтры по данным, названным в этом сообщении: дословно №7/№8/№9 и блок.
    merged = {**lead, **out["extracted"]}
    age = merged.get("age")
    if isinstance(age, int) and not (MIN_AGE <= age <= MAX_AGE):
        reply = await _scenario_reply(7 if age < MIN_AGE else 8)
        if reply:
            reply.update(extracted=out["extracted"], rag_estado=estado, rag_meta=meta)
            return reply
    if out["extracted"].get("is_single") is False:
        reply = await _scenario_reply(9)
        if reply:
            reply.update(extracted=out["extracted"], rag_estado=estado, rag_meta=meta)
            return reply

    bubbles, reason, notes = check_reply(out["bubbles"], mem=mem, fichas=fichas, lead=merged,
                                         user_text=user_text)
    meta["notas"] = notes
    if reason:
        return _silent(reason, **extra)

    try:
        claims = await verify_claims(bubbles, fichas, card, mem["lead_texts"], cost)
    except Exception:
        logger.exception("rag: проверка второй моделью упала → silent")
        return _silent("проверка утверждений недоступна", **extra)
    if claims:
        return _silent(f"утверждения без источника: {claims}", **extra)

    result = _result(bubbles, extracted=out["extracted"], funnel_stage=out["funnel_stage"],
                     rag_estado=estado, rag_meta=meta)
    if cal["pasar_a_llamada"]:
        result["needs_escalation"] = True
    # Видео — один раз, вместе с первой ссылкой на билет.
    link = (settings.get("event_link") or "").strip()
    if any("[event_link]" in b or (link and link in b) for b in bubbles) and \
            not mem["ya_hecho"].get("video_enviado"):
        result["send_event_video"] = True
        await ai._maybe_announce_event_video(result, None, lead)
        if result.get("video_caption"):
            result["video_caption"] = VIDEO_CAPTION
    if out["send_event_photo"] and not mem["ya_hecho"].get("foto_enviada"):
        result["send_event_photo"] = True
        await ai._maybe_announce_event_photo(result, None, lead)
        if result.get("photo_caption"):
            result["photo_caption"] = PHOTO_CAPTION
    # Запись на звонок — только в этапе venta, с именем, почтой и показанной ценой услуги.
    if out["proposed_videocall_at"]:
        result["proposed_videocall_at"] = out["proposed_videocall_at"]
        result = ai._enforce_videocall_proposal_needs_current_signal(result, user_text)
        price_shown = mem["ya_hecho"].get("precio_servicio_dado") or any(
            _SERVICE_PRICE_RE.search(b) for b in bubbles)
        if result.get("proposed_videocall_at") and (
                cal["etapa"] != "venta" or ai._missing_qualification_field(merged)
                or not merged.get("email") or not price_shown):
            result = ai._clear_videocall_proposal(result)
            notes.append("отменила запись на звонок (не все данные)")
    return result
