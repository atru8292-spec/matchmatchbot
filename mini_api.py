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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel

import ai
import db
import escalation
import funnel
import media
import scheduler
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
    """Справочники для фильтров фронта: стадии воронки (код+название) + глобальная пауза бота."""
    paused = False
    if db.is_ready():
        try:
            paused = (await db.get_settings(["bot_paused"])).get("bot_paused") == "1"
        except Exception:
            paused = False
    return {
        "stages": [{"code": code, "label": label}
                   for code, label in funnel.FUNNEL_STAGES.items()],
        "activeStages": list(funnel.ACTIVE_STAGES),
        "botPaused": paused,
    }


class BotPauseIn(BaseModel):
    paused: bool


@router.post("/bot/pause")
async def set_bot_pause(body: BotPauseIn, _: dict = Depends(require_admin)) -> dict:
    """Глобальная пауза бота (тех. режим): вкл → бот молчит всем, кроме bypass-номеров."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        await db.set_setting("bot_paused", "1" if body.paused else "0")
    except Exception as e:
        await _alert_500("set_bot_pause", e)
    return {"botPaused": body.paused}


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
    # override=true — Аня подтвердила отправку лиду с do_not_contact (opt-out). Без него
    # бэкенд отдаёт 409, фронт показывает предупреждение (как дубль-варнинг для day-of).
    override: bool = False


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
        # opt-out: лид просил не писать. Не блокируем Аню жёстко (человек решает), но
        # требуем явного override — фронт показывает предупреждение и переспрашивает.
        if lead.get("do_not_contact") and not body.override:
            raise HTTPException(
                status_code=409,
                detail="Este lead pidió no ser contactado (opt-out). Confirma para enviar de todos modos.")
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
    "event_active", "event_date", "event_time", "event_start", "event_end",
    "event_address",
    "event_price_member", "event_price_nonmember", "event_price_old",
    "event_link", "course_link", "invitation_url", "invitation_ready",
    "event_guest_tab",
]


class EventSettingsIn(BaseModel):
    eventActive: bool = False
    eventDate: Optional[str] = None
    eventStart: Optional[str] = None
    eventEnd: Optional[str] = None
    eventAddress: Optional[str] = None
    eventPriceMember: Optional[str] = None
    eventPriceNonmember: Optional[str] = None
    eventPriceOld: Optional[str] = None
    eventLink: Optional[str] = None
    courseLink: Optional[str] = None
    invitationUrl: Optional[str] = None
    invitationReady: bool = False
    eventGuestTab: Optional[str] = None  # имя вкладки гостевого списка в книге Ани
    # eventTime не редактируется отдельно — зеркалим из eventStart (см. put_event),
    # чтобы #15/#47/#54 ([event_time]) и #51/#52 ([event_start]) не расходились.
    eventTime: Optional[str] = None


def _event_out(s: dict) -> dict:
    return {
        "eventActive": s.get("event_active") == "1",
        "eventDate": s.get("event_date") or "",
        "eventStart": s.get("event_start") or "",
        "eventEnd": s.get("event_end") or "",
        "eventTime": s.get("event_time") or "",
        "eventAddress": s.get("event_address") or "",
        "eventPriceMember": s.get("event_price_member") or "",
        "eventPriceNonmember": s.get("event_price_nonmember") or "",
        "eventPriceOld": s.get("event_price_old") or "",
        "eventLink": s.get("event_link") or "",
        "courseLink": s.get("course_link") or "",
        "invitationUrl": s.get("invitation_url") or "",
        "invitationReady": s.get("invitation_ready") == "1",
        "eventGuestTab": s.get("event_guest_tab") or "",
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
    start = (body.eventStart or "").strip()
    end = (body.eventEnd or "").strip()
    address = (body.eventAddress or "").strip()
    price_member = (body.eventPriceMember or "").strip()
    price_nonmember = (body.eventPriceNonmember or "").strip()
    price_old = (body.eventPriceOld or "").strip()
    event_link = (body.eventLink or "").strip()
    course_link = (body.courseLink or "").strip()
    invitation_url = (body.invitationUrl or "").strip()

    # дата: ISO ГГГГ-ММ-ДД (планировщик считает по ней, бот форматирует в испанскую)
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Дата должна быть в формате ГГГГ-ММ-ДД")
    # цены: непустые — только цифры/запятые/точки/пробелы (отображаемая строка, напр. «6,000»)
    for val, name in ((price_member, "цена члена"), (price_nonmember, "цена не-члена"),
                      (price_old, "старая цена")):
        if val and not all(ch.isdigit() or ch in ",. " for ch in val):
            raise HTTPException(status_code=422, detail=f"{name}: только цифры и разделители")
    # ссылки: непустые — только http…
    for val, name in ((event_link, "ссылка оплаты"), (course_link, "ссылка курсов"),
                      (invitation_url, "URL картинки")):
        if val and not val.lower().startswith("http"):
            raise HTTPException(status_code=422, detail=f"{name}: нужен URL (http…)")
    # инварианты: для активного ивента нужны дата, время начала и адрес
    if body.eventActive and not (date and start and address):
        raise HTTPException(status_code=422,
                            detail="Для активного ивента нужны дата, время начала и адрес")
    if body.invitationReady and not invitation_url:
        raise HTTPException(status_code=422,
                            detail="Чтобы отправлять приглашение, задайте URL картинки")

    values = {
        "event_active": "1" if body.eventActive else "0",
        "event_date": date,
        "event_start": start,
        "event_end": end,
        "event_time": start,  # зеркало: #15/#47/#54 читают [event_time] = времени начала
        "event_address": address,
        "event_price_member": price_member,
        "event_price_nonmember": price_nonmember,
        "event_price_old": price_old,
        "event_link": event_link,
        "course_link": course_link,
        "invitation_url": invitation_url,
        "invitation_ready": "1" if body.invitationReady else "0",
        "event_guest_tab": (body.eventGuestTab or "").strip(),  # strip: у вкладок бывают хвостовые пробелы
    }
    try:
        for key, val in values.items():
            await db.set_setting(key, val)
    except Exception as e:
        await _alert_500("put_event", e)
    return _event_out(values)


# ===== Напоминание дня ивента (предпросмотр + ручная отправка) =====
# Тот же kind/date, что у планировщика (_send_event_daytime) → идемпотентность в обе
# стороны: ручная отправка не даст планировщику продублировать, и наоборот.
_DAY_OF_KIND = "remind_day"
_DAY_OF_MAX_RECIPIENTS = 30  # ручная отправка — точечная; массовую делает планировщик


def _split_template(tmpl: str) -> list[str]:
    """Шаблон → сырые бабблы по \\n\\n (как ai._split_template / scheduler._bubbles)."""
    return [p.strip() for p in (tmpl or "").split("\n\n") if p.strip()]


def _default_template(funnel_stage: str | None) -> str:
    """A — оплатившим/членам (PAID_STAGES), B — остальным. Как в _send_event_daytime."""
    return "A" if funnel_stage in scheduler.PAID_STAGES else "B"


def _lead_display_name(lead: dict) -> str:
    return lead.get("name") or lead.get("whatsapp_name") or (lead.get("phone") or "").replace("wa_", "", 1)


async def _render_day_of_template(template_id: int) -> list[str]:
    """Готовые (с подставленными значениями) бабблы шаблона для предпросмотра/отправки."""
    tmpl = await db.get_scenario_template(template_id)
    if not tmpl:
        return []
    # phone=None + allow_repeat=True: дедуп ссылок не применяем (это предпросмотр)
    return await sender.render_bubbles(_split_template(tmpl), phone=None, allow_repeat_links=True)


@router.get("/event/day-of/preview")
async def day_of_preview(_: dict = Depends(require_admin)) -> dict:
    """Предпросмотр обоих шаблонов дня ивента с реальными значениями (read-only).
    A (#47) — оплатившим, без ссылки; B (#54) — неоплатившим, со ссылкой на билет."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        a = await _render_day_of_template(scheduler.REMIND_DAY_SCENARIO)
        b = await _render_day_of_template(scheduler.REMIND_DAY_UNPAID_SCENARIO)
    except Exception as e:
        await _alert_500("day_of_preview", e)
    return {"templateA": a, "templateB": b}


@router.get("/event/day-of/recipients")
async def day_of_recipients(_: dict = Depends(require_admin)) -> dict:
    """Кандидаты для ручного напоминания: лиды с selected_service='event'. У каждого —
    какой шаблон уйдёт по умолчанию (A/B по funnel_stage) и слали ли уже (идемпотентность)."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        event_date = ((await db.get_settings(["event_date"])).get("event_date") or "").strip()
        cands = await db.event_lead_candidates()
        out = []
        for c in cands:
            phone = c["phone"]
            at = (await db.event_reminder_sent_at(phone, _DAY_OF_KIND, event_date)
                  if event_date else None)
            out.append({
                "phone": phone,
                "name": _lead_display_name(c),
                "funnelStage": c.get("funnel_stage"),
                "funnelStageLabel": funnel.stage_label(c.get("funnel_stage")),
                "template": _default_template(c.get("funnel_stage")),
                "alreadySent": at is not None,
                "sentAt": _iso(at),
            })
    except Exception as e:
        await _alert_500("day_of_recipients", e)
    return {"recipients": out, "eventDate": event_date}


class DayOfRecipientIn(BaseModel):
    phone: str
    template: str = "auto"  # auto (по funnel_stage) | A | B (override)


class DayOfSendIn(BaseModel):
    recipients: List[DayOfRecipientIn]
    force: bool = False  # повторить, даже если уже слали (после подтверждения дубля)


@router.post("/event/day-of/send")
async def day_of_send(body: DayOfSendIn, user: dict = Depends(require_admin)) -> dict:
    """Ручная отправка напоминания дня ивента выбранным лидам.

    Шаблон: override (A/B) или auto по funnel_stage. Дубль (уже слали — планировщиком
    или вручную) при force=false НЕ шлётся, попадает в duplicates с датой. force=true —
    шлём повторно. do_not_contact уважается; авто-takeover НЕ делаем (режим не трогаем);
    после отправки логируем маркер (kind=remind_day) — планировщик не продублирует."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    if not body.recipients:
        raise HTTPException(status_code=422, detail="recipients required")
    if len(body.recipients) > _DAY_OF_MAX_RECIPIENTS:
        raise HTTPException(status_code=422,
                            detail=f"Не больше {_DAY_OF_MAX_RECIPIENTS} за раз "
                                   "(массовую рассылку планировщик сделает сам в день ивента)")
    event_date = ((await db.get_settings(["event_date"])).get("event_date") or "").strip()
    if not event_date:
        raise HTTPException(status_code=422, detail="Дата ивента не задана")

    try:
        raw = {
            "A": _split_template(await db.get_scenario_template(scheduler.REMIND_DAY_SCENARIO)),
            "B": _split_template(await db.get_scenario_template(scheduler.REMIND_DAY_UNPAID_SCENARIO)),
        }
        if not raw["A"] or not raw["B"]:
            raise HTTPException(status_code=500, detail="Шаблон #47/#54 не найден")

        sent, duplicates, failed = [], [], []
        for r in body.recipients:
            try:
                phone = _norm_phone(r.phone)
            except HTTPException:
                failed.append({"phone": r.phone, "name": r.phone, "reason": "неверный номер"})
                continue
            lead = await db.get_lead_by_phone(phone)
            if not lead:
                failed.append({"phone": phone, "name": phone, "reason": "лид не найден"})
                continue
            name = _lead_display_name(lead)
            if lead.get("do_not_contact"):
                failed.append({"phone": phone, "name": name, "reason": "бот не пишет (заблокирован)"})
                continue
            tmpl = r.template if r.template in ("A", "B") else _default_template(lead.get("funnel_stage"))
            already = await db.event_reminder_sent(phone, _DAY_OF_KIND, event_date)
            if already and not body.force:
                at = await db.event_reminder_sent_at(phone, _DAY_OF_KIND, event_date)
                duplicates.append({"phone": phone, "name": name, "template": tmpl, "sentAt": _iso(at)})
                continue
            # рендер + отправка бабблами; meta.manual=true (в таймлайне «Anna вручную»)
            bubbles = await sender.render_bubbles(raw[tmpl], phone, allow_repeat_links=(tmpl == "B"))
            chat_id = phone.replace("wa_", "", 1)
            delivered_any = False
            for b in bubbles:
                await asyncio.sleep(sender.compute_delay(b))
                ok = await sender.send_one(chat_id, b)
                await db.save_manual_message(phone, b, ok)
                delivered_any = delivered_any or ok
            if delivered_any:
                if not already:  # маркер уже есть при force-повторе — не дублируем
                    await db.log_event_reminder(phone, _DAY_OF_KIND, event_date)
                sent.append({"phone": phone, "name": name, "template": tmpl})
            else:
                failed.append({"phone": phone, "name": name, "reason": "не доставлено"})
    except HTTPException:
        raise
    except Exception as e:
        await _alert_500("day_of_send", e)
    return {"sent": sent, "duplicates": duplicates, "failed": failed}


# ===== Тест переписки (/api/mini/test-chat) — песочница, НИЧЕГО не пишет в БД =====
# Прогоняет сообщение через РЕАЛЬНЫЙ ai.generate_reply (тот же пайплайн, что для
# боевых лидов: RAG + funnel-guard + контекст-фолбэк + генерация). READ-ONLY:
# не создаёт лида (leads), не пишет messages, не трогает фото. История диалога и
# профиль тест-лида приходят из тела запроса (память сессии живёт на фронте).

class TestChatProfile(BaseModel):
    """Накопленный профиль тест-лида (фронт мёржит extracted после каждого ответа)."""
    isSingle: Optional[bool] = None
    age: Optional[int] = None
    profession: Optional[str] = None
    city: Optional[str] = None
    interest: Optional[str] = None
    photoReceived: bool = False
    funnelStage: Optional[str] = None
    whatsappName: Optional[str] = "Test"


class TestChatMessage(BaseModel):
    sender: str  # "lead" | "anna"
    text: str


class TestChatIn(BaseModel):
    leadProfile: TestChatProfile = TestChatProfile()
    history: List[TestChatMessage] = []
    message: str


@router.post("/test-chat")
async def test_chat(body: TestChatIn, _: dict = Depends(require_admin)) -> dict:
    """Песочница: ответ бота на сообщение без записи в БД.

    Изоляция: строим синтетический lead-dict в памяти (без phone → нечего писать),
    зовём только ai.generate_reply (read-only) + sender.render_bubbles(phone=None).
    НЕ вызываем insert_message / upsert_lead / save_photo.
    """
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Пустое сообщение")

    p = body.leadProfile
    lead = {
        "phone": None,
        "whatsapp_name": (p.whatsappName or "Test"),
        "is_single": p.isSingle, "age": p.age, "profession": p.profession,
        "city": p.city, "interest": p.interest,
        "photo_received": p.photoReceived, "funnel_stage": p.funnelStage,
    }
    history = [{"sender": m.sender, "text": m.text} for m in body.history]

    try:
        result = await ai.generate_reply(lead, history, text)
        # рендер как увидит лид (подстановка [event_link]/[event_date]…; phone=None → без дедупа)
        rendered = await sender.render_bubbles(result.get("messages") or [], phone=None)
        # дебаг: сырые RAG-кандидаты со score + названия (для отладки, какой сценарий матчил)
        try:
            cands = await ai.search_scenarios(text, top_k=3)
        except Exception:
            logger.exception("test_chat: RAG-дебаг упал (не критично)")
            cands = []
        rag = [{"id": c["id"], "score": round(c.get("score", 0.0), 3),
                "title": await db.get_scenario_title(c["id"])} for c in cands]
        used_id = result.get("used_scenario_id")
        used_title = await db.get_scenario_title(used_id) if isinstance(used_id, int) else None
    except Exception as e:
        await _alert_500("test_chat", e)

    return {
        "messages": rendered,
        "extracted": result.get("extracted") or {},
        "funnelStage": result.get("funnel_stage"),
        "usedScenarioId": used_id,
        "usedScenarioTitle": used_title,
        "action": result.get("action"),
        "needsEscalation": bool(result.get("needs_escalation")),
        "ragCandidates": rag,
    }


# ===== Медиа с ивентов (/api/mini/event/media) — фото/видео для отправки ботом =====
# Хранение — Supabase Storage (префикс event-media/), тот же механизм, что приглашение.
# Видео перекодируется под требования Wazzup (mp4/H.264/AAC ≤16 МБ); не влезло после
# сжатия → 422 с понятным сообщением (Аня обрежет сама).

def _media_out(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "url": row.get("storage_url"),
        "mediaType": row.get("media_type"),
        "sizeBytes": row.get("size_bytes"),
        "isActive": bool(row.get("is_active")),
        "createdAt": _iso(row.get("created_at")),
    }


@router.get("/event/media")
async def list_event_media(_: dict = Depends(require_admin)) -> dict:
    """Список медиа с ивентов (для CRM)."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        rows = await db.list_event_media()
    except Exception as e:
        await _alert_500("list_event_media", e)
    return {"media": [_media_out(r) for r in rows]}


@router.post("/event/media")
async def upload_event_media(file: UploadFile = File(...),
                             _: dict = Depends(require_admin)) -> dict:
    """Загрузить фото/видео с ивента (multipart). Видео сжимается под лимит Wazzup.

    Фото > 5 МБ или не-jpg/png/webp → 422. Видео: транскод в mp4/H.264 ≤16 МБ; если после
    сжатия всё равно тяжелее — 422 с объяснением (Аня обрежет и попробует снова)."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    kind = media.classify(file.content_type or "")
    if not kind:
        raise HTTPException(status_code=422,
                            detail="Formato no soportado. Sube imagen (jpg/png/webp) o video (mp4).")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Archivo vacío")
    if len(raw) > media.UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Archivo demasiado grande (máx {media.UPLOAD_MAX_BYTES // (1024*1024)} MB).")

    if kind == "image":
        if len(raw) > media.IMAGE_MAX_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"La imagen supera {media.IMAGE_MAX_BYTES // (1024*1024)} MB.")
        data, ext, ctype = raw, media.IMAGE_EXTS[(file.content_type or "").lower()], file.content_type
    else:  # video → транскод под Wazzup (в отдельном потоке, ffmpeg блокирующий)
        try:
            data = await asyncio.to_thread(media.transcode_video, raw)
        except media.VideoTooLargeError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except media.VideoProcessingError as e:
            raise HTTPException(status_code=422, detail=str(e))
        ext, ctype = "mp4", "video/mp4"

    url, path = await vision.upload_event_media(data, ext, ctype)
    if not url:
        raise HTTPException(status_code=502, detail="No se pudo subir al almacenamiento")
    try:
        row = await db.add_event_media(url, path, kind, len(data))
    except Exception as e:
        await _alert_500("upload_event_media", e)
    return _media_out(row)


@router.delete("/event/media/{media_id}")
async def delete_event_media(media_id: int, _: dict = Depends(require_admin)) -> dict:
    """Удалить медиа с ивента по id."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        ok = await db.delete_event_media(media_id)
    except Exception as e:
        await _alert_500("delete_event_media", e)
    if not ok:
        raise HTTPException(status_code=404, detail="Media not found")
    return {"deleted": media_id}
