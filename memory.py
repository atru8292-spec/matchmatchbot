"""Память лида для режима rag (5 слоёв, всё из БД):

1. карточка лида (поля leads) и слоты квалификации с попытками по всей истории;
2. последние 40 сообщений целиком: fecha (CDMX), autor (lead | anna_bot | anya_manual |
   sistema), texto; между сообщениями с паузой > 6 ч — строка «— 3 días después —»;
3. сводка старой части (leads.memoria_resumen, gpt-4.1-mini) — только про лида и разговор,
   источником фактов об агентстве НЕ является;
4. журнал ya_hecho по ВСЕЙ переписке (не по окну): что уже сказано и отправлено,
   привязано к текущему ивенту; то, что было до объявления текущего ивента, — pasado;
5. 5 последних внутренних заметок (lead_notes) — модели «не цитировать».

Плюс режим возвращения (regreso): null (< 6 ч), corto (6 ч – 3 дня), largo (> 3 дней).
Чистые функции тестируются без БД; build() собирает всё из БД.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import conversation_rules
import db

logger = logging.getLogger(__name__)

CDMX = ZoneInfo("America/Mexico_City")
WINDOW = 40
FULL_HISTORY_LIMIT = 3000
GAP_HOURS = 6
REGRESO_LARGO_DAYS = 3
NOTES_LIMIT = 5

EVENT_KEYS = ["event_active", "event_date", "event_start", "event_address",
              "event_price_nonmember", "event_price_next", "event_link"]

_SYSTEM_TEXT_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")
_EVENT_TALK_RE = re.compile(r"evento|boleto|mxn|rusaencdmx|slavic", re.IGNORECASE)
_SERVICE_PRICE_RE = re.compile(r"\b10[.,]?000\b|\b10\s*mil\b|\$?\s*10\s*k\b", re.IGNORECASE)
_PRICE_OBJECTION_RE = re.compile(r"\bcar[oa]\b|no\s+me\s+alcanza|es\s+mucho|muy\s+costos",
                                 re.IGNORECASE)
_HESITATION_RE = re.compile(r"\bpensar(?:lo|[eé])?\b|\bno\s+s[eé]\b|\btal\s+vez\b|"
                            r"\bquiz[aá]s?\b|\bluego\s+(?:te\s+)?(?:aviso|digo)\b", re.IGNORECASE)
_FAREWELL_RE = re.compile(r"\b(?:adi[oó]s|bye|nos\s+vemos|hasta\s+luego|buenas\s+noches|"
                          r"gracias,?\s+(?:eso\s+es\s+todo|bye))\b", re.IGNORECASE)

# Вопросы квалификации в исходящих (бот или Аня) — считаем попытки по всей истории.
SLOT_QUESTION_RE = {
    "name": re.compile(r"c[oó]mo\s+te\s+llamas|tu\s+nombre", re.IGNORECASE),
    "is_single": re.compile(r"soltero", re.IGNORECASE),
    "age": re.compile(r"qu[eé]\s+edad|cu[aá]ntos\s+a[ñn]os", re.IGNORECASE),
    "profession": re.compile(r"a\s+qu[eé]\s+te\s+dedicas|tu\s+profesi[oó]n|en\s+qu[eé]\s+trabajas",
                             re.IGNORECASE),
    "photo": re.compile(r"(?:manda|env[ií]a|mandar|enviar)\w*\s+(?:una\s+|unas\s+)?fotos?",
                        re.IGNORECASE),
    "email": re.compile(r"correo", re.IGNORECASE),
}
REQUIRED = {"evento": ["name", "is_single"],
            "servicio": ["name", "is_single", "age", "profession", "photo"],
            "sin_definir": ["name", "is_single"]}
OPTIONAL = {"evento": ["age", "profession"], "servicio": [], "sin_definir": []}
MAX_SLOT_TRIES = 2


# ───────────────────────── сообщения ─────────────────────────

def autor(msg: dict) -> str:
    text = msg.get("text") or ""
    if msg.get("sender") == "lead" or msg.get("direction") == "inbound":
        return "lead"                    # «[voice message]» от лида — тоже лид
    if _SYSTEM_TEXT_RE.match(text):
        return "sistema"
    meta = msg.get("meta") or {}
    if meta.get("manual") or meta.get("source") == "echo":
        return "anya_manual"
    return "anna_bot"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fecha(dt: datetime) -> str:
    return _aware(dt).astimezone(CDMX).strftime("%Y-%m-%d %H:%M")


def gap_text(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 24:
        n = int(hours)
        return f"— {n} horas después —"
    days = int(hours // 24)
    if days < 14:
        return f"— {days} {'día' if days == 1 else 'días'} después —"
    weeks = days // 7
    return f"— {weeks} semanas después —"


def split_current_burst(msgs: list[dict], user_text: str) -> tuple[list[dict], list[dict]]:
    """Входящие текущего залпа уже лежат в БД (main сохраняет до ответа). Отделяем их от
    прошлой переписки: хвост из сообщений лида после последнего не-лидового, чьи тексты
    входят в user_text."""
    i = len(msgs)
    while i > 0 and autor(msgs[i - 1]) == "lead":
        t = (msgs[i - 1].get("text") or "").strip()
        # медиа без подписи и служебные «[…]» входят в залп вместе с текстом
        if t and not _SYSTEM_TEXT_RE.match(t) and t not in (user_text or ""):
            break
        i -= 1
    return msgs[:i], msgs[i:]


def historial(prior: list[dict]) -> list[dict]:
    """Последние WINDOW сообщений хронологически, с отметками пауз > GAP_HOURS."""
    window = prior[-WINDOW:]
    out: list[dict] = []
    prev = None
    for m in window:
        ts = m.get("created_at")
        if prev is not None and isinstance(ts, datetime):
            gap = (_aware(ts) - _aware(prev)).total_seconds()
            if gap > GAP_HOURS * 3600:
                out.append({"pausa": gap_text(gap)})
        out.append({"fecha": fecha(ts) if isinstance(ts, datetime) else None,
                    "autor": autor(m), "texto": m.get("text") or ""})
        if isinstance(ts, datetime):
            prev = ts
    return out


def regreso(prior: list[dict], now: datetime) -> str | None:
    last = next((m.get("created_at") for m in reversed(prior)
                 if isinstance(m.get("created_at"), datetime)), None)
    if last is None:
        return None
    gap = (_aware(now) - _aware(last)).total_seconds()
    if gap < GAP_HOURS * 3600:
        return None
    return "corto" if gap <= REGRESO_LARGO_DAYS * 86400 else "largo"


# ───────────────────────── ya_hecho ─────────────────────────

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _has_price(text: str, price: str) -> bool:
    p = _digits(price)
    return bool(p) and any(_digits(n) == p for n in re.findall(r"\d[\d.,]*", text or ""))


def ya_hecho(prior: list[dict], lead: dict, settings: dict,
             evento_desde: datetime | None, now: datetime) -> dict:
    """Журнал по всей прошлой переписке. evento_desde — когда объявлен текущий ивент
    (обновление event_date/event_link в настройках): всё раньше — pasado."""
    out: dict = {
        "presentacion": False, "precio_evento_dado": None, "enlace_enviado": None,
        "direccion_dada": None, "halloween_mencionado": None, "video_enviado": None,
        "foto_enviada": None, "precio_servicio_dado": None,
        "preguntas_calificacion": {k: 0 for k in SLOT_QUESTION_RE},
        "objeciones": [], "preguntas_sin_respuesta": [],
        "boleto_comprado": lead.get("funnel_stage") == "event_attended",
        "llamada_agendada": None, "ana_escribio_a_mano": None, "pasado": [],
    }
    link = (settings.get("event_link") or "").strip()
    price = settings.get("event_price_nonmember") or ""
    address = (settings.get("event_address") or "").split(",")[0].strip().lower()
    ev_date = settings.get("event_date") or ""
    video_marker = db.media_marker("video", ev_date) if ev_date else None
    photo_marker = db.media_marker("image", ev_date) if ev_date else None

    for idx, m in enumerate(prior):
        who, text = autor(m), m.get("text") or ""
        ts = m.get("created_at")
        when = fecha(ts) if isinstance(ts, datetime) else None
        current = not (evento_desde and isinstance(ts, datetime)
                       and _aware(ts) < _aware(evento_desde))
        if who == "sistema":
            if video_marker and text.strip() == video_marker:
                out["video_enviado"] = when
            if photo_marker and text.strip() == photo_marker:
                out["foto_enviada"] = when
            continue
        if who == "lead":
            if _PRICE_OBJECTION_RE.search(text):
                out["objeciones"].append({"tipo": "precio", "fecha": when})
            elif _HESITATION_RE.search(text):
                out["objeciones"].append({"tipo": "lo_pensara", "fecha": when})
            nxt = prior[idx + 1] if idx + 1 < len(prior) else None
            if "?" in text and (nxt is None or autor(nxt) == "lead"):
                out["preguntas_sin_respuesta"].append({"texto": text[:200], "fecha": when})
            continue
        # исходящее: бот или Аня вручную
        out["presentacion"] = True
        if who == "anya_manual":
            out["ana_escribio_a_mano"] = when
        if _SERVICE_PRICE_RE.search(text):
            out["precio_servicio_dado"] = when
        if "?" in text:
            for slot, rx in SLOT_QUESTION_RE.items():
                if rx.search(text):
                    out["preguntas_calificacion"][slot] += 1
        if not current:
            if _EVENT_TALK_RE.search(text):
                out["pasado"].append({"fecha": when, "texto": text[:160]})
            continue
        if price and _has_price(text, price):
            out["precio_evento_dado"] = when
        if link and link in text:
            out["enlace_enviado"] = when
        if address and address in text.lower():
            out["direccion_dada"] = when
        if "halloween" in text.lower():
            out["halloween_mencionado"] = when

    out["objeciones"] = out["objeciones"][-5:]
    out["preguntas_sin_respuesta"] = out["preguntas_sin_respuesta"][-3:]
    out["pasado"] = out["pasado"][-5:]
    vc = lead.get("videocall_at")
    if isinstance(vc, datetime) and _aware(vc) > _aware(now):
        out["llamada_agendada"] = fecha(vc)
    elif lead.get("funnel_stage") == "videocall_set":
        out["llamada_agendada"] = "sí"
    return out


# ───────────────────────── слоты ─────────────────────────

# Без «noche»/«fiesta»: «buenas noches» — обычное приветствие (code-review 2026-10-21).
_EVENT_WORD_RE = re.compile(r"evento|boleto|halloween|slavic", re.IGNORECASE)


def ruta(lead: dict, user_text: str) -> str:
    interest = lead.get("interest")
    if interest == "event":
        return "evento"
    if interest == "agency":
        return "servicio"
    if interest == "both":
        return "evento" if _EVENT_WORD_RE.search(user_text or "") else "servicio"
    return "evento" if _EVENT_WORD_RE.search(user_text or "") else "sin_definir"


def _have(lead: dict, slot: str) -> bool:
    if slot == "is_single":
        return lead.get("is_single") is True
    if slot == "photo":
        return bool(lead.get("photo_received"))
    return bool(lead.get(slot))


def calificacion(lead: dict, ya: dict, user_text: str, last_outbound: str | None) -> dict:
    r = ruta(lead, user_text)
    tries = ya["preguntas_calificacion"]
    tenemos = {s: (lead.get("is_single") if s == "is_single" else
                   bool(lead.get("photo_received")) if s == "photo" else lead.get(s))
               for s in ("name", "is_single", "age", "profession", "photo", "email")}
    siguiente, pasar_a_llamada = None, False
    for slot in REQUIRED[r]:
        if _have(lead, slot):
            continue
        if tries.get(slot, 0) >= MAX_SLOT_TRIES:
            if r == "servicio":
                pasar_a_llamada = True   # услуга: после 2 попыток — звонок и Аня
                break
            continue                     # ивент: слот пропускаем
        siguiente = slot
        break
    required_done = siguiente is None
    if required_done and not pasar_a_llamada:
        for slot in OPTIONAL[r]:
            if not _have(lead, slot) and tries.get(slot, 0) < MAX_SLOT_TRIES:
                siguiente = slot
                break
    if ya.get("boleto_comprado"):
        etapa = "cliente"
    elif required_done or pasar_a_llamada:
        etapa = "venta"
    else:
        etapa = "calificacion"
    asked_last = bool(siguiente and last_outbound and "?" in last_outbound
                      and SLOT_QUESTION_RE[siguiente].search(last_outbound))
    blocked = bool(_PRICE_OBJECTION_RE.search(user_text or "") or _HESITATION_RE.search(user_text or "")
                   or _FAREWELL_RE.search(user_text or ""))
    return {
        "ruta": r, "tenemos": tenemos, "siguiente": siguiente,
        "preguntado_en_ultimo_mensaje": asked_last,
        "puede_preguntar": bool(siguiente) and not asked_last and not blocked,
        "intentos": dict(tries), "etapa": etapa, "pasar_a_llamada": pasar_a_llamada,
    }


# ───────────────────────── сборка из БД ─────────────────────────

async def _evento_desde() -> datetime | None:
    """Когда объявлен текущий ивент: app_settings.event_announced_at (мини-CRM ставит его,
    только когда реально меняется дата или ссылка). Нет ключа → updated_at event_date/link
    (сдвигается при любом пересохранении формы — поэтому только запасной вариант)."""
    try:
        raw = await db.get_setting("event_announced_at")
        if raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        rows = await db._get_pool().fetch(
            "SELECT updated_at FROM app_settings WHERE key = ANY($1::text[])",
            ["event_date", "event_link"])
    except Exception:
        logger.exception("memory: не прочитала дату объявления ивента")
        return None
    dates = [r["updated_at"] for r in rows if r["updated_at"]]
    return max(dates) if dates else None


async def build(lead: dict, user_text: str, now: datetime | None = None) -> dict:
    """Собрать память лида из БД. Номера нет (песочница) — пустая история."""
    now = now or datetime.now(timezone.utc)
    phone = lead.get("phone")
    settings = await db.get_settings(EVENT_KEYS)
    msgs, notes, desde = [], [], None
    if phone:
        msgs = await db.get_conversation_history(phone, FULL_HISTORY_LIMIT)
        try:
            notes = (await db.get_lead_notes(phone))[-NOTES_LIMIT:]
        except Exception:
            logger.exception("memory: заметки не прочитались [%s]", phone)
        desde = await _evento_desde()
    prior, burst = split_current_burst(msgs, user_text)
    ya = ya_hecho(prior, lead, settings, desde, now)
    last_out = next((m.get("text") for m in reversed(prior)
                     if autor(m) in ("anna_bot", "anya_manual")), None)
    lead_lines = [m.get("text") or "" for m in prior if autor(m) == "lead"]
    # Последние ответы Анны (бот или вручную) по текущему ивенту — источник для сверки.
    anna_reciente = [m.get("text") or "" for m in prior
                     if autor(m) in ("anna_bot", "anya_manual")
                     and not (desde and isinstance(m.get("created_at"), datetime)
                              and _aware(m["created_at"]) < _aware(desde))][-5:]
    # Реакции в начале последних 3 ответов бота (повтор «¡Qué bien!» подряд — шаблон).
    inicios, prev_out = [], False
    for m in prior:
        is_bot = autor(m) == "anna_bot"
        if is_bot and not prev_out:
            r = conversation_rules.reaction_start(m.get("text") or "")
            inicios.append(r)
        prev_out = autor(m) in ("anna_bot", "sistema") and (is_bot or prev_out)
    return {
        "inicios_recientes": [r for r in inicios[-3:] if r],
        "anna_reciente": anna_reciente,
        "settings": settings,
        "historial": historial(prior),
        "mensajes_totales": len(prior),
        "regreso": regreso(prior, now),
        "ya_hecho": ya,
        "calificacion": calificacion(lead, ya, user_text, last_out),
        "resumen": lead.get("memoria_resumen") or None,
        "notas_internas": [n.get("text") for n in notes if n.get("text")],
        "primer_mensaje_del_lead": not lead_lines,
        "ultimo_mensaje_anna": last_out,
        "lead_texts": lead_lines + [user_text or ""],
        "prior": prior,
    }


def evento_actual(settings: dict) -> dict | None:
    """Карточка текущего ивента для модели (токены, не цифры: подставит sender)."""
    if settings.get("event_active") != "1":
        return None
    card = {"nombre": "Slavic-Latino Connect Night", "fecha": "[event_date]",
            "inicio": "[event_start]", "lugar": "[event_address]",
            "precio": "[event_price_nonmember] MXN"}
    if (settings.get("event_price_next") or "").strip():
        card["siguiente_precio"] = "[event_price_next] MXN"
    card["valores"] = {k: settings.get(k) for k in
                       ("event_date", "event_start", "event_address", "event_price_nonmember",
                        "event_price_next") if settings.get(k)}
    return card
