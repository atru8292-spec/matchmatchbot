"""Решения Арины 2026-10-24.

1. Цену услуги бот не называет никогда: ни в готовых текстах, ни в ответе модели (сумма или
   валюта — ответ не уходит, одна повторная попытка, потом молчание и алерт). «¿Cuánto cuesta?» и
   «es muy caro» на пути услуги — звонок вместо суммы; согласие на звонок — ручной режим и алерт
   «Лид готов к созвону» Ане и Арине, время бот не назначает.
2. Не из Мехико или не может в среду — персональный подбор и короткий звонок.
3. Чек об оплате после ссылки на билет (фото, PDF, «te mando el comprobante») — не селфи и не
   модель: один ответ, алерт «Возможно, оплатил» с кнопкой подтверждения; повтор за 10 минут —
   без второго ответа и алерта; «ya pagué» вместе с фото — один ответ.
4. Фото первым сообщением — не больше двух эмодзи (🎃 темы не считается).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import ai_conversation as rag
import db
import escalation
import filters
import knowledge
import main
import pago_recibo
import servicio
from tests.test_rag_e2e import LINK, SETTINGS, Dialog, all_text, model

THEME = "Esta vez es temática de Halloween 🎃"
EV = {**SETTINGS, "event_theme_phrase": THEME}
SERVICE_SUMS = ["10,000", "10000", "3,000", "14,000", "45,000", "5 mil", "USD", "dólares"]


def started(d):
    """Разговор уже идёт (приветствие было) — иначе первый ход лида получит приветствие."""
    d._add("lead", "Hola")
    d._add("anna", "¡Hola! 🤍 Soy Anna, fundadora de Match Match Agency.")
    return d


def no_sums(texts):
    return not any(rag.suma_no_evento(t) for t in texts) and \
        not any(s.lower() in t.lower() for t in texts for s in SERVICE_SUMS)


# ───────────────────────── 1. услуга без цены ─────────────────────────

def test_seed_has_no_service_sums():
    for r in knowledge.load_seed():
        text = " ".join(str(r.get(k) or "") for k in ("contenido_es", "como_responder", "frases_ejemplo"))
        assert not rag.suma_no_evento(text), r["id"]
        for s in ("10,000", "3,000 USD", "14,000", "45,000", "5 mil mensuales"):
            assert s not in text, (r["id"], s)


def test_prompts_have_no_service_sums():
    for path in ("anna_prompt_conversation.md", "anna_prompt_v5.md"):
        text = open(path, encoding="utf-8").read()
        for s in ("10,000", "$3,000", "14,000", "45,000", "5 mil mensuales"):
            assert s not in text, (path, s)


# ───────────────────────── 3. чек об оплате ─────────────────────────

NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)


def _h(direction, text, minutes_ago, mid=None):
    return {"id": mid or f"h{minutes_ago}{direction}", "direction": direction,
            "sender": "lead" if direction == "inbound" else "anna", "text": text,
            "created_at": NOW - timedelta(minutes=minutes_ago), "meta": {}}


def _photo(mid="p1", caption=None):
    return {"id": mid, "text": "[photo received]",
            "meta": {"content_type": "photo", "content_uri": "https://x/p.jpg", "caption": caption}}


def _doc(mid="d1"):
    return {"id": mid, "text": "[document]", "meta": {"content_type": "document"}}


def _txt(text, mid="t1"):
    return {"id": mid, "text": text, "meta": {"content_type": "text"}}


AFTER_LINK = [_h("outbound", f"Aquí puedes apartar tu lugar: {LINK}", 60)]


class TestReciboUnit:
    def test_context_link(self):
        ctx = pago_recibo.contexto(AFTER_LINK, set(), LINK, NOW)
        assert ctx == {"enlace": True, "reclamo": False, "ack_reciente": False}

    def test_photo_request_after_link_means_selfie(self):
        hist = AFTER_LINK + [_h("outbound", "¿Me mandas una foto tuya?", 30)]
        assert pago_recibo.contexto(hist, set(), LINK, NOW)["enlace"] is False

    def test_otra_foto_request_after_link_means_selfie(self):
        hist = AFTER_LINK + [_h("outbound", "Me mandas otra foto donde estés tú solito y se vea bien tu cara porfa? 🤍", 30)]
        assert pago_recibo.contexto(hist, set(), LINK, NOW)["enlace"] is False

    def test_recent_claim(self):
        hist = [_h("inbound", "ya pagué", 5)]
        assert pago_recibo.contexto(hist, set(), LINK, NOW)["reclamo"] is True

    def test_ack_dedup_window(self):
        assert pago_recibo.contexto([_h("outbound", pago_recibo.ACK, 9)], set(), None, NOW)["ack_reciente"]
        assert not pago_recibo.contexto([_h("outbound", pago_recibo.ACK, 11)], set(), None, NOW)["ack_reciente"]

    @pytest.mark.parametrize("msgs", [[_photo()], [_doc()], [_txt("te mando el comprobante")],
                                      [_photo(caption="aquí está el comprobante")],
                                      [_txt("listo"), _photo()]], ids=["photo", "pdf", "text", "caption", "listo+photo"])
    def test_receipt_after_link(self, msgs):
        ctx = {"enlace": True, "reclamo": False}
        if msgs[0]["text"] == "listo":
            # «listo» + фото — текст не про оплату: обычный путь (фото = селфи)
            assert pago_recibo.es_recibo(msgs, ctx) is None
        else:
            assert pago_recibo.es_recibo(msgs, ctx)

    def test_no_link_no_receipt(self):
        assert pago_recibo.es_recibo([_photo()], {"enlace": False, "reclamo": False}) is None

    def test_question_is_not_receipt(self):
        assert pago_recibo.es_recibo([_txt("¿te mando el comprobante?")], {"enlace": True}) is None


class TestReciboMain:
    PHONE = "wa_5215512345678"

    def _setup(self, monkeypatch, msgs, history, decision="needs_ai"):
        monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
        monkeypatch.setattr(db, "mark_messages_processed", AsyncMock())
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": self.PHONE, "whatsapp_name": "T"}))
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset()))
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"bot_paused": "0", "event_link": LINK}))
        monkeypatch.setattr(db, "get_conversation_history",
                            AsyncMock(return_value=history + [dict(m, direction="inbound",
                                                                   created_at=NOW) for m in msgs]))
        monkeypatch.setattr(pago_recibo, "_now", lambda: NOW)
        if decision != "real":
            monkeypatch.setattr(filters, "decide", lambda *a, **k: filters.Decision(decision, "r"))
        self.photos = AsyncMock(); monkeypatch.setattr(main, "_process_photos", self.photos)
        self.ai = AsyncMock(); monkeypatch.setattr(main, "_run_ai", self.ai)
        self.send = AsyncMock(return_value=1); monkeypatch.setattr(main.sender, "send", self.send)
        self.alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_payment", self.alert)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("msgs", [[_photo()], [_doc()], [_txt("te mando el comprobante")]],
                             ids=["photo", "pdf", "comprobante"])
    async def test_receipt_after_link_no_selfie_no_model(self, monkeypatch, msgs):
        self._setup(monkeypatch, msgs, AFTER_LINK)
        await main._process_burst(self.PHONE)
        self.photos.assert_not_awaited()
        self.ai.assert_not_awaited()
        self.send.assert_awaited_once_with(self.PHONE, [pago_recibo.ACK])
        self.alert.assert_awaited_once()
        assert self.alert.call_args.kwargs["posible"] is True

    @pytest.mark.asyncio
    async def test_photo_without_link_is_selfie(self, monkeypatch):
        self._setup(monkeypatch, [_photo()], [])
        await main._process_burst(self.PHONE)
        self.photos.assert_awaited_once()
        self.alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_within_10_min_silent(self, monkeypatch):
        hist = AFTER_LINK + [_h("inbound", "ya pagué", 6), _h("outbound", pago_recibo.ACK, 5)]
        self._setup(monkeypatch, [_photo()], hist)
        await main._process_burst(self.PHONE)
        self.send.assert_not_awaited()
        self.alert.assert_not_awaited()
        self.photos.assert_not_awaited()
        self.ai.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ya_pague_twice_one_reply(self, monkeypatch):
        hist = AFTER_LINK + [_h("inbound", "ya pagué", 3), _h("outbound", pago_recibo.ACK, 2)]
        self._setup(monkeypatch, [_txt("ya pagué")], hist, decision="real")
        await main._process_burst(self.PHONE)
        self.send.assert_not_awaited()
        self.alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ya_pague_plus_photo_one_reply_one_alert(self, monkeypatch):
        self._setup(monkeypatch, [_txt("ya pagué"), _photo(mid="p2")], AFTER_LINK, decision="real")
        await main._process_burst(self.PHONE)
        self.send.assert_awaited_once_with(self.PHONE, [pago_recibo.ACK])
        self.alert.assert_awaited_once()
        assert "фото" in self.alert.call_args.kwargs["detalle"]
        self.photos.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ya_pague_after_11_min_new_reply(self, monkeypatch):
        hist = AFTER_LINK + [_h("outbound", pago_recibo.ACK, 11)]
        self._setup(monkeypatch, [_txt("ya pagué")], hist, decision="real")
        await main._process_burst(self.PHONE)
        self.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_payment_alert_text_and_buttons(monkeypatch):
    monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "111")
    monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "222")
    send = AsyncMock(); monkeypatch.setattr(escalation, "_send_telegram", send)
    await escalation.notify_payment({"phone": "wa_5215512345678"}, posible=True, detalle="Прислал фото")
    assert {c.args[1] for c in send.await_args_list} == {"111", "222"}
    text, kb = send.await_args_list[0].args[2], send.await_args_list[0].args[3]
    assert text.startswith("💰 Возможно, лид оплатил, проверь")
    assert kb == escalation.payment_action_kb("wa_5215512345678")


# ───────────────────────── 4. фото первым сообщением ─────────────────────────

@pytest.mark.asyncio
async def test_photo_first_greeting_max_two_emojis(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    r = await d.say("", photo=True)
    t = all_text(r)
    assert t[0].startswith("¡Gracias por tu foto!")
    no_theme = [x.replace(THEME, "") for x in t]
    assert rag.emoji_count(no_theme) <= 2, t
    assert any(THEME in x or "[event_theme_phrase]" in x for x in t), "тема ивента осталась"


@pytest.mark.asyncio
async def test_payment_alert_survives_send_failure(monkeypatch):
    monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.sender, "send", AsyncMock(side_effect=RuntimeError("wazzup")))
    alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_payment", alert)
    err = AsyncMock(); monkeypatch.setattr(escalation, "notify_error", err)
    await main._payment_reply("wa_5215512345678", {}, posible=True, detalle="x")
    alert.assert_awaited_once()
    err.assert_awaited_once()
