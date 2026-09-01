"""AI-ядро бота Anna: RAG по сценариям + генерация ответа (OpenAI) с защитой от галлюцинаций.

System prompt читается из anna_prompt_v5.md (не хардкодим).
RAG: текст лида → эмбеддинг (text-embedding-3-small) → cosine по scenarios.embedding,
top_k=5 кандидатов (generate_reply).

Архитектура (рефакторинг 2026-09, ветка refactor/ai-routing-simplify): AI всегда решает
с полным контекстом переписки, читая RAG-кандидатов как референс, а не приказ. Два типа
кода вокруг него:
  - Candidate-pool операции (до вызова OpenAI) — только ФИЛЬТРУЮТ или ДОПОЛНЯЮТ список
    кандидатов, никогда не выбирают победителя за AI: post-event гейт (фильтр),
    augment "novia rusa"/тёплый лид+цена ивента (добавляют кандидата, не заменяют top),
    ambiguity-гейт (топ-1/топ-2 слишком близко → не доверяем детерминированным веткам).
  - Guardrail'ы (после ответа AI) — проверяют небольшой набор жёстких инвариантов и
    при нарушении ЗАМЕНЯЮТ весь ответ или довешивают недостающее ОТДЕЛЬНЫМ бабблом,
    никогда не правят текст AI хирургически: _enforce_service_price_gate (холодному
    нельзя цену $10k), _enforce_link_presence (детали ивента без ссылки на билет),
    _tag_event_interest / _enforce_nurture_stage (funnel_stage/extracted, которые
    нельзя доверить только промпту — model нарушала даже прямые текстовые запреты).
  - _block_candidate_ok — тот же принцип уверенности, что у детерминированной блокировки
    (FIXED_BLOCK_SCORE + не ambiguous), распространён на ЛЮБОЙ blocks_lead-кандидат
    независимо от ai_allowed — не даёт AI повод блокировать лида по случайному RAG-матчу.

Ветки диспетчеризации (score = 1 - cosine_distance):
  - ai_allowed=false + уверенный матч (score >= FIXED_SCORE/FIXED_BLOCK_SCORE, не
    ambiguous) → template_es ДОСЛОВНО, OpenAI НЕ вызывается (ноль галлюцинаций +
    экономия токенов). Сейчас таким остался небольшой хвост сценариев — большинство
    business-critical (цена/детали ивента, крючки, отказы) переведены на ai_allowed=true.
  - иначе → OpenAI генерит в тоне Anna, видя confident-кандидатов (score >= FALLBACK_SCORE)
    как референс.
  - score < FALLBACK_SCORE → в контекст не кладём сомнительный сценарий; промпт сам
    даёт вежливый fallback + видеозвонок без выдумок.
Выход — строго JSON (формат в anna_prompt_v5.md).
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
_FALLBACK_MESSAGE = "Ahorita te contesto 🤍"

# mode сценария → действие после ответа. to_anna_silent → 'silent' (НЕ 'escalate'):
# по замыслу бот вообще ничего не пишет лиду (клиент агентства, Аня ведёт лично),
# 'escalate' всё же отправлял бы messages лиду — нарушение замысла (найдено 2026-08-06).
_MODE_TO_ACTION = {
    "bot_auto": "respond",
    "bot_then_block": "block",
    "bot_then_anna": "escalate",
    "to_anna_silent": "silent",
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

# Сценарии, где ответ сам говорит «жду»/«лист ожидания» — funnel_stage должен стать
# 'nurture', иначе лид остаётся на активной стадии → получает обычный фоллоу-ап через
# 24-48ч, противореча тексту, который только что отправили (найдено 2026-08-06):
#   #17 «no me interesa/no gracias/paso» — явный отказ, а не «жди»
#   #10 «bajo ingreso» — template буквально говорит «lista de espera 6-12 meses», а получал бы
#       догон #36 «sigues soltero?» уже через 24ч — прямое противоречие
# 'nurture' — в NO_FOLLOWUP_STAGES (funnel.py), автодогон не сработает; лид остаётся видимым
# как «лист ожидания», не удаляется. Проставляется гарантированно через
# _enforce_nurture_stage() (см. ниже) — не хардкодом внутри _fixed_reply, единой
# пост-генерационной точкой для фикс- И AI-ветки (тот же паттерн, что у
# _tag_event_interest).
_NURTURE_SCENARIOS = {10, 17}

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
        # 'silent' → [] всегда, независимо от template_es (полная тишина лиду — весь смысл
        # to_anna_silent; не доверяем содержимому template гарантировать пустоту сама по себе).
        "messages": [] if action == "silent" else _split_template(scenario.get("template_es", "")),
        # стадию обычно решит интеграция (block→lost); фикс её не меняет — nurture (#10/#17)
        # дописывает _enforce_nurture_stage() ПОСЛЕ вызова этой функции (см. generate_reply).
        "funnel_stage": None,
        "action": action,
        # Фикс-сценарии (ai_allowed=false) идут в обход OpenAI → extracted обычно {} (некому
        # извлекать поля). interest='event' для #51/#52 дописывает _tag_event_interest()
        # ПОСЛЕ вызова этой функции — единая пост-генерационная точка что для фикс-, что
        # для AI-ветки (см. вызов ниже в generate_reply), а не хардкод здесь.
        "extracted": {},
        "needs_escalation": action in ("escalate", "silent"),
        "used_scenario_id": scenario.get("id"),
        # детали ивента (#51/#52) → прикладываем explainer-видео Ани (дедуп по типу в actions)
        "send_event_photo": False,
        "send_event_video": scenario.get("id") in _EVENT_DETAIL_SCENARIOS,
    }


def _tag_event_interest(result: dict, used: dict | None) -> dict:
    """Если реально использовался сценарий деталей ивента (№51/№52) — фиксируем
    extracted.interest='event' явно, единой пост-генерационной точкой для фикс- И
    AI-ветки. Раньше это было захардкожено только внутри _fixed_reply (регресс
    2026-08-26: фикс-сценарии идут в обход OpenAI, extracted оставался пустым, интерес
    лида терялся — "hola evento" матчил детерминированный №52, а после квалификации+фото
    бот пичил дефолтный сервис без единого упоминания ивента). Теперь применяется
    одинаково независимо от пути — актуально и для AI-ветки после augment №51 в
    тёплый/photo-approved контекст (задачи #4/#7).
    """
    if not used or used.get("id") not in _EVENT_DETAIL_SCENARIOS:
        return result
    result = dict(result)
    result["extracted"] = {**result.get("extracted", {}), "interest": "event"}
    return result


def _enforce_nurture_stage(result: dict, used: dict | None) -> dict:
    """funnel_stage='nurture' для сценариев #10 (bajo ingreso) / #17 (no me interesa) —
    единая пост-генерационная точка для фикс- И AI-ветки. Промпт теперь тоже явно об
    этом просит (anna_prompt_v5.md), но полагаться только на промпт для business-
    critical побочного эффекта недостаточно (тот же класс риска, что у
    _enforce_service_price_gate — модель нарушала даже прямые текстовые запреты).
    Без nurture лид получил бы противоречащий автодогон "¿sigues soltero?" через
    24-48ч сразу после того как ему сказали "espera 6-12 meses" (регресс 2026-08-06).
    """
    if not used or used.get("id") not in _NURTURE_SCENARIOS:
        return result
    if result.get("action") != "respond" or result.get("funnel_stage") == "nurture":
        return result
    result = dict(result)
    result["funnel_stage"] = "nurture"
    return result


_EVENT_LINK_PLACEHOLDER = "[event_link]"
_EVENT_LINK_BUBBLE = ("Aquí está el enlace para tu boleto, con fotos y videos de eventos "
                       f"pasados: {_EVENT_LINK_PLACEHOLDER} 🤍")


def _enforce_link_presence(result: dict, used: dict | None) -> dict:
    """Гарантия: если реально использовался сценарий деталей ивента (№51/№52), но AI
    забыл дать ссылку на билет — довешиваем её ОТДЕЛЬНЫМ бабблом (не мержим в текст).
    sender.py сам подставит [event_link] и продедуплицирует повтор (db.link_already_sent).

    Регресс найден 2026-08-26: свободная генерация при обсуждении деталей ивента иногда
    теряла ссылку и застревала на лишних уточняющих вопросах вместо того чтобы её дать.

    Проверяем не только литеральный плейсхолдер, но и "http" — sender.py подставляет
    [event_link] в РЕАЛЬНЫЙ URL ДО записи в историю (render_bubbles → save_outbound),
    так что если лид уже видел ссылку раньше, она лежит в history как resolved URL, а не
    как плейсхолдер, и AI, ссылаясь на неё, естественно печатает тот же URL текстом —
    без проверки на "http" гейт решил бы, что ссылки нет, и довесил бы дубликат
    (найдено 2026-09-01 в eval, кейс r5).
    """
    if not used or used.get("id") not in _EVENT_DETAIL_SCENARIOS:
        return result
    if result.get("action") != "respond":
        return result
    messages = result.get("messages", [])
    if any(_EVENT_LINK_PLACEHOLDER in m or "http" in m for m in messages):
        return result
    logger.info("guardrail: детали ивента (#%s) без ссылки → довешиваю %s",
                used["id"], _EVENT_LINK_PLACEHOLDER)
    messages = list(messages)
    if len(messages) >= MAX_MESSAGES:
        messages[-1] = _EVENT_LINK_BUBBLE
    else:
        messages.append(_EVENT_LINK_BUBBLE)
    result = dict(result)
    result["messages"] = messages
    return result


_SERVICE_PRICE_PATTERN = re.compile(r"\b10[.,]?000\b")


async def _enforce_service_price_gate(result: dict, lead: dict) -> dict:
    """Гарантия: холодному/неквалифицированному лиду (is_single != True) НЕ раскрываем
    цену сервиса ($10,000 USD) — правило владельца (CLAUDE.md: "холодному — сначала
    ценность + квалификация, ведёт на звонок"). Промпт этому уже учит, но полагаться
    только на промпт для business-critical факта недостаточно — модель нарушала даже
    прямые текстовые запреты (слово "perfil" продолжало проскакивать несмотря на явный
    бан). При нарушении заменяем ВЕСЬ ответ на крючок-квалификатор (сценарий №2), а не
    вырезаем цифру из текста — обрезка оставила бы грамматически сломанное сообщение.

    Одинаковый is_single-гейт с тем, что раньше форсил cold-lead price router (задачи
    #6/#7) — эта проверка её точная страховка, не новая более строгая политика.
    """
    if result.get("action") != "respond":
        return result
    if lead.get("is_single") is True:
        return result
    if not any(_SERVICE_PRICE_PATTERN.search(m) for m in result.get("messages", [])):
        return result
    row = await db.get_scenario_row(2)
    if not row:
        return result
    logger.warning("guardrail: холодному лиду чуть не ушла цена сервиса ($10,000) → "
                    "заменяю на крючок №2")
    result = dict(result)
    result["messages"] = _split_template(row.get("template_es", ""))
    result["used_scenario_id"] = 2
    return result


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
# 'silent' — бот НЕ пишет лиду вообще (напр. похоже на существующего клиента агентства,
# to_anna_silent, ver anna_prompt_v5.md). Единственный action, где messages=[] — законно.
_VALID_ACTIONS = {"respond", "block", "escalate", "silent"}


def _validate_output(data: dict) -> dict:
    """Привести ответ AI к контракту: messages 1-4 (кроме action='silent' — там всегда []),
    валидный action, чистый extracted."""
    if not isinstance(data, dict):
        raise ValueError("ответ AI не dict")

    action = data.get("action")
    if action not in _VALID_ACTIONS:
        action = "respond"

    if action == "silent":
        # Форсируем messages=[] независимо от того, что вернула модель — 'silent' обязан
        # значить полную тишину лиду, не доверяем тексту модели гарантировать это сама.
        messages: list[str] = []
    else:
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages пуст или не список")
        messages = [str(m) for m in messages if str(m).strip()]
        if not messages:
            raise ValueError("messages пуст после чистки")
        if len(messages) > MAX_MESSAGES:
            logger.warning("AI вернул %d сообщений, обрезаю до %d", len(messages), MAX_MESSAGES)
            messages = messages[:MAX_MESSAGES]

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


# Явное упоминание СЕРВИСА (не ивента) в ТЕКУЩЕМ сообщении лида. Нужно, чтобы явный
# вопрос про сервис не перебивался залипшим lead.interest="event" от более раннего
# сообщения (регресс найден 2026-08-31: лид, который раньше спрашивал про ивент, потом
# явно написал "el servicio de matchmaking cuanto cuesta" — а старый interest=event всё
# равно форсил цену ивента, игнорируя явный текущий вопрос).
_SERVICE_KEYWORD_RE = re.compile(
    r"\bservicio\b|\bmatchmaking\b|\bpersonalizado\b|\bacompañamiento\b",
    re.IGNORECASE,
)


def _explicitly_about_service(text: str) -> bool:
    """Текущее сообщение явно про СЕРВИС (не про ивент) — приоритет над interest="event"."""
    return bool(_SERVICE_KEYWORD_RE.search(text or ""))


# Кодовая фраза из рекламы (CTA в объявлении: «напиши novia rusa») — лид, который пишет
# это, ОДНОЗНАЧНО имеет в виду ивент, RAG на короткие фразы ненадёжен (см. регрессы выше),
# поэтому форсим сценарий №2 напрямую по id, не полагаясь на его score вообще
# (найдено 2026-08-26, прямая инструкция владелицы).
_AD_KEYWORD_RE = re.compile(r"novia\s+rusa", re.IGNORECASE)


def _is_ad_keyword(text: str) -> bool:
    """Кодовая фраза объявления ('novia rusa') → лид хочет ивент."""
    return bool(_AD_KEYWORD_RE.search(text or ""))


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
        # top_k=5 (было 3): чем больше кандидатов видит AI, тем меньше шанс, что единственный
        # (возможно случайный) RAG-матч продавит решение — без этого модель почти всегда
        # берёт единственный предложенный сценарий за неимением альтернатив.
        scenarios = await search_scenarios(user_text, top_k=5)
    except Exception:
        logger.exception("RAG-поиск упал, иду в OpenAI без сценариев")
        scenarios = []

    top = scenarios[0] if scenarios else None

    # Фото только что одобрено, и лид изначально интересовался ивентом (interest='event',
    # см. _fixed_reply/_EVENT_DETAIL_SCENARIOS) — форсим №51 (цена+детали+ссылка) напрямую,
    # вместо того чтобы дать AI придумывать питч с нуля. Без этого AI свободно генерил новый
    # "cómo funciona el evento" питч — путал скидку за друга, ЗАБЫВАЛ дать [event_link] и
    # застревал на лишних уточняющих вопросах ("¿reservo tu lugar o solo te aviso la fecha?")
    # вместо того чтобы просто дать ссылку (регресс найден 2026-08-26, живой тест).
    photo_thanks_prefix = False
    if user_text == "[фото одобрено]" and lead.get("interest") == "event":
        row = await db.get_scenario_row(51)
        if row:
            logger.info("фото одобрено + interest=event → форс №51 (был top=%s)",
                        top.get("id") if top else None)
            row["score"] = 1.0
            scenarios, top = [row], row
            photo_thanks_prefix = True

    # Контекст-фолбэк: если голого текста не хватило (нет уверенного матча, top < FALLBACK) —
    # перезапрос с последней репликой Anna из истории («вопрос бота + ответ лида»). Чинит
    # короткие/контекстные ответы («sí soltero», «va», «ok») разом. Самодостаточные сообщения
    # сюда не попадают (у них top уже >= FALLBACK) — то, что матчится, не ломается.
    if top is None or top.get("score", 0) < FALLBACK_SCORE:
        last_bot = _last_anna_text(history)
        if last_bot:
            try:
                ctx = await search_scenarios(f"{last_bot} {user_text}", top_k=5)
            except Exception:
                logger.exception("контекст-фолбэк RAG упал, оставляю bare")
                ctx = []
            if ctx and (top is None or ctx[0].get("score", 0) > top.get("score", 0)):
                logger.info("контекст-фолбэк: bare=%.3f → ctx #%s=%.3f",
                            (top or {}).get("score", 0), ctx[0]["id"], ctx[0]["score"])
                scenarios, top = ctx, ctx[0]

    # Сценарии, применимые ТОЛЬКО к лиду, который уже был на конкретном ивенте
    # (funnel_stage='event_attended', ставится при подтверждении оплаты — см. actions.py).
    # Без этого гейта короткие сообщения вроде «hola evento» от совсем нового лида иногда
    # матчили по RAG на #24 (там просто много слова «evento» в trigger_es) — а #24 это
    # bot_then_anna, и лида форсили в эскалацию на Аню/Милу вообще без повода (регресс
    # найден 2026-08-24 на живом тесте через мини-CRM).
    _POST_EVENT_ONLY = {24, 25, 57}
    if lead.get("funnel_stage") != "event_attended":
        filtered = [s for s in scenarios if s.get("id") not in _POST_EVENT_ONLY]
        if len(filtered) != len(scenarios):
            logger.info("не event_attended → убрал post-event сценарии из кандидатов (был top=%s)",
                        top.get("id") if top else None)
            scenarios = filtered
            top = scenarios[0] if scenarios else None

    # Кодовая фраза объявления ("novia rusa") — лид, который её пишет, ОДНОЗНАЧНО имеет
    # в виду ивент. Раньше форсили top/#2 напрямую по id (терялся контекст: если фраза
    # встречалась не первым сообщением, а посреди диалога, бот всё равно ресетил на
    # приветствие с нуля). Теперь просто ДОБАВЛЯЕМ №2 как кандидата в RAG-пул (не
    # заменяя top, ставим ПОСЛЕ контекст-фолбэка и post-event-фильтра, чтобы кандидата
    # не потеряли по пути) — AI видит его как референс среди остальных сценариев и решает
    # с полным контекстом переписки, что уместнее: приветствие-крючок или продолжение диалога.
    if _is_ad_keyword(user_text) and not any(s.get("id") == 2 for s in scenarios):
        row = await db.get_scenario_row(2)
        if row:
            logger.info("кодовая фраза объявления ('novia rusa') → добавляю №2 в кандидаты (top=%s)",
                        top.get("id") if top else None)
            row["score"] = 0.5
            scenarios = scenarios + [row]

    # Холодному/неквалифицированному лиду (is_single != True), который спрашивает про
    # ивент БЕЗ явного вопроса о цене (RAG нашёл №51 сам на общее "cuéntame del evento",
    # а не на прямой ценовой вопрос) — даём №52 (детали без цены) вместо полного №51.
    # Пейсинг, не гейт безопасности: цену ивента можно называть любому лиду сразу
    # (владелец подтвердил 2026-08-22/26) — это НЕ то же самое, что цена сервиса ($10k),
    # ту холодному действительно нельзя, но за это отвечает отдельный пост-генерационный
    # guardrail _enforce_service_price_gate, а не форс здесь. Явный ценовой вопрос
    # ("cuanto cuesta el evento" — буквально пример в trigger_es №51) НЕ понижаем: RAG и
    # так матчит №51 с высоким score, форс не нужен.
    # №52 не имеет собственных trigger_es (сверено в БД) — достижим только через этот форс.
    #
    # Раньше здесь было ещё 2 форса (прямой вопрос про цену ивента → №51 напрямую; любой
    # другой ценовой вопрос → №2-крючок) — оба были нужны только чтобы обойти жёсткий гейт
    # цены сервиса, который раньше стоял здесь же (регресс 2026-08-31). Гейт переехал в
    # _enforce_service_price_gate (пост-генерация, единственный ответственный за него),
    # форс на №2 стал не нужен вообще.
    if (lead.get("is_single") is not True and top and top.get("id") == 51
            and not _is_price_question(user_text)):
        row = await db.get_scenario_row(52)
        if row:
            logger.info("холодный лид + №51 (не ценовой вопрос) → №52 (детали без цены)")
            row["score"] = 1.0
            scenarios, top = [row], row
    elif (
        lead.get("is_single") is True
        and top and top.get("id") != 51 and _is_price_question(user_text)
        and not _explicitly_about_service(user_text)
        and ("evento" in user_text.lower() or lead.get("interest") == "event")
        and not any(s.get("id") == 51 for s in scenarios)
    ):
        # Лид уже квалифицирован и спрашивает про ЦЕНУ ИВЕНТА конкретно — частый случай:
        # подтверждает анкету («soy soltero, 35») И спрашивает цену (и часто заодно
        # локацию) в ОДНОМ сообщении. RAG-эмбеддинг для таких смешанных фраз ненадёжен
        # (нашли 2026-08-21: матчит на №4; нашли 2026-08-26: даже после ре-эмбеддинга №51
        # набирает низкий score и попадает под гейт неоднозначности) — раз бизнес-правило
        # и так однозначно («квалифицирован + спрашивает про ивент → давай №51»), ДОБАВЛЯЕМ
        # №51 в кандидаты (не форсим top/scenarios целиком — раньше форсили напрямую по id,
        # но это тот же антипаттерн, что у "novia rusa": теряется контекст остального
        # сообщения). №4 (исторически неверный топ) ai_allowed=true — не хардкодит ответ
        # через fixed-reply, так что AI всё равно решает с полным контекстом, просто
        # видит №51 среди кандидатов и не может его "не заметить". Пропущенную ссылку в
        # ответе AI подстрахует guardrail _enforce_link_presence.
        #
        # КРИТИЧНО (регресс найден 2026-08-31): раньше условие срабатывало на ЛЮБОЙ
        # ценовой вопрос от тёплого лида, включая явное "cuanto cuesta el SERVICIO" —
        # лид уже получил питч сервиса ($10k), явно переспрашивает ИМЕННО его цену, а
        # получал в ответ цену и детали ивента. Теперь добавляем №51 только если вопрос
        # реально про ивент (mentiona "evento" или interest уже 'event') — иначе (вопрос
        # про сервис) не трогаем ничего, идёт обычная обработка/AI с полным контекстом.
        row = await db.get_scenario_row(51)
        if row:
            logger.info("тёплый лид + вопрос про ЦЕНУ ИВЕНТА → добавляю №51 в кандидаты (top=%s)",
                        top.get("id"))
            row["score"] = 0.5
            scenarios = scenarios + [row]

    # Неоднозначность RAG: топ-1 и топ-2 слишком близко по score — значит эмбеддинг
    # реально не уверен, какой сценарий подходит (частый случай для коротких/расплывчатых
    # сообщений типа «Evento», где несколько разных по смыслу сценариев набирают почти
    # одинаковый score просто из-за общего слова). В таком случае НЕ доверяем topу
    # решительные действия (детерминированный фикс-ответ, форс-эскалацию ниже) — отдаём
    # решение LLM с полным контекстом вместо того, чтобы патчить каждый похожий случай
    # отдельно (см. регрессы #51 2026-08-21 и #24 2026-08-24 — оба ровно об этом).
    AMBIGUITY_MARGIN = 0.05
    ambiguous = (
        len(scenarios) >= 2
        and (scenarios[0].get("score", 0) - scenarios[1].get("score", 0)) < AMBIGUITY_MARGIN
    )
    if ambiguous:
        logger.info("RAG неоднозначен: топ-2 в пределах %.2f (%s)", AMBIGUITY_MARGIN,
                    [(s["id"], round(s.get("score", 0), 3)) for s in scenarios[:3]])

    # Ветка 1: фиксированный сценарий (ai_allowed=false) → template дословно, без OpenAI.
    # Порог зависит от необратимости: блокировка требует высокой уверенности (0.60),
    # обычный фикс-ответ — ниже (0.45). Ниже порога → уходим в AI.
    if top and not top.get("ai_allowed"):
        is_block = top.get("mode") == "bot_then_block" or top.get("blocks_lead")
        threshold = FIXED_BLOCK_SCORE if is_block else FIXED_SCORE
        if top.get("score", 0) >= threshold and not ambiguous:
            logger.info("фикс-сценарий #%s (score=%.3f >= %.2f, block=%s), OpenAI не вызываю",
                        top["id"], top["score"], threshold, is_block)
            reply = _enforce_nurture_stage(_tag_event_interest(_fixed_reply(top), top), top)
            if photo_thanks_prefix and reply["messages"]:
                # Мердж короткого "спасибо за фото" в первый баббл (не отдельным сообщением —
                # см. правило "NUNCA mandes un mensaje suelto de gracias por tu foto").
                reply["messages"][0] = "¡Gracias por tu foto! 😊 " + reply["messages"][0]
            await _maybe_announce_event_video(reply, top, lead)
            return reply
        logger.info("фикс-сценарий #%s score=%.3f < %.2f (block=%s, ambiguous=%s) → в AI",
                    top["id"], top["score"], threshold, is_block, ambiguous)

    # Ветка 2/3: AI генерит. При низком score сценарии не передаём (fallback в промпте).
    # Блокирующие сценарии (bot_then_block/blocks_lead) требуют ту же высокую уверенность и
    # отсутствие неоднозначности, что и в детерминированной ветке выше (FIXED_BLOCK_SCORE +
    # ambiguous) — раньше это применялось только при ai_allowed=false. Для ai_allowed=true
    # блокирующих сценариев такой защиты не было вообще: они просто попадали в 'confident'
    # наравне со всеми, и AI мог блокировать реального лида по случайному RAG-совпадению
    # (найдено 2026-09-01, baseline-кейс r8: "1991\n25-35" — дата рождения + желаемый возраст
    # партнёрши в одном сообщении — ложно матчило №7 "tengo 25 años", лид 35 лет получал отказ
    # по возрасту). Чистый фильтр кандидатов — ничего не форсим, просто не даём AI
    # ненадёжный повод для необратимого действия.
    def _block_candidate_ok(s: dict) -> bool:
        if not (s.get("mode") == "bot_then_block" or s.get("blocks_lead")):
            return True
        return s.get("score", 0) >= FIXED_BLOCK_SCORE and not ambiguous

    confident = [
        s for s in scenarios
        if s.get("score", 0) >= FALLBACK_SCORE and _block_candidate_ok(s)
    ]
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
    #
    # Смотрим на сценарий, который РЕАЛЬНО использовал LLM (used_scenario_id), а не на top
    # (топ RAG-рейтинга) — LLM выбирает из confident свободно, и его выбор может отличаться
    # от top (напр. top=bot_auto по эмбеддингу, а LLM использовал более подходящий по смыслу
    # bot_then_anna из того же confident). Проверка top.mode пропускала форс-эскалацию именно
    # в таком случае.
    # used_scenario_id=None валиден (LLM не опёрся ни на один сценарий, промпт это разрешает) —
    # тогда откатываемся на top как перестраховку: если РЕАЛЬНО подходящий по теме сценарий
    # (top) требует хэндофф, лучше форсировать эскалацию даже без явного подтверждения LLM,
    # чем рискнуть пропустить лида без единой проверки (см. комментарий выше). НО если top сам
    # неоднозначен (ambiguous) — не откатываемся на него: доверять шаткому RAG-топу решение
    # об эскалации рискованнее, чем положиться на явный action от LLM.
    llm_used = next((s for s in confident if s.get("id") == result.get("used_scenario_id")), None)
    used = llm_used if llm_used is not None else (None if ambiguous else top)
    if used and used.get("mode") == "bot_then_anna" and used.get("score", 0) >= FALLBACK_SCORE:
        if result["action"] != "escalate":
            logger.info("bot_then_anna #%s → форсирую escalate (LLM вернул %s)",
                        used["id"], result["action"])
        result["action"] = "escalate"
        result["needs_escalation"] = True
        if not result.get("used_scenario_id"):
            result["used_scenario_id"] = used["id"]
    result = _tag_event_interest(result, used)
    result = _enforce_nurture_stage(result, used)
    result = _enforce_link_presence(result, used)
    result = await _enforce_service_price_gate(result, lead)
    return result
