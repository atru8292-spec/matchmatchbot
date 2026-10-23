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


@pytest.mark.parametrize("text", [servicio.PRECIO_A, servicio.PRECIO_B, servicio.CARO,
                                  servicio.FUERA, servicio.LLAMADA_OK])
def test_texts_no_sums_and_max_two_emojis(text):
    assert no_sums([text])
    assert rag.emoji_count([text]) <= 2
    assert "perfil" not in text.lower() and "—" not in text


@pytest.mark.asyncio
async def test_live_service_scenario(monkeypatch):
    """Сценарий для живого прогона: услуга → анкета → фото → цена → как работает → реально ли →
    дорого → «sí, llámame». Ни в одном ответе нет суммы; согласие — готов к созвону."""
    d = Dialog(monkeypatch, settings=EV)
    r = await d.say("Hola, busco conocer una mujer eslava para algo serio")
    assert d.lead.get("interest") == "agency"
    assert all_text(r)[-1] == rag.cuestionario.COMPOUND_SERVICE
    r = await d.say("Luis, 35, abogado, soltero")
    assert all_text(r)[-1].endswith(rag.cuestionario.FOTO_PEDIDO)
    r = await d.say("", photo=True)
    assert r["rag_meta"]["fuentes"] == ["service_pitch"]
    assert no_sums(all_text(r))
    r = await d.say("¿cuánto cuesta?")
    assert all_text(r) == [servicio.PRECIO] and d.model_calls == []
    d.models["¿cómo funciona?"] = model(
        ["Busco mujeres eslavas que encajen contigo y te las presento una por una."],
        fuentes=["service_what"])
    r = await d.say("¿cómo funciona?")
    assert r["action"] == "respond" and no_sums(all_text(r))
    d.models["¿es real, no es una estafa?"] = model(
        ["Sí, es real: soy Anna y puedes conocerme en Instagram, @rusaencdmx."],
        fuentes=["agency_trust"])
    r = await d.say("¿es real, no es una estafa?")
    assert no_sums(all_text(r))
    r = await d.say("es muy caro")
    assert all_text(r) == [servicio.CARO]
    r = await d.say("sí, llámame")
    assert all_text(r) == [servicio.LLAMADA_OK] and r.get("llamada_lista") is True
    assert no_sums([m["text"] for m in d.history if m["sender"] == "anna"])


@pytest.mark.asyncio
async def test_second_price_question_goes_to_anya(monkeypatch):
    d = Dialog(monkeypatch, lead={"interest": "agency", "name": "Luis", "is_single": True})
    d._add("anna", "Platícame de ti: ¿cómo te llamas, cuántos años tienes, a qué te dedicas y estás soltero? 🤍")
    r = await d.say("¿cuánto cuesta el servicio?")
    assert all_text(r) == [servicio.PRECIO]
    r = await d.say("pero dime cuánto cuesta")
    assert r["action"] == "silent" and d.model_calls == []


@pytest.mark.asyncio
async def test_price_question_with_service_word_on_event_route(monkeypatch):
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    r = await d.say("¿y cuánto cuesta el servicio personalizado?")
    assert all_text(r) == [servicio.PRECIO]


@pytest.mark.asyncio
async def test_model_sum_retry_then_silent(monkeypatch):
    """Модель дважды пишет сумму услуги → ответ не уходит, одна повторная попытка, молчание."""
    d = Dialog(monkeypatch, lead={"interest": "agency", "name": "Luis", "is_single": True,
                                  "age": 35, "profession": "abogado", "photo_received": True})
    d._add("anna", "¿Te parece si lo platicamos en una llamada corta?")
    d.models["¿y qué incluye el paquete?"] = model(["El paquete de 3 matches son 3,000 USD."])
    r = await d.say("¿y qué incluye el paquete?")
    assert r["action"] == "silent" and r["messages"] == []
    assert len(d.model_calls) == 2
    assert r["silent_kind"] == rag.VERIFICACION
    assert rag.MONEY_REASON in r["silent_reason"]


@pytest.mark.asyncio
async def test_model_sum_retry_then_clean_answer(monkeypatch):
    d = Dialog(monkeypatch, lead={"interest": "agency", "name": "Luis", "is_single": True,
                                  "age": 35, "profession": "abogado", "photo_received": True})
    answers = iter([model(["Son 10 mil dólares."]),
                    model(["Depende de lo que buscas; lo platicamos en una llamada corta."])])
    calls = []

    async def chat(model_name, system, user, cost, **kw):
        if model_name == rag.CHECK_MODEL:
            return {"afirmaciones": []}
        calls.append(user.get("reintento"))
        return next(answers)
    monkeypatch.setattr(rag, "_chat", chat)
    r = await d.say("¿y qué incluye?")
    assert r["action"] == "respond" and no_sums(r["messages"])
    assert calls[1] and "monto" in calls[1]


@pytest.mark.asyncio
async def test_service_objection_twice_goes_to_anya(monkeypatch):
    d = started(Dialog(monkeypatch, lead={"interest": "agency", "name": "Luis", "is_single": True}))
    r = await d.say("está muy caro")
    assert all_text(r) == [servicio.CARO]
    r = await d.say("sigue siendo caro")
    assert r["action"] == "silent"


@pytest.mark.parametrize("text,ok", [
    ("sí", True), ("Sí, llámame", True), ("va", True), ("claro, ¿a qué hora?", True),
    ("sí me parece", True), ("no gracias", False), ("prefiero por aquí", False),
    ("¿cuánto cuesta?", False), ("sí, pero ¿cómo funciona?", False),
    ("Sí, soy Juan, 45 años, médico", False), ("Claro, soy soltero", False),
    ("sí pero cuánto cuesta", False)])
def test_acepta_llamada(text, ok):
    assert servicio.acepta_llamada(text) is ok


@pytest.mark.asyncio
async def test_yes_without_call_offer_is_not_call_ready(monkeypatch):
    d = started(Dialog(monkeypatch, lead={"interest": "agency", "name": "Luis", "is_single": True}))
    d._add("anna", "¿Cuántos años tienes y a qué te dedicas?")
    d.models["sí"] = model(["Perfecto."])
    r = await d.say("sí")
    assert not r.get("llamada_lista")


@pytest.mark.asyncio
async def test_main_call_ready_manual_and_alert(monkeypatch):
    sent, upd = AsyncMock(return_value=1), AsyncMock()
    monkeypatch.setattr(main.sender, "send", sent)
    monkeypatch.setattr(db, "update_lead_fields", upd)
    monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
    monkeypatch.setattr(db, "reset_followup_timer", AsyncMock())
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"mode": "auto"}))
    alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_call_ready", alert)
    result = rag._result([servicio.LLAMADA_OK], llamada_lista=True, rag_meta={})
    await main._apply_ai_result("wa_5215512345678", {"phone": "wa_5215512345678"}, "sí", result)
    upd.assert_any_await("wa_5215512345678", mode="manual")
    alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_ready_alert_to_anya_and_arina(monkeypatch):
    monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "111")
    monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "222")
    send = AsyncMock(); monkeypatch.setattr(escalation, "_send_telegram", send)
    await escalation.notify_call_ready({"phone": "wa_5215512345678", "name": "Luis"}, "sí")
    chats = {c.args[1] for c in send.await_args_list}
    assert chats == {"111", "222"}
    assert "Лид готов к созвону" in send.await_args_list[0].args[2]
    assert "+5215512345678" in send.await_args_list[0].args[2]


# ───────────────────────── 2. не из Мехико / не в среду ─────────────────────────

@pytest.mark.parametrize("text", ["no vivo en CDMX", "vivo en Monterrey", "soy de Guadalajara",
                                  "estoy fuera de México", "no puedo los miércoles",
                                  "el miércoles se me complica", "ese día no puedo"])
@pytest.mark.asyncio
async def test_fuera_or_wednesday_offers_service_and_call(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    r = await d.say(text)
    assert all_text(r) == [servicio.FUERA] and d.model_calls == []
    assert no_sums(all_text(r))
    r = await d.say("sí")
    assert r.get("llamada_lista") is True


@pytest.mark.parametrize("text", ["vivo en la CDMX", "vivo en Polanco", "el miércoles sí puedo",
                                  "vivo en la Narvarte", "vivo en Santa Fe", "vivo en la ciudad",
                                  "vivo en el D.F."])
def test_fuera_not_triggered(text):
    assert servicio.fuera_o_miercoles(text) is None


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
async def test_fuera_with_age_goes_to_filters(monkeypatch):
    """«Tengo 80 años, vivo en Monterrey» — фильтр возраста важнее предложения подбора."""
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    d.models["tengo 80 años, vivo en Monterrey"] = model(["Gracias."], extracted={"age": 80})
    r = await d.say("tengo 80 años, vivo en Monterrey")
    assert servicio.FUERA not in all_text(r)


@pytest.mark.asyncio
async def test_price_plus_discount_goes_silent(monkeypatch):
    """Скидка — молча Ане (решение владелицы) важнее ответа про цену услуги."""
    d = started(Dialog(monkeypatch, lead={"interest": "agency", "name": "Luis", "is_single": True}))
    r = await d.say("¿cuánto cuesta el servicio? ¿hay descuento?")
    assert r["action"] == "silent"


@pytest.mark.asyncio
async def test_payment_alert_survives_send_failure(monkeypatch):
    monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.sender, "send", AsyncMock(side_effect=RuntimeError("wazzup")))
    alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_payment", alert)
    err = AsyncMock(); monkeypatch.setattr(escalation, "notify_error", err)
    await main._payment_reply("wa_5215512345678", {}, posible=True, detalle="x")
    alert.assert_awaited_once()
    err.assert_awaited_once()
