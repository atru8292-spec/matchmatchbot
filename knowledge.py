"""База знаний режима rag: загрузка, отбор действующих записей, поиск по смыслу.

Источник: таблица knowledge (миграция 008). Пока таблицы нет (до деплоя) или для тестов —
knowledge/seed.json, эмбеддинги считаются один раз при первом поиске и живут в памяти.
Записи кэшируются на KB_TTL_SEC: правка в БД подхватывается без рестарта.

В поиск идут только действующие записи: activo, confirmado, сегодня (по Мехико) внутри
valid_from..valid_to, и все токены записи заполнены в настройках (ивент выключен →
записи ивента не участвуют). style и style_pair в поиск не идут: они всегда в контексте.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import db

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).resolve().parent / "knowledge" / "seed.json"
KB_TTL_SEC = 60
_CDMX = ZoneInfo("America/Mexico_City")
_TOKEN_RE = re.compile(r"\[([a-z_]+)\]")
# Токены, которые подставляет не настройка ивента (их наличие не требует event_active).
_NON_EVENT_TOKENS = {"course_link", "horario_llamada"}
ALWAYS_IN_CONTEXT = {"style", "style_pair"}

_cache: dict = {"at": 0.0, "records": None, "source": None}
_seed_embeddings: dict[str, list[float]] = {}


def load_seed(path: Path = SEED_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["records"]


async def _load_from_db() -> list[dict] | None:
    """Записи из таблицы knowledge. None — таблицы нет (миграция не применена)."""
    try:
        rows = await db._get_pool().fetch(
            "SELECT *, embedding::text AS embedding_text FROM knowledge WHERE activo")
    except Exception as e:
        if "knowledge" in str(e) and "does not exist" in str(e):
            return None
        raise
    out = []
    for r in rows:
        d = dict(r)
        emb = d.pop("embedding_text", None)
        d.pop("embedding", None)
        d["_embedding"] = json.loads(emb) if emb else None
        for k in ("valid_from", "valid_to"):
            if isinstance(d.get(k), date):
                d[k] = d[k].isoformat()
        out.append(d)
    return out


async def records() -> list[dict]:
    """Все активные записи (кэш KB_TTL_SEC). БД недоступна или таблицы нет → seed."""
    if _cache["records"] is not None and time.monotonic() - _cache["at"] < KB_TTL_SEC:
        return _cache["records"]
    recs, source = None, "db"
    try:
        recs = await _load_from_db()
    except Exception as e:
        # Сбой чтения (не «таблицы нет»): последние записи из БД лучше seed.json — в seed
        # могут быть записи, которые владелица в БД выключила (code-review 2026-10-22).
        logger.exception("knowledge: не прочитала таблицу")
        await _alert_fallback(repr(e))
        if _cache["records"] is not None and _cache["source"] == "db":
            _cache["at"] = time.monotonic()
            return _cache["records"]
    if not recs:   # нет таблицы или она пустая (импорт ещё не делали) → seed
        recs, source = [r for r in load_seed() if r.get("activo", True)], "seed"
    if source != _cache["source"]:
        logger.info("knowledge: источник %s, записей %d", source, len(recs))
    _cache.update(at=time.monotonic(), records=recs, source=source)
    return recs


_last_alert = {"at": 0.0}
ALERT_EVERY_SEC = 600


async def _alert_fallback(error: str) -> None:
    """Техалерт о сбое чтения базы знаний, не чаще раза в 10 минут; его сбой не роняет ответ."""
    if time.monotonic() - _last_alert["at"] < ALERT_EVERY_SEC and _last_alert["at"]:
        return
    _last_alert["at"] = time.monotonic()
    try:
        import escalation
        await escalation.notify_error("knowledge.records", f"база знаний не читается: {error}", None)
    except Exception:
        logger.exception("knowledge: техалерт не ушёл")


def reset_cache() -> None:
    _cache.update(at=0.0, records=None, source=None)


def today_cdmx(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(_CDMX).date().isoformat()


def tokens_of(rec: dict) -> set[str]:
    text = " ".join(str(rec.get(k) or "") for k in ("contenido_es", "bueno"))
    return set(_TOKEN_RE.findall(text))


def is_usable(rec: dict, settings: dict, today: str) -> bool:
    """Запись можно подавать модели/лиду сейчас."""
    if not rec.get("activo", True) or not rec.get("confirmado"):
        return False
    if rec.get("valid_from") and today < rec["valid_from"]:
        return False
    if rec.get("valid_to") and today > rec["valid_to"]:
        return False
    event_tokens = tokens_of(rec) - _NON_EVENT_TOKENS
    if (event_tokens or rec.get("evento_id")) and settings.get("event_active") != "1":
        return False
    # Пустая настройка (напр. event_price_next не задана) → запись не подаём, а не «de  MXN».
    return all((settings.get(t) or "").strip() for t in event_tokens)


# Необязательные токены: если настройка пуста, убираем только фразу с ними, а не всю запись
# («Ahora cuesta 6000 MXN.» остаётся, «Más adelante el precio sube a  MXN.» — нет).
OPTIONAL_TOKENS = {"event_price_next"}


def _without_empty_optional(rec: dict, settings: dict) -> dict:
    empty = {t for t in tokens_of(rec) & OPTIONAL_TOKENS if not (settings.get(t) or "").strip()}
    if not empty:
        return rec
    out = dict(rec)
    for k in ("contenido_es", "bueno"):
        if out.get(k):
            parts = re.split(r"(?<=[.!?])\s+", out[k])
            out[k] = " ".join(p for p in parts if not any(f"[{t}]" in p for t in empty)).strip()
    return out


def usable(recs: list[dict], settings: dict, today: str) -> list[dict]:
    out = []
    for rec in recs:
        r = _without_empty_optional(rec, settings)
        if rec.get("contenido_es") and not r.get("contenido_es"):
            continue                     # вся запись была про пустую ступень цены
        if is_usable(r, settings, today):
            out.append(r)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embedding_of(rec: dict) -> list[float] | None:
    return rec.get("_embedding") or _seed_embeddings.get(rec["id"])


async def ensure_seed_embeddings(recs: list[dict], embed_batch) -> None:
    """Для записей без эмбеддинга (источник seed) — посчитать одним запросом и запомнить."""
    missing = [r for r in recs if not r.get("_embedding") and r["id"] not in _seed_embeddings]
    if not missing:
        return
    vecs = await embed_batch([r["cuando_usar"] or r["titulo"] for r in missing])
    for r, v in zip(missing, vecs):
        _seed_embeddings[r["id"]] = v


async def search(query_vec: list[float], recs: list[dict], embed_batch, top_k: int = 5,
                 min_score: float = 0.25) -> list[dict]:
    """top_k ближайших записей (кроме style/style_pair) по косинусу к запросу.
    Возвращает копии записей с полем score, по убыванию."""
    pool = [r for r in recs if r["tipo"] not in ALWAYS_IN_CONTEXT]
    await ensure_seed_embeddings(pool, embed_batch)
    scored = []
    for r in pool:
        emb = _embedding_of(r)
        if emb:
            scored.append({**r, "score": _cosine(query_vec, emb)})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return [r for r in scored[:top_k] if r["score"] >= min_score]


def always_in_context(recs: list[dict]) -> dict:
    """Стиль и хорошие половины пар — в каждый запрос модели. Поле malo НЕ передаётся."""
    style = [{"id": r["id"], "regla": r["contenido_es"], "como": r.get("como_responder")}
             for r in recs if r["tipo"] == "style"]
    pairs = [{"lead": r["cuando_usar"], "bien": r["bueno"], "por_que": r["por_que"]}
             for r in recs if r["tipo"] == "style_pair"]
    return {"estilo": style, "ejemplos_buenos": pairs}


def ficha_for_model(rec: dict) -> dict:
    """Запись в виде «фичи» для контекста модели: без ru, malo, служебных полей."""
    out = {"id": rec["id"], "tipo": rec["tipo"], "titulo": rec["titulo"]}
    for k in ("contenido_es", "como_responder", "notas_tono"):
        if rec.get(k):
            out[k] = rec[k]
    if rec.get("frases_ejemplo"):
        out["frases_ejemplo"] = rec["frases_ejemplo"]
    if rec.get("accion") == "silent_escalate":
        out["accion"] = "silent"
    if rec.get("literal"):
        out["literal"] = True
    return out
