"""API-слой мини-CRM (Telegram Mini App) — роуты /api/mini/*.

Тонкая обёртка поверх готовых db-функций. Вся авторизация — через зависимость
mini_auth.require_admin (проверка Telegram initData + сверка admin_ids). Роуты
НЕ дублируют бизнес-логику бота: они читают/пишут ту же Supabase-базу.

Соглашение по ответам: наружу отдаём camelCase (идиоматично для TS-фронта),
внутри БД остаётся snake_case. Даты — ISO-8601 строки (или null).
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

import db
import escalation
import funnel
import sender
import vision
from mini_auth import require_admin

logger = logging.getLogger("mini_api")

router = APIRouter(prefix="/api/mini", tags=["mini-crm"])


def _iso(value) -> str | None:
    """datetime/date → ISO-строка, иначе None (для JSON-сериализации дат из БД)."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None


def _serialize_lead(row: dict) -> dict:
    """Строка leads (+ превью/флаги) → плоский camelCase-объект для списка."""
    stage = row.get("funnel_stage")
    return {
        "phone": row.get("phone"),
        # человекочитаемое имя: name → whatsapp_name → «Без имени»
        "name": row.get("name") or row.get("whatsapp_name") or None,
        "whatsappName": row.get("whatsapp_name"),
        "funnelStage": stage or funnel.DEFAULT_STAGE,
        "funnelStageLabel": funnel.stage_label(stage),
        "mode": row.get("mode") or "auto",
        "interest": row.get("interest"),
        "age": row.get("age"),
        "profession": row.get("profession"),
        "city": row.get("city"),
        "isClient": bool(row.get("is_client")),
        "lastMessageAt": _iso(row.get("last_message_at")),
        "lastInboundAt": _iso(row.get("last_inbound_at")),
        "lastMessagePreview": row.get("last_message_text"),
        "lastMessageSender": row.get("last_message_sender"),
        "lastMessageDirection": row.get("last_message_direction"),
    }


@router.get("/me")
async def me(user: dict = Depends(require_admin)) -> dict:
    """Кто я — проверка авторизации фронтом на старте (и dev-режима)."""
    return {"id": user.get("id"), "firstName": user.get("first_name"),
            "username": user.get("username"), "isDev": bool(user.get("is_dev"))}


@router.get("/meta")
async def meta(_: dict = Depends(require_admin)) -> dict:
    """Справочники для фильтров фронта: стадии воронки (код+название)."""
    return {
        "stages": [{"code": code, "label": label}
                   for code, label in funnel.FUNNEL_STAGES.items()],
        "activeStages": list(funnel.ACTIVE_STAGES),
    }


@router.get("/leads")
async def list_leads(
    _: dict = Depends(require_admin),
    stage: Optional[List[str]] = Query(default=None, description="Коды стадий (можно несколько)"),
    mode: Optional[str] = Query(default=None, description="auto | manual"),
    interest: Optional[str] = Query(default=None, description="event | agency | both"),
    since: Optional[str] = Query(default=None, description="ISO-дата: last_message_at >= since"),
    search: Optional[str] = Query(default=None, description="Поиск по имени/телефону"),
    sort: str = Query(default="recent", description="recent | stage"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Список лидов с фильтрами/поиском/сортировкой/пагинацией.

    Ответ: {leads, total, limit, offset, hasMore}. Без DB (dev без Supabase) —
    503, чтобы фронт показал понятную ошибку, а не пустой список как «всё ок».
    """
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")

    # Валидируем перечисляемые значения — иначе тихо вернём пустой список.
    if mode is not None and mode not in ("auto", "manual"):
        raise HTTPException(status_code=422, detail="mode must be auto|manual")
    if sort not in ("recent", "stage"):
        raise HTTPException(status_code=422, detail="sort must be recent|stage")
    if stage:
        bad = [s for s in stage if s not in funnel.FUNNEL_STAGES]
        if bad:
            raise HTTPException(status_code=422, detail=f"unknown stage(s): {bad}")
    if since is not None:
        # Валидируем дату сами → 422, иначе asyncpg бросит DataError и уйдёт в 500.
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="since must be ISO-8601 date")

    try:
        result = await db.list_leads_page(
            stages=stage, mode=mode, interest=interest, since=since,
            search=search, sort=sort, limit=limit, offset=offset,
        )
    except Exception as e:
        # Конвенция проекта: сбой не молчит — лог + алерт разработке в Telegram.
        logger.exception("GET /leads failed")
        await escalation.notify_error("mini_api.list_leads", repr(e))
        raise HTTPException(status_code=500, detail="Failed to load leads") from e

    leads = [_serialize_lead(r) for r in result["leads"]]
    total = result["total"]
    return {
        "leads": leads,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + len(leads) < total,
    }


# ===== Экспорт лидов в CSV (/api/mini/leads/export) =====

# Интерес — человекочитаемо для менеджера (файл открывает Аня в Excel/Sheets).
_INTEREST_RU = {"event": "Ивент", "agency": "Агентство", "both": "Ивент+агентство"}

_CSV_HEADERS = [
    "Имя", "Телефон", "Стадия", "Интерес", "Возраст", "Профессия", "Город",
    "Клиент", "Последнее сообщение", "Дата последнего сообщения",
]


def _leads_to_csv(rows: list) -> str:
    """Собрать CSV (разделитель ';' — дружелюбно к Excel в RU/ES-локалях; Google
    Sheets автоопределяет). Кодировку/BOM добавляет вызывающий."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(_CSV_HEADERS)
    for r in rows:
        digits = "".join(ch for ch in (r.get("phone") or "") if ch.isdigit())
        dt = r.get("last_message_at")
        w.writerow([
            r.get("name") or r.get("whatsapp_name") or "",
            ("+" + digits) if digits else "",
            funnel.stage_label(r.get("funnel_stage")),
            _INTEREST_RU.get(r.get("interest"), r.get("interest") or ""),
            r.get("age") if r.get("age") is not None else "",
            r.get("profession") or "",
            r.get("city") or "",
            "Да" if r.get("is_client") else "",
            r.get("last_message_text") or "",
            dt.strftime("%Y-%m-%d %H:%M") if isinstance(dt, datetime) else "",
        ])
    return buf.getvalue()


@router.get("/leads/export")
async def export_leads(
    _: dict = Depends(require_admin),
    stage: Optional[List[str]] = Query(default=None),
    mode: Optional[str] = Query(default=None),
    interest: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    sort: str = Query(default="recent"),
) -> Response:
    """Экспорт ТЕКУЩЕЙ отфильтрованной выборки лидов (те же фильтры, что /leads) в CSV.
    Без пагинации — весь набор. UTF-8 с BOM, чтобы Excel корректно показал кириллицу."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    if mode is not None and mode not in ("auto", "manual"):
        raise HTTPException(status_code=422, detail="mode must be auto|manual")
    if sort not in ("recent", "stage"):
        raise HTTPException(status_code=422, detail="sort must be recent|stage")
    if stage:
        bad = [s for s in stage if s not in funnel.FUNNEL_STAGES]
        if bad:
            raise HTTPException(status_code=422, detail=f"unknown stage(s): {bad}")
    if since is not None:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="since must be ISO-8601 date")

    try:
        rows = await db.list_leads_for_export(
            stages=stage, mode=mode, interest=interest, since=since, search=search, sort=sort,
        )
    except Exception as e:
        await _alert_500("export_leads", e)

    # BOM (﻿) → Excel распознаёт UTF-8 и не ломает кириллицу/ñ/á.
    body = "﻿" + _leads_to_csv(rows)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="matchmatch-leads.csv"'},
    )


# ===== Карточка лида: детали, таймлайн, заметки, действия =====

class NoteIn(BaseModel):
    text: str


class WhitelistIn(BaseModel):
    reason: Optional[str] = None


def _norm_phone(raw: str) -> str:
    """Телефон из пути → бизнес-ключ 'wa_<digits>'. Мусор → 422."""
    try:
        return db._wa_phone(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid phone")


def _actor(user: dict) -> str:
    """Кто выполнил действие — для performed_by в manager_actions."""
    uname = user.get("username")
    return f"@{uname}" if uname else f"tg:{user.get('id')}"


async def _alert_500(name: str, e: Exception):
    """Единая обработка сбоя мутации: лог + алерт + 500 (конвенция проекта)."""
    logger.exception("%s failed", name)
    await escalation.notify_error(f"mini_api.{name}", repr(e))
    raise HTTPException(status_code=500, detail="Action failed")


# Коды manager_actions → фронтовые типы событий таймлайна. Незнакомые (напр. старые
# approve_photo/block из прод-истории) в ленту не выводим.
_ACTION_MAP = {
    "takeover": "takeover", "release": "release", "stop": "stop", "resume": "resume",
    "whitelist_add": "client_add", "whitelist_remove": "client_remove",
}


def _serialize_photo(row: dict) -> dict:
    return {
        "url": row.get("storage_url"),
        "verdict": row.get("vision_verdict"),
        "receivedAt": _iso(row.get("received_at")),
    }


def _serialize_note(row: dict) -> dict:
    """Заметка в форме элемента таймлайна (kind=note) — удобно и для списка, и для POST."""
    return {
        "kind": "note",
        "id": f"note{row.get('id')}",
        "text": row.get("text"),
        "createdAt": _iso(row.get("created_at")),
    }


def _serialize_lead_detail(lead: dict, wl: Optional[dict], photos: list) -> dict:
    """Факты лида для карточки (без таймлайна — его отдаёт /history)."""
    stage = lead.get("funnel_stage")
    return {
        "phone": lead.get("phone"),
        "name": lead.get("name") or lead.get("whatsapp_name") or None,
        "whatsappName": lead.get("whatsapp_name"),
        "funnelStage": stage or funnel.DEFAULT_STAGE,
        "funnelStageLabel": funnel.stage_label(stage),
        "mode": lead.get("mode") or "auto",
        "interest": lead.get("interest"),
        "age": lead.get("age"),
        "profession": lead.get("profession"),
        "city": lead.get("city"),
        "isClient": wl is not None,
        "lastMessageAt": _iso(lead.get("last_message_at")),
        "lastInboundAt": _iso(lead.get("last_inbound_at")),
        "lastMessagePreview": None,
        "lastMessageSender": None,
        "lastMessageDirection": None,
        "firstMessageAt": _iso(lead.get("created_at")),
        "doNotContact": bool(lead.get("do_not_contact")),
        "clientReason": (wl or {}).get("reason"),
        "clientAddedBy": (wl or {}).get("added_by"),
        "photos": [_serialize_photo(p) for p in photos],
    }


def _api_sender(m: dict) -> str:
    """Семантический отправитель для фронта: lead / anna (авто-бот) / manager (ручной).

    В БД CHECK разрешает только lead/mila/anna — 'manager' там невозможен, поэтому
    ручные ответы помечаются флагом meta.manual (jsonb). Здесь разворачиваем это в
    'manager' → фронт покажет подпись «Anna», авто-ответы ('anna'/'mila') → «Бот»."""
    if m.get("sender") == "lead":
        return "lead"
    meta = m.get("meta") or {}
    if isinstance(meta, dict) and meta.get("manual"):
        return "manager"
    return "anna"


def _serialize_timeline(messages: list, events: list, actions: list, notes: list) -> list:
    """Слить messages + funnel_events + manager_actions + notes в единый таймлайн,
    отсортированный по времени (старые → новые). Чистая функция — легко тестить.

    Подпись исходящего сообщения задаёт фронт (по sender). Здесь только раскладываем
    источники в единый формат TimelineItem и сортируем."""
    items: list = []  # список (dt, item)
    for m in messages:
        dt = m.get("created_at")
        meta = m.get("meta") or {}
        status = meta.get("status") if isinstance(meta, dict) else None
        items.append((dt, {
            "kind": "message", "id": f"msg{m.get('id')}",
            "sender": _api_sender(m), "direction": m.get("direction"),
            "text": m.get("text"), "createdAt": _iso(dt),
            "status": status,  # sent|failed для ручных отправок, иначе None
        }))
    for e in events:
        dt = e.get("changed_at")
        items.append((dt, {
            "kind": "stage", "id": f"fe{e.get('id')}",
            "fromStage": e.get("from_stage"), "toStage": e.get("to_stage"),
            "createdAt": _iso(dt),
        }))
    for a in actions:
        mapped = _ACTION_MAP.get(a.get("action"))
        if not mapped:
            continue
        dt = a.get("created_at")
        items.append((dt, {
            "kind": "action", "id": f"ma{a.get('id')}",
            "action": mapped, "createdAt": _iso(dt),
        }))
    for n in notes:
        dt = n.get("created_at")
        items.append((dt, _serialize_note(n)))
    # None-времена в конец (страховка), остальное по возрастанию.
    items.sort(key=lambda t: (t[0] is None, t[0]))
    return [item for _, item in items]


@router.get("/lead/{phone}")
async def get_lead(phone: str, _: dict = Depends(require_admin)) -> dict:
    """Факты лида для карточки (инфо-панель + флаги + фото)."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    p = _norm_phone(phone)
    try:
        lead = await db.get_lead_by_phone(p)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        wl = await db.get_whitelist_entry(p)
        photos = await db.get_lead_photos(p)
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("get_lead", e)
    return _serialize_lead_detail(lead, wl, photos)


@router.get("/lead/{phone}/history")
async def get_history(phone: str, _: dict = Depends(require_admin)) -> dict:
    """Единый таймлайн лида: сообщения + смены стадий + действия + заметки."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    p = _norm_phone(phone)
    try:
        lead = await db.get_lead_by_phone(p)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        messages = await db.get_conversation_history(p, limit=200)
        events = await db.get_funnel_events(p)
        actions = await db.get_manager_actions(p)
        notes = await db.get_lead_notes(p)
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("history", e)
    return {"timeline": _serialize_timeline(messages, events, actions, notes)}


@router.get("/lead/{phone}/notes")
async def list_notes(phone: str, _: dict = Depends(require_admin)) -> dict:
    """Список заметок лида (для возможного notes-view; карточке хватает /history)."""
    p = _norm_phone(phone)
    try:
        notes = await db.get_lead_notes(p)
    except Exception as e:
        await _alert_500("list_notes", e)
    return {"notes": [_serialize_note(n) for n in notes]}


@router.post("/lead/{phone}/notes")
async def add_note(phone: str, body: NoteIn, _: dict = Depends(require_admin)) -> dict:
    """Добавить внутреннюю заметку. Возврат — заметка как элемент таймлайна."""
    p = _norm_phone(phone)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text required")
    try:
        lead = await db.get_lead_by_phone(p)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        note = await db.add_lead_note(p, text)
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("add_note", e)
    return _serialize_note(note)


class MessageIn(BaseModel):
    text: str


@router.post("/lead/{phone}/message")
async def send_manual_message(phone: str, body: MessageIn, user: dict = Depends(require_admin)) -> dict:
    """Отправить лиду сообщение вручную (от имени Anna) прямо из карточки.

    Конфликт бот/человек: если лид на авто-режиме — сначала АВТО-TAKEOVER (mode→manual,
    бот замолкает), чтобы бот и менеджер не ответили одновременно.
    Отправка — через sender.py (антибан-задержка + Wazzup, не дублируем HTTP).
    Сообщение сохраняется с sender='anna'+meta.manual (в UI «Anna») и meta.status
    (sent|failed) — неудачная доставка видна в таймлайне (sender.send_one ещё и
    алертит в Telegram), не теряется молча."""
    p = _norm_phone(phone)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text required")
    try:
        lead = await db.get_lead_by_phone(p)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        # авто-takeover: пишем руками → бот не должен отвечать сам
        took_over = False
        if lead.get("mode") == "auto":
            if await db.set_manual(p):
                await db.log_manager_action(p, "takeover", _actor(user))
                took_over = True
        # реальная отправка: антибан-пауза + Wazzup через sender.py
        chat_id = p.replace("wa_", "", 1)
        await asyncio.sleep(sender.compute_delay(text))
        delivered = await sender.send_one(chat_id, text)
        msg = await db.save_manual_message(p, text, delivered)
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("send_manual_message", e)
    return {
        "message": {
            "kind": "message", "id": f"msg{msg.get('id')}", "sender": "manager",
            "direction": "outbound", "text": msg.get("text"),
            "createdAt": _iso(msg.get("created_at")),
            "status": "sent" if delivered else "failed",
        },
        "delivered": delivered,
        "tookOver": took_over,
    }


async def _toggle_action(phone: str, user: dict, *, db_call, action: str, result: dict) -> dict:
    """Общий каркас для takeover/release/resume: атомарный db-вызов (возвращает
    'найден ли лид') + лог действия. 404 если лида нет."""
    p = _norm_phone(phone)
    try:
        if not await db_call(p):
            raise HTTPException(status_code=404, detail="Lead not found")
        await db.log_manager_action(p, action, _actor(user))
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500(action, e)
    return {"ok": True, **result}


@router.post("/lead/{phone}/takeover")
async def takeover(phone: str, user: dict = Depends(require_admin)) -> dict:
    """Взять переписку вручную (mode→manual)."""
    return await _toggle_action(phone, user, db_call=db.set_manual,
                                action="takeover", result={"mode": "manual"})


@router.post("/lead/{phone}/release")
async def release(phone: str, user: dict = Depends(require_admin)) -> dict:
    """Вернуть боту (mode→auto)."""
    return await _toggle_action(phone, user, db_call=db.set_auto,
                                action="release", result={"mode": "auto"})


@router.post("/lead/{phone}/resume")
async def resume(phone: str, user: dict = Depends(require_admin)) -> dict:
    """Снять «бот больше не пишет» (do_not_contact=false, mode→auto)."""
    return await _toggle_action(phone, user, db_call=db.resume_lead,
                                action="resume", result={"doNotContact": False, "mode": "auto"})


@router.post("/lead/{phone}/stop")
async def stop(phone: str, user: dict = Depends(require_admin)) -> dict:
    """Прекратить диалог (бот больше не пишет). Как /stop менеджер-бота — block_lead."""
    p = _norm_phone(phone)
    try:
        lead = await db.get_lead_by_phone(p)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        await db.block_lead(p, "прекращено из мини-CRM")
        await db.log_manager_action(p, "stop", _actor(user))
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("stop", e)
    return {"ok": True, "doNotContact": True}


@router.post("/lead/{phone}/whitelist")
async def add_whitelist(phone: str, body: WhitelistIn, user: dict = Depends(require_admin)) -> dict:
    """Добавить в список клиентов (бот замолкает). add_to_whitelist атомарен (upsert)."""
    p = _norm_phone(phone)
    reason = (body.reason or "").strip() or "из мини-CRM"
    try:
        await db.add_to_whitelist(p, reason, _actor(user))
        await db.log_manager_action(p, "whitelist_add", _actor(user), {"reason": reason})
    except Exception as e:
        await _alert_500("whitelist_add", e)
    return {"ok": True, "isClient": True}


@router.delete("/lead/{phone}/whitelist")
async def remove_whitelist(phone: str, user: dict = Depends(require_admin)) -> dict:
    """Убрать из списка клиентов. remove_from_whitelist атомарен (delete)."""
    p = _norm_phone(phone)
    try:
        await db.remove_from_whitelist(p)
        await db.log_manager_action(p, "whitelist_remove", _actor(user))
    except Exception as e:
        await _alert_500("whitelist_remove", e)
    return {"ok": True, "isClient": False}


# ===== Экран «Клиенты» (whitelist-менеджмент, /api/mini/whitelist) =====

class ClientAddIn(BaseModel):
    phone: str
    reason: Optional[str] = None


def _serialize_client(row: dict) -> dict:
    """Запись whitelist для экрана «Клиенты» (camelCase)."""
    return {
        "phone": row.get("phone"),
        "name": row.get("name") or row.get("whatsapp_name") or None,
        "reason": row.get("reason"),
        "addedBy": row.get("added_by"),
        "addedAt": _iso(row.get("added_at")),
    }


@router.get("/whitelist")
async def list_clients(_: dict = Depends(require_admin)) -> dict:
    """Список клиентов (bot_whitelist) с именами лидов, новые сверху."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        rows = await db.list_whitelist_with_names()
    except Exception as e:
        await _alert_500("list_clients", e)
    return {"clients": [_serialize_client(r) for r in rows]}


@router.post("/whitelist")
async def add_client(body: ClientAddIn, user: dict = Depends(require_admin)) -> dict:
    """Добавить клиента по номеру телефона (+ причина). Возврат — созданная запись.

    Номер нормализуется в 'wa_<digits>'; клиент не обязан быть лидом в leads."""
    try:
        p = db._wa_phone(body.phone)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid phone")
    reason = (body.reason or "").strip() or "из мини-CRM"
    try:
        await db.add_to_whitelist(p, reason, _actor(user))
        await db.log_manager_action(p, "whitelist_add", _actor(user), {"reason": reason})
        entry = await db.get_whitelist_entry(p)
        lead = await db.get_lead_by_phone(p)
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("add_client", e)
    row = {**(entry or {}), "name": (lead or {}).get("name"),
           "whatsapp_name": (lead or {}).get("whatsapp_name")}
    return _serialize_client(row)


@router.delete("/whitelist/{phone}")
async def remove_client(phone: str, user: dict = Depends(require_admin)) -> dict:
    """Убрать клиента из списка по номеру."""
    p = _norm_phone(phone)
    try:
        await db.remove_from_whitelist(p)
        await db.log_manager_action(p, "whitelist_remove", _actor(user))
    except Exception as e:
        await _alert_500("remove_client", e)
    return {"ok": True, "phone": p}


# ===== Статистика (дашборд, /api/mini/stats) =====

def _serialize_stats(funnel_rows: list, counts: dict, esc: list, esc_count: int) -> dict:
    """Собрать дашборд: воронку (в каноничном порядке стадий, с % от общего),
    счётчики новых лидов и зависшие эскалации."""
    total = int(counts.get("total") or 0)
    by_stage = {r.get("funnel_stage"): r for r in funnel_rows}
    funnel_out = []
    for code in funnel.FUNNEL_STAGES:  # каноничный порядок стадий
        r = by_stage.get(code)
        if not r:
            continue
        t = int(r.get("total") or 0)
        funnel_out.append({
            "stage": code,
            "label": funnel.stage_label(code),
            "total": t,
            "last24h": int(r.get("last_24h") or 0),
            "last7d": int(r.get("last_7d") or 0),
            "percent": round(t * 100 / total) if total else 0,
        })

    def _min_left(v):
        return int(v) if v is not None else None

    return {
        "totalLeads": total,
        "newToday": int(counts.get("today") or 0),
        "newWeek": int(counts.get("week") or 0),
        "funnel": funnel_out,
        "pendingEscalations": {
            "count": esc_count,
            "items": [{
                "phone": e.get("phone"),
                "name": e.get("whatsapp_name") or None,
                "reason": e.get("escalate_reason"),
                "minutesLeft": _min_left(e.get("minutes_left")),
                "lastInboundAt": _iso(e.get("last_inbound_at")),
            } for e in esc],
        },
    }


@router.get("/stats")
async def stats(_: dict = Depends(require_admin)) -> dict:
    """Дашборд: воронка по стадиям, новые лиды сегодня/неделя, зависшие эскалации.
    Агрегаты берём из вьюх БД (v_funnel_stats, v_pending_escalations)."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        funnel_rows = await db.get_funnel_stats()
        counts = await db.get_lead_counts()
        esc = await db.get_pending_escalations()
        esc_count = await db.count_pending_escalations()
    except Exception as e:
        await _alert_500("stats", e)
    return _serialize_stats(funnel_rows, counts, esc, esc_count)


# ===== Настройки ивента (/api/mini/event) — тонкая обёртка над app_settings =====
# ТЕ ЖЕ ключи, что читает/пишет менеджер-бот (/set_event, /set_invitation, ...) и
# планировщик — форма и команды работают с одним источником, без дублирования логики.
_EVENT_KEYS = [
    "event_active", "event_date", "event_time", "event_address",
    "event_link", "course_link", "invitation_url", "invitation_ready",
]


class EventSettingsIn(BaseModel):
    eventActive: bool = False
    eventDate: Optional[str] = None
    eventTime: Optional[str] = None
    eventAddress: Optional[str] = None
    eventLink: Optional[str] = None
    courseLink: Optional[str] = None
    invitationUrl: Optional[str] = None
    invitationReady: bool = False


def _event_out(s: dict) -> dict:
    return {
        "eventActive": s.get("event_active") == "1",
        "eventDate": s.get("event_date") or "",
        "eventTime": s.get("event_time") or "",
        "eventAddress": s.get("event_address") or "",
        "eventLink": s.get("event_link") or "",
        "courseLink": s.get("course_link") or "",
        "invitationUrl": s.get("invitation_url") or "",
        "invitationReady": s.get("invitation_ready") == "1",
    }


@router.get("/event")
async def get_event(_: dict = Depends(require_admin)) -> dict:
    """Текущие настройки ивента (из app_settings — тот же источник, что у бота)."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        s = await db.get_settings(_EVENT_KEYS)
    except Exception as e:
        await _alert_500("get_event", e)
    return _event_out(s)


class InvitationUploadIn(BaseModel):
    contentBase64: str
    contentType: str


@router.post("/event/invitation")
async def upload_invitation(body: InvitationUploadIn, _: dict = Depends(require_admin)) -> dict:
    """Загрузить файл картинки-приглашения (base64) → Supabase Storage → public URL.
    Возврат {url}; фронт кладёт его в поле invitationUrl и сохраняет обычным PUT /event."""
    if not (body.contentType or "").lower().startswith("image/"):
        raise HTTPException(status_code=422, detail="Нужен файл изображения")
    try:
        raw = base64.b64decode(body.contentBase64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="Битые данные файла")
    if not raw:
        raise HTTPException(status_code=422, detail="Пустой файл")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл больше 5 МБ")
    try:
        url = await vision.upload_invitation(raw, body.contentType)
    except Exception as e:
        await _alert_500("upload_invitation", e)
    if not url:
        raise HTTPException(status_code=502, detail="Не удалось загрузить в хранилище")
    return {"url": url}


@router.put("/event")
async def put_event(body: EventSettingsIn, _: dict = Depends(require_admin)) -> dict:
    """Сохранить настройки ивента. Валидация — как в командах бота: дата ГГГГ-ММ-ДД,
    ссылки http…, для активного ивента нужны дата/время/адрес, для «отправлять
    приглашение» нужен URL картинки."""
    date = (body.eventDate or "").strip()
    time = (body.eventTime or "").strip()
    address = (body.eventAddress or "").strip()
    event_link = (body.eventLink or "").strip()
    course_link = (body.courseLink or "").strip()
    invitation_url = (body.invitationUrl or "").strip()

    # дата: формат ГГГГ-ММ-ДД (иначе планировщик не распознает)
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Дата должна быть в формате ГГГГ-ММ-ДД")
    # ссылки: непустые — только http…
    for val, name in ((event_link, "ссылка оплаты"), (course_link, "ссылка курсов"),
                      (invitation_url, "URL картинки")):
        if val and not val.lower().startswith("http"):
            raise HTTPException(status_code=422, detail=f"{name}: нужен URL (http…)")
    # инварианты команд бота
    if body.eventActive and not (date and time and address):
        raise HTTPException(status_code=422,
                            detail="Для активного ивента нужны дата, время и адрес")
    if body.invitationReady and not invitation_url:
        raise HTTPException(status_code=422,
                            detail="Чтобы отправлять приглашение, задайте URL картинки")

    values = {
        "event_active": "1" if body.eventActive else "0",
        "event_date": date,
        "event_time": time,
        "event_address": address,
        "event_link": event_link,
        "course_link": course_link,
        "invitation_url": invitation_url,
        "invitation_ready": "1" if body.invitationReady else "0",
    }
    try:
        for key, val in values.items():
            await db.set_setting(key, val)
    except Exception as e:
        await _alert_500("put_event", e)
    return _event_out(values)
