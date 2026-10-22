"""Сквозные диалоги режима rag (2026-10-22): весь путь лида, а не куски.

Настоящие: memory.build по растущей переписке, база знаний (seed), готовые ходы кодом,
все проверки ответа, сохранение extracted в лида (как main._apply_ai_result), отметка
одобренного фото (как main._process_photos). Подменены: модель (заглушка по сообщению лида),
эмбеддинги, БД (переписка в памяти теста), подписи к медиа.

Ловят то, что пропустили тесты по кускам: прод 2026-10-22 — после одобренного фото бот
ответил ценой и снова попросил фото, потому что интерес к ивенту не сохранился, а
благодарность «tu foto» срезалась как просьба о фото.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import ai
import ai_conversation as rag
import db
import knowledge
import memory

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
LINK = "https://www.rusaencdmx.com/18-11-26-slavic-latino-night-halloween"
SETTINGS = {"event_active": "1", "event_date": "2026-11-18", "event_start": "8:30 PM",
            "event_address": "Roma Norte, CDMX", "event_price_nonmember": "6000",
            "event_price_old": "9,000", "event_link": LINK}
PHONE = "wa_79635708880"
PHOTO_OK = "[фото одобрено]"
SEED = knowledge.load_seed()
BY_ID = {r["id"]: r for r in SEED}
S1 = BY_ID["greeting_hola"]["contenido_es"].split("\n\n")
VIDEO_MARK = db.media_marker("video", SETTINGS["event_date"])
PHOTO_MARK = db.media_marker("image", SETTINGS["event_date"])


def model(bubbles, extracted=None, paso=None, **kw):
    return {"bubbles": bubbles, "accion": "respond", "fuentes": [], "falta_dato": False,
            "en_tema": True, "extracted": extracted or {},
            "paso_extra": {"tipo": "pregunta", "texto": paso} if paso else {"tipo": None, "texto": ""},
            **kw}


class Dialog:
    """Лид пишет боту; переписка и профиль живут в памяти теста, как в БД."""

    def __init__(self, monkeypatch, lead=None, settings=None):
        self.lead = {"phone": PHONE, "whatsapp_name": "Arina", "mode": "auto", **(lead or {})}
        self.history: list[dict] = []
        self.models: dict[str, dict] = {}       # сообщение лида → ответ заглушки модели
        self.model_calls: list[str] = []
        self.t = NOW - timedelta(hours=2)
        knowledge.reset_cache()
        knowledge._seed_embeddings.clear()
        monkeypatch.setattr(knowledge, "_load_from_db", AsyncMock(return_value=None))
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=settings or SETTINGS))
        monkeypatch.setattr(db, "get_conversation_history",
                            AsyncMock(side_effect=lambda phone, limit=30: list(self.history)))
        monkeypatch.setattr(db, "get_lead_notes", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "get_funnel_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(memory, "_evento_desde", AsyncMock(return_value=NOW - timedelta(days=2)))
        monkeypatch.setattr(db, "search_scenarios_by_vector", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "add_daily_amount", AsyncMock(return_value=0.01))
        monkeypatch.setattr(db, "get_daily_amount", AsyncMock(return_value=0.0))

        async def no_caption(reply, scenario, lead):
            return None
        monkeypatch.setattr(ai, "_maybe_announce_event_video", no_caption)
        monkeypatch.setattr(ai, "_maybe_announce_event_photo", no_caption)
        monkeypatch.setattr(rag, "load_prompt", lambda: "PROMPT")

        async def embed(texts, cost=None):
            return [[1.0, float(len(t) % 7)] for t in texts]
        monkeypatch.setattr(rag, "_embed_batch", embed)

        async def chat(model_name, system, user, cost, **kw):
            if model_name == rag.CHECK_MODEL:
                return {"afirmaciones": []}
            msg = user["lead_message"]
            self.model_calls.append(msg)
            if msg not in self.models:
                raise AssertionError(f"модель не должна была понадобиться на «{msg}»")
            return self.models[msg]
        monkeypatch.setattr(rag, "_chat", chat)

    def _add(self, sender, text, meta=None):
        self.t += timedelta(minutes=1)
        self.history.append({"id": f"m{len(self.history)}", "sender": sender,
                             "direction": "inbound" if sender == "lead" else "outbound",
                             "text": text, "created_at": self.t, "meta": meta or {}})

    async def say(self, text, photo=False):
        """Сообщение лида. photo=True — фото прошло проверку (main ставит photo_received и
        маркер «[фото одобрено]» в текст для AI)."""
        if photo:
            self.lead["photo_received"] = True
            self._add("lead", "[photo received]", {"content_type": "photo"})
            user_text = f"{text}\n\n{PHOTO_OK}" if text else PHOTO_OK
        else:
            self._add("lead", text)
            user_text = text
        r = await rag.generate_reply(dict(self.lead), [], user_text)
        # как main._apply_ai_result: сохранить extracted, отправить, медиа, после медиа
        for k, v in (r.get("extracted") or {}).items():
            self.lead[k] = v
        if r.get("rag_estado") is not None:
            self.lead["rag_estado"] = r["rag_estado"]
        sent = list(r.get("messages") or [])
        for b in sent:
            self._add("anna", b)
        if r.get("send_event_video"):
            self._add("anna", VIDEO_MARK)
        if r.get("send_event_photo"):
            self._add("anna", PHOTO_MARK)
        for b in r.get("after_media") or []:
            self._add("anna", b)
        if r["action"] == "silent":
            self.lead["mode"] = "manual"
        return r


def all_text(r):
    return list(r.get("messages") or []) + list(r.get("after_media") or [])


@pytest.mark.asyncio
async def test_a_event_path_photo_turn(monkeypatch):
    """(а) «hola» → «Me interesa el evento» → имя и «soltero» → фото одобрено → шаг после фото
    целиком, без модели и без повторной просьбы о фото."""
    d = Dialog(monkeypatch)
    r = await d.say("hola")
    assert r["messages"][0] == S1[0] and d.model_calls == []

    # модель не извлекла interest (как в проде) — код определяет и сохраняет его сам
    d.models["Me interesa el evento"] = model(
        ["Es una noche para conocer en persona a mujeres eslavas solteras que buscan una relación seria."],
        paso="¿Cómo te llamas y estás soltero?")
    r = await d.say("Me interesa el evento")
    assert d.lead.get("interest") == "event"
    assert all_text(r)[-1] == "¿Cómo te llamas y estás soltero?"

    d.models["Me llamo Luis, soy soltero"] = model(["¡Gracias, Luis! 😊"],
                                                   extracted={"name": "Luis", "is_single": True})
    r = await d.say("Me llamo Luis, soy soltero")
    assert d.lead["name"] == "Luis" and d.lead["is_single"] is True
    assert all_text(r) == ["¡Gracias, Luis! 😊", "¿Cuántos años tienes y a qué te dedicas?"]

    calls = len(d.model_calls)
    r = await d.say("", photo=True)
    assert len(d.model_calls) == calls, "шаг после фото — без модели"
    assert r["rag_meta"]["fuentes"] == ["venta_tras_foto"]
    m = r["messages"]
    assert m[0] == "¡Gracias por tu foto, Luis! 😊"
    assert m[1].startswith("Te espero el [event_date] a las [event_start] en [event_address]")
    assert m[2] == "Aquí puedes apartar tu lugar: [event_link]"
    assert r["send_event_video"] is True and r["send_event_photo"] is True
    assert r["after_media"] == ["¿Cuántos años tienes y a qué te dedicas?"]
    assert not any("foto tuya" in x for x in all_text(r))


@pytest.mark.asyncio
async def test_a2_prod_order_age_before_photo(monkeypatch):
    """Порядок как у Арины в проде: имя → возраст и профессия → фото. Интерес назван один раз."""
    d = Dialog(monkeypatch)
    await d.say("Hola")
    d.models["Me interesa el evento"] = model(
        ["Es una noche para conocer en persona a mujeres eslavas solteras que buscan una relación seria."],
        paso="¿Cómo te llamas y estás soltero?")
    await d.say("Me interesa el evento")
    d.models["Luis, sí soy soltero"] = model(["¡Gracias, Luis! 😊"], extracted={"name": "Luis", "is_single": True},
                                             paso="¿Cuántos años tienes y a qué te dedicas?")
    await d.say("Luis, sí soy soltero")
    d.models["35, soy abogado"] = model(
        ["¡Gracias, Luis! 😊", "Para apartar tu lugar solo falta una foto tuya, puede ser tipo selfie y reciente.",
         "¿Podrías mandarme una foto tuya? Solo la reviso."],
        extracted={"age": "35", "profession": "abogado"})
    r = await d.say("35, soy abogado")
    assert d.lead["age"] == 35
    assert all_text(r) == ["¡Gracias, Luis! 😊", "¿Podrías mandarme una foto tuya?"]

    calls = len(d.model_calls)
    r = await d.say("", photo=True)
    assert len(d.model_calls) == calls and r["rag_meta"]["fuentes"] == ["venta_tras_foto"]
    assert "Aquí puedes apartar tu lugar: [event_link]" in r["messages"]
    assert r["after_media"] == []                      # возраст и профессия уже есть
    assert not any("foto tuya" in x for x in all_text(r))


@pytest.mark.asyncio
async def test_b_buy_without_photo_link_at_once(monkeypatch):
    """(б) «hola» → имя → «¿cómo compro?» без фото → ссылка сразу, без вопроса про фото."""
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["Me llamo Luis, soy soltero"] = model(["¡Gracias, Luis! 😊"], extracted={"name": "Luis", "is_single": True})
    await d.say("Me llamo Luis, soy soltero")
    calls = len(d.model_calls)
    r = await d.say("¿cómo compro el boleto?")
    assert len(d.model_calls) == calls
    assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE] and r["send_event_video"] is True
    assert not any("foto" in x.lower() and "?" in x for x in all_text(r))
    assert d.lead.get("interest") == "event"            # покупка билета — интерес к ивенту


@pytest.mark.asyncio
async def test_b2_buy_without_name(monkeypatch):
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["¿cómo compro el boleto?"] = model(["Puedes comprar tu boleto aquí: [event_link]"])
    r = await d.say("¿cómo compro el boleto?")
    assert all_text(r) == [rag.BUY_ONLINE_SENTENCE, "¿Cómo te llamas y estás soltero?"]


@pytest.mark.asyncio
async def test_c_service_path(monkeypatch):
    """(в) услуга: «hola» → «quiero conocer mujeres» → имя → возраст → профессия → фото → цена."""
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["quiero conocer mujeres rusas"] = model(
        ["Como matchmaker personal, te presento a mujeres eslavas que buscan una relación seria."],
        paso="¿Cómo te llamas y estás soltero?")
    r = await d.say("quiero conocer mujeres rusas")
    assert d.lead.get("interest") == "agency"
    d.models["Luis, soy soltero"] = model(["¡Gracias, Luis! 😊"], extracted={"name": "Luis", "is_single": True})
    r = await d.say("Luis, soy soltero")
    assert all_text(r)[-1] == "¿Cuántos años tienes y a qué te dedicas?"
    d.models["tengo 41"] = model(["¡Gracias! 😊"], extracted={"age": 41})
    r = await d.say("tengo 41")
    assert all_text(r)[-1] == "¿A qué te dedicas?"
    d.models["soy abogado"] = model(["¡Gracias! 😊"], extracted={"profession": "abogado"})
    r = await d.say("soy abogado")
    assert all_text(r)[-1] == "¿Me mandas una foto tuya?"
    r = await d.say("", photo=True)
    assert r["rag_meta"]["fuentes"] == ["service_pitch"]
    assert any("10,000 USD" in x for x in r["messages"])
    assert not any("[event_link]" in x for x in all_text(r))


@pytest.mark.asyncio
async def test_d_age_profession_answer(monkeypatch):
    """(г) «35, soy abogado» → благодарность и следующий шаг без пояснений."""
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event"})
    d._add("anna", "¿Cuántos años tienes y a qué te dedicas?")
    d.models["35, soy abogado"] = model(
        ["¡Gracias, Luis! 😊", "Para apartar tu lugar solo falta una foto tuya, puede ser tipo selfie y reciente.",
         "¿Podrías mandarme una foto tuya? Solo la reviso."],
        extracted={"age": "35", "profession": "abogado"})
    r = await d.say("35, soy abogado")
    assert r["action"] == "respond"
    assert all_text(r) == ["¡Gracias, Luis! 😊", "¿Podrías mandarme una foto tuya?"]
    assert d.lead["age"] == 35 and d.lead["profession"] == "abogado"


@pytest.mark.asyncio
async def test_e_interest_in_history_only_old_lead(monkeypatch):
    """Лид как у Арины сейчас: интерес «Me interesa el evento» в переписке, в профиле пусто,
    имя, «soltero», возраст и профессия есть → фото одобрено → шаг после фото."""
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "age": 35, "profession": "abogado"})
    d._add("lead", "Hola")
    for b in S1:
        d._add("anna", b)
    d._add("lead", "Me interesa el evento")
    d._add("anna", "¿Cómo te llamas y estás soltero?")
    d._add("lead", "Luis, sí soy soltero")
    d._add("anna", "¿Cuántos años tienes y a qué te dedicas?")
    d._add("lead", "35, soy abogado")
    d._add("anna", "¿Podrías mandarme una foto tuya?")
    r = await d.say("", photo=True)
    assert d.model_calls == []
    assert r["rag_meta"]["fuentes"] == ["venta_tras_foto"]
    assert not any("foto tuya" in x for x in all_text(r))
    assert d.lead.get("interest") == "event"


@pytest.mark.asyncio
async def test_f_photo_thanks_from_model_kept(monkeypatch):
    """Модель благодарит за фото («Gracias por tu foto») — благодарность остаётся, фото не
    просим снова (прод 2026-10-22 15:53)."""
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event"})
    d._add("anna", f"Puedes comprar tu boleto aquí: {LINK}")       # ссылка уже была → модель
    d.models[PHOTO_OK] = model(["¡Gracias por tu foto, Luis! 😊"])
    r = await d.say("", photo=True)
    assert r["messages"][0] == "¡Gracias por tu foto, Luis! 😊"
    assert not any("foto tuya" in x for x in all_text(r))


@pytest.mark.asyncio
async def test_live_2026_10_22_no_offer_question_after_data(monkeypatch):
    """Живой прогон 2026-10-22: на «35, soy abogado» после шага с фото модель дописала
    «¿Tienes alguna duda sobre el evento o necesitas ayuda para comprar tu boleto?» —
    вопрос-предложение, о чём лид не спрашивал. Остаётся только благодарность."""
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event",
                                  "photo_received": True})
    d._add("anna", f"Aquí puedes apartar tu lugar: {LINK}")
    d._add("anna", "¿Cuántos años tienes y a qué te dedicas?")
    d.models["35, soy abogado"] = model(
        ["¡Gracias, Luis! 😊 ¿Tienes alguna duda sobre el evento o necesitas ayuda para comprar tu boleto?"],
        extracted={"age": 35, "profession": "abogado"})
    r = await d.say("35, soy abogado")
    assert all_text(r) == ["¡Gracias, Luis! 😊"]


@pytest.mark.parametrize("text", ["¿Tienes alguna duda?", "¿Tienes otra pregunta sobre el evento?",
                                  "¿Necesitas ayuda para comprar tu boleto?",
                                  "¿Te gustaría saber algo más del evento?"])
def test_offer_questions_detected(text):
    assert rag._OFFER_MORE_RE.search(text)


@pytest.mark.parametrize("text", ["¿Cuántos años tienes y a qué te dedicas?", "¿Me mandas una foto tuya?",
                                  "¿Cómo te llamas y estás soltero?"])
def test_slot_questions_not_offers(text):
    assert not rag._OFFER_MORE_RE.search(text)
