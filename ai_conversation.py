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

import asyncio
import collections
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import httpx

import ai
import conversation_rules
import cuestionario
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
# Технический сбой (таймаут, 5xx, лимит 429, битый JSON): до TECH_ATTEMPTS попыток основной
# моделью с паузами, потом запасная модель (решение Арины 2026-10-23: не молчать из-за ошибок).
FALLBACK_MODEL = "gpt-4o"
# GPT-5 (рассуждающая модель) — для сравнения в смоуке (решение Арины 2026-10-23: сравнить в
# прогоне, по умолчанию gpt-4.1). У неё нет temperature, длина — max_completion_tokens (туда же
# входит рассуждение), уровень рассуждения — reasoning_effort.
GPT5_REASONING_EFFORT = "low"
GPT5_MAX_COMPLETION_TOKENS = 4000
CHECK_FALLBACK_MODEL = "gpt-4o-mini"
TECH_ATTEMPTS = 3
FALLBACK_ATTEMPTS = 2
RETRY_PAUSES = (2.0, 5.0)        # пауза после 1-й и 2-й неудачи, сек
_sleep = asyncio.sleep           # тесты подменяют, чтобы не ждать
SUMMARY_MODEL = "gpt-4.1-mini"
EMBED_MODEL = "text-embedding-3-small"
TEMPERATURE = 0.3
MAX_BUBBLES = 4
TOP_K = 5
COST_COUNTER = "rag_cost_usd"
DAILY_COST_WARN_USD = 5.0
# Жёсткий лимит за сутки (app_settings.rag_daily_hard_limit_usd, по умолчанию 15): выше него
# режим rag OpenAI не зовёт — молчание, Аня отвечает сама, техалерт раз в сутки (ревью 2026-10-22).
HARD_LIMIT_KEY = "rag_daily_hard_limit_usd"
HARD_LIMIT_DEFAULT_USD = 15.0
_hard_limit_alerted: dict = {"day": None}

# Дословно без модели (решение владелицы): «ты бот?» и блоки.
VERBATIM_IDS = {40, 9, 12, 27, 28, 29, 7, 8}
# base_script, которые код отправляет сам при совпадении ситуации.
CODE_LITERAL_IDS = ("greeting_hola", "greeting_event", "greeting_service", "service_pitch")

# Цена за 1M токенов, USD (OpenAI, 2026-10). cached — часть prompt из кэша.
PRICES = {
    "gpt-4.1": {"in": 2.00, "cached": 0.50, "out": 8.00},
    "gpt-4.1-mini": {"in": 0.40, "cached": 0.10, "out": 1.60},
    "text-embedding-3-small": {"in": 0.02, "cached": 0.02, "out": 0.0},
    "gpt-4o": {"in": 2.5, "cached": 1.25, "out": 10.0},
    "gpt-5": {"in": 1.25, "cached": 0.125, "out": 10.0},
    "gpt-5-mini": {"in": 0.25, "cached": 0.025, "out": 2.0},
    "gpt-4o-mini": {"in": 0.15, "cached": 0.075, "out": 0.6},
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
                max_tokens: int = 700, temperature: float = TEMPERATURE,
                retries: int | None = None) -> dict:
    content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False, default=str)
    r = await ai._openai_post("https://api.openai.com/v1/chat/completions",
                              chat_payload(model, system, content, max_tokens, temperature),
                              timeout=90 if model.startswith("gpt-5") else 60, max_retries=retries)
    data = r.json()
    cost.add(model, data.get("usage"))
    return json.loads(data["choices"][0]["message"]["content"])


def chat_payload(model: str, system: str, content: str, max_tokens: int, temperature: float) -> dict:
    """Тело запроса chat/completions: gpt-4.x — temperature и max_tokens; gpt-5 — без
    temperature, max_completion_tokens с запасом на рассуждение, reasoning_effort."""
    body = {"model": model, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    if model.startswith("gpt-5"):
        body["max_completion_tokens"] = max(GPT5_MAX_COMPLETION_TOKENS, max_tokens)
        body["reasoning_effort"] = GPT5_REASONING_EFFORT
    else:
        body["temperature"] = temperature
        body["max_tokens"] = max_tokens
    return body


class TechFailure(Exception):
    """OpenAI не ответил после повторов и запасной модели. motivo — причина по-русски."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def motivo_tecnico(e: BaseException) -> str:
    """Причина технического сбоя простыми словами (для алерта Ане)."""
    if isinstance(e, TechFailure):
        return e.motivo
    if isinstance(e, httpx.TimeoutException):
        return "OpenAI не ответил вовремя (таймаут)"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 429:
            return "OpenAI ограничил запросы (лимит, 429)"
        if code >= 500:
            return f"у OpenAI сбой на сервере ({code})"
        if code in (401, 403):
            return f"OpenAI не принял ключ ({code})"
        return f"OpenAI отклонил запрос ({code})"
    if isinstance(e, httpx.TransportError):
        return "нет связи с OpenAI"
    if isinstance(e, (json.JSONDecodeError, ValueError, KeyError, TypeError, IndexError)):
        return "модель вернула битый ответ (не JSON)"
    return f"непредвиденная ошибка ({type(e).__name__})"


def _retryable(e: BaseException) -> bool:
    """4xx, кроме 429, повтором не лечится (ключ, формат запроса)."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code == 429 or code >= 500
    return True


async def _chat_robust(model: str, fallback: str, system: str, user: dict | str, cost: Cost,
                       parse=None, **kw) -> dict:
    """_chat с повторами: TECH_ATTEMPTS попыток основной моделью (паузы RETRY_PAUSES), затем
    FALLBACK_ATTEMPTS — запасной. parse — разбор ответа (его сбой = битый ответ, повторяем).
    Всё упало → TechFailure с причиной последней ошибки."""
    last: BaseException | None = None
    for m, attempts in ((model, TECH_ATTEMPTS), (fallback, FALLBACK_ATTEMPTS)):
        for i in range(attempts):
            try:
                raw = await _chat(m, system, user, cost, retries=0, **kw)
                if not isinstance(raw, dict):
                    raise ValueError("ответ модели не объект JSON")
                return parse(raw) if parse else raw
            except Exception as e:
                last = e
                logger.warning("rag: %s попытка %d/%d: %s", m, i + 1, attempts, motivo_tecnico(e))
                if not _retryable(e):
                    break
                if i + 1 < attempts:
                    await _sleep(RETRY_PAUSES[min(i, len(RETRY_PAUSES) - 1)])
        if m == model:
            logger.warning("rag: %s не ответил — запасная модель %s", model, fallback)
    raise TechFailure(motivo_tecnico(last) if last else "OpenAI не ответил")


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
    r"\bcompr(?:o|ar|arlo|arla|a)\b|\bpag(?:o|ar|arlo)\b|\bse\s+paga\b|\blink\b|\benlace\b|"
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
    r"cancelar\s+(?:mi\s+)?boleto|"
    # ревью 2026-10-22: карта, рассрочка, оплата на входе — тоже Аня
    r"\btarjeta|meses\s+sin\s+intereses|\bmsi\b|"
    r"pag(?:ar|o|a|as)\s+(?:all[aá]\s+|ah[ií]\s+)?(?:en|a)\s+(?:la\s+)?(?:entrada|puerta)|"
    r"en\s+la\s+(?:entrada|puerta)\s+(?:se\s+)?pag", re.IGNORECASE)


# Не покупка (ревью 2026-10-22): возражение или колебание («me gustaría ir pero está muy caro»),
# отрицание («no puedo pagar ahorita»), прошедшее время («ya me registré», «ya pagué»).
_NOT_BUYING_RE = re.compile(
    r"\bno\s+(?:puedo|podr[eé]|podr[ií]a|voy\s+a|quiero|pienso|creo|tengo\s+(?:para|dinero)|"
    r"me\s+alcanza(?:\s+para)?)\s+(?:\w+\s+)?(?:pag|compr|reserv|apart|ir\b|asistir)|"
    r"\b(?:registr[eé]|pagu[eé]|compr[eé]|reserv[eé]|apart[eé])\b|ya\s+me\s+registr|ya\s+(?:lo\s+)?pag",
    re.IGNORECASE)
# Колебание без «no sé»: «no sé cómo pagar» — это как раз вопрос о покупке.
_HESITATE_RE = re.compile(r"\bpensar(?:lo|[eé])?\b|lo\s+pienso|\btal\s+vez\b|\bquiz[aá]s?\b|"
                          r"\bm[aá]s\s+adelante\b|\bdespu[eé]s\s+(?:veo|te\s+digo)|"
                          r"\bluego\s+(?:te\s+)?(?:aviso|digo)\b", re.IGNORECASE)


def is_purchase(text: str) -> bool:
    t = text or ""
    return bool(_PURCHASE_RE.search(t)) and not _NOT_BUYING_RE.search(t) \
        and not memory._PRICE_OBJECTION_RE.search(t) and not _HESITATE_RE.search(t)


def wants_to_buy(user_text: str, last_anna: str | None,
                 recent_lead: list[str] | None = None) -> bool:
    """Лид хочет купить: спросил сейчас, согласился на предложенную ссылку или спрашивал,
    как купить, в последних сообщениях (ссылку тогда придержали до имени и «холост ли»)."""
    if is_purchase(user_text):
        return True
    if last_anna and _OFFER_LINK_RE.search(last_anna) and _AFFIRM_RE.search(user_text or "") \
            and not _NOT_BUYING_RE.search(user_text or ""):
        return True
    return any(is_purchase(t) for t in (recent_lead or [])[-3:])


def solo_compra(user_text: str) -> bool:
    """В сообщении только покупка: других вопросов нет («¿Cómo pago? ¿Y cuántas mujeres van?»
    — второй вопрос отвечает модель, а не готовый ход покупки)."""
    qs = _QUESTION_SENT_RE.findall(user_text or "")
    return all(is_purchase(q) for q in qs)


def compra_pendiente(mem: dict, user_text: str) -> bool:
    """Лид спрашивал, как купить (сейчас или в последних 10 сообщениях), а ссылку ещё не
    получил: её придержали до имени, «холост ли» и одобренного фото."""
    if mem["ya_hecho"].get("enlace_enviado"):
        return False
    if wants_to_buy(user_text, mem.get("ultimo_mensaje_anna"), (mem.get("lead_texts") or [])[:-1]):
        return True
    # Глубже 3 сообщений — только вопрос или просьба о покупке, а не «me registré en Instagram».
    return any(is_purchase(t) and (has_question(t) or _BUY_REQUEST_RE.search(t))
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


def interes_en_extracted(result: dict, lead: dict, user_text: str) -> None:
    """Интерес лида (ивент или услуга), названный в сообщении, сохраняется кодом во всех
    путях ответа — готовых ходах, модели, молчании (main пишет extracted в лида). Модель его
    извлекает не всегда: прод 2026-10-22, «Me interesa el evento» не сохранилось, и после фото
    бот не понял, что лид про ивент."""
    if lead.get("interest"):
        return
    ex = result.get("extracted")
    if not isinstance(ex, dict):
        ex = result["extracted"] = {}
    if not ex.get("interest") and interes_de(user_text) in ("event", "agency"):
        ex["interest"] = interes_de(user_text)


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
        elif key == "auto_saludo":
            have = bool(ya.get("auto_saludo"))     # автоприветствие WhatsApp уже ушло
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


# Числа словами перед существительным-счётом: «unas cuarenta mujeres», «docenas de personas»
# (ревью 2026-10-22). Отдельно «dos» не считаем числом: «hay dos formas» — не факт о людях.
_WORD_NUMS = {"dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7",
              "ocho": "8", "nueve": "9", "diez": "10", "once": "11", "doce": "12", "quince": "15",
              "veinte": "20", "treinta": "30", "cuarenta": "40", "cincuenta": "50",
              "sesenta": "60", "setenta": "70", "ochenta": "80", "noventa": "90", "cien": "100",
              "ciento": "100", "docenas": "docenas", "decenas": "decenas", "cientos": "cientos",
              "miles": "miles"}
_COUNT_NOUN = (r"(?:mujeres|hombres|personas|chicas|chicos|invitad[oa]s|asistentes|parejas|"
               r"solter[oa]s|eslavas|eventos|boletos|lugares)")
_WORD_NUM_RE = re.compile(rf"\b({'|'.join(_WORD_NUMS)})\s+(?:de\s+)?{_COUNT_NOUN}", re.IGNORECASE)
_NUM_NOUN_RE = re.compile(rf"(\d[\d,.]*|\b(?:{'|'.join(_WORD_NUMS)})\b)\s+(?:de\s+)?([a-záéíóúñ]+)",
                          re.IGNORECASE)


def numbers_in(text: str) -> set[str]:
    text = _URL_RE.sub(" ", _TOKEN_RE.sub(" ", text or ""))   # цифры внутри ссылок не считаем
    return {_norm_num(n) for n in _NUM_RE.findall(text)} | \
        {_WORD_NUMS[m.group(1).lower()] for m in _WORD_NUM_RE.finditer(text)}


def num_pairs(text: str) -> set[tuple[str, str]]:
    """Пары «число + слово после него»: «30 mujeres eslavas» → ('30', 'muje')."""
    out = set()
    for m in _NUM_NOUN_RE.finditer(_TOKEN_RE.sub(" ", text or "")):
        raw, word = m.group(1).lower(), m.group(2)
        num = _WORD_NUMS.get(raw) or _norm_num(raw)
        stem = next(iter(_stems(word)), None)
        if stem:
            out.add((num, stem))
    return out


def render_tokens(text: str, settings: dict) -> str:
    """Подставить токены ивента для сверки (как сделает sender)."""
    def val(m):
        v = sender.event_token_value(m.group(0)[1:-1], settings)
        return "" if v is None else v
    return _TOKEN_RE.sub(val, text or "")


def allowed_numbers(fichas: list[dict], settings: dict, lead: dict, lead_texts: list[str]) -> set[str]:
    """Числа из подтверждённых фичей и карточки ивента. Возраст лида числом-фактом не
    считается («30 mujeres» у лида 30 лет не пройдёт); lead_texts в режиме rag пустой."""
    nums: set[str] = set()
    for f in fichas:
        nums |= numbers_in(render_tokens(f.get("contenido_es") or "", settings))
    for k in ("event_date", "event_start", "event_price_nonmember", "event_price_old",
              "event_presale_until"):
        nums |= numbers_in(sender.event_token_value(k, settings) or "")
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


PRICE_SENTENCE = "El precio de preventa es de [event_price_nonmember] MXN[event_promo]."
# Лестница цен убрана (2026-10-22): «más adelante el precio sube», «la siguiente etapa».
_LADDER_RE = re.compile(r"m[aá]s\s+adelante\s+(?:el\s+precio\s+)?sube|precio\s+sube|"
                        r"siguiente\s+(?:etapa|precio|es\s+de)|despu[eé]s\s+(?:sube|costar[aá])", re.IGNORECASE)
_GRACIAS_START_RE = re.compile(r"^\W*(?:muchas\s+|mil\s+)?gracias\b[^.!?]*[.!?]*\s*", re.IGNORECASE)
_COMPLIMENT_RE = re.compile(
    r"eres\s+(?:muy\s+)?(?:linda|bonita|hermosa|guapa|amable|lind[ií]sima)|"
    r"qu[eé]\s+(?:linda|bonita|amable|hermosa)|felicidades|buen\s+trabajo|me\s+encanta\s+(?:tu|lo\s+que)",
    re.IGNORECASE)


_LEAD_THANKS_RE = re.compile(r"\bgracias\b|\bte\s+agradezco\b|\bmuy\s+amable\b", re.IGNORECASE)


# Вежливый вопрос лида («¿y tú cómo estás?», «bien y tú?», «¿qué tal tu día?») — отвечаем коротко
# и тепло, «gracias» здесь уместно (прогон 2026-10-23: «Gracias por preguntar» срезалось целиком).
_COURTESY_RE = re.compile(
    r"\by\s+t[uú]\b|c[oó]mo\s+(?:est[aá]s|te\s+va|has\s+estado|amaneciste|va\s+tu\s+d[ií]a)|"
    r"qu[eé]\s+tal(?:\s+(?:tu\s+d[ií]a|todo|est[aá]s))?\b", re.IGNORECASE)
_COURTESY_ONLY_RE = re.compile(
    r"^\W*(?:(?:hola|oye|jaja\w*|(?:muy\s+)?bien|todo\s+bien|gracias|y|t[uú]|c[oó]mo|est[aá]s|te|va|"
    r"has|estado|amaneciste|tu|d[ií]a|qu[eé]|tal|todo)\W*)+$", re.IGNORECASE)
COURTESY_REPLY = "Muy bien, gracias 😊"


def es_cortesia(text: str) -> bool:
    """Сообщение — только вежливый вопрос («¿y tú cómo estás?»), без другого вопроса или данных."""
    text = text or ""
    return bool(_COURTESY_RE.search(text)) and bool(_COURTESY_ONLY_RE.match(text))


def lead_dio_algo(user_text: str, extracted: dict, lead: dict | None = None) -> bool:
    """Лид что-то дал: новые данные о себе, фото, благодарность или комплимент — тогда
    «Gracias» уместно. Данные, которые уже были в профиле, новыми не считаются."""
    lead = lead or {}
    datos = {k: v for k, v in (extracted or {}).items()
             if k != "interest" and v not in (None, "") and lead.get(k) != v}
    return bool(datos) or "[фото одобрено" in (user_text or "") or \
        bool(_COMPLIMENT_RE.search(user_text or "")) or bool(_LEAD_THANKS_RE.search(user_text or "")) \
        or bool(_COURTESY_RE.search(user_text or ""))


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
    """Цена ивента — предпродажа с зачёркнутой старой ценой (решение владелицы 2026-10-22):
    «El precio de preventa es de [event_price_nonmember] MXN[event_promo].» Лестницы цен
    больше нет: фразы про следующую ступень («Más adelante el precio sube a…») убираем.
    К фразе с текущей ценой без [event_promo] добавляем его (sender подставит «~9,000 MXN~»
    или ничего, если старой цены в настройках нет)."""
    now = re.sub(r"\D", "", settings.get("event_price_nonmember") or "")
    nxt = re.sub(r"\D", "", settings.get("event_price_next") or "")

    def says(sent, token, value):
        return token in sent or bool(value and value in {re.sub(r"\D", "", n) for n in numbers_in(sent)})

    def ladder(sent):
        return says(sent, "[event_price_next]", nxt) or bool(_LADDER_RE.search(sent))
    def cut_ladder(sent):
        # «Ahora cuesta 6000 MXN; la siguiente etapa es de 7000 MXN.» → часть про текущую цену
        keep = [c.strip() for c in re.split(r"\s*;\s*|,\s+", sent.rstrip(".!")) if c.strip() and not ladder(c)]
        return (", ".join(keep) + ".") if keep else ""
    old = re.sub(r"\D", "", settings.get("event_price_old") or "")

    def old_by_hand(part):
        return bool(old and old in {re.sub(r"\D", "", n) for n in numbers_in(part)})
    out = []
    for b in bubbles:
        # Зачёркнутую цену рисует [event_promo]; «~9,000~» и «antes costaba 9,000 MXN», написанные
        # моделью вручную, убираем (прогон 2026-10-22: двойная зачёркнутая и ложное молчание).
        b = re.sub(r"\s*~[^~\n]+~", "", b)
        for sent in _sentences(b):
            if old_by_hand(sent):
                keep = [c.strip() for c in re.split(r"\s*;\s*|,\s+", sent.rstrip(".!"))
                        if c.strip() and not old_by_hand(c)]
                b = b.replace(sent, (", ".join(keep) + ".") if keep else "", 1)
        for sent in _sentences(b):
            if ladder(sent):
                b = b.replace(sent, cut_ladder(sent), 1)
        if "[event_promo]" not in b and not _SERVICE_PRICE_RE.search(b):
            m = re.search(r"(\[event_price_nonmember\]|(?<![\d,.])" + (re.escape(now) if now else "@@")
                          + r"(?![\d,.]))\s*MXN", b)
            if m:
                b = b[:m.end()] + "[event_promo]" + b[m.end():]
        out.append(re.sub(r"[ \t]{2,}", " ", b).strip())
    return [b for b in out if b]


BUY_ONLINE_SENTENCE = "El boleto se compra en línea, en la página del evento."
PHOTO_REQUEST = "¿Me mandas una foto tuya?"
# «para completar tu registro», «para continuar» — объяснение, зачем фото (владелица: без него)
_PHOTO_WHY_RE = re.compile(r"\s*,?\s*(?:para|antes\s+de)\s+(?:completar|continuar|seguir|terminar|"
                           r"avanzar|registrarte|darte|enviarte|pasarte|confirmar)\b[^.?!¿]*",
                           re.IGNORECASE)
_ORPHAN_LA_RE = re.compile(r"¿?\s*me\s+la\s+(?:puedes\s+)?(?:mandar|enviar|mandas|env[ií]as|compartes)"
                           r"[^?]*\??", re.IGNORECASE)


# Пояснение, зачем фото, даже без слова «foto»: «Solo la reviso.», «Es solo para verificar.»
_PHOTO_EXPLAIN_RE = re.compile(
    r"^\W*(?:y\s+)?(?:s[oó]lo\s+)?(?:la|lo)\s+(?:reviso|revisamos|verifico|verificamos|veo|vemos|"
    r"uso|usamos|necesito|necesitamos)\b|"
    r"\b(?:es|ser[aá])\s+(?:s[oó]lo\s+)?para\s+(?:revisar|verificar|confirmar|validar)\b|"
    r"\bs[oó]lo\s+para\s+(?:revisar|verificar|confirmar|validar)\b", re.IGNORECASE)

# Фраза именно просит фото («falta una foto tuya», «mándame tu foto»), а не благодарит за него.
_PHOTO_ASK_RE = re.compile(r"\bfalta|\bnecesit|\bmand|\benv[ií]|\bcompart|\bp[aá]s(?:a|ame)|\bsub[ei]|"
                           r"\bpuedes|\bpodr[ií]as|\bme\s+(?:regalas|haces)", re.IGNORECASE)

# Фото самого лида (не «fotos de eventos pasados»).
_LEAD_PHOTO_RE = re.compile(r"\bfoto\s+tuya|\btu\s+foto\b|\buna\s+foto\b(?!s)(?!\s+de\s+(?:los|la|el|nuestros?)\s+event)"
                            r"|\bselfie", re.IGNORECASE)


def purchase_before_photo(bubbles: list[str], mem: dict, user_text: str) -> list[str]:
    """Лид спросил, как купить, а ссылку давать рано (этап calificacion): ответ по сути
    («se compra en línea…») и просьба фото, без объяснений вслух, зачем (решение 2026-10-22).
    Плюс осиротевшее «¿Me la puedes mandar?» после срезанной фразы про фото."""
    cal = mem["calificacion"]
    if "[фото одобрено" in (user_text or "") or (cal.get("tenemos") or {}).get("photo"):
        return bubbles      # фото уже есть: «Gracias por tu foto» — не просьба (прод 2026-10-22 15:53)
    # Просьба о фото лида — утверждением с объяснением («Para apartar tu lugar solo falta una
    # foto tuya, puede ser tipo selfie y reciente.», прод 2026-10-22): объяснений не даём, а
    # сверка такую фразу не пропускает — заменяем на короткий вопрос.
    replaced = False
    fixed = []
    for b in bubbles:
        # «Solo la reviso.», «Es solo para verificar.» — объяснение, зачем фото (прод 2026-10-22)
        b = _drop_sentences(b, lambda x: "?" not in x and bool(_PHOTO_EXPLAIN_RE.search(x)))
        new = _drop_sentences(b, lambda x: "?" not in x and bool(_LEAD_PHOTO_RE.search(x))
                              and bool(_PHOTO_ASK_RE.search(x)))
        replaced = replaced or new != b
        fixed.append(new)
    bubbles = [b for b in fixed if b.strip() and _words(b)] or bubbles
    if replaced:
        # «¿Me la mandas por aquí?» без фразы про фото — висит, заменяется общим вопросом
        bubbles = [_ORPHAN_LA_RE.sub("", b).strip() for b in bubbles]
        bubbles = [b for b in bubbles if b and _words(b)]
        if not any(memory.pregunta_slot("photo", b) for b in bubbles):
            bubbles.append(PHOTO_REQUEST)
    out = []
    for b in bubbles:
        if memory.pregunta_slot("photo", b) or re.search(r"\bfoto", b, re.IGNORECASE) \
                or _PHOTO_WHY_RE.search(b):
            b = _PHOTO_WHY_RE.sub("", b)
            # «Es para completar tu registro.» → без огрызка «Es.»
            b = _drop_sentences(b, lambda x: not _content_stems(x) and "?" not in x)
            b = re.sub(r"^\s*(?:s[oó]lo\s+)?(?:me\s+)?falta\s+una\s+foto\s+tuya\s*[.,]?\s*", "", b,
                       flags=re.IGNORECASE)
        out.append(b.strip())
    text = " ".join(out)
    if not re.search(r"\bfoto", text, re.IGNORECASE):
        out = [_ORPHAN_LA_RE.sub(PHOTO_REQUEST, b) for b in out]
    out = [b for b in out if b and _words(b)]
    buying = wants_to_buy(user_text, mem.get("ultimo_mensaje_anna"))
    nxt = cal.get("siguiente")
    if buying and cal["etapa"] == "calificacion" and nxt in ("name", "is_single", "photo"):
        text = " ".join(out)
        if not re.search(r"en\s+l[ií]nea|p[aá]gina", text, re.IGNORECASE):
            out.insert(0, BUY_ONLINE_SENTENCE)
        # До ссылки не хватает имени и «холост ли» — спрашиваем их (фото для покупки не нужно).
        question = slot_question({**cal, "puede_preguntar": True}) if nxt != "photo" else PHOTO_REQUEST
        if question and not memory.pregunta_slot(nxt, text) and \
                not memory.pregunta_slot(nxt, mem.get("ultimo_mensaje_anna")):
            out.append(question)
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


_INTEREST_RE = re.compile(r"\bme\s+interes|\bquiero\s+(?:saber|info|ir|asistir|participar|conocer)|"
                          r"\binfo\b|informaci[oó]n|\binformes\b|me\s+gustar[ií]a\s+(?:ir|saber|asistir)|"
                          r"\bcu[eé]ntame\b", re.IGNORECASE)


_NO_INTEREST_RE = re.compile(r"\b(?:no|ya\s+no|ni|sin)\s+(?:me\s+interes|necesito\s+info|quiero\s+(?:saber|info)|"
                             r"info|informaci)", re.IGNORECASE)


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
    # Лид выразил интерес или попросил информацию — описание из базы не срезаем (прогон 2026-10-23:
    # «Me interesa el evento» → пустое «¡Qué gusto que te interese!»).
    asked = temas_preguntados(user_text)
    interes = bool(_INTEREST_RE.search(user_text or "")) and not _NO_INTEREST_RE.search(user_text or "")
    broad = interes or bool(_BROAD_ASK_RE.search(user_text or "")
                            or memory._PRICE_OBJECTION_RE.search(user_text or ""))
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


_SLOT_QUESTIONS_CORTAS = {"profession": "¿Y a qué te dedicas?", "age": "¿Y cuántos años tienes?",
                          "is_single": "¿Y estás soltero?", "name": "¿Y cómo te llamas?"}


def slot_question(cal: dict) -> str | None:
    """Готовый вопрос по следующему слоту (имя и «холост ли» вместе, возраст и профессия
    вместе), если его сейчас можно задать. None — спрашивать нечего или не время. Лид ответил на
    часть пары — недостающая часть коротко: «¿Y a qué te dedicas?» (решение 2026-10-23)."""
    nxt = cal.get("siguiente")
    if not nxt or not cal.get("puede_preguntar"):
        return None
    if cal.get("pregunta_corta"):
        return _SLOT_QUESTIONS_CORTAS.get(nxt)
    pair = {"name": ("name", "is_single"), "is_single": ("name", "is_single"),
            "age": ("age", "profession"), "profession": ("age", "profession")}.get(nxt, (nxt,))
    tenemos = cal.get("tenemos") or {}
    missing = tuple(s for s in pair if not (tenemos.get(s) is True if s == "is_single" else tenemos.get(s)))
    return _SLOT_QUESTIONS.get(missing or (nxt,))


_SLOT_PAIR = {"name": ("name", "is_single"), "is_single": ("name", "is_single"),
              "age": ("age", "profession"), "profession": ("age", "profession")}


def slots_permitidos(cal: dict) -> frozenset:
    """О каких слотах сейчас можно спросить: следующий (с парой), если вопрос сейчас уместен.
    Слот, ответ на который уже есть, — нет («¿Cuántos años…?» лиду, назвавшему возраст)."""
    nxt = cal.get("siguiente")
    if not nxt or not cal.get("puede_preguntar"):
        return frozenset()
    tenemos = cal.get("tenemos") or {}
    return frozenset(s for s in _SLOT_PAIR.get(nxt, (nxt,))
                     if not (tenemos.get(s) is True if s == "is_single" else tenemos.get(s)))


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
    asked = {s for s in memory.SLOT_QUESTION_RE if memory.pregunta_slot(s, texto)}
    # только то, что сейчас можно спросить; про известное — нет (вместо него — вопрос кодом)
    return texto if asked and asked <= slots_permitidos(cal) else None


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
                            r"(?:algo\s+)?m[aá]s\b|"
                            # «¿Tienes alguna duda sobre el evento o necesitas ayuda para comprar tu
                            # boleto?» — вопрос-предложение, о чём лид не спрашивал (прогон 2026-10-22)
                            r"¿\s*tienes\s+(?:alguna\s+)?(?:otra\s+)?(?:duda|pregunta)|"
                            r"¿[^?]*\bnecesitas\s+ayuda\s+(?:para|con)\b", re.IGNORECASE)
_DANGLING_RE = re.compile(r"[,:;—–-]\s*$")


# «Es requisito para registrarte al evento.» — объяснение, зачем фото или данные (владелица: без него).
_REQUISITO_RE = re.compile(r"\brequisito\b|para\s+(?:tu\s+)?regist|para\s+completar\s+tu", re.IGNORECASE)


def drop_offer_more(bubbles: list[str]) -> list[str]:
    out = []
    for b in bubbles:
        # вопрос-предложение вырезаем от «¿» до «?», не задевая остальное («¡Gracias! 😊 ¿…?»)
        b = re.sub(r"\s*¿[^?¿]*\?", lambda m: "" if _OFFER_MORE_RE.search(m.group(0)) else m.group(0), b)
        b = _drop_sentences(b, lambda s: bool(_OFFER_MORE_RE.search(s) or _REQUISITO_RE.search(s)))
        out.append(b.strip())
    return [b for b in out if b and _words(b)]


def drop_dangling(bubbles: list[str]) -> list[str]:
    """Пузырь оборван на запятой («…emocionante. Volviendo al evento,»: вопрос модель унесла
    в paso_extra) — хвост после последнего законченного предложения убираем."""
    out = []
    for b in bubbles:
        if _DANGLING_RE.search(b):
            ends = [m.end() for m in re.finditer(r"[.!?]", b)]
            if ends:
                b = b[:ends[-1]]
            else:
                # «Jajaja, no lo vi, volviendo al evento,» → «Jajaja, no lo vi.»
                parts = [p for p in re.split(r"\s*,\s*", b.rstrip(" ,:;—–-")) if p.strip()]
                b = (", ".join(parts[:-1]) + ".") if len(parts) > 1 else ""
        if b.strip() and _words(b):
            out.append(b.strip())
    return out


_DATE_YEAR_RE = re.compile(rf"(\b\d{{1,2}}\s+de\s+(?:{memory._MONTHS}))\s+(?:de|del)\s+\d{{4}}\b",
                           re.IGNORECASE)


_END_TIME_Q_RE = re.compile(r"\btermina|\bacaba|hasta\s+qu[eé]\s+hora|cu[aá]nto\s+dura|"
                            r"a\s+qu[eé]\s+hora\s+(?:sale|se\s+acaba|cierra)", re.IGNORECASE)
_END_TIME_A_RE = re.compile(r"termin|acab|dura|cierr|hasta\s+(?:las|la|el)\b", re.IGNORECASE)


_CALL_RE = re.compile(r"videollamada|video\s*llamada|\bllamada\b|\bzoom\b|google\s+meet|"
                      r"te\s+llamo|platiquemos\s+por\s+tel", re.IGNORECASE)


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
    if permitidos is not None:
        # вопросы не по очереди и про уже известное — до добавления вопроса кодом
        new = drop_out_of_order_slot_questions(trimmed, permitidos)
        if new != trimmed:
            notes.append("убрала вопрос по слоту не по порядку")
            trimmed = new
    new = add_slot_question(trimmed, pregunta_siguiente, user_text, answered)
    if not new:
        notes.append("факт без вопроса лида оставлен: без него ответ пустой")
    else:
        if trimmed != bubbles:
            notes.append("убрала факт без вопроса лида")
        if new != trimmed:
            notes.append("добавила вопрос по следующему слоту")
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
    # Воронка ивента без видеозвонка (решение владелицы 2026-10-23): фразы про звонок убираем.
    if cal.get("ruta") == "evento":
        new = [_drop_sentences(b, lambda x: bool(_CALL_RE.search(x))) for b in bubbles]
        new = [b for b in new if b.strip() and _words(b)]
        if new and new != bubbles:
            notes.append("убрала предложение созвониться (воронка ивента)")
            bubbles = new
    # Эмодзи утверждённой фразы темы ивента (🎃) в лимит не считаются (решение 2026-10-23).
    theme = (settings.get("event_theme_phrase") or "").strip()
    hold = "\u0000THEME\u0000"
    if theme:
        bubbles = [b.replace(theme, hold) for b in bubbles]
    bubbles = keep_last_question(keep_emojis(bubbles))
    if theme:
        bubbles = [b.replace(hold, theme) for b in bubbles]
    # после удаления лишних вопросов могла остаться подводка «Volviendo al evento,» (прогон 6)
    new = drop_dangling(bubbles)
    if new and new != bubbles:
        notes.append("убрала оборванную фразу")
        bubbles = new
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
    r"\bcena\b", r"barra\s+libre", r"alcoh[oó]l", r"estacionamiento|\bvalet\b",
    rf"(?:\d[\d,.]*|\b(?:{'|'.join(_WORD_NUMS)}))\s+(?:de\s+)?(?:mujeres|hombres|personas|chicas|"
    r"invitad[oa]s|asistentes)",
    r"\btermina\b|\bacaba\b|hasta\s+las\b|medianoche", r"garant[ií]a", r"\bcupo\b|\blugares\b")]


# Фраза касается агентства, ивента или услуги (или в ней число/токен). Личная реплика вроде
# «no le voy a ningún equipo» — не утверждение о нас, даже если сверщик её занёс (прогон 5, Д3).
_DOMAIN_RE = re.compile(
    r"\d|\[[a-z_]+\]|evento|agencia|servicio|boleto|precio|cuest|costo|mxn|usd|d[oó]lar|\bpag|"
    r"mujer|chica|hombre|persona|gente|pareja|matchmak|\bcita|noche|fiesta|polanco|cdmx|lugar|"
    r"\bhora|fecha|inclu|bebida|botana|comida|cena|disfra|m[aá]scara|halloween|video|foto|llamada|"
    r"garant|segur|selecci|contrato|reembolso|regist|p[aá]gina|correo|organiz|asist|invitad|"
    r"slavic|eslav|rusa|ucrania|bielorrus|nosotr|aqu[ií]\s+nos|entrada|parking|valet|estaciona|"
    r"descuent|promoci|gratis|cupo|reserv|apart|vestimenta|\bropa\b|c[oó]digo|\btraje|jeans|"
    r"\btarde\b|llegar", re.IGNORECASE)
# Личная реплика Анны («no le voy a ningún equipo», «me gusta ver los partidos»): из списка
# сверщика её не проверяем, если в ней нет темы агентства, ивента, цен и людей.
_PERSONAL_RE = re.compile(r"\b(?:me|mi|yo|disfruto|prefiero|creo|pienso|le\s+voy|vi|veo|s[eé]|"
                          r"estoy|suelo|jaja\w*|seguro\s+(?:estuvo|fue|est[aá]))\b", re.IGNORECASE)


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


class Src(str):
    """Предложение источника с типом записи (event/service/…): тема «N mujeres» из записи
    услуги не подтверждает «N mujeres al evento» (ревью 2026-10-22)."""
    tipo: str = ""


def _src(text: str, tipo: str) -> Src:
    s = Src(text)
    s.tipo = tipo or ""
    return s


_CLAIM_EVENT_RE = re.compile(r"evento|boleto|\bnoche\b|fiesta|asist|\bvan\b", re.IGNORECASE)
_CLAIM_SERVICE_RE = re.compile(r"servicio|selecci|te\s+presento|agencia|trabajo\s+con", re.IGNORECASE)
_PEOPLE_COUNT_RE = re.compile(
    rf"(?:\d[\d,.]*|\b(?:{'|'.join(_WORD_NUMS)}))\s+(?:de\s+)?(?:mujeres|hombres|personas|chicas|"
    r"invitad[oa]s|asistentes)", re.IGNORECASE)


def _rec_topic(rec: dict) -> str:
    """Тема записи: service (услуга, тариф) или event; по id, а не по tipo — service_pitch
    имеет tipo base_script, а говорит об услуге."""
    rid, tipo = rec.get("id") or "", rec.get("tipo") or ""
    if tipo in ("service", "tariff") or rid.startswith(("service", "tariff", "obj_service",
                                                         "greeting_service")):
        return "service"
    if tipo == "event" or rid.startswith(("event", "obj_event", "greeting_event")):
        return "event"
    return tipo


def _same_topic(claim: str, src: str) -> bool:
    tipo = getattr(src, "tipo", "") or ""
    if _CLAIM_EVENT_RE.search(claim) and tipo.startswith("service"):
        return False
    if _CLAIM_SERVICE_RE.search(claim) and tipo == "event":
        return False
    return True


def _fits_num(claim: str, sent: str, min_overlap: float) -> bool:
    """Утверждение с числом: одно предложение источника — числа точно, пары «число + слово»,
    тема и отрицание вместе, без склейки двух фраз («evento número 30» + «3,000 mujeres» ≠
    «Van 30 mujeres al evento»). Слова — значимые основы (без «ahora», «pero»…)."""
    if not numbers_in(claim) <= numbers_in(sent) or _neg(claim) != _neg(sent):
        return False
    if not num_pairs(claim) <= num_pairs(sent) or not _same_topic(claim, sent):
        return False
    words = _content_stems(claim)
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
    if numbers_in(claim):
        if any(_fits_num(claim, sent, 0.6) for sent in sources):
            return True
        if respaldo and _stems(respaldo):
            return any(len(_stems(respaldo) & _stems(sent)) >= 0.8 * min(len(_stems(respaldo)),
                                                                          len(_stems(sent)) or 1)
                       and _fits_num(claim, sent, 0.5) for sent in sources)
        return False
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
                and num_pairs(sent) <= num_pairs(src) and _same_topic(sent, src) \
                and _neg(sent) == _neg(src) and len(stems & _stems(src)) * 4 >= len(stems):
            return True
    if _PEOPLE_COUNT_RE.search(sent):
        return False        # «N mujeres/personas» — только одним предложением, без склейки
    if not any(all(rx.search(src) for rx in topics) for src in sources):
        return False        # темы («estacionamiento», «tarjeta») нет ни в одной фразе базы
    return claim_supported(sent, sources)


def fact_sources(recs: list[dict], card: dict | None, settings: dict) -> list[str]:
    """Источники фактов — предложения ТОЛЬКО подтверждённых записей базы (knowledge.usable)
    и карточки ивента. Прошлые ответы бота и слова лида — никогда: у старых лидов там выдумки
    legacy («puedes pagar en OXXO», «30 mujeres») — решение владелицы 2026-10-22."""
    out: list[str] = []
    for r in recs:
        txt = r.get("contenido_es") or ""
        tipo = _rec_topic(r)
        for variant in (txt, render_tokens(txt, settings)) if txt else ():
            for sent in (x for part in variant.split("\n") for x in _sentences(part)):
                out.append(_src(sent, tipo))
                # части фразы: «…puedes venir disfrazado y, si no tienes disfraz, …» — отрицание
                # второй части не должно мешать подтвердить первую (повторное ревью 2026-10-22)
                out += [_src(c.strip(), tipo) for c in _clauses(sent) if c.strip() != sent and _stems(c)]
    if card:
        out += [_src(render_tokens(f"{k}: {v}", settings), "event") for k, v in card.items()
                if k != "valores"]
        out += [_src(f"{k}: {v}", "event") for k, v in (card.get("valores") or {}).items()]
        if (card.get("valores") or {}).get("event_date"):
            out.append(_src(sender._fmt_date_es(card["valores"]["event_date"]), "event"))
        # Одна фраза со всей карточкой: «El evento Slavic-Latino Connect Night será el 18 de
        # noviembre en Polanco» подтверждается целиком (смоук 2026-10-22).
        out.append(_src(render_tokens(
            f"El evento {card.get('nombre', '')} es y será el {card.get('dia', '')} "
            f"{card.get('fecha', '')}, empieza a las {card.get('inicio', '')}, en "
            f"{card.get('lugar', '')}; cuesta {card.get('precio', '')}.", settings), "event"))
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


def drop_claims(bubbles: list[str], claims: list[str]) -> list[str]:
    """Убрать из ответа фразы с утверждениями без источника; пустые пузыри — тоже."""
    def norm(t):
        return re.sub(r"\s+", " ", _EMOJI_RE.sub("", t or "")).strip(" .!¡¿?").lower()
    bad = [norm(c.split(" (вопрос да/нет")[0]) for c in claims]
    bad = [b for b in bad if b]

    def is_bad(sent):
        # «Sí, Pedro, ya me dijiste…» против «Sí, ya me dijiste…»: вставленное имя — та же фраза
        n = norm(sent)
        return bool(n) and any(b in n or n in b or _fits(b, n, 0.8) for b in bad)
    out = [_drop_sentences(b, is_bad) for b in bubbles]
    return [b for b in out if b.strip() and _words(b) and not is_bad(b)]


def con_sustancia(bubbles: list[str], user_text: str) -> bool:
    """Остаток ответа можно отправить: лид спросил — есть фраза по сути (не «Gracias» и не
    вопрос по слоту); лид дал данные — достаточно чего угодно."""
    if not bubbles:
        return False
    if not has_question(user_text):
        return True
    for b in bubbles:
        for sent in _sentences(b):
            if "?" in sent or _THANKS_RE.match(sent) and len(_words(sent)) <= 3:
                continue
            if len(_words(sent)) >= 2 or _TOKEN_RE.search(sent) or re.search(r"\d", sent):
                return True
    return False


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
    out = await _chat_robust(CHECK_MODEL, CHECK_FALLBACK_MODEL, VERIFY_SYSTEM, payload, cost,
                             max_tokens=500, temperature=0)
    items = out.get("afirmaciones")
    if items is None:                       # старый формат ответа
        items = [{"texto": c, "respaldo": None} for c in out.get("sin_fuente") or []]
    sources = fact_sources(recs, card, settings or {})
    claims: list[str] = []
    for it in items:
        it = it if isinstance(it, dict) else {"texto": it, "respaldo": None}
        for sent in _fact_sentences(str(it.get("texto") or ""), name):
            # без темы агентства и ивента пропускаем только личную реплику Анны; остальное
            # (в т.ч. с respaldo=null) сверяем (ревью 2026-10-22)
            if not (_DOMAIN_RE.search(sent) or any(rx.search(sent) for rx in _HARD_FACT_RES)) \
                    and _PERSONAL_RE.search(sent):
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


# Вопрос да/нет лида и короткий ответ «¡Sí!» / «Claro» / «No.» (ревью 2026-10-22): само «Sí»
# сверке нечего сравнивать, поэтому проверяем утверждение «вопрос лида + ответ».
_YESNO_START_RE = re.compile(r"^\W*(s[ií]|claro|por\s+supuesto|desde\s+luego|no)\b", re.IGNORECASE)
_WH_QUESTION_RE = re.compile(r"^\W*(?:y\s+)?(?:qu[eé]|cu[aá]l|cu[aá]nt|c[oó]mo|d[oó]nde|cu[aá]ndo|"
                             r"qui[eé]n|por\s*qu[eé]|a\s+qu[eé]|de\s+qu[eé]|hasta\s+qu[eé])", re.IGNORECASE)


def yes_no_claim(user_text: str, bubbles: list[str]) -> str | None:
    """Утверждение из вопроса да/нет лида и короткого ответа («¿Hay estacionamiento?» +
    «Sí, hay.» → «Sí hay estacionamiento»). None — не тот случай."""
    if not bubbles:
        return None
    first = (_sentences(bubbles[0]) or [""])[0]
    m = _YESNO_START_RE.match(first)
    if not m or len(_content_stems(first[m.end():])) > 1:
        return None
    for q in _QUESTION_SENT_RE.findall(user_text or ""):
        body = q.strip(" ¿?¡!.,")
        if not body or _WH_QUESTION_RE.match(body):
            continue
        if any(rx.search(body) for rx in _HARD_FACT_RES) or _DOMAIN_RE.search(body):
            negative = m.group(1).lower() == "no"
            return ("No " if negative and not _neg(body) else "") + body
    return None


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


async def _literal_checked(result: dict, lead: dict, fuente: str, meta: dict) -> dict:
    """Готовый ход без модели (приветствие, дословный сценарий, покупка, ход после фото):
    все токены должны подставиться до отправки. Хоть один не подставился — не шлём ничего:
    молчание (main: ручной режим и алерт Ане) и техалерт (ревью 2026-10-22)."""
    problem = await unrendered_tokens((result.get("messages") or []) + (result.get("after_media") or []))
    if problem:
        await _tech_alert(f"токен не подставился ({fuente}): {problem}", lead.get("phone"))
        return _silent(f"токен не подставился ({problem}) — ответь сама",
                       rag_meta={**meta, "fuentes": [fuente]})
    return result


# Три вида молчания (решение Арины 2026-10-23): алерт говорит, какой случай; счёт — в логе.
TECNICO, VERIFICACION, FUERA_DE_BASE = "tecnico", "verificacion", "fuera_de_base"
_KIND_TAG = {TECNICO: "[техническая ошибка]", VERIFICACION: "[проверка не пройдена]",
             FUERA_DE_BASE: "[вне базы]", "filtro": "[не прошёл анкету]"}
_KIND_PREFIX = {TECNICO: "Техническая ошибка", VERIFICACION: "Проверка не пройдена",
                FUERA_DE_BASE: "Вопрос вне базы", "filtro": "Лид не прошёл первый вопрос"}
_KIND_BY_TEXT = [
    (TECNICO, ("сбой", "техническ", "недоступн", "лимит расхода", "токен не подставился")),
    (VERIFICACION, ("утверждения без источника", "числа без источника", "в ответе 5,500",
                    "ссылка не из базы", "после проверок ответ пустой", "проверк")),
]
SILENT_COUNTS: collections.Counter = collections.Counter()


def _kind_of(reason: str) -> str:
    for kind, keys in _KIND_BY_TEXT:
        if any(k in reason for k in keys):
            return kind
    return FUERA_DE_BASE


def _silent(reason: str, kind: str | None = None, **extra) -> dict:
    kind = kind or _kind_of(reason)
    SILENT_COUNTS[kind] += 1
    logger.info("rag silencio tipo=%s motivo=%s | всего: %s", kind, reason, dict(SILENT_COUNTS))
    return _result([], "silent", silent_reason=f"Режим rag {_KIND_TAG[kind]}: {reason}",
                   silent_kind=kind, **extra)


# Алерт «бот промолчал» простым текстом (решение 2026-10-22): одна фраза причины.
UNVERIFIED = "Бот не нашел подтвержденного ответа и ничего не отправил, чтобы не соврать"
FAILURE = "Не получилось составить ответ, лид ничего не получил"
_ALERT_CAUSES = [
    (("утверждения без источника", "числа без источника", "в ответе 5,500", "ссылка не из базы",
      "после проверок ответ пустой"), UNVERIFIED),
    (("policy_anya_promise",), "Лид спрашивает о том, что ты обещала ему лично, а в базе этого нет"),
    (("скидка, способ оплаты или возврат",), "Лид спрашивает про скидку, способ оплаты или возврат, это решаешь ты"),
    (("Лид без фото хочет билет",), "Лид хочет билет, но не прислал фото после двух просьб"),
    (("третий раз подряд не по теме",), "Лид третий раз подряд пишет не по теме"),
    (("время окончания",), "Лид спрашивает, когда заканчивается ивент, а этого нет в базе"),
    (("хочет оплатить переводом",), "Лид хочет оплатить переводом, нужны реквизиты"),
    (("спрашивает про места",), "Лид спрашивает про места"),
    (("второй раз возражает по цене",), "Лид второй раз говорит, что дорого (по цене), реши сама"),
    (("нет данных в базе",), "На этот вопрос в базе нет ответа"),
    (("модель решила передать Ане", "политика молчания"), "Это вопрос, который по правилам решаешь ты"),
    (("дневной лимит",), "Бот исчерпал дневной лимит расходов и сегодня больше не отвечает"),
]


def silent_alert(silent_reason: str | None) -> str:
    """Фраза «Причина: …» для алерта: какой из трёх случаев и что именно. Неизвестное — сбой."""
    r = silent_reason or ""
    kind = next((k for k, tag in _KIND_TAG.items() if tag in r), None) or _kind_of(r)
    if kind == "filtro":
        # «Лид не прошёл первый вопрос: «solo curiosidad»» — что ответил лид (решение 2026-10-23)
        said = r.split("лид не прошёл первый вопрос:", 1)[-1].strip()
        return f"{_KIND_PREFIX[kind]}: {said}"
    if kind == TECNICO and "техническая ошибка:" in r:
        detail = r.split("техническая ошибка:", 1)[1].strip()
        return f"{_KIND_PREFIX[kind]}: {detail}. Повторы и запасная модель не помогли, лид ничего не получил"
    phrase = next((p for keys, p in _ALERT_CAUSES if any(k in r for k in keys)), FAILURE)
    return f"{_KIND_PREFIX[kind]}: {phrase}"


def _clean_extracted(ex: dict) -> dict:
    """Типы под колонки leads (прод 2026-10-22: модель вернула age "35" строкой — запись в БД
    упала, возраст не сохранился). age — целое, is_single — bool; непонятное — выкидываем."""
    out = dict(ex)
    if "age" in out:
        m = re.search(r"\d{1,3}", str(out["age"]))
        if m:
            out["age"] = int(m.group(0))
        else:
            out.pop("age")
    if "is_single" in out and not isinstance(out["is_single"], bool):
        v = str(out["is_single"]).strip().lower()
        if v in ("true", "sí", "si", "yes", "1"):
            out["is_single"] = True
        elif v in ("false", "no", "0"):
            out["is_single"] = False
        else:
            out.pop("is_single")
    return out


def _parse_model(raw: dict) -> dict:
    bubbles = raw.get("bubbles") or raw.get("messages") or []
    if not isinstance(bubbles, list):
        bubbles = [str(bubbles)]
    raw_ex = raw.get("extracted") or {}
    extracted = _clean_extracted({k: raw_ex.get(k) for k in ai._EXTRACTED_KEYS if raw_ex.get(k) is not None})
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


# Прямой вопрос о покупке (решение владелицы 2026-10-22): ссылка сразу, фото лида не нужно,
# нужны имя и «холост ли». Пузыри: ссылка, оплата на странице (там же фото и детали), видео
# (если ещё не слали). Без просьбы фото, без вопроса о возрасте, без фото прошлых ивентов.
BUY_LINK = "Puedes comprar tu boleto aquí: [event_link]"
BUY_PAGE = "El pago se hace en la página y ahí mismo ves fotos de eventos pasados y todos los detalles."
BUY_VIDEO_CAPTION = "Te dejo también un video del evento."


def venta_por_compra(lead: dict, mem: dict, user_text: str) -> dict | None:
    """Ход покупки по прямому вопросу. lead — с данными этого сообщения (имя и «холост ли»
    могли прийти сейчас). None — случай не наш: не ивент, ивент выключен, уже купил,
    нет имени или «холост ли»."""
    ya, settings = mem["ya_hecho"], mem["settings"]
    ruta = memory.ruta(lead, user_text)
    # интерес не известен — нужен признак ивента в самом сообщении («¿cómo se paga el servicio?»)
    if ruta == "sin_definir" and not memory._EVENT_WORD_RE.search(user_text or ""):
        return None
    if ruta == "servicio" or ya.get("boleto_comprado") \
            or settings.get("event_active") != "1" or not (settings.get("event_link") or "").strip():
        return None
    if not (lead.get("name") and lead.get("is_single") is True):
        return None
    return _result([BUY_LINK, BUY_PAGE], send_event_video=not ya.get("video_enviado"))


async def _venta_por_compra_lista(result: dict, lead: dict, meta: dict) -> dict:
    checked = await _literal_checked(result, lead, "venta_por_compra", meta)
    if checked is not result:
        return checked
    result["rag_meta"] = {**meta, "fuentes": ["venta_por_compra"]}
    await _attach_media_captions(result, lead)
    if result.get("video_caption"):
        result["video_caption"] = _caption_within_budget(BUY_VIDEO_CAPTION, result["messages"])
    return result


def media_fits(mem: dict) -> bool:
    """Фото прошлых ивентов к месту: лид в последних сообщениях не возражал, не прощался
    и не злился."""
    recent = [t for t in (mem.get("lead_texts") or [])[-3:] if t]
    return not any(memory._PRICE_OBJECTION_RE.search(t) or memory._FAREWELL_RE.search(t)
                   or _ANGER_RE.search(t) or filters.is_aggression(t) for t in recent)


# Подпись к фото — только имя и данные о себе («soy Carlos, 35 años, ingeniero»). Вопрос,
# покупка, спутник, звонок, оплата — уже не «только данные»: отвечает модель (ревью 2026-10-22).
_NOT_SELF_RE = re.compile(r"\?|cu[aá]nt|c[oó]mo|d[oó]nde|cu[aá]ndo|qu[eé]\s|compr|pag|precio|"
                          r"boleto|novia|amig|espos|pareja|llam|escrib|descuent|tarjeta|efectivo",
                          re.IGNORECASE)


def caption_only_self_data(rest: str) -> bool:
    """Подписи нет или в ней только имя и данные о себе — тогда ход после фото собирает код."""
    rest = (rest or "").strip()
    if not rest:
        return True
    return len(rest.split()) <= 12 and not _NOT_SELF_RE.search(rest)


_OBJ_PHRASE_RE = re.compile(
    r"(?:\b(?:qu[eé]|muy|tan|demasiado|est[aá]|es|se\s+me\s+hace|bien|s[uú]per|uf+|ay|pero|"
    r"me\s+parece|lo\s+veo|sale)\s*,?\s*)*(?:" + memory.PRICE_OBJECTION_RE.pattern + r")\w*",
    re.IGNORECASE)
_OBJ_OTHER_Q_RE = re.compile(r"\b(?:cu[aá]nd[oa]|d[oó]nde|c[oó]mo|cu[aá]l|cu[aá]nt[oa]s?|qui[eé]n|"
                             r"hay\s+descuento|incluye)\b", re.IGNORECASE)


# Вопрос (не любое упоминание: «estoy seguro», «trabajo en seguridad») о том, чего нет в базе.
_ASK_SENT_RE = re.compile(
    r"[?¿]|^\s*(?:y\s+|oye,?\s+|hola,?\s+)?(?:qu[eé]|c[oó]mo|cu[aá]nt\w*|cu[aá]l\w*|d[oó]nde|"
    r"qui[eé]n\w*|hay|es|son|quedan?|tienen|habr[aá]|puedo|se\s+puede)\b|"
    r"\b(?:dime|cu[eé]ntame|plat[ií]came|expl[ií]came|(?:quisiera|quiero|me\s+gustar[ií]a)\s+saber)\b",
    re.IGNORECASE)
_PEOPLE = r"(?:invitad\w*|asistent\w*|participant\w*|gente|personas)"
_FUERA_DE_BASE = [
    ("безопасность", re.compile(r"\bes\s+segur[oa]\b|\bqu[eé]\s+tan\s+segur[oa]\b|\bseguridad\b|"
                                r"\bpeligr\w*|\briesgos[oa]?\b", re.IGNORECASE)),
    ("отзывы", re.compile(r"testimoni\w*|\brese[ñn]as?\b|\bopiniones\b|\breferencias\b|"
                          r"\bqui[eé]n(?:es)?\s+(?:ya\s+)?(?:ha|han)\s+(?:ido|asistido)\b", re.IGNORECASE)),
    ("отбор гостей", re.compile(r"\b(?:seleccion\w*|escog\w*|elig\w*|filtr\w*|verific\w*)\b[^.?!]{0,30}\b"
                                + _PEOPLE + r"|\bcualquier(?:a|\s+persona)\s+puede\s+(?:ir|entrar|asistir)\b",
                                re.IGNORECASE)),
    ("остались ли места", re.compile(
        r"\bquedan?\b[^.?!]{0,15}\b(?:lugar|lugares|boletos?|espacios?|cupos?|entradas?)\b|"
        r"\bhay\s+(?:todav[ií]a\s+|a[uú]n\s+)?(?:lugar|lugares|cupo|espacio|boletos?|entradas?)\b|"
        r"\bcupos?\b|\baforo\b|\b(?:lugares|boletos?)\s+disponibles\b|\bagotad[oa]s?\b|\bse\s+agot\w*|"
        r"\bse\s+acabaron\s+(?:los\s+|las\s+)?(?:boletos|lugares|entradas)\b", re.IGNORECASE)),
]


def tema_fuera_de_base(text: str) -> str | None:
    """Тема вопроса, на который в базе нет ответа (тема и вопрос — в одном предложении)."""
    for sent in re.split(r"(?<=[.!?…])\s+|\n+|[,;]?\s+(?=¿)", (text or "").strip()):
        if not _ASK_SENT_RE.search(sent):
            continue
        for tema, rx in _FUERA_DE_BASE:
            if rx.search(sent):
                return tema
    return None


def objecion_precio(lead: dict, mem: dict, user_text: str, by_id: dict) -> list[str] | str | None:
    """Возражение по цене ивента: первое в диалоге — пузыри утверждённого текста
    obj_event_price (дословно, без модели), второе — "segunda" (молча Ане). None — не наш случай:
    нет возражения, путь услуги, или в сообщении есть другой вопрос."""
    text = user_text or ""
    if not memory.es_objecion_precio(text) or memory.ruta(lead, text) == "servicio":
        return None
    # «uf, qué caro», «es caro ¿no?» — не вопрос; «está caro, ¿cuándo es?» — вопрос
    other = _OBJ_PHRASE_RE.sub(" ", text)
    other = re.sub(r"¿?\s*(?:no|verdad|cierto|neta)\s*\?", " ", other, flags=re.IGNORECASE)
    if "?" in other or "¿" in other or _OBJ_OTHER_Q_RE.search(other):
        return None
    previas = [o for o in mem["ya_hecho"].get("objeciones") or []
               if o.get("tipo") == "precio" and o.get("actual", True)]
    if previas:
        return "segunda"
    rec = by_id.get("obj_event_price")
    # дословно — только утверждённая запись с literal (старая запись в БД до импорта seed
    # literal=false: тогда как раньше, через модель)
    if not rec or not rec.get("confirmado") or not rec.get("literal") \
            or not (rec.get("contenido_es") or "").strip():
        return None
    bubbles = [b.strip() for b in rec["contenido_es"].split("\n\n") if b.strip()]
    return keep_emojis(bubbles, per_bubble=MAX_EMOJI_TOTAL)


def venta_tras_foto(lead: dict, mem: dict, by_id: dict, user_text: str,
                    requiere_datos: bool = True) -> dict | None:
    """Ответ на одобренное фото лида ивента, у которого есть имя и «холост ли»: сразу к
    покупке. None — случай не наш (другая ветка, ссылку уже давали, в сообщении есть вопрос)."""
    if "[фото одобрено" not in (user_text or ""):
        return None
    rest = re.sub(r"\[фото одобрено[^\]]*\]", "", user_text).strip()
    ya, settings = mem["ya_hecho"], mem["settings"]
    if not caption_only_self_data(rest):
        return None             # в подписи вопрос или что-то кроме данных о себе — модель
    ruta = memory.ruta(lead, user_text)
    desde_historia = False
    if ruta == "sin_definir" and any(interes_de(t) == "event" for t in (mem.get("lead_texts") or [])[-10:]):
        ruta, desde_historia = "evento", True   # назван раньше, но не сохранился (прод 2026-10-22)
    if ruta != "evento" or ya.get("enlace_enviado") \
            or ya.get("boleto_comprado") or settings.get("event_active") != "1" \
            or not (settings.get("event_link") or "").strip():
        return None
    if requiere_datos and not (lead.get("name") and lead.get("is_single") is True):
        return None
    name = lead.get("name") or ai._plausible_name(lead.get("whatsapp_name"))
    # благодарность по очереди, как у Ани (решение 2026-10-23)
    bubbles = [cuestionario.gracias(ya.get("turnos_bot", 0), name) + " 😊"]
    story = _story_after_photo(by_id, set(ya.get("hechos_dichos") or []))
    if story:
        bubbles.append(story)
    bubbles.append(CTA_LINK)
    # Вопрос — возраст/профессия, если их нет. Фото в ответ на вопрос о возрасте — это ответ
    # лида, а не игнор: спрашиваем последним пузырём (решение 2026-10-22). Больше двух раз
    # одно и то же не спросим — это держит счётчик попыток в calificacion.
    cal = memory.calificacion(lead, ya, user_text, None)
    question = slot_question(cal) if cal.get("siguiente") in ("age", "profession") else None
    return _result(bubbles, funnel_stage="qualified",
                   extracted={"interest": "event"} if desde_historia else {},
                   send_event_video=not ya.get("video_enviado"),
                   send_event_photo=not ya.get("foto_enviada") and media_fits(mem),
                   after_media=[question] if question else [])


def venta_tras_datos(lead: dict, merged: dict, mem: dict, by_id: dict, user_text: str) -> dict | None:
    """Фото уже одобрено раньше, а имя и «холост ли» лид дал только сейчас (прогон 2026-10-23:
    бот только благодарил) — тот же шаг, что после фото: благодарность, ивент одной фразой,
    ссылка, видео и фото прошлых ивентов. Фото не просим, возраст и профессию не спрашиваем."""
    ya, settings = mem["ya_hecho"], mem["settings"]
    if not lead.get("photo_received") or "[фото" in (user_text or ""):
        return None
    if lead.get("name") and lead.get("is_single") is True:
        return None              # данные были и раньше — это не момент «закрыли анкету»
    if not (merged.get("name") and merged.get("is_single") is True):
        return None
    # данные — в самом сообщении лида, а не только в извлечении модели (ревью 2026-10-23)
    text = user_text or ""
    if not lead.get("name") and str(merged["name"]).split()[0].lower() not in text.lower():
        return None
    if lead.get("is_single") is not True and not memory.responde_slot("is_single", text, False):
        return None
    if not caption_only_self_data(user_text or ""):
        return None              # в сообщении ещё и вопрос — пусть отвечает модель
    if memory.ruta(merged, user_text) == "servicio" or ya.get("enlace_enviado") \
            or ya.get("boleto_comprado") or settings.get("event_active") != "1" \
            or not (settings.get("event_link") or "").strip():
        return None
    bubbles = [cuestionario.gracias(ya.get("turnos_bot", 0), merged.get("name")) + " 😊"]
    story = _story_after_photo(by_id, set(ya.get("hechos_dichos") or []))
    if story:
        bubbles.append(story)
    bubbles.append(CTA_LINK)
    # возраст и профессию — только если ещё не спрашивали (правило «один вопрос — один раз»)
    cal = memory.calificacion(merged, ya, user_text, None)
    question = slot_question(cal) if cal.get("siguiente") in ("age", "profession") else None
    return _result(bubbles, funnel_stage="qualified",
                   send_event_video=not ya.get("video_enviado"),
                   send_event_photo=not ya.get("foto_enviada") and media_fits(mem),
                   after_media=[question] if question else [])


PHOTO_FIRST_THANKS = "¡Gracias por tu foto! 😊"
DUDA_OPENER = "Claro, sin problema 🤍"
PAYMENT_QUESTION = "¿Prefieres pagar con tarjeta o con transferencia?"
SEATS_YES = "Todavía hay lugares disponibles 🤍"
_PAY_ASK_RE = re.compile(r"\btarjeta|transferencia|c[oó]mo\s+(?:te\s+|les\s+)?(?:pago|se\s+paga|puedo\s+pagar|"
                         r"lo\s+pago|hago\s+el\s+pago)|forma\s+de\s+pago|m[eé]todos?\s+de\s+pago|"
                         r"d[oó]nde\s+(?:pago|deposito)", re.IGNORECASE)
_SERVICE_WORD_RE = re.compile(r"\bservicio|\bpaquete|membres[ií]a|\bcontrato", re.IGNORECASE)
_PAY_CARD_RE = re.compile(r"\btarjeta|cr[eé]dito|d[eé]bito|\bcard\b", re.IGNORECASE)
_PAY_TRANSFER_RE = re.compile(r"transferencia|transfer|depositar|por\s+dep[oó]sito|"
                              r"hacer\s+(?:un\s+|el\s+)?dep[oó]sito|\bspei\b", re.IGNORECASE)


def pago_paso(lead: dict, mem: dict, user_text: str) -> dict | str | None:
    """Оплата (решение владелицы 2026-10-23): вопрос «¿aceptan tarjeta?», «¿cómo pago?» →
    «¿Prefieres pagar con tarjeta o con transferencia?»; tarjeta — ссылка на билет;
    transferencia — "transfer" (молча Ане, реквизиты только у неё). None — не про это."""
    text = user_text or ""
    if memory.ruta(lead, text) == "servicio" or _SERVICE_WORD_RE.search(text):
        return None                     # оплата услуги — не этот шаг (модель или Аня)
    # другой вопрос в том же сообщении («¿Cómo pago? ¿Y cuántas mujeres van?») — модель
    if any(not _PAY_ASK_RE.search(q) for q in _QUESTION_SENT_RE.findall(text)):
        return None
    asked = PAYMENT_QUESTION in (mem.get("ultimo_mensaje_anna") or "")
    card, transfer = bool(_PAY_CARD_RE.search(text)), bool(_PAY_TRANSFER_RE.search(text))
    if transfer and not card:
        return "transfer"               # перевод (и вопрос «¿puedo hacer transferencia?») — Ане
    if asked or not has_question(text):
        if card and not transfer and (asked or re.search(r"pag|compr|quiero|prefiero|con\s+tarjeta", text, re.IGNORECASE)):
            settings = mem["settings"]
            if settings.get("event_active") != "1" or not (settings.get("event_link") or "").strip():
                return "transfer"          # нет ссылки — решает Аня
            # ссылка — по тем же правилам, что «¿cómo compro?»: нужны имя и «холост»
            compra = venta_por_compra({**lead, "interest": lead.get("interest") or "event"}, mem,
                                      text + " boleto")
            if compra:
                return compra
            ya = mem["ya_hecho"]
            extra = [] if ya.get("cuestionario") is not None else [
                cuestionario.pregunta_compuesta("evento", _foto(lead, text))]
            return _result([BUY_ONLINE_SENTENCE] + extra)
    if has_question(text) and _PAY_ASK_RE.search(text):
        return _result([PAYMENT_QUESTION])
    return None
FILTRO = "filtro"


def _foto(lead: dict, user_text: str) -> bool:
    return bool(lead.get("photo_received")) or "[фото одобрено" in (user_text or "")


def interes_tras_saludo(lead: dict, mem: dict, by_id: dict, user_text: str) -> dict | None:
    """«Hola» → приветствие №1 → «me interesa el evento»: описание ивента (или услуги) и анкета
    одним сообщением. None — не наш случай (анкету уже задавали, есть вопрос, нет интереса)."""
    ya = mem["ya_hecho"]
    if ya.get("cuestionario") is not None or not ya.get("presentacion") or has_question(user_text):
        return None
    if "[фото" in (user_text or "") or ya.get("enlace_enviado"):
        return None
    if any((ya.get("preguntas_calificacion") or {}).get(s) for s in ("name", "is_single", "age",
                                                                     "profession", "photo")):
        return None      # вопросы анкеты уже задавали по одному — анкета целиком их повторила бы
    interes = interes_de(user_text)
    if interes not in ("event", "agency"):
        return None
    rid = "greeting_event" if interes == "event" else "greeting_service"
    if interes == "event" and mem["settings"].get("event_active") != "1":
        return None
    desc = [b for b in _bubbles_of(by_id.get(rid))[1:] if not cuestionario.es_compuesta(b)]
    if not desc:
        return None
    ruta = "evento" if interes == "event" else "servicio"
    return _result(desc + [cuestionario.pregunta_compuesta(ruta, _foto(lead, user_text))],
                   funnel_stage="qualifying", extracted={"interest": interes})


def _datos_cuestionario(lead: dict, r: dict, estado: dict) -> dict:
    datos = {k: (lead.get(k) if lead.get(k) not in (None, "") else r.get(k))
             for k in ("name", "age", "profession")}
    datos["is_single"] = r["is_single"] if r.get("is_single") is not None else lead.get("is_single")
    datos["seria"] = r.get("seria") or estado.get("relacion_seria")
    return datos


def _ruta_cuestionario(lead: dict, user_text: str) -> str:
    return "servicio" if memory.ruta(lead, user_text) == "servicio" else "evento"


def _datos_falta(falta: list[str]) -> list[str]:
    return [f for f in falta if f != "photo"]


def cuestionario_pendiente(lead: dict, mem: dict, user_text: str) -> bool:
    """Анкету задали, а для шага к покупке чего-то не хватает (ссылку ещё не давали)."""
    ya = mem["ya_hecho"]
    if ya.get("cuestionario") is None or ya.get("enlace_enviado") or ya.get("precio_servicio_dado"):
        return False
    ruta = _ruta_cuestionario(lead, user_text)
    datos = _datos_cuestionario(lead, {}, dict(lead.get("rag_estado") or {}))
    return bool(cuestionario.faltantes(datos, ruta, _foto(lead, user_text)))


def _cola_cuestionario(falta: list[str], ya: dict, thanks: str) -> str | None:
    """Что спросить после ответа на анкету (один раз каждое): недостающие данные одним
    сообщением; потом — фото («Y por último, mándame 2 o 3 fotos…»), когда лид ответил."""
    datos = _datos_falta(falta)
    if datos and ya.get("faltante") is None:
        return cuestionario.mensaje_faltante(datos, "", thanks).strip()
    foto_pedida = (ya.get("preguntas_calificacion") or {}).get("photo", 0) >= 1
    if "photo" in falta and not foto_pedida:
        return cuestionario.pedir_fotos(thanks)
    return None


async def cuestionario_paso(lead: dict, mem: dict, by_id: dict, user_text: str,
                            meta: dict) -> dict | None:
    """Ответ лида на анкету. None — не наш случай или лид задал вопрос (тогда модель, а
    недостающее добавит _generate через mem['cuestionario_modo'])."""
    ya = mem["ya_hecho"]
    if not cuestionario_pendiente(lead, mem, user_text):
        return None
    ruta = _ruta_cuestionario(lead, user_text)
    estado = dict(lead.get("rag_estado") or {})
    last = mem.get("ultimo_mensaje_anna") or ""
    r = cuestionario.analizar(user_text, cuestionario.es_compuesta(last) or cuestionario.es_faltante(last))
    if r.get("seria"):
        estado["relacion_seria"] = r["seria"]
    extracted = {k: r[k] for k in ("name", "age", "profession", "is_single")
                 if r.get(k) is not None and lead.get(k) in (None, "")}
    if r.get("is_single") is False:
        extracted["is_single"] = False
    datos = _datos_cuestionario(lead, r, estado)
    extra = {"extracted": extracted, "rag_estado": estado}
    rest = re.sub(r"\[фото[^\]]*\]", "", user_text or "").strip()
    # явный отказ от серьёзных отношений или женат — молча Ане (решение владелицы 2026-10-23)
    if (ruta != "servicio" and r.get("seria") == "no") or r.get("is_single") is False:
        return _silent(f"лид не прошёл первый вопрос: «{rest[:200]}»", kind=FILTRO,
                       rag_meta={**meta, "fuentes": ["cuestionario"]}, **extra)
    age = datos.get("age")
    if isinstance(age, int) and not (MIN_AGE <= age <= MAX_AGE):
        reply = await _scenario_reply(7 if age < MIN_AGE else 8) or _result(
            list(ai._AGE_OUT_OF_RANGE_BUBBLES), "block")
        reply.update(rag_meta={**meta, "fuentes": ["cuestionario"]}, **extra)
        return reply
    foto = _foto(lead, user_text)
    falta = cuestionario.faltantes(datos, ruta, foto)
    if ruta != "servicio" and r.get("seria") == "duda" and not has_question(rest):
        # «solo curiosidad», «no sé»: тепло, информация об ивенте, один мягкий возврат к анкете
        bubbles = [DUDA_OPENER]
        info = " ".join(b for b in (_bubbles_of(by_id.get("event_what")) +
                                    _bubbles_of(by_id.get("event_date_time"))) if b)
        if info:
            bubbles.append(info)
        if _datos_falta(falta) and ya.get("faltante") is None:
            back = cuestionario.mensaje_faltante(_datos_falta(falta), ruta, "").strip()
            bubbles.append("Si te animas, " + back[0].lower() + back[1:])
        literal = _result(bubbles, funnel_stage="qualifying",
                          rag_meta={**meta, "fuentes": ["cuestionario", "event_what"], "literal": True},
                          **extra)
        return await _literal_checked(literal, lead, "cuestionario", meta)
    if has_question(rest):
        # вопрос лида — отвечает модель по базе; недостающее — к её ответу, один раз
        mem["cuestionario_modo"] = {"ruta": ruta, "falta": falta, "extra": extra,
                                    "pedir": _cola_cuestionario(falta, ya, "")}
        return None
    merged = {**lead, **{k: v for k, v in extracted.items()}}
    thanks = cuestionario.gracias(ya.get("turnos_bot", 0), datos.get("name"))
    # к покупке: холост, имя, фото. Про серьёзные отношения не ответил — не блок (спросили один
    # раз в «Me falta saber»); «no» или неясно — уже молчание выше.
    # как раньше для ивента: имя и «холост» после двух вопросов (анкета и «Me falta saber») не
    # блокируют — «casado» уже молчание выше
    tries = ya.get("preguntas_calificacion") or {}
    single_ok = datos.get("is_single") is True or tries.get("is_single", 0) >= memory.MAX_SLOT_TRIES
    name_ok = bool(datos.get("name")) or tries.get("name", 0) >= memory.MAX_SLOT_TRIES
    if foto and single_ok and name_ok:
        if ruta == "servicio":
            rec = by_id.get("service_pitch")
            if rec and rec.get("literal"):
                pitch = _result(_bubbles_of(rec), funnel_stage="pitched",
                                rag_meta={**meta, "fuentes": ["service_pitch"], "literal": True}, **extra)
                return await _literal_checked(pitch, lead, "service_pitch", meta)
        else:
            venta = venta_tras_foto({**merged, "photo_received": True}, mem, by_id,
                                    user_text if "[фото одобрено" in (user_text or "")
                                    else "[фото одобрено]", requiere_datos=False)
            if venta:
                venta["after_media"] = []           # анкету уже задали целиком — не переспрашиваем
                venta.update(extra)
                venta["extracted"] = {**venta.get("extracted", {}), **extracted}
                checked = await _literal_checked(venta, lead, "venta_tras_foto", meta)
                if checked is not venta:
                    return checked
                venta["rag_meta"] = {**meta, "fuentes": ["venta_tras_foto", "cuestionario"]}
                await _attach_media_captions(venta, lead)
                return venta
    cola = _cola_cuestionario(falta, ya, thanks)
    if cola:
        bubbles = [cola]
    elif extracted or r.get("seria"):
        bubbles = [thanks + " 😊"]           # дал новые данные — коротко спасибо
    else:
        # ничего нового («ok», «jaja»): не крутим «gracias», отвечает модель (без вопросов анкеты)
        mem["cuestionario_modo"] = {"ruta": ruta, "falta": falta, "extra": extra, "pedir": None}
        return None
    literal = _result(bubbles, funnel_stage="qualifying",
                      rag_meta={**meta, "fuentes": ["cuestionario"], "literal": True}, **extra)
    return await _literal_checked(literal, lead, "cuestionario", meta)


# Голое приветствие без вопроса («hola», «buenas», «buenos días»): ответ всегда готовым текстом
# без модели, тишины не бывает (решение 2026-10-22: при выключенном ивенте модель на «Hola»
# сочинила описание агентства, проверка его отбросила, лид остался без ответа).
_BARE_GREETING_RE = re.compile(
    r"^\W*(?:(?:hola+|holi|buenas|buen[oa]s?\s+(?:d[ií]as?|tardes|noches)|buen\s+d[ií]a|"
    r"qu[eé]\s+tal|hey|saludos|que\s+onda|ana|anna|"
    # английский и русский — ответ всё равно на испанском (2026-10-22)
    r"hello|hi|good\s+(?:morning|afternoon|evening)|"
    r"привет\w*|здравствуй\w*|добр(?:ый|ое)\s+(?:день|утро|вечер))\W*)+$", re.IGNORECASE)

# Приветствие на первое «hola» — сценарий №1 дословно (greeting_hola). Пока ивент включён и
# его дата не прошла (по Мехико), в конец абзаца про ивент добавляется одна фраза (решение
# владелицы 2026-10-22). Сценарий №1 в БД не меняется.
HALLOWEEN_PHRASE = "Esta vez es temática de Halloween 🎃"
_EVENT_PARAGRAPH_END = "una relación seria."


def halloween_activo(settings: dict) -> bool:
    if settings.get("event_active") != "1":
        return False
    return (settings.get("event_date") or "") >= knowledge.today_cdmx()


def greeting_hola_bubbles(rec: dict | None, settings: dict) -> list[str]:
    bubbles = _bubbles_of(rec)
    if not halloween_activo(settings):
        return bubbles
    out, added = [], False
    for b in bubbles:
        if not added and "evento" in b and b.endswith(_EVENT_PARAGRAPH_END):
            b, added = f"{b} {HALLOWEEN_PHRASE}", True
        out.append(b)
    return out


def is_bare_greeting(text: str) -> bool:
    return bool(_BARE_GREETING_RE.match((text or "").strip())) and "?" not in (text or "")


def _bubbles_of(rec: dict | None) -> list[str]:
    return [b.strip() for b in ((rec or {}).get("contenido_es") or "").split("\n\n") if b.strip()]


def saludo_fijo(user_text: str, mem: dict, by_id: dict) -> list[str] | None:
    """Ответ на голое приветствие из готовых текстов базы. None — не приветствие или
    подставить нечего (тогда — модель).
    - Анна ещё не представлялась и автоприветствия не было: greeting_hola (сценарий №1)
      и фраза про Хеллоуин, если ивент включён и не прошёл.
    - Иначе «¡Hola! 🤍» и вопрос по следующему слоту анкеты, если его сейчас можно задать."""
    if not is_bare_greeting(user_text):
        return None
    ya = mem["ya_hecho"]
    if not ya.get("presentacion") and not ya.get("auto_saludo"):
        return greeting_hola_bubbles(by_id.get("greeting_hola"), mem["settings"]) or None
    hola = (_bubbles_of(by_id.get("greeting_hola")) or [None])[0]
    question = slot_question(mem["calificacion"])
    return [hola, question] if hola and question else None


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
    if mem.get("cuestionario_modo"):
        # анкету задали целиком: отдельных вопросов по слотам нет, недостающее добавит _generate
        cal_next = {**cal_next, "puede_preguntar": False}
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


async def over_hard_limit(phone: str | None) -> float | None:
    """Расход за сутки превысил жёсткий лимит → сумма, иначе None. Сбой чтения — не блокируем."""
    try:
        raw = (await db.get_settings([HARD_LIMIT_KEY])).get(HARD_LIMIT_KEY)
        limit = float(raw) if raw and raw.strip() else HARD_LIMIT_DEFAULT_USD
        spent = await db.get_daily_amount(COST_COUNTER)
    except Exception:
        logger.exception("rag: не прочитала дневной лимит расхода")
        return None
    if spent < limit:
        return None
    day = knowledge.today_cdmx()
    if _hard_limit_alerted["day"] != day:
        _hard_limit_alerted["day"] = day
        await _tech_alert(f"режим rag: расход за сутки ${spent:.2f} ≥ лимита ${limit:.2f} — "
                          f"OpenAI не зову до завтра, лиды уходят Ане", phone)
    return spent


async def generate_reply(lead: dict, history: list[dict], user_text: str) -> dict:
    """Ответ в режиме rag. Контракт результата тот же, что у ai.generate_reply, плюс
    silent_reason (текст алерта Ане) и rag_estado (сохраняет main)."""
    prompt = load_prompt()          # нет промпта → NotImplementedError → legacy
    cost = Cost()
    lead = dict(lead or {})
    spent = await over_hard_limit(lead.get("phone"))
    if spent is not None:
        result = _silent(f"дневной лимит расхода OpenAI исчерпан (${spent:.2f}) — ответь сама")
        result["rag_meta"] = {"usd": 0.0, "tokens": {}, "fuentes": ["hard_limit"]}
        return result
    try:
        result = await _generate(lead, user_text, prompt, cost)
    except Exception as e:
        logger.exception("rag: сбой режима")
        await _tech_alert(repr(e), lead.get("phone"))
        result = _silent(f"техническая ошибка: {motivo_tecnico(e)}", kind=TECNICO)
    finally:
        await _record_daily_cost(cost)
    interes_en_extracted(result, lead, user_text)
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
        return await _literal_checked(reply, lead, f"scenario:{top['id']}", meta)

    # 2. base_script при точном совпадении ситуации.
    recs = knowledge.usable(await knowledge.records(), settings, knowledge.today_cdmx())
    by_id = {r["id"]: r for r in recs}
    for rid in CODE_LITERAL_IDS:
        rec = by_id.get(rid)
        if rec and rec.get("literal") and situacion_ok(rec.get("situacion") or {}, mem,
                                                        user_text, lead):
            stage = "pitched" if rid == "service_pitch" else "qualifying"
            interest = {"greeting_event": "event", "greeting_service": "agency"}.get(rid)
            text = (greeting_hola_bubbles(rec, settings) if rid == "greeting_hola"
                    else [b.strip() for b in rec["contenido_es"].split("\n\n") if b.strip()])
            foto = bool(lead.get("photo_received")) or "[фото одобрено" in (user_text or "")
            if rid == "greeting_hola" and "[фото" in (user_text or ""):
                # первым сообщением прислал фото — сначала спасибо (решение 2026-10-23)
                text = [PHOTO_FIRST_THANKS] + text
            if rid in ("greeting_event", "greeting_service"):
                # анкета одним сообщением; фото уже есть — без просьбы о фото
                ruta_q = "servicio" if rid == "greeting_service" else "evento"
                text = [cuestionario.pregunta_compuesta(ruta_q, foto) if cuestionario.es_compuesta(b)
                        else b for b in text]
            literal = _result(text,
                              funnel_stage=stage, extracted={"interest": interest} if interest else {},
                              rag_meta={**meta, "fuentes": [rid], "literal": True})
            return await _literal_checked(literal, lead, rid, meta)

    # 2.1. Голое приветствие — готовый текст без модели, никогда не тишина.
    saludo = saludo_fijo(user_text, mem, by_id)
    if saludo:
        literal = _result(saludo, funnel_stage="qualifying",
                          rag_meta={**meta, "fuentes": ["saludo_fijo"], "literal": True})
        return await _literal_checked(literal, lead, "saludo_fijo", meta)

    # 2.2. Только вежливый вопрос — коротко и тепло, дальше вопрос по слоту, если его ещё можно
    # задать (правило «один вопрос — один раз»), без модели.
    if es_cortesia(user_text):
        q = slot_question(cal)
        literal = _result([COURTESY_REPLY] + ([q] if q else []),
                          rag_meta={**meta, "fuentes": ["cortesia"], "literal": True})
        return await _literal_checked(literal, lead, "cortesia", meta)

    # 2.3. «Ya te dije», а спрашивать больше нечего — извиниться, не молчать (решение 2026-10-23).
    if memory.YA_TE_DIJE_RE.search(user_text or "") and not has_question(user_text) \
            and not slot_question(cal) and not cuestionario_pendiente(lead, mem, user_text):
        literal = _result([cuestionario.YA_TE_DIJE_REPLY],
                          rag_meta={**meta, "fuentes": ["ya_te_dije"], "literal": True})
        return await _literal_checked(literal, lead, "ya_te_dije", meta)

    # 2.4. Интерес к ивенту или услуге после приветствия («Hola» → «me interesa el evento»):
    # описание и анкета одним сообщением, как в первом ответе (решение 2026-10-23).
    interes_paso = interes_tras_saludo(lead, mem, by_id, user_text)
    if interes_paso:
        interes_paso["rag_meta"] = {**meta, "fuentes": ["cuestionario"], "literal": True}
        return await _literal_checked(interes_paso, lead, "cuestionario", meta)

    # 2.45. Оплата: «¿tarjeta o transferencia?»; tarjeta — ссылка; transferencia — Ане.
    pago = pago_paso(lead, mem, user_text)
    if pago == "transfer":
        return _silent("лид хочет оплатить переводом, нужны реквизиты", kind=FUERA_DE_BASE,
                       rag_meta={**meta, "fuentes": ["pago_transferencia"]})
    if pago is not None:
        if BUY_LINK in pago["messages"]:
            return await _venta_por_compra_lista(pago, lead, meta)
        pago["rag_meta"] = {**meta, "fuentes": ["pago_pregunta"], "literal": True}
        return await _literal_checked(pago, lead, "pago_pregunta", meta)

    # 2.5. Скидки, наличные, перевод, возврат — решение владелицы: молча Ане.
    if _SILENT_TOPIC_RE.search(user_text or ""):
        return _silent("скидка, способ оплаты или возврат (решение владелицы)",
                       rag_meta={**meta, "fuentes": ["policy_silent_topic"]})
    # 2.51. Безопасность, отзывы, отбор гостей, остались ли места — в базе этого нет: молча Ане
    # до модели (прогон 2026-10-23: модель уходила от «¿es seguro?» и выдумывала «sí, hay lugares»).
    tema = tema_fuera_de_base(user_text)
    if tema == "остались ли места":
        # места: «Todavía hay lugares» только при app_settings.event_seats_available='1'
        if settings.get("event_seats_available") == "1":
            literal = _result([SEATS_YES], rag_meta={**meta, "fuentes": ["event_seats"], "literal": True})
            return await _literal_checked(literal, lead, "event_seats", meta)
        return _silent("лид спрашивает про места", kind=FUERA_DE_BASE,
                       rag_meta={**meta, "fuentes": ["policy_fuera_de_base"]})
    if tema:
        return _silent(f"нет данных в базе знаний: {tema}", kind=FUERA_DE_BASE,
                       rag_meta={**meta, "fuentes": ["policy_fuera_de_base"]})

    # 2.52. «Está muy caro» (ивент): первый раз — утверждённый текст владелицы дословно, второй —
    # молча Ане (решение 2026-10-23). Возражение плюс другой вопрос — модель.
    objecion = objecion_precio(lead, mem, user_text, by_id)
    if objecion is not None:
        if objecion == "segunda":
            return _silent("лид второй раз возражает по цене — реши сама", kind=FUERA_DE_BASE,
                           rag_meta={**meta, "fuentes": ["obj_event_price"]})
        literal = _result(objecion, rag_meta={**meta, "fuentes": ["obj_event_price"], "literal": True})
        return await _literal_checked(literal, lead, "obj_event_price", meta)

    # 2.55. Прямой вопрос о покупке в этом сообщении, имя и «холост ли» есть — ссылка сразу,
    # без фото (решение владелицы 2026-10-22). Давний «¿y cómo pago?» в переписке не считается:
    # иначе ссылка ушла бы в ответ на «¿cuántas mujeres van?».
    if wants_to_buy(user_text, mem["ultimo_mensaje_anna"]) and solo_compra(user_text):
        compra = venta_por_compra(lead, mem, user_text)
        if compra:
            return await _venta_por_compra_lista(compra, lead, meta)

    # 2.6. Ивент: фото не прислал после двух просьб — ссылки без фото нет, решает Аня.
    if cal.get("foto_agotada") and cal["etapa"] == "calificacion":
        return _silent("Лид без фото хочет билет, реши сама (две просьбы прислать фото без ответа)",
                       rag_meta={**meta, "fuentes": ["foto_agotada"]})

    # 2.65. Ответ на анкету (cuestionario): разбор кодом, недостающее одним сообщением один раз,
    # «no»/«solo curiosidad»/неясно про серьёзные отношения — молча Ане; вопрос лида — модель
    # (недостающее добавится к её ответу один раз).
    paso = await cuestionario_paso(lead, mem, by_id, user_text, meta)
    if paso is not None:
        return paso

    # 2.7. Одобренное фото лида ивента с именем и «холост ли» — сразу к покупке (кодом).
    venta = venta_tras_foto(lead, mem, by_id, user_text)
    if venta:
        checked = await _literal_checked(venta, lead, "venta_tras_foto", meta)
        if checked is not venta:
            return checked
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

    # Модель → проверки. Проверка не прошла: отправляем прошедшие пузыри; ничего не прошло —
    # одна повторная попытка с другим поиском в базе, потом молчание (решение 2026-10-23).
    estado = dict(lead.get("rag_estado") or {})
    fallo = None
    for intento in (1, 2):
        try:
            out = await _chat_robust(MODEL, FALLBACK_MODEL, prompt, context, cost, parse=_parse_model)
            if out["falta_dato"] and out["query_extra"]:
                vec2 = (await _embed_batch([out["query_extra"]], cost))[0]
                more = await knowledge.search(vec2, recs, embed_records, top_k=TOP_K)
                seen = {f["id"] for f in fichas}
                fichas = fichas + [f for f in more if f["id"] not in seen]
                context["fichas"] = [knowledge.ficha_for_model(f) for f in fichas]
                context["reintento"] = "Segunda búsqueda con query_extra. Si aún falta el dato: falta_dato=true."
                out = await _chat_robust(MODEL, FALLBACK_MODEL, prompt, context, cost, parse=_parse_model)
        except Exception as e:
            # Сбой после повторов и запасной модели: старый ответ НЕ отправляем — молчание,
            # ручной режим, алерт Ане с причиной и техалерт (решение владелицы 2026-10-22).
            logger.exception("rag: OpenAI/разбор ответа упал после повторов")
            await _tech_alert(repr(e), lead.get("phone"))
            return _silent(f"техническая ошибка: {motivo_tecnico(e)}", kind=TECNICO, rag_meta=meta)

        meta["fuentes"] = out["fuentes"]
        if intento == 1:
            if out["en_tema"]:
                estado["fuera_de_tema_seguidos"] = 0
                estado["regresos"] = 0
            else:
                estado["fuera_de_tema_seguidos"] = int(estado.get("fuera_de_tema_seguidos") or 0) + 1
        extra = {"rag_estado": estado, "extracted": out["extracted"], "rag_meta": meta}

        if out["falta_dato"]:
            return _silent("нет данных в базе знаний", kind=FUERA_DE_BASE, **extra)
        if out["accion"] == "silent":
            return _silent("модель решила передать Ане", kind=FUERA_DE_BASE, **extra)
        silent_ids = {f["id"] for f in fichas if f.get("accion") == "silent_escalate"}
        if silent_ids & set(out["fuentes"]):
            return _silent(f"политика молчания ({', '.join(sorted(silent_ids & set(out['fuentes'])))})",
                           kind=FUERA_DE_BASE, **extra)
        if not out["en_tema"] and intento == 1:
            if estado["fuera_de_tema_seguidos"] >= 3 or int(estado.get("regresos") or 0) >= 2:
                return _silent("лид третий раз подряд не по теме", kind=FUERA_DE_BASE, **extra)
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

        # Фото одобрено раньше, имя и «холост ли» пришли сейчас — шаг к покупке, как после фото.
        venta = venta_tras_datos(lead, merged, mem, by_id, user_text)
        if venta:
            venta.update(extracted=out["extracted"], rag_estado=estado,
                         rag_meta={**meta, "fuentes": ["venta_tras_datos"]})
            checked = await _literal_checked(venta, lead, "venta_tras_datos", meta)
            if checked is not venta:
                return checked
            await _attach_media_captions(venta, lead)
            return venta

        # Купить хотел раньше, имя и «холост ли» пришли сейчас — ссылка сразу после его ответа.
        if mem["compra_pendiente"] and not (lead.get("name") and lead.get("is_single") is True) \
                and "[фото одобрено" not in (user_text or ""):
            compra = venta_por_compra(merged, mem, user_text)
            if compra:
                compra.update(extracted=out["extracted"], rag_estado=estado)
                return await _venta_por_compra_lista(compra, lead, meta)

        bubbles, reason, notes = revisar(out, mem, lead, merged, user_text, fichas)
        if reason:
            # весь ответ отклонён (число, ссылка, 5,500) — оставляем пузыри, прошедшие проверку
            good = [b for b in out["bubbles"]
                    if not revisar({**out, "bubbles": [b], "paso_extra": {}}, mem, lead, merged,
                                   user_text, fichas)[1]]
            if good and len(good) < len(out["bubbles"]):
                bubbles, reason, notes = revisar({**out, "bubbles": good}, mem, lead, merged,
                                                 user_text, fichas)
                notes.append(f"отправлены пузыри, прошедшие проверку ({len(good)} из {len(out['bubbles'])})")
        meta["notas"] = notes
        if reason:
            fallo = (reason, "числа" in reason)
        else:
            if any(_NO_DATA_RE.search(x) for x in bubbles):
                return _silent("нет данных в базе (модель ответила отпиской)", kind=FUERA_DE_BASE, **extra)
            # Спросил, когда заканчивается, а в ответе про окончание ничего (времени конца в базе
            # нет) — уклонение вроде «La hora de inicio es…»: молча Ане (прогон 2026-10-22).
            if _END_TIME_Q_RE.search(user_text or "") and not _END_TIME_A_RE.search(" ".join(bubbles)):
                return _silent("спросил время окончания — в базе его нет", kind=FUERA_DE_BASE, **extra)
            try:
                claims = await verify_claims(bubbles, recs, card, mem["lead_texts"], cost, settings,
                                             merged.get("name") or ai._plausible_name(lead.get("whatsapp_name")))
            except Exception as e:
                logger.exception("rag: проверка второй моделью упала после повторов")
                return _silent(f"техническая ошибка: проверяющая модель недоступна — {motivo_tecnico(e)}",
                               kind=TECNICO, **extra)
            yn = yes_no_claim(user_text, bubbles)
            if yn:
                sources = fact_sources(recs, card, settings)
                hard = any(rx.search(yn) for rx in _HARD_FACT_RES)
                if not (hard_fact_supported(yn, sources) if hard else sentence_supported(yn, sources)):
                    claims = list(claims) + [f"{yn} (вопрос да/нет: «{bubbles[0][:60]}»)"]
            if claims and promesa_de_anya(claims, mem.get("historial") or []):
                return _silent(f"policy_anya_promise: лид спрашивает о том, что обещала Аня, в базе "
                               f"этого нет — реши сама ({claims})", kind=FUERA_DE_BASE, **extra)
            if claims:
                kept = drop_claims(bubbles, claims)
                if con_sustancia(kept, user_text):
                    notes.append(f"убрала утверждения без источника, остальное отправлено: {claims}")
                    bubbles, claims = kept, []
            if not claims:
                break
            fallo = (f"утверждения без источника: {claims}", f"«{claims[0]}»")
        if intento == 1:
            # повторная попытка: поиск по одному тексту лида, шире, и подсказка модели
            logger.info("rag: проверка не прошла (%s) — повторная попытка с другим поиском", fallo[0])
            more = await knowledge.search(vec_user, recs, embed_records, top_k=TOP_K * 2)
            seen = {f["id"] for f in fichas}
            fichas = fichas + [f for f in more if f["id"] not in seen]
            context["fichas"] = [knowledge.ficha_for_model(f) for f in fichas]
            context["reintento"] = (
                "Tu respuesta anterior no pasó la verificación: " + str(fallo[0])[:300] +
                ". Responde solo con datos de las fichas y del evento actual, sin repetir eso. "
                "Si el dato no está: falta_dato=true.")
    else:
        reason, detail = fallo
        return _silent(reason, kind=VERIFICACION,
                       silent_detail=(reason if detail is True else detail or None), **extra)

    # Токены: каждый должен подставиться (нет event_price_next в настройках → не отправляем).
    problem = await unrendered_tokens(bubbles)
    if problem:
        await _tech_alert(f"токен не подставился: {problem}", lead.get("phone"))
        return _silent(f"токен не подставился ({problem}) — ответь сама", kind=TECNICO, **extra)

    modo = mem.get("cuestionario_modo")
    if modo:
        # данные из ответа на анкету (кодом) — к извлечению модели; недостающее — один раз
        out["extracted"] = {**modo["extra"]["extracted"], **{k: v for k, v in out["extracted"].items()
                                                             if v not in (None, "")}}
        estado = {**estado, **modo["extra"]["rag_estado"]}
        if modo["pedir"]:
            bubbles = bubbles + [modo["pedir"]]
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
