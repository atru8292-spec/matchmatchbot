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
# Запасной текст, если сценария №9 нет в БД (иначе лид без блока получил бы обычный ответ).
NOT_SINGLE_FALLBACK = ("Te entiendo, pero trabajamos solo con hombres solteros: las mujeres "
                       "buscan algo serio. Te deseo lo mejor 🤍")
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

# Вопрос без «?» в любом месте сообщения («Hola buenas tardes\ncuanto cuesta el boleto»):
# code-review 2026-10-21 — иначе первое сообщение с вопросом получало дословное приветствие.
_QUESTION_WORD_RE = re.compile(
    r"\b(?:cu[aá]nto|cu[aá]ntos|cu[aá]ntas|cu[aá]ndo|d[oó]nde|c[oó]mo|cu[aá]l|cu[aá]les|"
    r"qui[eé]n|por\s*qu[eé]|se\s+puede|aceptan|incluye|precio|costo|requisitos)\b|"
    r"(?:^|\n|[,.]\s*)(?:qu[eé]|hay|puedo|tienen|tienes|es)\s",
    re.IGNORECASE)
_EVENT_INTEREST_RE = re.compile(r"evento|boleto|halloween|slavic|novia\s+rusa", re.IGNORECASE)
_AGENCY_INTEREST_RE = re.compile(r"rus[ao]s?|eslav[ao]s?|ucranian|bielorrus|matchmak|agencia|"
                                 r"servicio|conocer\s+mujeres|busco\s+(?:pareja|esposa|novia)",
                                 re.IGNORECASE)
_PURCHASE_RE = re.compile(
    r"\bcompr(?:o|ar|arlo|arla|a)\b|\bpag(?:o|ar|arlo)\b|\blink\b|\benlace\b|"
    r"\breserv(?:o|ar)\b|\bapart(?:o|ar)\b|quiero\s+(?:ir|asistir|mi\s+boleto|el\s+boleto|comprar)|"
    r"me\s+gustar[ií]a\s+(?:ir|asistir)|quiero\s+(?:\d+|un|uno|dos)\s+boletos?|"
    r"d[oó]nde\s+(?:consigo|compro|pago|obtengo)|c[oó]mo\s+(?:consigo|obtengo)|"
    r"me\s+apunto|inscrib|registr|ya\s+me\s+decid", re.IGNORECASE)
# Анна предложила ссылку, лид согласился — это тоже покупка.
_OFFER_LINK_RE = re.compile(
    r"(?:te\s+paso|quieres|te\s+mando|te\s+comparto)\s+(?:el\s+)?(?:link|enlace)|"
    r"te\s+lo\s+(?:mando|paso|comparto)|te\s+gustar[ií]a\s+(?:el\s+|que\s+te\s+(?:mande|pase)\s+el\s+)"
    r"(?:link|enlace)|apartar\s+tu\s+lugar|reservar\s+tu\s+lugar", re.IGNORECASE)
_AFFIRM_RE = re.compile(r"^\W*(?:s[ií]|claro|ok|okay|dale|por\s+favor|me\s+interesa|sale|"
                        r"de\s+acuerdo)\b", re.IGNORECASE)
# Решения владелицы: скидки, наличные, перевод, возврат — молча Ане (не полагаемся на модель).
# «promo» — только вопрос о скидке («¿hay promo?», «¿tienen alguna promoción?», «¿y la promo?»),
# не «vi la promo en Instagram» / «vi su promoción» (решение владелицы 2026-10-21).
_SILENT_TOPIC_RE = re.compile(
    r"\bdescuento|\bcup[oó]n|c[oó]digo\s+(?:de\s+)?(?:descuento|promo\w*)|alg[uú]n\s+c[oó]digo|"
    r"c[oó]digo\s*\?|"
    r"\b(?:hay|tienen|tienes|manejan|aplica|aplican|existe|dan|alg[uú]n|alguna)\s+"
    r"(?:alguna?\s+|una\s+)?promo(?:ci[oó]n|ciones)?\b|\bpromo(?:ci[oó]n)?\s*\?|"
    r"quiero\s+(?:la|el|una|un)\s+(?:promo\w*|descuento)|"
    r"promo(?:ci[oó]n)?\s+(?:sigue|est[aá]|aplica|vigente)|"
    r"\brebaja|m[aá]s\s+barato|"
    r"precio\s+especial|\befectivo\b|\boxxo\b|\bwalmart\b|transferencia|dep[oó]sito|"
    r"depositar|reembolso|devoluci[oó]n|regr[eé]s\w*\s+(?:mi|el)\s+dinero|"
    r"cancelar\s+(?:mi\s+)?boleto", re.IGNORECASE)


def wants_to_buy(user_text: str, last_anna: str | None,
                 recent_lead: list[str] | None = None) -> bool:
    """Лид хочет купить: спросил сейчас, согласился на предложенную ссылку или спрашивал,
    как купить, в последних сообщениях (ссылку тогда придержали до имени и «холост ли»)."""
    if _PURCHASE_RE.search(user_text or ""):
        return True
    if last_anna and _OFFER_LINK_RE.search(last_anna) and _AFFIRM_RE.search(user_text or ""):
        return True
    return any(_PURCHASE_RE.search(t or "") for t in (recent_lead or [])[-3:])


def compra_pendiente(mem: dict, user_text: str) -> bool:
    """Лид спрашивал, как купить (сейчас или в последних 10 сообщениях), а ссылку ещё не
    получил: её придержали до имени, «холост ли» и одобренного фото."""
    if mem["ya_hecho"].get("enlace_enviado"):
        return False
    if wants_to_buy(user_text, mem.get("ultimo_mensaje_anna"), (mem.get("lead_texts") or [])[:-1]):
        return True
    # Глубже 3 сообщений — только вопрос или просьба о покупке, а не «me registré en Instagram».
    return any(_PURCHASE_RE.search(t or "") and (has_question(t) or _BUY_REQUEST_RE.search(t))
               for t in (mem.get("lead_texts") or [])[:-1][-10:])


_BUY_REQUEST_RE = re.compile(r"quiero|me\s+apunto|me\s+decid|p[aá]same|m[aá]ndame|ap[aá]rtame",
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
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\b(?:[\w-]+\.)+(?:com|mx|net|org|io)\b(?:/\S*)?",
                     re.IGNORECASE)
_URL_TRAIL = ".,;:!?)]}\"'»*"
_5500_RE = re.compile(r"(?<!\d)5[\s.,]?500(?!\d)|cinco\s+mil\s+quinientos|5[.,]5\s*(?:mil|k)\b",
                      re.IGNORECASE)
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]\uFE0F?")
_QUESTION_SENT_RE = re.compile(r"¿[^?]*\?|[^.!¿?]*\?")
_INTRO_RE = re.compile(r"\bsoy\s+anna\b[^.!?]*[.!?]?\s*", re.IGNORECASE)
_HOLA_START_RE = re.compile(r"^\s*¡?\s*hola\b[^.!?,]*[.!?,]?\s*", re.IGNORECASE)
_SERVICE_PRICE_RE = re.compile(r"10[.,]?000\s*(?:usd|d[oó]lares)?|\$\s*10", re.IGNORECASE)


def _norm_num(s: str) -> str:
    if ":" in s:
        return s
    s = re.sub(r"[.,]00$", "", s)          # 6,000.00 → 6,000
    return s.replace(",", "").replace(".", "")


def _norm_url(u: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", u.rstrip(_URL_TRAIL).lower())


def numbers_in(text: str) -> set[str]:
    text = _URL_RE.sub(" ", _TOKEN_RE.sub(" ", text or ""))   # цифры внутри ссылок не считаем
    return {_norm_num(n) for n in _NUM_RE.findall(text)}


def render_tokens(text: str, settings: dict) -> str:
    """Подставить токены ивента для сверки (как сделает sender)."""
    def val(m):
        k = m.group(0)[1:-1]
        v = (settings.get(k) or "").strip()
        return sender._fmt_date_es(v) if k == "event_date" and v else v
    return _TOKEN_RE.sub(val, text or "")


def allowed_numbers(fichas: list[dict], settings: dict, lead: dict, lead_texts: list[str]) -> set[str]:
    """Числа из фичей, карточки ивента, текущего сообщения лида и недавних ответов Анны
    по текущему ивенту, плюс возраст из профиля. Сводка памяти источником не является."""
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
    parts = _sentences(text)
    if not any(pred(p) for p in parts):
        return text
    return " ".join(p for p in parts if not pred(p)).strip()


MAX_EMOJI_TOTAL = 2       # на весь ответ (решение владелицы 2026-10-21)
MAX_EMOJI_PER_BUBBLE = 1


def keep_emojis(bubbles: list[str], total: int = MAX_EMOJI_TOTAL,
                per_bubble: int = MAX_EMOJI_PER_BUBBLE) -> list[str]:
    """Не больше total эмодзи на ответ и per_bubble в одном пузыре; лишние вырезаем."""
    used = 0
    out = []
    for b in bubbles:
        in_bubble = 0

        def repl(m):
            nonlocal used, in_bubble
            if used >= total or in_bubble >= per_bubble:
                return ""
            used += 1
            in_bubble += 1
            return m.group(0)
        out.append(re.sub(r"[ \t]{2,}", " ", _EMOJI_RE.sub(repl, b)).strip())
    return out


def emoji_count(texts: list[str]) -> int:
    return sum(len(_EMOJI_RE.findall(t or "")) for t in texts)


def drop_repeated_reaction(bubbles: list[str], recent: list[str]) -> list[str]:
    """Одинаковая реакция в начале («¡Qué bien!», «Jaja», «Claro,») — не чаще раза за
    последние 3 ответа бота: при повторе её срезаем, ответ остаётся."""
    if not bubbles:
        return bubbles
    r = conversation_rules.reaction_start(bubbles[0])
    if not r or r not in set(recent or []):
        return bubbles
    rest = conversation_rules.REACTION_START_RE.sub("", bubbles[0], count=1)
    return ([_capitalize(rest)] if rest.strip() else []) + bubbles[1:]


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
            rest = b[m.end():]
            # «¡Perfecto, Luis! Gracias…» — запятая, потом имя: срезаем и обращение (смоук 2026-10-22)
            rest = re.sub(r"^\s*,", "", rest, count=1)
            tail = re.match(r"^[^,.!:;?¿]{0,15}[,.!:;]", rest)   # «Claro que sí, …»
            b = re.sub(r"^[\s,.!:;]+", "", rest[tail.end():] if tail else rest)
            b = _capitalize(b)
        b = _drop_sentences(b, lambda s: bool(conversation_rules.BANNED_PHRASE_RE.search(s)
                                              or conversation_rules.SLANG_RE.search(s)))
        out.append(b)
    return out


def _capitalize(b: str) -> str:
    """Заглавная первая буква, кроме ссылок и токенов (Https:// ломает сверку ссылки)."""
    if not b or re.match(r"^(?:https?://|www\.|\[)", b, re.IGNORECASE):
        return b
    return b[:1].upper() + b[1:]


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
        if not (link and _norm_url(url) == _norm_url(link)):
            return [], f"ссылка не из базы: {url}", ["url"]
    sources = [user_text] + list(mem.get("anna_reciente") or [])
    extra = set(numbers_in(text)) - allowed_numbers(fichas, settings, lead, sources)
    if extra:
        return [], f"числа без источника: {sorted(extra)}", ["numbers"]

    # Ссылка на билет: только на вопрос о покупке, только в этапе venta, без повтора.
    def has_link(b):
        return "[event_link]" in b or bool(link and _norm_url(link) in b.lower())
    if any(has_link(b) for b in bubbles):
        reason = None
        if not (mem.get("compra_pendiente") or wants_to_buy(
                user_text, mem.get("ultimo_mensaje_anna"), (mem.get("lead_texts") or [])[:-1])):
            reason = "лид не спрашивал, как купить"
        elif cal["etapa"] == "calificacion":
            reason = "этап calificacion"
        elif ya.get("enlace_enviado") and not _LINK_REQUEST_RE.search(user_text or ""):
            reason = "ссылка уже отправлена"
        if reason:
            # убираем только предложение со ссылкой, остальной текст пузыря сохраняем
            bubbles = [_drop_sentences(b, has_link) if has_link(b) else b for b in bubbles]
            bubbles = [b for b in bubbles if b and not has_link(b)]
            notes.append(f"убрала ссылку ({reason})")
    # Цена услуги — в этапе venta или на прямой вопрос о цене (вопрос лида не ждёт слотов).
    if cal["etapa"] == "calificacion" and cal["ruta"] == "servicio" \
            and not ai._is_price_question(user_text):
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
    bubbles = drop_repeated_reaction(bubbles, mem.get("inicios_recientes") or [])
    bubbles = keep_last_question(keep_emojis(bubbles))
    bubbles = [_capitalize(b) for b in (b.strip() for b in bubbles) if b]
    if len(bubbles) > MAX_BUBBLES:
        bubbles = bubbles[:MAX_BUBBLES]
    if not bubbles:
        return [], "после проверок ответ пустой", notes
    return bubbles, None, notes


VERIFY_SYSTEM = (
    "Eres un verificador. Recibes la RESPUESTA de una asesora a un cliente, las FUENTES "
    "permitidas y la CONVERSACION. Busca en la RESPUESTA las afirmaciones de hecho sobre el "
    "evento, la agencia, el servicio, precios, fechas, lugares, cantidades, condiciones o "
    "promesas al cliente. Para cada una, copia la frase de FUENTES o de CONVERSACION que la "
    "respalda: vale si lo dice con otras palabras o es una consecuencia directa. Si ninguna "
    "la respalda, respaldo = null. NO son afirmaciones y no las listes: preguntas, pedidos al "
    "cliente (nombre, edad, profesión, foto), agradecimientos, saludos, reacciones, empatía, "
    "bromas o comentarios personales de la asesora sin datos, y lo que dijo el cliente. Los "
    "tokens entre corchetes como [event_price_nonmember] son valores de las fuentes. "
    'Responde SOLO JSON: {"afirmaciones": [{"texto": "...", "respaldo": "cita" o null}]}.'
)

# Пустая отписка вместо ответа («No puedo decirte…») — данных нет: молча Ане, как falta_dato.
_NO_DATA_RE = re.compile(
    r"no\s+(?:puedo\s+decirte|te\s+puedo\s+decir|tengo\s+(?:ese\s+dato|esa\s+informaci[oó]n|"
    r"el\s+dato)|s[eé]\s+(?:cu[aá]nt|exactamente))", re.IGNORECASE)
_THANKS_RE = re.compile(r"^\W*(?:gracias|muchas\s+gracias|mil\s+gracias|hola|jaja)", re.IGNORECASE)


def _words(text: str) -> set[str]:
    t = (text or "").lower().translate(str.maketrans("áéíóúü", "aeiouu"))
    # слова от 4 букв и любые числа («30 mujeres» без источника не пройдёт по словам)
    return {w for w in re.findall(r"[a-zñ0-9]+", t) if len(w) >= 4 or w.isdigit()}


def _supported_by_text(claim: str, sources: list[str]) -> bool:
    """Почти все значимые слова утверждения есть в одном-двух источниках — это не выдумка
    (gpt-4.1-mini в смоуке помечал дословные фразы из фич, 2026-10-22). Модель часто склеивает
    две фичи в одну фразу («más de 3,000 mujeres eslavas que buscan pareja»)."""
    words = _words(claim)
    if not words:
        return True
    hits = sorted((words & _words(src) for src in sources), key=len, reverse=True)
    best = len(hits[0]) if hits else 0
    for i, a in enumerate(hits[:5]):            # лучшая пара источников
        for b in hits[i + 1:6]:
            best = max(best, len(a | b))
    return best >= 0.8 * len(words)


async def verify_claims(bubbles: list[str], fichas: list[dict], card: dict | None,
                        lead_texts: list[str], cost: Cost,
                        anna_reciente: list[str] | None = None,
                        recs: list[dict] | None = None,
                        historial: list[dict] | None = None) -> list[str]:
    """Утверждения ответа без источника. Источник — подтверждённые записи базы (все usable,
    а не только найденные поиском), карточка ивента, прошлые ответы Анны и переписка."""
    contenidos = [f.get("contenido_es") for f in (recs or fichas) if f.get("contenido_es")]
    conversacion = [f"{m.get('autor')}: {m.get('texto')}" for m in (historial or [])[-20:]
                    if m.get("texto")]
    payload = {
        "RESPUESTA": bubbles,
        "FUENTES": {"evento_actual": card, "fichas": contenidos,
                    "lo_que_ya_dijo_anna": list(anna_reciente or [])},
        "CONVERSACION": conversacion,
        "LO_QUE_DIJO_EL_CLIENTE": lead_texts[-15:],
    }
    out = await _chat(CHECK_MODEL, VERIFY_SYSTEM, payload, cost, max_tokens=500, temperature=0)
    items = out.get("afirmaciones")
    if items is None:                       # старый формат ответа
        items = [{"texto": c, "respaldo": None} for c in out.get("sin_fuente") or []]
    sources = contenidos + list(anna_reciente or []) + conversacion + list(lead_texts)
    if card:
        sources.append(json.dumps(card, ensure_ascii=False))
    claims = []
    for it in items:
        texto = str((it or {}).get("texto") if isinstance(it, dict) else it).strip()
        if not texto or (isinstance(it, dict) and it.get("respaldo")):
            continue
        if texto.rstrip().endswith("?") or _THANKS_RE.match(texto):
            continue                        # вопрос или благодарность — не факт
        if _supported_by_text(texto, sources):
            continue
        claims.append(texto)
    return claims


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


def _caption_within_budget(caption: str, bubbles: list[str]) -> str:
    """Подпись, которую добавляет код, тоже в лимите эмодзи на ответ."""
    if emoji_count(bubbles) + emoji_count([caption]) <= MAX_EMOJI_TOTAL:
        return caption
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", caption)).strip()


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
    cal["compra_pendiente"] = bool(mem.get("compra_pendiente"))
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
    mem["compra_pendiente"] = compra_pendiente(mem, user_text)
    if mem["compra_pendiente"]:
        # хочет купить: возраст и профессию не спрашиваем, следующий вопрос — фото
        mem["calificacion"] = memory.calificacion(lead, mem["ya_hecho"], user_text,
                                                  mem["ultimo_mensaje_anna"], compra=True)
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
    # Склейка с последней репликой Анны — только для поиска по базе знаний. Дословные
    # блок-сценарии ищем по одному тексту лида, как legacy (иначе «45» после «¿Qué edad
    # tienes?» могло набрать порог блока №7/№8).
    texts = [user_text or ""] + ([query] if query != user_text else [])
    vecs = await _embed_batch(texts, cost)
    vec_user, vec = vecs[0], vecs[-1]

    # 1. Дословные сценарии (бот?, блоки) — как в legacy, без модели.
    try:
        top = await _verbatim_scenario(vec_user)
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

    # 2.5. Скидки, наличные, перевод, возврат — решение владелицы: молча Ане.
    if _SILENT_TOPIC_RE.search(user_text or ""):
        return _silent("скидка, способ оплаты или возврат (решение владелицы)",
                       rag_meta={**meta, "fuentes": ["policy_silent_topic"]})

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
        estado["regresos"] = 0
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
    if isinstance(age, str) and age.strip().isdigit():
        age = int(age.strip())
    if isinstance(age, int) and not (MIN_AGE <= age <= MAX_AGE):
        reply = await _scenario_reply(7 if age < MIN_AGE else 8) or _result(
            list(ai._AGE_OUT_OF_RANGE_BUBBLES), "block")
        reply.update(extracted=out["extracted"], rag_estado=estado, rag_meta=meta)
        return reply
    if out["extracted"].get("is_single") is False:
        reply = await _scenario_reply(9) or _result([NOT_SINGLE_FALLBACK], "block")
        reply.update(extracted=out["extracted"], rag_estado=estado, rag_meta=meta)
        return reply

    bubbles, reason, notes = check_reply(out["bubbles"], mem=mem, fichas=fichas, lead=merged,
                                         user_text=user_text)
    meta["notas"] = notes
    if reason:
        return _silent(reason, **extra)
    if any(_NO_DATA_RE.search(x) for x in bubbles):
        return _silent("нет данных в базе (модель ответила отпиской)", **extra)

    try:
        claims = await verify_claims(bubbles, fichas, card, mem["lead_texts"], cost,
                                     mem.get("anna_reciente"), recs=recs,
                                     historial=mem.get("historial"))
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
            result["video_caption"] = _caption_within_budget(VIDEO_CAPTION, bubbles)
    if out["send_event_photo"] and not mem["ya_hecho"].get("foto_enviada"):
        result["send_event_photo"] = True
        await ai._maybe_announce_event_photo(result, None, lead)
        if result.get("photo_caption"):
            result["photo_caption"] = _caption_within_budget(PHOTO_CAPTION, bubbles)
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
