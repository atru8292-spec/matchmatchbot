"""AI-ядро бота Anna: RAG по сценариям + генерация ответа (OpenAI) с защитой от галлюцинаций.

System prompt читается из anna_prompt_v2.md (не хардкодим).
RAG: текст лида → эмбеддинг (text-embedding-3-small) → cosine по scenarios.embedding.
Ветки:
  - ai_allowed=false + уверенный матч → template_es ДОСЛОВНО, OpenAI НЕ вызывается
    (блокировки/фикс-ответы: ноль галлюцинаций + экономия токенов).
  - ai_allowed=true (или низкий score) → OpenAI генерит в тоне Anna по образцу.
  - score < FALLBACK_SCORE → в контекст не кладём сомнительный сценарий; промпт сам
    даёт вежливый fallback + видеозвонок без выдумок.
Выход — строго JSON (формат в anna_prompt_v2.md).
Ошибка/таймаут OpenAI → не падаем: fallback-сообщение + escalate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

import db
import funnel
from config import settings

_CDMX = ZoneInfo("America/Mexico_City")
_ES_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_ES_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _ahora_cdmx() -> str:
    """Текущие дата/день недели/время CDMX для AI (парсинг «el jueves»/«mañana»)."""
    n = datetime.now(_CDMX)
    return f"{_ES_DAYS[n.weekday()]} {n.day} de {_ES_MONTHS[n.month - 1]} de {n.year}, {n.strftime('%H:%M')}"

logger = logging.getLogger("matchmatch.ai")

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "anna_prompt_v5.md")
_system_prompt_cache: str | None = None

# Пороги уверенности RAG (score = 1 - cosine_distance):
# - FIXED_BLOCK_SCORE: блокирующий фикс-сценарий (bot_then_block / blocks_lead) отдаём
#   дословно только при ВЫСОКОЙ уверенности — блок необратим (бан навсегда).
#   Ниже порога → в AI (v4 с гибкостью разрулит по контексту).
# - FIXED_SCORE: не-блокирующий фикс-ответ (скидка, "ты бот") — обычный ответ,
#   корректируется в диалоге, порог ниже.
# - FALLBACK_SCORE: ниже — подходящего сценария нет, в контекст AI не кладём.
FIXED_BLOCK_SCORE = 0.60
FIXED_SCORE = 0.45
FALLBACK_SCORE = 0.40
MAX_MESSAGES = 4
_FALLBACK_MESSAGE = "Ahorita te contesto guapo 🤍"

# mode сценария → действие после ответа.
_MODE_TO_ACTION = {
    "bot_auto": "respond",
    "bot_then_block": "block",
    "bot_then_anna": "escalate",
    "to_anna_silent": "escalate",
}


def load_system_prompt() -> str:
    """Прочитать system prompt из файла (кэш на процесс)."""
    global _system_prompt_cache
    if _system_prompt_cache is None:
        with open(_PROMPT_PATH, encoding="utf-8") as f:
            _system_prompt_cache = f.read()
    return _system_prompt_cache


# ===== OpenAI с ретраями (429/5xx/сеть) =====

# Поток лидов: временный 429 (rate limit) или 5xx не должен сразу уводить в fallback.
# Ретраим с экспоненциальным backoff; после исчерпания — пробрасываем (уйдёт в fallback/эскалацию).
OPENAI_MAX_RETRIES = 3
OPENAI_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
OPENAI_BACKOFF_BASE = 1.0  # сек; задержка attempt = base * 2**attempt (1, 2, 4)
OPENAI_MAX_RETRY_AFTER = 60.0  # верхний предел Retry-After (защита от sleep(3600))


def _backoff(attempt: int) -> float:
    return OPENAI_BACKOFF_BASE * (2 ** attempt)


def _retry_after(r: httpx.Response) -> float | None:
    """Retry-After (секунды) из ответа, если валидный и положительный. Кап 60с —
    иначе кривой/огромный заголовок подвесил бы _process_burst и graceful shutdown."""
    v = r.headers.get("retry-after")
    if not v:
        return None
    try:
        val = float(v)
    except ValueError:
        return None
    if val <= 0:
        return None
    return min(val, OPENAI_MAX_RETRY_AFTER)


async def _openai_post(url: str, payload: dict, timeout: float) -> httpx.Response:
    """POST к OpenAI с ретраями на 429/5xx и сетевые сбои. Возвращает успешный Response.

    После OPENAI_MAX_RETRIES безуспешных попыток пробрасывает исключение — вызывающий
    (generate_reply) уводит в fallback, фото-ветка в manual.
    """
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    for attempt in range(OPENAI_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt >= OPENAI_MAX_RETRIES:
                raise
            logger.warning("OpenAI сеть %r — ретрай %d/%d", e, attempt + 1, OPENAI_MAX_RETRIES)
            await asyncio.sleep(_backoff(attempt))
            continue
        if r.status_code in OPENAI_RETRY_STATUSES and attempt < OPENAI_MAX_RETRIES:
            delay = _retry_after(r) or _backoff(attempt)
            logger.warning("OpenAI %d — ретрай %d/%d через %.1fs",
                           r.status_code, attempt + 1, OPENAI_MAX_RETRIES, delay)
            await asyncio.sleep(delay)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("OpenAI: ретраи исчерпаны")  # недостижимо (loop выходит return/raise)


# ===== RAG =====

async def _embed(text: str) -> list[float]:
    """Эмбеддинг текста (для поиска сценария). Испанский текст лида."""
    r = await _openai_post(
        "https://api.openai.com/v1/embeddings",
        {"model": settings.openai_embedding_model, "input": text},
        timeout=30,
    )
    return r.json()["data"][0]["embedding"]


async def search_scenarios(text: str, top_k: int = 3) -> list[dict]:
    """Найти top-K сценариев по косинусной близости к тексту лида.

    Возвращает [{id, template_es, mode, ai_allowed, blocks_lead, score}], score по убыванию.
    Логируем реальные score (первое время следим за порогом).
    """
    vec = await _embed(text)
    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    rows = await db.search_scenarios_by_vector(literal, top_k)
    result = [dict(r) for r in rows]
    if result:
        logger.info(
            "RAG '%s' → %s",
            (text or "")[:50],
            [(r["id"], round(r["score"], 3)) for r in result],
        )
    else:
        logger.info("RAG '%s' → пусто (нет сценариев)", (text or "")[:50])
    return result


# ===== чистые хелперы (тестируются без сети) =====

def _split_template(template_es: str) -> list[str]:
    """Разбить template_es на бабблы по '\\n\\n', обрезать до MAX_MESSAGES."""
    parts = [p.strip() for p in (template_es or "").split("\n\n") if p.strip()]
    return parts[:MAX_MESSAGES]


# Фикс-сценарии деталей ивента (#51 цена/детали, #52 детали без цены) — ai_allowed=false,
# идут в обход OpenAI, поэтому AI не может выставить send_event_video сам. Прикрепляем видео
# на уровне кода: это ровно «детальный вопрос про ивент» из правила медиа. Дедуп — в actions.
_EVENT_DETAIL_SCENARIOS = {51, 52}

# Анонс explainer-видео (Аня лично отвечает на частые вопросы про ивент) — дописывается
# в ПОСЛЕДНИЙ баббл #51/#52, только когда видео реально уйдёт (не слали + пул не пуст).
# Так текст не обещает видео, которого не будет (см. _maybe_announce_event_video).
_EVENT_VIDEO_ANNOUNCE = (
    "Te dejo también un video donde te respondo las dudas más frecuentes "
    "y te explico los detalles del evento con calma 🤍"
)


def _fixed_reply(scenario: dict) -> dict:
    """Ответ по фиксированному сценарию (ai_allowed=false) — template дословно, без OpenAI."""
    mode = scenario.get("mode")
    if scenario.get("blocks_lead"):
        action = "block"
    else:
        action = _MODE_TO_ACTION.get(mode, "respond")
    return {
        "messages": _split_template(scenario.get("template_es", "")),
        "funnel_stage": None,  # стадию решит интеграция (block→lost); фикс её не меняет
        "action": action,
        "extracted": {},
        "needs_escalation": action == "escalate",
        "used_scenario_id": scenario.get("id"),
        # детали ивента (#51/#52) → прикладываем explainer-видео Ани (дедуп по типу в actions)
        "send_event_photo": False,
        "send_event_video": scenario.get("id") in _EVENT_DETAIL_SCENARIOS,
    }


def _fallback_reply() -> dict:
    """Ответ при сбое OpenAI: не молчим, но эскалируем на Аню."""
    return {
        "messages": [_FALLBACK_MESSAGE],
        "funnel_stage": None,
        "action": "escalate",
        "extracted": {},
        "needs_escalation": True,
        "used_scenario_id": None,
    }


# Поля из чат-квалификации + анкеты-в-чате. Анкетные (name/last_name/email/date_of_birth/
# country/business_link/desired_partner_age) AI извлекает при сборе анкеты после питча.
# date_of_birth AI отдаёт строкой ISO — в date конвертирует main перед записью в БД.
_EXTRACTED_KEYS = ("age", "profession", "is_single", "city", "interest",
                   "name", "last_name", "email", "date_of_birth", "country",
                   "business_link", "desired_partner_age")
_VALID_ACTIONS = {"respond", "block", "escalate"}


def _validate_output(data: dict) -> dict:
    """Привести ответ AI к контракту: messages 1-4, валидный action, чистый extracted."""
    if not isinstance(data, dict):
        raise ValueError("ответ AI не dict")

    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages пуст или не список")
    messages = [str(m) for m in messages if str(m).strip()]
    if not messages:
        raise ValueError("messages пуст после чистки")
    if len(messages) > MAX_MESSAGES:
        logger.warning("AI вернул %d сообщений, обрезаю до %d", len(messages), MAX_MESSAGES)
        messages = messages[:MAX_MESSAGES]

    action = data.get("action")
    if action not in _VALID_ACTIONS:
        action = "respond"

    raw_extracted = data.get("extracted") or {}
    extracted = {k: raw_extracted.get(k) for k in _EXTRACTED_KEYS if raw_extracted.get(k) is not None}

    # funnel_stage: AI может вернуть выдуманный код (в промпте список неполный, client_*).
    # Валидируем против реальных стадий, иначе set_funnel_stage бросит ValueError в интеграции.
    raw_stage = data.get("funnel_stage")
    if raw_stage in funnel.FUNNEL_STAGES:
        funnel_stage = raw_stage
    else:
        if raw_stage:
            logger.warning("AI вернул неизвестную funnel_stage %r → None", raw_stage)
        funnel_stage = None

    # used_scenario_id — доверяем AI как есть. Lookup по нему (main._run_ai через
    # db.get_scenario_title) None-безопасен: несуществующий/невалидный id → None → фолбэк.
    return {
        "messages": messages,
        "funnel_stage": funnel_stage,
        "action": action,
        "extracted": extracted,
        "needs_escalation": bool(data.get("needs_escalation")),
        "used_scenario_id": data.get("used_scenario_id"),
        # Блок 13: AI ставит true, когда лид спрашивает детали/локацию ивента —
        # main тогда шлёт картинку-приглашение (если она готова в app_settings).
        "send_invitation": bool(data.get("send_invitation")),
        # Медиа прошлых ивентов — два независимых «инструмента», AI ставит по контексту
        # (критерии в промпте). Дедуп по типу (не слать повторно этому лиду) — в actions/db.
        "send_event_photo": bool(data.get("send_event_photo")),
        "send_event_video": bool(data.get("send_event_video")),
        # #53 автозапись: ISO-время (CDMX), когда лид назвал КОНКРЕТНЫЙ день+час для
        # звонка; иначе None. main запускает booking.resolve_and_book.
        "proposed_videocall_at": (data.get("proposed_videocall_at")
                                  if isinstance(data.get("proposed_videocall_at"), str)
                                  and data.get("proposed_videocall_at").strip() else None),
    }


# Ценовой вопрос: деньги/стоимость/дороговизна. Для funnel-guard холодного лида по №51.
_PRICE_RE = re.compile(
    r"cu[aá]nto\s+(cuesta|sale|es|vale|cobran|cobra|ser[ií]a)|"
    r"\bprecio\b|\bcosto\b|\bcuesta\b|\bvale\b|\bcaro\b|\bcara\b|\bpagar\b|\bpago\b|"
    r"\bpesos\b|\bmxn\b|\bdinero\b|\binversi[oó]n\b|\bmensualidad\b|\bcosta\b|\$",
    re.IGNORECASE,
)


def _is_price_question(text: str) -> bool:
    """Есть ли в сообщении лида ценовой смысл (деньги/стоимость/дорого)."""
    return bool(_PRICE_RE.search(text or ""))


def _last_anna_text(history: list[dict]) -> str | None:
    """Последняя реплика бота (sender='anna') из истории — контекст для RAG-фолбэка."""
    for m in reversed(history or []):
        if m.get("sender") == "anna" and (m.get("text") or "").strip():
            return m["text"]
    return None


# whatsapp_name приходит от лида как есть (профиль WhatsApp) — может быть кириллица,
# эмодзи, ник или что угодно. Показываем AI только похожее на настоящее имя (латиница +
# европейские диакритики), иначе AI обращается по имени только после явного вопроса
# анкеты (nombre completo, anna_prompt_v5.md) — не раньше.
_PLAUSIBLE_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'\-.\s]{1,39}$")


def _plausible_name(name: str | None) -> str | None:
    """whatsapp_name → сам же, если похож на настоящее имя, иначе None."""
    name = (name or "").strip()
    if not name or not _PLAUSIBLE_NAME_RE.match(name):
        return None
    return name


def _build_user_context(lead: dict, history: list[dict], user_text: str,
                        scenarios: list[dict]) -> str:
    """Собрать пользовательский контекст для AI: профиль + история + RAG-сценарии + текст."""
    profile = {k: lead.get(k) for k in
               ("age", "profession", "is_single", "city", "interest", "funnel_stage",
                "photo_received", "whatsapp_name",
                # анкетные поля — чтобы AI спрашивал только недостающее, не повторялся
                "name", "last_name", "email", "date_of_birth", "country",
                "business_link", "desired_partner_age")}
    profile["whatsapp_name"] = _plausible_name(profile.get("whatsapp_name"))
    hist = [{"sender": m.get("sender"), "text": m.get("text")} for m in history[-10:]]
    if scenarios:
        rag = [{"id": s["id"], "mode": s["mode"], "template_es": s["template_es"],
                "score": round(s["score"], 3)} for s in scenarios]
    else:
        rag = "sin escenario claro — responde amable y general, invita a videollamada, sin inventar"

    return json.dumps({
        # Текущее «сейчас» CDMX — чтобы AI парсил относительные даты (el jueves/mañana)
        # правильно; у модели нет доступа к реальному времени без явной передачи.
        "ahora_cdmx": _ahora_cdmx(),
        "lead_profile": profile,
        "conversation_history": hist,
        "rag_scenarios": rag,
        "lead_message": user_text,
    }, ensure_ascii=False, default=str)  # default=str: date_of_birth (date) → строка


async def _call_openai(user_context: str) -> dict:
    """Вызвать OpenAI chat (JSON-режим). Ретраи на 429/5xx; бросает после исчерпания."""
    r = await _openai_post(
        "https://api.openai.com/v1/chat/completions",
        {
            "model": settings.openai_chat_model,
            "temperature": settings.openai_temperature,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": load_system_prompt()},
                {"role": "user", "content": user_context},
            ],
        },
        timeout=60,
    )
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    # Расход токенов — для мониторинга стоимости. cached_tokens: часть prompt,
    # покрытая prompt-кэшем OpenAI (дешевле в 4 раза) — почти весь system prompt.
    usage = data.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    logger.info(
        "OpenAI usage: prompt=%s (cached=%s) completion=%s total=%s",
        usage.get("prompt_tokens"), cached,
        usage.get("completion_tokens"), usage.get("total_tokens"),
    )
    return json.loads(content)


# ===== главная точка входа =====

async def _maybe_announce_event_video(reply: dict, scenario: dict, lead: dict) -> None:
    """Дописать анонс explainer-видео в последний баббл #51/#52 — ЕСЛИ видео реально уйдёт.

    Не обещаем то, что не отправится. Анонс добавляем только когда выполнены ВСЕ условия:
      • action != 'block' — при блоке main шлёт прощальное сообщение и делает return ДО
        диспетча видео (main.py), т.е. видео не уйдёт → анонс в нём был бы ложью. Защищает
        от случая, если #51/#52 когда-либо станет blocks_lead=True (правкой сценария в проде);
      • сценарий из _EVENT_DETAIL_SCENARIOS и send_event_video выставлен;
      • видео этому лиду на ЭТОТ ивент ещё НЕ слали (дедуп по дате, вар. B);
      • в пуле есть активное видео (иначе actions.send_event_video пришлёт 0).
    event_date берём из app_settings — тот же источник, что actions.send_event_video при
    реальной отправке (event_date=None → settings), поэтому проверка и отправка смотрят на
    один и тот же дедуп-маркер. Любой сбой БД → анонс НЕ добавляем (лучше промолчать, чем
    соврать). Мутирует reply["messages"] на месте; лимит бабблов не растёт (дописываем в
    последний через '\\n\\n', render_bubbles по '\\n\\n' не режет).
    """
    if reply.get("action") == "block":
        return  # видео при блоке не уйдёт (main возвращается раньше) — не анонсируем
    if not reply.get("send_event_video") or scenario.get("id") not in _EVENT_DETAIL_SCENARIOS:
        return
    messages = reply.get("messages")
    phone = lead.get("phone")
    if not messages or not phone:
        return
    try:
        s = await db.get_settings(["event_date"])
        event_date = s.get("event_date") or None
        if await db.event_media_sent(phone, "video", event_date):
            return  # уже слали видео на этот ивент — анонс не нужен (текст кончается как есть)
        if not await db.random_event_media("video", 1):
            return  # пул пуст / нет активного видео — не анонсируем то, что не придёт
    except Exception:
        logger.exception("анонс видео #%s: проверка упала — анонс не добавляю", scenario.get("id"))
        return
    messages[-1] = messages[-1] + "\n\n" + _EVENT_VIDEO_ANNOUNCE
    logger.info("анонс explainer-видео дописан в #%s для %s", scenario.get("id"), phone)


async def generate_reply(lead: dict, history: list[dict], user_text: str) -> dict:
    """Сгенерировать ответ бота на склеенный текст лида.

    Возвращает dict контракта: messages, funnel_stage, action, extracted,
    needs_escalation, used_scenario_id. Никогда не бросает — при сбоях fallback.
    """
    lead = lead or {}
    try:
        scenarios = await search_scenarios(user_text)
    except Exception:
        logger.exception("RAG-поиск упал, иду в OpenAI без сценариев")
        scenarios = []

    top = scenarios[0] if scenarios else None

    # Контекст-фолбэк: если голого текста не хватило (нет уверенного матча, top < FALLBACK) —
    # перезапрос с последней репликой Anna из истории («вопрос бота + ответ лида»). Чинит
    # короткие/контекстные ответы («sí soltero», «va», «ok») разом. Самодостаточные сообщения
    # сюда не попадают (у них top уже >= FALLBACK) — то, что матчится, не ломается.
    if top is None or top.get("score", 0) < FALLBACK_SCORE:
        last_bot = _last_anna_text(history)
        if last_bot:
            try:
                ctx = await search_scenarios(f"{last_bot} {user_text}")
            except Exception:
                logger.exception("контекст-фолбэк RAG упал, оставляю bare")
                ctx = []
            if ctx and (top is None or ctx[0].get("score", 0) > top.get("score", 0)):
                logger.info("контекст-фолбэк: bare=%.3f → ctx #%s=%.3f",
                            (top or {}).get("score", 0), ctx[0]["id"], ctx[0]["score"])
                scenarios, top = ctx, ctx[0]

    # Холодному/неквалифицированному лиду (is_single != True) роутим по типу вопроса:
    #   • любой ценовой вопрос (cuánto sale / precio / caro …) → крючок №2 (сначала квалификация);
    #     правило «no precio a lead frío» касается любого ценового вопроса, не только через №51.
    #   • детали ивента без денег (qué incluye / cuéntame …) и RAG=№51 → №52 (детали без цены).
    # Квалифицированный лид получает полный №51 (с ценой) как есть.
    if lead.get("is_single") is not True:
        if _is_price_question(user_text):
            row = await db.get_scenario_row(2)
            if row:
                logger.info("холодный лид + ценовой вопрос → №2 (крючок), был top=%s",
                            top.get("id") if top else None)
                row["score"] = 1.0
                scenarios, top = [row], row
        elif top and top.get("id") == 51:
            row = await db.get_scenario_row(52)
            if row:
                logger.info("холодный лид + №51 → №52 (детали без цены)")
                row["score"] = 1.0
                scenarios, top = [row], row

    # Ветка 1: фиксированный сценарий (ai_allowed=false) → template дословно, без OpenAI.
    # Порог зависит от необратимости: блокировка требует высокой уверенности (0.60),
    # обычный фикс-ответ — ниже (0.45). Ниже порога → уходим в AI.
    if top and not top.get("ai_allowed"):
        is_block = top.get("mode") == "bot_then_block" or top.get("blocks_lead")
        threshold = FIXED_BLOCK_SCORE if is_block else FIXED_SCORE
        if top.get("score", 0) >= threshold:
            logger.info("фикс-сценарий #%s (score=%.3f >= %.2f, block=%s), OpenAI не вызываю",
                        top["id"], top["score"], threshold, is_block)
            reply = _fixed_reply(top)
            await _maybe_announce_event_video(reply, top, lead)
            return reply
        logger.info("фикс-сценарий #%s score=%.3f < %.2f (block=%s) → в AI",
                    top["id"], top["score"], threshold, is_block)

    # Ветка 2/3: AI генерит. При низком score сценарии не передаём (fallback в промпте).
    confident = [s for s in scenarios if s.get("score", 0) >= FALLBACK_SCORE]
    context = _build_user_context(lead, history, user_text, confident)
    try:
        raw = await _call_openai(context)
        result = _validate_output(raw)
    except Exception:
        logger.exception("OpenAI/парсинг упал → fallback + escalate")
        return _fallback_reply()

    # Handoff-сценарии (bot_then_anna) эскалируем детерминированно, не полагаясь на то,
    # что LLM сам вернёт escalate. Через main (escalate → mode='manual') гарантирует, что
    # дальше лид ведётся Аней, а бот не отвечает повторно. Напр. №48 "no puedo ir" →
    # выпадает из завтрашнего check-in №23; также №14/№19/№24/№26/№41/№42/№53.
    if top and top.get("mode") == "bot_then_anna" and top.get("score", 0) >= FALLBACK_SCORE:
        if result["action"] != "escalate":
            logger.info("bot_then_anna #%s → форсирую escalate (LLM вернул %s)",
                        top["id"], result["action"])
        result["action"] = "escalate"
        result["needs_escalation"] = True
        if not result.get("used_scenario_id"):
            result["used_scenario_id"] = top["id"]
    return result
