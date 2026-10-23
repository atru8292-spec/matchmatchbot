"""Чек об оплате после ссылки на билет (решение Арины 2026-10-24).

Раньше фото чека бот принимал за селфи («Me mandas otra foto donde estés tú solito…»), а PDF и
«te mando el comprobante» отдавал модели. Теперь, если ссылка на билет уже ушла (или лид только
что писал «ya pagué»), фото или документ без текста и «te mando el comprobante» — возможная
оплата: модель не вызываем, селфи не просим, лиду один ответ, Ане и Арине алерт
«💰 Возможно, лид оплатил, проверь» с кнопкой «Подтвердить оплату» (та же, что у «ya pagué»:
подтверждение оплаты — только actions.confirm_payment по кнопке, своей отметки здесь нет).

Повтор «ya pagué» или чек в течение DEDUP_MIN минут — без второго ответа и второго алерта
(по нашему же ответу в messages: переживает рестарт).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import filters

ACK = "¡Genial! 🤍 Ahora lo confirmo y te escribo"
DEDUP_MIN = 10
CLAIM_RECENT_MIN = 30
HISTORY_LIMIT = 80

COMPROBANTE_RE = re.compile(
    r"comprobante|\brecibo\b|\bvoucher\b|\bticket\s+de\s+(?:pago|compra)|captura\s+del?\s+pago|"
    r"(?:aqu[ií]|ah[ií])\s+(?:est[aá]|va|te\s+va)\s+(?:el\s+)?pago|ya\s+qued[oó]\s+el\s+pago",
    re.IGNORECASE)
# Бот просил фото лида после ссылки — тогда фото это селфи, а не чек.
_PHOTO_ASK_RE = re.compile(r"foto\s+tuya|fotos\s+tuyas|tu\s+foto|(?:una|otra)\s+foto\s+donde\s+est[eé]s|"
                           r"m[aá]ndame\s+(?:2\s+o\s+3\s+)?fotos", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _outbound(m: dict) -> bool:
    return m.get("direction") == "outbound" or m.get("sender") == "anna"


def contexto(history: list[dict], burst_ids: set, event_link: str | None,
             now: datetime | None = None) -> dict:
    """По истории (старые → новые), без текущего залпа:
    enlace — ссылка на билет уже уходила и после неё бот не просил фото лида;
    reclamo — лид писал «ya pagué» за последние CLAIM_RECENT_MIN минут;
    ack_reciente — наш ответ про оплату был за последние DEDUP_MIN минут."""
    now = now or _now()
    link = (event_link or "").strip().lower()
    enlace, reclamo, ack = False, False, False
    for m in history:
        if m.get("id") in burst_ids:
            continue
        text = m.get("text") or ""
        ts = _aware(m.get("created_at"))
        if _outbound(m):
            if link and link in text.lower():
                enlace = True
            elif enlace and _PHOTO_ASK_RE.search(text):
                enlace = False          # после ссылки попросили фото — дальше фото это селфи
            if text.strip() == ACK and ts and now - ts <= timedelta(minutes=DEDUP_MIN):
                ack = True
        elif ts and now - ts <= timedelta(minutes=CLAIM_RECENT_MIN) and filters.is_payment_claim(text):
            reclamo = True
    return {"enlace": enlace, "reclamo": reclamo, "ack_reciente": ack}


def es_recibo(msgs: list[dict], ctx: dict) -> str | None:
    """Залп — возможный чек. Вернуть описание для алерта или None.
    Фото/документ без подписи или «te mando el comprobante» — только после ссылки на билет
    или недавнего «ya pagué»."""
    if not (ctx.get("enlace") or ctx.get("reclamo")):
        return None
    tipos, textos = [], []
    for m in msgs:
        meta = m.get("meta") or {}
        ct = meta.get("content_type")
        if ct in ("photo", "document"):
            tipos.append("фото" if ct == "photo" else "документ")
            cap = (meta.get("caption") or "").strip()
            if cap:
                textos.append(cap)
        elif ct in (None, "text"):
            t = (m.get("text") or "").strip()
            if t:
                textos.append(t)
        else:
            return None                 # голос/видео — не чек, обычный путь
    texto = " ".join(textos)
    if tipos and (not texto or COMPROBANTE_RE.search(texto) or filters.is_payment_claim(texto)):
        return f"Прислал {', '.join(sorted(set(tipos)))} без текста после ссылки на билет" if not texto \
            else f"Прислал {', '.join(sorted(set(tipos)))}: «{texto[:120]}»"
    if not tipos and texto and COMPROBANTE_RE.search(texto) and "?" not in texto:
        return f"Пишет: «{texto[:120]}»"
    return None


def media_en_rafaga(msgs: list[dict]) -> list[str]:
    """Типы медиа в залпе (для алерта к «ya pagué» вместе с фото)."""
    out = []
    for m in msgs:
        ct = (m.get("meta") or {}).get("content_type")
        if ct in ("photo", "document"):
            out.append("фото" if ct == "photo" else "документ")
    return sorted(set(out))
