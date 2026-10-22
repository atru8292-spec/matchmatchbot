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
import filters
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
PHOTO_CAPTION = "Así fue el evento pasado."

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
    """Числа из подтверждённых фичей и карточки ивента. Возраст лида числом-фактом не
    считается («30 mujeres» у лида 30 лет не пройдёт); lead_texts в режиме rag пустой."""
    nums: set[str] = set()
    for f in fichas:
        nums |= numbers_in(render_tokens(f.get("contenido_es") or "", settings))
    for k in ("event_date", "event_start", "event_price_nonmember", "event_price_next"):
        v = settings.get(k) or ""
        nums |= numbers_in(sender._fmt_date_es(v) if k == "event_date" and v else v)
    for t in lead_texts:
        nums |= numbers_in(t)
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


NEXT_PRICE_SENTENCE = "Más adelante el precio sube a [event_price_next] MXN."
CURRENT_PRICE_SENTENCE = "Ahora cuesta [event_price_nonmember] MXN."
_GRACIAS_START_RE = re.compile(r"^\W*(?:muchas\s+|mil\s+)?gracias\b[^.!?]*[.!?]*\s*", re.IGNORECASE)
_COMPLIMENT_RE = re.compile(
    r"eres\s+(?:muy\s+)?(?:linda|bonita|hermosa|guapa|amable|lind[ií]sima)|"
    r"qu[eé]\s+(?:linda|bonita|amable|hermosa)|felicidades|buen\s+trabajo|me\s+encanta\s+(?:tu|lo\s+que)",
    re.IGNORECASE)


_LEAD_THANKS_RE = re.compile(r"\bgracias\b|\bte\s+agradezco\b|\bmuy\s+amable\b", re.IGNORECASE)


def lead_dio_algo(user_text: str, extracted: dict, lead: dict | None = None) -> bool:
    """Лид что-то дал: новые данные о себе, фото, благодарность или комплимент — тогда
    «Gracias» уместно. Данные, которые уже были в профиле, новыми не считаются."""
    lead = lead or {}
    datos = {k: v for k, v in (extracted or {}).items()
             if k != "interest" and v not in (None, "") and lead.get(k) != v}
    return bool(datos) or "[фото одобрено" in (user_text or "") or \
        bool(_COMPLIMENT_RE.search(user_text or "")) or bool(_LEAD_THANKS_RE.search(user_text or ""))


def drop_repeated_slot_question(bubbles: list[str], last_reply: str | None,
                                answered: set[str] | frozenset = frozenset()) -> list[str]:
    """Вопрос по слоту, заданный в прошлом ответе и оставленный без ответа, не повторяем
    в следующем ответе подряд (решение владелицы 2026-10-22). Лид ответил на часть
    («¿Cómo te llamas y estás soltero?» → «Luis») — оставшееся переспросить можно."""
    asked = [slot for slot in memory.SLOT_QUESTION_RE if memory.pregunta_slot(slot, last_reply)]
    if not asked or set(asked) & set(answered):
        return bubbles

    def repeated(sent):
        return any(memory.pregunta_slot(slot, sent) for slot in asked)
    out = [_drop_sentences(b, repeated) for b in bubbles]
    return out if any(b.strip() for b in out) else bubbles   # пустым ответ не делаем


def drop_unearned_thanks(bubbles: list[str]) -> list[str]:
    """«Gracias…» в начале ответа, когда лид ничего не дал (просто вернулся или спросил)."""
    if not bubbles:
        return bubbles
    first = _GRACIAS_START_RE.sub("", bubbles[0], count=1)
    return [_capitalize(first.strip())] + bubbles[1:] if first != bubbles[0] else bubbles


def whole_price(bubbles: list[str], settings: dict) -> list[str]:
    """Цена ивента целой мыслью: «Ahora cuesta X MXN. Más adelante el precio sube a Y MXN.»
    Обрывки вроде «La siguiente es de 7000 MXN» заменяем; если сказана только текущая цена,
    а следующая ступень задана, добавляем фразу про неё."""
    now = re.sub(r"\D", "", settings.get("event_price_nonmember") or "")
    nxt = re.sub(r"\D", "", settings.get("event_price_next") or "")

    def says(sent, token, value):
        return token in sent or bool(value and value in {re.sub(r"\D", "", n) for n in numbers_in(sent)})
    out, has_next = [], False
    for b in bubbles:
        for sent in _sentences(b):          # заменяем фразу на месте — переносы строк целы
            if says(sent, "[event_price_next]", nxt):
                new = CURRENT_PRICE_SENTENCE + " " if says(sent, "[event_price_nonmember]", now) else ""
                new += "" if has_next else NEXT_PRICE_SENTENCE
                # «…sube a 7,000 MXN y se paga en la página.» — хвост после цены сохраняем
                tail = re.search(r".*(?:\[event_price_next\]|\d[\d,.]*)\s*MXN\s*,?\s*(?:y\s+)?(.+?)[.!]?$",
                                 sent, re.IGNORECASE)
                if tail and _words(tail.group(1)):
                    new += " " + _capitalize(tail.group(1).strip()) + "."
                b = b.replace(sent, new.strip(), 1)
                has_next = True
        out.append(re.sub(r"[ \t]{2,}", " ", b).strip())
    if nxt and not has_next:
        for i, b in enumerate(out):
            for sent in _sentences(b):
                if says(sent, "[event_price_nonmember]", now) and not _SERVICE_PRICE_RE.search(sent):
                    out[i] = b.replace(sent, f"{sent} {NEXT_PRICE_SENTENCE}", 1)
                    return out
    return out


BUY_ONLINE_SENTENCE = "El boleto se compra en línea, en la página del evento."
PHOTO_REQUEST = "¿Me mandas una foto tuya?"
# «para completar tu registro», «para continuar» — объяснение, зачем фото (владелица: без него)
_PHOTO_WHY_RE = re.compile(r"\s*,?\s*(?:para|antes\s+de)\s+(?:completar|continuar|seguir|terminar|"
                           r"avanzar|registrarte|darte|enviarte|pasarte|confirmar)\b[^.?!¿]*",
                           re.IGNORECASE)
_ORPHAN_LA_RE = re.compile(r"¿?\s*me\s+la\s+(?:puedes\s+)?(?:mandar|enviar|mandas|env[ií]as|compartes)"
                           r"[^?]*\??", re.IGNORECASE)


def purchase_before_photo(bubbles: list[str], mem: dict, user_text: str) -> list[str]:
    """Лид спросил, как купить, а ссылку давать рано (этап calificacion): ответ по сути
    («se compra en línea…») и просьба фото, без объяснений вслух, зачем (решение 2026-10-22).
    Плюс осиротевшее «¿Me la puedes mandar?» после срезанной фразы про фото."""
    cal = mem["calificacion"]
    out = []
    for b in bubbles:
        if memory.pregunta_slot("photo", b) or re.search(r"\bfoto", b, re.IGNORECASE):
            b = _PHOTO_WHY_RE.sub("", b)
            b = re.sub(r"^\s*(?:s[oó]lo\s+)?(?:me\s+)?falta\s+una\s+foto\s+tuya\s*[.,]?\s*", "", b,
                       flags=re.IGNORECASE)
        out.append(b.strip())
    text = " ".join(out)
    if not re.search(r"\bfoto", text, re.IGNORECASE):
        out = [_ORPHAN_LA_RE.sub(PHOTO_REQUEST, b) for b in out]
    out = [b for b in out if b and _words(b)]
    buying = wants_to_buy(user_text, mem.get("ultimo_mensaje_anna"))
    if buying and cal["etapa"] == "calificacion" and cal.get("siguiente") == "photo":
        text = " ".join(out)
        if not re.search(r"en\s+l[ií]nea|p[aá]gina", text, re.IGNORECASE):
            out.insert(0, BUY_ONLINE_SENTENCE)
        if not memory.pregunta_slot("photo", text) and \
                not memory.pregunta_slot("photo", mem.get("ultimo_mensaje_anna")):
            out.append(PHOTO_REQUEST)
    return out or bubbles


# Темы, о которых лид спросил (факт по теме в ответе уместен). Ключи — как memory.HECHOS.
_ASK_TEMA = {
    "halloween": re.compile(r"disfra|halloween|m[aá]scara|tem[aá]tic|vest|ropa|dress|c[oó]digo",
                            re.IGNORECASE),
    "evento_30": re.compile(r"cu[aá]ntos\s+eventos|experiencia|primer|segur|confiable|\breal\b|"
                            r"estafa|cu[aá]nto\s+tiempo\s+llevan", re.IGNORECASE),
    "parejas": re.compile(r"funciona|resultad|pareja|vale\s+la\s+pena|[eé]xito|\bsirve", re.IGNORECASE),
    "incluye": re.compile(r"inclu|botana|bebida|comida|cena|trago|alcohol|barra", re.IGNORECASE),
    "ambiente": re.compile(r"gente|ambiente|amig|negocio|networking|qui[eé]n(?:es)?\s+va|tipo\s+de",
                           re.IGNORECASE),
    "fecha": re.compile(r"cu[aá]ndo|fecha|\bd[ií]a\b", re.IGNORECASE),
    "hora": re.compile(r"\bhora|cu[aá]ndo|empieza|inicia|termina|acaba", re.IGNORECASE),
    "lugar": re.compile(r"d[oó]nde|lugar|direcci|ubicaci|\bzona|\bsede|c[oó]mo\s+llego", re.IGNORECASE),
    "precio": re.compile(r"cu[aá]nto|precio|cuesta|costo|cobran|\bcar[oa]\b|\bpag|compr|boleto",
                         re.IGNORECASE),
}
# Общий вопрос про ивент или услугу («¿de qué se trata?») — можно рассказать, но без повтора.
_BROAD_ASK_RE = re.compile(r"de\s+qu[eé]\s+(?:se\s+trata|va|es)|c[oó]mo\s+(?:es|funciona|ser[aá])|"
                           r"cu[eé]ntame|m[aá]s\s+(?:info|detalles)|\binfo\b|informaci[oó]n|detalles|"
                           r"qu[eé]\s+es\s+(?:el|eso|esto)", re.IGNORECASE)


def temas_preguntados(user_text: str) -> set[str]:
    return {tema for tema, rx in _ASK_TEMA.items() if rx.search(user_text or "")}


def drop_unasked_facts(bubbles: list[str], user_text: str, mem: dict) -> list[str]:
    """Факт про ивент — только на вопрос лида (решение владелицы 2026-10-22).
    Убираем предложения с фактом, о котором лид не спрашивал: «Por cierto, el evento… es el 18
    de noviembre» после разговора о футболе, «Este es nuestro evento número 30.» после имени.
    На общий вопрос («¿de qué se trata?», возражение по цене) факт можно, но не уже сказанный
    (ya_hecho.hechos_dichos). Не трогаем: ход с одобренным фото (рассказ, ссылка, видео),
    ответ со ссылкой (покупка), вопросы и пересказ «la última vez platicamos…» после долгой паузы."""
    if "[фото одобрено" in (user_text or ""):
        return bubbles
    settings = mem["settings"]
    link = (settings.get("event_link") or "").strip()
    if any("[event_link]" in b or (link and link in b) for b in bubbles):
        return bubbles
    asked = temas_preguntados(user_text)
    broad = bool(_BROAD_ASK_RE.search(user_text or "") or memory._PRICE_OBJECTION_RE.search(user_text or ""))
    said = set(mem["ya_hecho"].get("hechos_dichos") or [])

    def unasked(sent):
        if "?" in sent or (mem.get("regreso") == "largo" and _RECAP_RE.match(sent)):
            return False
        temas = memory.hechos_en(sent, settings)
        if not temas or temas & asked:
            return False
        return not broad or bool(temas & said)
    out = [_drop_sentences(b, unasked) for b in bubbles]
    return [b for b in out if b.strip() and _words(b)]


_SLOT_QUESTIONS = {
    ("name", "is_single"): "¿Cómo te llamas y estás soltero?", ("name",): "¿Cómo te llamas?",
    ("is_single",): "¿Estás soltero?",
    ("age", "profession"): "¿Cuántos años tienes y a qué te dedicas?", ("age",): "¿Cuántos años tienes?",
    ("profession",): "¿A qué te dedicas?", ("photo",): PHOTO_REQUEST,
}


def slot_question(cal: dict) -> str | None:
    """Готовый вопрос по следующему слоту (имя и «холост ли» вместе, возраст и профессия
    вместе), если его сейчас можно задать. None — спрашивать нечего или не время."""
    nxt = cal.get("siguiente")
    if not nxt or not cal.get("puede_preguntar"):
        return None
    pair = {"name": ("name", "is_single"), "is_single": ("name", "is_single"),
            "age": ("age", "profession"), "profession": ("age", "profession")}.get(nxt, (nxt,))
    tenemos = cal.get("tenemos") or {}
    missing = tuple(s for s in pair if not (tenemos.get(s) is True if s == "is_single" else tenemos.get(s)))
    return _SLOT_QUESTIONS.get(missing or (nxt,))


_SLOT_PAIR = {"name": ("name", "is_single"), "is_single": ("name", "is_single"),
              "age": ("age", "profession"), "profession": ("age", "profession")}


def slots_permitidos(cal: dict) -> frozenset:
    """О каких слотах сейчас можно спросить: следующий (с парой), если вопрос сейчас уместен."""
    nxt = cal.get("siguiente")
    if not nxt or not cal.get("puede_preguntar"):
        return frozenset()
    return frozenset(_SLOT_PAIR.get(nxt, (nxt,)))


def drop_out_of_order_slot_questions(bubbles: list[str], permitidos: frozenset) -> list[str]:
    """Вопрос по слоту не по порядку («¿te gustaría mandarme una foto tuya?», когда очередь
    возраста, а вопрос о возрасте только что остался без ответа) — убираем. Порядок: имя
    и «холост ли», возраст и профессия, фото (прогон 4, Д3)."""
    def wrong(sent):
        asked = {slot for slot in memory.SLOT_QUESTION_RE if memory.pregunta_slot(slot, sent)}
        return bool(asked) and not asked <= permitidos
    out = [_drop_sentences(b, wrong) for b in bubbles]
    out = [b for b in out if b.strip() and _words(b)]
    return out or bubbles


def paso_extra_ok(paso: dict, cal: dict) -> str | None:
    """Дополнительный шаг модели — только вопрос по слоту, который сейчас можно задать.
    Факт или другой вопрос «вдогонку» не берём."""
    texto = (paso or {}).get("texto") or ""
    if (paso or {}).get("tipo") != "pregunta" or "?" not in texto or not cal.get("puede_preguntar"):
        return None
    return texto if any(memory.pregunta_slot(s, texto) for s in memory.SLOT_QUESTION_RE) else None


def add_slot_question(bubbles: list[str], question: str | None, user_text: str,
                      answered: set[str] | frozenset) -> list[str]:
    """Лид ответил на вопрос по слоту (или просто написал, ничего не спросив) — следующий
    ход сразу следующий вопрос по слоту: «me llamo Javier, sí soltero» → «¡Gracias, Javier! 😊
    ¿Cuántos años tienes y a qué te dedicas?». Если лид сам спросил — вопрос не обязателен."""
    if not question or any("?" in b for b in bubbles):
        return bubbles
    if "?" in (user_text or "") and not answered:
        return bubbles
    return bubbles + [question]


# «¿Te gustaría saber algo más sobre el evento?» — предложение рассказать, о чём лид не спрашивал.
_OFFER_MORE_RE = re.compile(r"¿\s*(?:te\s+gustar[ií]a|quieres|te\s+interesa)\s+(?:saber|conocer)\s+"
                            r"(?:algo\s+)?m[aá]s\b", re.IGNORECASE)
_DANGLING_RE = re.compile(r"[,:;—–-]\s*$")


def drop_offer_more(bubbles: list[str]) -> list[str]:
    out = [_drop_sentences(b, lambda s: bool(_OFFER_MORE_RE.search(s))) for b in bubbles]
    return [b for b in out if b.strip()]


def drop_dangling(bubbles: list[str]) -> list[str]:
    """Пузырь оборван на запятой («…emocionante. Volviendo al evento,»: вопрос модель унесла
    в paso_extra) — хвост после последнего законченного предложения убираем."""
    out = []
    for b in bubbles:
        if _DANGLING_RE.search(b):
            ends = [m.end() for m in re.finditer(r"[.!?]", b)]
            b = b[:ends[-1]] if ends else ""
        if b.strip():
            out.append(b.strip())
    return out


_DATE_YEAR_RE = re.compile(rf"(\b\d{{1,2}}\s+de\s+(?:{memory._MONTHS}))\s+(?:de|del)\s+\d{{4}}\b",
                           re.IGNORECASE)


def check_reply(bubbles: list[str], *, mem: dict, fichas: list[dict], lead: dict,
                user_text: str, dio_algo: bool = True,
                answered: set[str] | frozenset = frozenset(),
                pregunta_siguiente: str | None = None,
                permitidos: frozenset | None = None) -> tuple[list[str], str | None, list[str]]:
    """Проверки кодом. Возвращает (пузыри, причина silent или None, список правок)."""
    notes: list[str] = []
    settings, ya, cal = mem["settings"], mem["ya_hecho"], mem["calificacion"]
    # Дата лиду без года: «el 18 de noviembre», не «el 18 de noviembre de 2026».
    new = [_DATE_YEAR_RE.sub(r"\1", b) for b in bubbles]
    if new != bubbles:
        notes.append("убрала год из даты")
        bubbles = new
    text = "\n\n".join(bubbles)
    if _5500_RE.search(_TOKEN_RE.sub(" ", text)):
        return [], "в ответе 5,500", ["5500"]
    link = (settings.get("event_link") or "").strip()
    for url in _URL_RE.findall(text):
        if not (link and _norm_url(url) == _norm_url(link)):
            return [], f"ссылка не из базы: {url}", ["url"]
    # Числа — только из подтверждённой базы, карточки ивента и профиля лида (возраст).
    # Прошлые ответы бота и сообщения лида источником не считаются (решение 2026-10-22).
    extra = set(numbers_in(text)) - allowed_numbers(fichas, settings, lead, [])
    # Возраст лида можно повторить как «45 años» — но не как «30 mujeres» (ревью 2026-10-22).
    own = {str(lead.get(k)) for k in ("age",) if lead.get(k)} | numbers_in(user_text)
    extra = {n for n in extra if not (n in own and re.search(rf"(?<!\d){re.escape(n)}\s+a[ñn]os", text))}
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
    # Факт без вопроса лида убираем; на его место — следующий вопрос по слоту, если он нужен.
    # Если от ответа ничего не остаётся — оставляем как есть: ложное молчание хуже.
    trimmed = drop_unasked_facts(bubbles, user_text, mem)
    new = add_slot_question(trimmed, pregunta_siguiente, user_text, answered)
    if not new:
        notes.append("факт без вопроса лида оставлен: без него ответ пустой")
    else:
        if trimmed != bubbles:
            notes.append("убрала факт без вопроса лида")
        if new != trimmed:
            notes.append("добавила вопрос по следующему слоту")
        bubbles = new
    if permitidos is not None:
        new = drop_out_of_order_slot_questions(bubbles, permitidos)
        if new != bubbles:
            notes.append("убрала вопрос по слоту не по порядку")
            bubbles = new
    new = drop_repeated_slot_question(bubbles, mem.get("ultimo_mensaje_anna"), answered)
    if new != bubbles:
        notes.append("убрала повтор вопроса по слоту")
        bubbles = new
    if not dio_algo:
        new = drop_unearned_thanks(bubbles)
        if new != bubbles:
            notes.append("убрала «Gracias» без повода")
            bubbles = new
    new = purchase_before_photo(bubbles, mem, user_text)
    if new != bubbles:
        notes.append("покупка до фото: ответ по сути и просьба фото без объяснений")
        bubbles = new
    new = whole_price(bubbles, settings)
    if new != bubbles:
        notes.append("цена целой мыслью")
        bubbles = new
    new = drop_dangling(drop_offer_more(bubbles))
    if new != bubbles and new:
        notes.append("убрала оборванную фразу или предложение рассказать ещё")
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
    "Eres un verificador. Recibes la RESPUESTA de una asesora a un cliente y las FUENTES "
    "permitidas (fichas confirmadas y el evento actual). Busca en la RESPUESTA las "
    "afirmaciones de hecho sobre el evento, la agencia, el servicio, precios, pagos, fechas, "
    "horarios, lugares, cantidades, condiciones o promesas al cliente. Para cada una, copia "
    "la frase de FUENTES que la respalda: vale si lo dice con otras palabras o es una "
    "consecuencia directa. Si ninguna la respalda, respaldo = null. Lo que dijo el cliente o "
    "lo que se dijo antes en la conversación NO respalda nada sobre el evento o la agencia. "
    "NO son afirmaciones y no las listes: preguntas, pedidos al cliente (nombre, edad, "
    "profesión, foto), agradecimientos, saludos, reacciones, empatía, bromas o comentarios "
    "personales de la asesora sin datos, y lo que el cliente contó de sí mismo. Los tokens "
    "entre corchetes como [event_price_nonmember] son valores de las fuentes. "
    'Responde SOLO JSON: {"afirmaciones": [{"texto": "...", "respaldo": "cita" o null}]}.'
)

# Пустая отписка вместо ответа («No puedo decirte…») — данных нет: молча Ане, как falta_dato.
_NO_DATA_RE = re.compile(
    r"no\s+(?:puedo\s+(?:decirte|darte)|te\s+puedo\s+(?:decir|dar)|tengo\s+(?:ese\s+dato|esa\s+informaci[oó]n|"
    r"el\s+dato)|s[eé]\s+(?:cu[aá]nt|exactamente))", re.IGNORECASE)
_THANKS_RE = re.compile(r"^\W*(?:gracias|muchas\s+gracias|mil\s+gracias|hola|jaja)", re.IGNORECASE)
# Отрицание меняет смысл: «No incluye botanas» не подтверждается фразой «Incluye … botanas».
# «solo/únicamente» не считаем отрицанием: «Solo trabajamos con hombres solteros» ≈ «Aceptamos
# hombres solteros» (повторное ревью 2026-10-22). Отрицание сравниваем по частям фразы.
_NEG_RE = re.compile(r"\b(?:no|ni|nunca|sin|ning[uú]n[oa]?|tampoco|jam[aá]s|nada|nadie)\b",
                     re.IGNORECASE)
# Темы, где старый бот и модель выдумывали (оплата, что входит, скидки, время, люди):
# каждое такое предложение ответа код сверяет сам, даже если модель-сверщик его не назвала
# (code-review 2026-10-22: сверщик fail-open — пустой список пропускал всё).
_HARD_FACT_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"\befectivo\b", r"\boxxo\b", r"\bwalmart\b", r"transferencia", r"dep[oó]sito", r"\btarjeta",
    r"meses\s+sin\s+intereses", r"\bpag(?:o|ar|as|a)\b", r"\bgratis\b|\bgratuit", r"descuento",
    r"promoci[oó]n", r"reembolso|devoluci[oó]n", r"\bno\s+(?:se\s+)?inclu",
    r"\bcena\b", r"barra\s+libre", r"alcohol", r"estacionamiento|\bvalet\b",
    r"\d[\d,.]*\s+(?:mujeres|hombres|personas|chicas|invitad[oa]s|asistentes)",
    r"\btermina\b|\bacaba\b|hasta\s+las\b|medianoche", r"garant[ií]a", r"\bcupo\b|\blugares\b")]


# Фраза касается агентства, ивента или услуги (или в ней число/токен). Личная реплика вроде
# «no le voy a ningún equipo» — не утверждение о нас, даже если сверщик её занёс (прогон 5, Д3).
_DOMAIN_RE = re.compile(
    r"\d|\[[a-z_]+\]|evento|agencia|servicio|boleto|precio|cuest|costo|mxn|usd|d[oó]lar|\bpag|"
    r"mujer|chica|hombre|persona|gente|pareja|matchmak|\bcita|noche|fiesta|polanco|cdmx|lugar|"
    r"\bhora|fecha|inclu|bebida|botana|comida|cena|disfra|m[aá]scara|halloween|video|foto|llamada|"
    r"garant|segur|selecci|contrato|reembolso|regist|p[aá]gina|correo|organiz|asist|invitad|"
    r"slavic|eslav|rusa|ucrania|bielorrus|nosotr|aqu[ií]\s+nos|entrada|parking|valet|estaciona|"
    r"descuent|promoci|gratis|cupo|reserv|apart", re.IGNORECASE)


def _words(text: str) -> set[str]:
    t = (text or "").lower().translate(str.maketrans("áéíóúü", "aeiouu"))
    return {w for w in re.findall(r"[a-zñ]+", t) if len(w) >= 4}


def _stems(text: str) -> set[str]:
    return {w[:4] for w in _words(text)}


def _neg(text: str) -> bool:
    return bool(_NEG_RE.search(text or ""))


def _fits(claim: str, sent: str, min_overlap: float) -> bool:
    """Одно предложение источника подтверждает утверждение: все числа (суммы, даты, время)
    есть в этом предложении точно, отрицание совпадает, значимых слов ≥ min_overlap."""
    words = _stems(claim)       # основы: «compra» ≈ «comprar», «incluidas» ≈ «incluye»
    if not numbers_in(claim) <= numbers_in(sent) or _neg(claim) != _neg(sent):
        return False
    return not words or len(words & _stems(sent)) >= min_overlap * len(words)


def claim_supported(claim: str, sources: list[str], respaldo: str | None = None) -> bool:
    """Код решает, подтверждено ли утверждение; источники — отдельные ПРЕДЛОЖЕНИЯ
    подтверждённых фич и карточки ивента (не запись целиком: «No …» в одной фразе записи
    не должно подтверждать отрицание в другой).

    1. Одно предложение: числа точно, отрицание совпадает, ≥ 80 % слов.
    2. Склейка двух предложений («más de 3,000 mujeres eslavas que buscan pareja»): только
       без отрицания; если есть числа — одно из двух само подтверждает числа и половину слов.
    3. Цитата модели, если она реально есть в источниках: тем же предложением числа и
       отрицание как в п. 1, слов ≥ 50 % (пересказ «No es obligatorio ir disfrazado»).
    """
    if any(_fits(claim, sent, 0.8) for sent in sources):
        return True
    words = _stems(claim)
    if words and not _neg(claim):
        plain = [w & words for w in (_stems(x) for x in sources if not _neg(x))]
        plain = sorted(plain, key=len, reverse=True)[:6]
        anchors = ([_stems(x) & words for x in sources if _fits(claim, x, 0.5)]
                   if numbers_in(claim) else plain)
        if any(len(a | b) >= 0.8 * len(words) for a in anchors for b in plain):
            return True
    if respaldo and _stems(respaldo):
        for sent in sources:
            if len(_stems(respaldo) & _stems(sent)) >= 0.8 * min(len(_stems(respaldo)),
                                                                   len(_stems(sent)) or 1):
                if _fits(claim, sent, 0.5):
                    return True
    return False


def _clauses(text: str) -> list[str]:
    """Части фразы: по , ; : и « y »."""
    return [c for c in re.split(r"[,;:]\s*|\s+y\s+", text or "") if c.strip()]


def hard_fact_supported(sent: str, sources: list[str]) -> bool:
    """Каждая часть ответа с «опасной» темой подтверждается отдельно: «El boleto cuesta
    6,000 MXN y se paga en la página» — цена и оплата из разных фраз базы."""
    for part in _clauses(sent) if len(_clauses(sent)) > 1 else [sent]:
        if any(rx.search(part) for rx in _HARD_FACT_RES) and not _hard_part_supported(part, sources):
            return False
    return True


def _hard_part_supported(sent: str, sources: list[str]) -> bool:
    """Предложение на «опасную» тему: в одном предложении источника есть те же темы,
    числа, то же отрицание и хотя бы четверть основ слов («Puedes pagar en la página del
    evento» ← «El pago se hace en la página…»). Выдумку отсекает тема: «efectivo», «OXXO»,
    «cena», «valet» в подтверждённой базе нет ни в одном предложении."""
    topics = [rx for rx in _HARD_FACT_RES if rx.search(sent)]
    stems = _stems(sent)
    for src in sources:
        if all(rx.search(src) for rx in topics) and numbers_in(sent) <= numbers_in(src) \
                and _neg(sent) == _neg(src) and len(stems & _stems(src)) * 4 >= len(stems):
            return True
    return claim_supported(sent, sources)


def fact_sources(recs: list[dict], card: dict | None, settings: dict) -> list[str]:
    """Источники фактов — предложения ТОЛЬКО подтверждённых записей базы (knowledge.usable)
    и карточки ивента. Прошлые ответы бота и слова лида — никогда: у старых лидов там выдумки
    legacy («puedes pagar en OXXO», «30 mujeres») — решение владелицы 2026-10-22."""
    out = []
    for r in recs:
        txt = r.get("contenido_es") or ""
        for variant in (txt, render_tokens(txt, settings)) if txt else ():
            for sent in (x for part in variant.split("\n") for x in _sentences(part)):
                out.append(sent)
                # части фразы: «…puedes venir disfrazado y, si no tienes disfraz, …» — отрицание
                # второй части не должно мешать подтвердить первую (повторное ревью 2026-10-22)
                out += [c.strip() for c in _clauses(sent) if c.strip() != sent and _stems(c)]
    if card:
        out += [render_tokens(f"{k}: {v}", settings) for k, v in card.items() if k != "valores"]
        out += [f"{k}: {v}" for k, v in (card.get("valores") or {}).items()]
        if (card.get("valores") or {}).get("event_date"):
            out.append(sender._fmt_date_es(card["valores"]["event_date"]))
        # Одна фраза со всей карточкой: «El evento Slavic-Latino Connect Night será el 18 de
        # noviembre en Polanco» подтверждается целиком (смоук 2026-10-22).
        out.append(render_tokens(
            f"El evento {card.get('nombre', '')} es y será el {card.get('fecha', '')}, empieza a "
            f"las {card.get('inicio', '')}, en {card.get('lugar', '')}; cuesta {card.get('precio', '')}.",
            settings))
    return out


# Пересказ разговора — не факт об ивенте: «La última vez platicamos del evento …» (смоук).
_RECAP_RE = re.compile(r"^\W*(?:la\s+)?[uú]ltima\s+vez\s+(?:que\s+)?(?:platicamos|hablamos|"
                       r"me\s+escribiste|nos\s+escribimos)\s*(?:de|del|sobre)?\s*", re.IGNORECASE)
# Служебные основы без фактов: «pero puedes hacerlo si quieres» — фактов нет.
_STOP_STEMS = {"pero", "pued", "hace", "quie", "tamb", "aqui", "ahor", "algo", "esto", "esta",
               "este", "como", "para", "todo", "toda", "bien", "clar", "much", "poco", "podr",
               "tien", "teng", "siem", "cada", "otra", "otro", "mism", "dond", "cuan", "porq",
               "verd", "cier", "segu", "rato", "tant", "quis"}


def _content_stems(text: str) -> set[str]:
    return _stems(text) - _STOP_STEMS


def _fact_sentences(text: str, name: str | None = None) -> list[str]:
    """Предложения-утверждения: без вопросов, без вводного «Gracias…/Hola…/Jaja…», без
    пересказа разговора и без имени лида («No es obligatorio ir disfrazado, Carlos.»)."""
    out = []
    for part in (text or "").split("\n"):
        for sent in _sentences(part):
            if name:
                sent = re.sub(rf",?\s*\b{re.escape(name)}\b", "", sent, flags=re.IGNORECASE)
            sent = _RECAP_RE.sub("", sent)
            sent = _GRACIAS_START_RE.sub("", sent).strip() if _THANKS_RE.match(sent) else sent
            sent = re.sub(r"^\W*(?:hola|jaja\w*)\b[^.!?]*[.!?,]+\s*", "", sent, flags=re.IGNORECASE)
            # «Incluye cena para 30 personas, ¿te late?» — утверждение с хвостом-вопросом
            sent = re.sub(r"[,;:\s]*¿[^?]*\?\s*$", "", sent).strip()
            if sent and not sent.endswith("?") and _content_stems(sent):
                out.append(sent)
    return out


def sentence_supported(sent: str, sources: list[str], respaldo: str | None = None) -> bool:
    """Целиком или по частям: «No es obligatorio ir disfrazado, pero puedes hacerlo si
    quieres» — вторая часть фактов не содержит."""
    if claim_supported(sent, sources, respaldo):
        return True
    clauses = _clauses(sent)
    parts = [c for c in clauses if _content_stems(c)]
    return len(clauses) > 1 and bool(parts) and all(claim_supported(c, sources, respaldo) for c in parts)


async def verify_claims(bubbles: list[str], recs: list[dict], card: dict | None,
                        lead_texts: list[str], cost: Cost,
                        settings: dict | None = None, name: str | None = None) -> list[str]:
    """Утверждения ответа без источника (список пуст — всё подтверждено)."""
    contenidos = [r.get("contenido_es") for r in recs if r.get("contenido_es")]
    payload = {
        "RESPUESTA": bubbles,
        "FUENTES": {"evento_actual": card, "fichas": contenidos},
        "LO_QUE_DIJO_EL_CLIENTE": lead_texts[-15:],
    }
    out = await _chat(CHECK_MODEL, VERIFY_SYSTEM, payload, cost, max_tokens=500, temperature=0)
    items = out.get("afirmaciones")
    if items is None:                       # старый формат ответа
        items = [{"texto": c, "respaldo": None} for c in out.get("sin_fuente") or []]
    sources = fact_sources(recs, card, settings or {})
    claims: list[str] = []
    for it in items:
        it = it if isinstance(it, dict) else {"texto": it, "respaldo": None}
        for sent in _fact_sentences(str(it.get("texto") or ""), name):
            if not (_DOMAIN_RE.search(sent) or any(rx.search(sent) for rx in _HARD_FACT_RES)):
                continue
            if not sentence_supported(sent, sources, it.get("respaldo")) and sent not in claims:
                claims.append(sent)
    # Страховка кодом: «опасные» темы проверяем сами, независимо от списка модели.
    for b in bubbles:
        for sent in _fact_sentences(b, name):
            if any(rx.search(sent) for rx in _HARD_FACT_RES) and \
                    not hard_fact_supported(sent, sources) and sent not in claims:
                claims.append(sent)
    return claims


def promesa_de_anya(claims: list[str], historial: list[dict]) -> bool:
    """Неподтверждённое утверждение повторяет то, что Аня писала вручную, — решает она
    (policy_anya_promise): бот не подтверждает и не отрицает."""
    anya = [m.get("texto") or "" for m in historial or [] if m.get("autor") == "anya_manual"]
    return any(len(_words(c) & _words(a)) >= 0.5 * max(1, len(_words(c)))
               for c in claims for a in anya)


# ───────────────────────── сводка памяти ─────────────────────────

SUMMARY_SYSTEM = (
    "Resume la parte antigua de una conversación de WhatsApp entre un cliente (lead) y Anna "
    "(asesora de una agencia de matchmaking). Escribe en español, máximo 120 palabras, en "
    "viñetas: qué contó el cliente de sí mismo (solo lo que él escribió, autor lead); qué "
    "preguntó; qué le prometió Anna escribiendo a mano (autor anya_manual); sus objeciones; en "
    "qué quedaron. No copies datos sobre el evento, precios, pagos o la agencia que dijo el bot "
    "(autor anna_bot): pueden tener errores. Si hay resumen_anterior, intégralo con los mensajes "
    "nuevos sin perder lo que contó el cliente. SOLO lo que está en los mensajes, sin "
    "suposiciones. Responde JSON: {\"resumen\": \"...\"}."
)
SUMMARY_LAG = 20
# Сводка, которую не удалось записать в БД (008 не применена, сбой): держим в памяти процесса,
# чтобы не пересчитывать её на каждом ответе (code-review 2026-10-22).
_summary_cache: dict[str, tuple[str, int]] = {}


async def maybe_refresh_summary(lead: dict, mem: dict, cost: Cost, force: bool = False) -> None:
    """Сводка всего, что старше окна в 40 сообщений, если её нет или она отстала на 20+.
    Накопительная: прошлая сводка + новые сообщения, ранние факты о лиде не теряются."""
    prior = mem["prior"]
    older = prior[:-memory.WINDOW]
    phone = lead.get("phone")
    if not older or not phone:
        return
    cached = _summary_cache.get(phone)
    if cached and cached[1] > int(lead.get("memoria_mensajes") or 0):
        lead["memoria_resumen"], lead["memoria_mensajes"] = cached
        mem["resumen"] = cached[0]
    covered = int(lead.get("memoria_mensajes") or 0)
    if not force and lead.get("memoria_resumen") and len(older) - covered < SUMMARY_LAG:
        return
    previo = lead.get("memoria_resumen") if 0 < covered <= len(older) else None
    nuevos = older[covered:] if previo else older
    lines = [{"fecha": memory.fecha(m["created_at"]) if isinstance(m.get("created_at"), datetime)
              else None, "autor": memory.autor(m), "texto": m.get("text") or ""}
             for m in nuevos[-400:]]
    payload = {"resumen_anterior": previo, "mensajes": lines}
    out = await _chat(SUMMARY_MODEL, SUMMARY_SYSTEM, payload, cost, max_tokens=400, temperature=0)
    resumen = (out.get("resumen") or "").strip()
    if not resumen:
        return
    last_id = older[-1].get("id")
    try:
        await db.update_memory_summary(phone, resumen, last_id, len(older))
        _summary_cache.pop(phone, None)
    except Exception as e:
        logger.exception("rag: не сохранила сводку памяти [%s]", phone)
        if phone not in _summary_cache:
            await _tech_alert(f"сводка памяти не записалась (миграция 008?): {e!r}", phone)
        _summary_cache[phone] = (resumen, len(older))
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


async def _attach_media_captions(result: dict, lead: dict) -> None:
    """Подписи к видео и фото ивента (только если медиа реально уйдёт), в общем лимите
    эмодзи на ход вместе с текстом."""
    text = result["messages"] + (result.get("after_media") or [])
    if result.get("send_event_video"):
        await ai._maybe_announce_event_video(result, None, lead)
        if result.get("video_caption"):
            result["video_caption"] = _caption_within_budget(VIDEO_CAPTION, text)
    if result.get("send_event_photo"):
        await ai._maybe_announce_event_photo(result, None, lead)
        if result.get("photo_caption"):
            result["photo_caption"] = _caption_within_budget(
                PHOTO_CAPTION, text + [result.get("video_caption") or ""])


def _caption_within_budget(caption: str, bubbles: list[str]) -> str:
    """Подпись, которую добавляет код, тоже в лимите эмодзи на ответ."""
    if emoji_count(bubbles) + emoji_count([caption]) <= MAX_EMOJI_TOTAL:
        return caption
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", caption)).strip()


async def _tech_alert(text: str, phone: str | None) -> None:
    """Техалерт в Telegram; его сбой ответ не роняет."""
    try:
        import escalation
        await escalation.notify_error("ai_conversation.generate_reply", text, phone)
    except Exception:
        logger.exception("rag: техалерт не ушёл")


async def unrendered_tokens(bubbles: list[str]) -> str | None:
    """Что не подставится при отправке: пузырь выпал (пустая настройка) или остался
    служебный текст ([токен]). None — всё подставилось."""
    rendered = await sender.render_bubbles(bubbles, None, True)
    if len(rendered) != len(bubbles):
        missing = sorted({t for b in bubbles for t in _TOKEN_RE.findall(b)})
        return f"пузырь не отрисовался, токены {missing}"
    for r in rendered:
        why = sender.service_text_reason(r)
        if why:
            return f"{why}: {r[:80]}"
    return None


def _silent(reason: str, **extra) -> dict:
    return _result([], "silent", silent_reason=f"Режим rag: {reason}", **extra)


def _parse_model(raw: dict) -> dict:
    bubbles = raw.get("bubbles") or raw.get("messages") or []
    if not isinstance(bubbles, list):
        bubbles = [str(bubbles)]
    raw_ex = raw.get("extracted") or {}
    extracted = {k: raw_ex.get(k) for k in ai._EXTRACTED_KEYS if raw_ex.get(k) is not None}
    stage = raw.get("funnel_stage")
    paso = raw.get("paso_extra") if isinstance(raw.get("paso_extra"), dict) else {}
    return {
        "bubbles": [str(b).strip() for b in bubbles if str(b).strip()],
        "paso_extra": {"tipo": paso.get("tipo") if paso.get("tipo") == "pregunta" else None,
                       "texto": str(paso.get("texto") or "").strip()},
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


# Ход после одобренного фото для ивента (решение владелицы 2026-10-22): имя, «холост ли» и
# фото есть — сразу к покупке. Пузыри по порядку: благодарность, рассказ (без уже сказанного),
# призыв со ссылкой; потом видео и фото прошлых ивентов; последним — вопрос о возрасте и
# профессии (если нужен). Текст собирает код из подтверждённых записей, без модели.
PHOTO_THANKS = "¡Gracias por tu foto, {name}! 😊"
PHOTO_THANKS_NO_NAME = "¡Gracias por tu foto! 😊"
CTA_LINK = "Aquí puedes apartar tu lugar: [event_link]"
SUMMARY_SENTENCE = "Te espero el [event_date] a las [event_start] en [event_address]"
_ANGER_RE = re.compile(r"molest|enoj|harto|hart[ao]|fastidi|spam|deja\s+de|no\s+me\s+escrib|"
                       r"no\s+me\s+interesa", re.IGNORECASE)


def _story_after_photo(by_id: dict, said: set[str]) -> str:
    """Сводка ивента одной фразой в момент покупки (решение владелицы 2026-10-22, правило «без
    повтора» здесь не действует): дата, время, место и что входит — токены карточки и запись
    event_includes. Хеллоуин — только если о нём ещё не говорили."""
    parts = []
    if by_id.get("event_date_time"):
        summary = SUMMARY_SENTENCE
        inc = by_id.get("event_includes")
        m = re.match(r"^\s*Incluye\s+(.+?)\.?\s*$", inc["contenido_es"]) if inc else None
        if m:
            summary += f", con {m.group(1)}"
        parts.append(summary + ".")
        if inc and not m:
            parts.append(inc["contenido_es"])
    elif by_id.get("event_includes"):
        parts.append(by_id["event_includes"]["contenido_es"])
    if by_id.get("event_halloween") and "halloween" not in said:
        parts.append(by_id["event_halloween"]["contenido_es"])
    return " ".join(parts)


def media_fits(mem: dict) -> bool:
    """Фото прошлых ивентов к месту: лид в последних сообщениях не возражал, не прощался
    и не злился."""
    recent = [t for t in (mem.get("lead_texts") or [])[-3:] if t]
    return not any(memory._PRICE_OBJECTION_RE.search(t) or memory._FAREWELL_RE.search(t)
                   or _ANGER_RE.search(t) or filters.is_aggression(t) for t in recent)


def venta_tras_foto(lead: dict, mem: dict, by_id: dict, user_text: str) -> dict | None:
    """Ответ на одобренное фото лида ивента, у которого есть имя и «холост ли»: сразу к
    покупке. None — случай не наш (другая ветка, ссылку уже давали, в сообщении есть вопрос)."""
    if "[фото одобрено" not in (user_text or ""):
        return None
    rest = re.sub(r"\[фото одобрено[^\]]*\]", "", user_text).strip()
    ya, settings = mem["ya_hecho"], mem["settings"]
    if "?" in rest or memory.ruta(lead, user_text) != "evento" or ya.get("enlace_enviado") \
            or ya.get("boleto_comprado") or settings.get("event_active") != "1" \
            or not (settings.get("event_link") or "").strip():
        return None
    if not (lead.get("name") and lead.get("is_single") is True):
        return None
    name = lead.get("name") or ai._plausible_name(lead.get("whatsapp_name"))
    bubbles = [PHOTO_THANKS.format(name=name) if name else PHOTO_THANKS_NO_NAME]
    story = _story_after_photo(by_id, set(ya.get("hechos_dichos") or []))
    if story:
        bubbles.append(story)
    bubbles.append(CTA_LINK)
    # Вопрос — только возраст/профессия, если их нет и он не остался без ответа в прошлый раз.
    cal = memory.calificacion(lead, ya, user_text, mem.get("ultimo_mensaje_anna"))
    question = slot_question(cal) if cal.get("siguiente") in ("age", "profession") else None
    return _result(bubbles, funnel_stage="qualified",
                   send_event_video=not ya.get("video_enviado"),
                   send_event_photo=not ya.get("foto_enviada") and media_fits(mem),
                   after_media=[question] if question else [])


def revisar(out: dict, mem: dict, lead: dict, merged: dict, user_text: str,
            fichas: list[dict]) -> tuple[list[str], str | None, list[str]]:
    """Ответ модели → пузыри для лида: основной ответ плюс не больше одного шага (вопрос по
    слоту из paso_extra или следующий вопрос по слоту кодом), затем все проверки check_reply.
    lead — профиль до этого сообщения, merged — с данными из него."""
    answered = {k for k, v in out["extracted"].items() if v not in (None, "")}
    # Слоты с учётом данных из этого сообщения. Лид ответил на вопрос — прошлый вопрос не
    # «проигнорирован», оставшееся можно спросить сразу.
    cal_next = memory.calificacion(merged, mem["ya_hecho"], user_text,
                                   None if answered else mem["ultimo_mensaje_anna"],
                                   compra=bool(mem.get("compra_pendiente")))
    bubbles = list(out["bubbles"])
    paso = paso_extra_ok(out.get("paso_extra") or {}, cal_next)
    if paso:
        bubbles.append(paso)
    # Числа — из фич, которые модель видела, и карточки ивента: «10,000» из тарифа услуги
    # в ответе про ивент не пройдёт (code-review 2026-10-22).
    bubbles, reason, notes = check_reply(bubbles, mem=mem, fichas=fichas, lead=merged,
                                         user_text=user_text,
                                         dio_algo=lead_dio_algo(user_text, out["extracted"], lead),
                                         answered=answered, pregunta_siguiente=slot_question(cal_next),
                                         permitidos=slots_permitidos(cal_next))
    if (out.get("paso_extra") or {}).get("texto") and not paso:
        notes.append("paso_extra не вопрос по слоту — убран")
    return bubbles, reason, notes


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
    except Exception as e:
        logger.exception("rag: сбой режима")
        await _tech_alert(repr(e), lead.get("phone"))
        result = _silent(f"сбой режима rag ({type(e).__name__}) — ответь сама")
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

    # 2.6. Ивент: фото не прислал после двух просьб — ссылки без фото нет, решает Аня.
    if cal.get("foto_agotada") and cal["etapa"] == "calificacion":
        return _silent("Лид без фото хочет билет, реши сама (две просьбы прислать фото без ответа)",
                       rag_meta={**meta, "fuentes": ["foto_agotada"]})

    # 2.7. Одобренное фото лида ивента с именем и «холост ли» — сразу к покупке (кодом).
    venta = venta_tras_foto(lead, mem, by_id, user_text)
    if venta:
        problem = await unrendered_tokens(venta["messages"] + venta["after_media"])
        if problem:
            await _tech_alert(f"токен не подставился: {problem}", lead.get("phone"))
            return _silent(f"токен не подставился ({problem}) — ответь сама",
                           rag_meta={**meta, "fuentes": ["venta_tras_foto"]})
        venta["rag_meta"] = {**meta, "fuentes": ["venta_tras_foto"]}
        await _attach_media_captions(venta, lead)
        return venta

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
        # Сбой нового режима: старый ответ НЕ отправляем — молчание, ручной режим, алерт Ане
        # и техалерт (решение владелицы 2026-10-22).
        logger.exception("rag: OpenAI/разбор ответа упал")
        await _tech_alert(repr(e), lead.get("phone"))
        return _silent(f"сбой OpenAI или ответа модели ({type(e).__name__}) — ответь сама",
                       rag_meta=meta)

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

    bubbles, reason, notes = revisar(out, mem, lead, merged, user_text, fichas)
    meta["notas"] = notes
    if reason:
        return _silent(reason, **extra)
    if any(_NO_DATA_RE.search(x) for x in bubbles):
        return _silent("нет данных в базе (модель ответила отпиской)", **extra)

    try:
        claims = await verify_claims(bubbles, recs, card, mem["lead_texts"], cost, settings,
                                     merged.get("name") or ai._plausible_name(lead.get("whatsapp_name")))
    except Exception:
        logger.exception("rag: проверка второй моделью упала → silent")
        return _silent("проверка утверждений недоступна", **extra)
    if claims and promesa_de_anya(claims, mem.get("historial") or []):
        return _silent(f"policy_anya_promise: лид спрашивает о том, что обещала Аня, в базе "
                       f"этого нет — реши сама ({claims})", **extra)
    if claims:
        return _silent(f"утверждения без источника: {claims}", **extra)

    # Токены: каждый должен подставиться (нет event_price_next в настройках → не отправляем).
    problem = await unrendered_tokens(bubbles)
    if problem:
        await _tech_alert(f"токен не подставился: {problem}", lead.get("phone"))
        return _silent(f"токен не подставился ({problem}) — ответь сама", **extra)

    result = _result(bubbles, extracted=out["extracted"], funnel_stage=out["funnel_stage"],
                     rag_estado=estado, rag_meta=meta)
    if cal["pasar_a_llamada"]:
        result["needs_escalation"] = True
    # Видео — один раз, вместе с первой ссылкой на билет.
    link = (settings.get("event_link") or "").strip()
    if any("[event_link]" in b or (link and link in b) for b in bubbles) and \
            not mem["ya_hecho"].get("video_enviado"):
        result["send_event_video"] = True
    if out["send_event_photo"] and not mem["ya_hecho"].get("foto_enviada"):
        result["send_event_photo"] = True
    await _attach_media_captions(result, lead)
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
