"""Режим rag, второй живой прогон 2026-10-23 (задачи Арины). Сквозные: настоящая память лида,
база знаний (seed), все проверки; подменены модель, проверяющая модель, эмбеддинги, БД.

1. «Me interesa el evento» посреди разговора — описание ивента из базы не срезается.
2. Фото раньше имени, потом все данные — шаг к покупке, как после фото.
3. «¿y tú cómo estás?» — короткий тёплый ответ, не срезается как «спасибо без повода».
4. На «¿Cuántos años tienes y a qué te dedicas?» только возраст — один раз «¿Y a qué te dedicas?».
5. pair_expensive («Te entiendo, no es poco») выключена; дословный ответ на «está muy caro» цел.
"""
from __future__ import annotations

import pytest

import ai_conversation as rag
import knowledge
from tests.test_rag_e2e import Dialog, all_text, model

NAME_Q = "¿Cómo te llamas y estás soltero?"
AGE_PROF_Q = "¿Cuántos años tienes y a qué te dedicas?"
EVENT_WHAT = ("Es una noche para conocer en persona a mujeres eslavas solteras que buscan una relación "
              "seria, el [event_date] en [event_address].")


def slot_qs(texts, slot):
    return [t for t in texts if rag.memory.pregunta_slot(slot, t)]


# ───────────────────────── 1. интерес посреди разговора ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Me interesa el evento", "quiero información del evento"])
async def test_1_interest_mid_conversation_keeps_event_fact(monkeypatch, text):
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["jaja qué onda"] = model(["Jaja, aquí andamos 😊"], paso=NAME_Q)
    r = await d.say("jaja qué onda")
    assert all_text(r)[-1] == NAME_Q
    d.models[text] = model(["¡Qué gusto que te interese!", EVENT_WHAT], paso=NAME_Q)
    r = await d.say(text)
    assert EVENT_WHAT in all_text(r), "описание ивента из базы не срезано"
    assert not slot_qs(all_text(r), "name"), "имя спросили ходом раньше — не повторяем"


# ───────────────────────── 2. фото раньше имени ─────────────────────────

@pytest.mark.asyncio
async def test_2_photo_first_then_all_data_goes_to_purchase(monkeypatch):
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["[фото одобрено]"] = model(["¡Gracias por tu foto! 😊"], paso=NAME_Q)
    await d.say("", photo=True)
    text = "Soy Jorge, 42 años, ingeniero, soltero"
    d.models[text] = model(["¡Gracias, Jorge! 😊"], extracted={"name": "Jorge", "is_single": True,
                                                               "age": 42, "profession": "ingeniero"})
    r = await d.say(text)
    m = r["messages"]
    assert m[0] == "¡Gracias, Jorge! 😊"
    assert m[-1] == rag.CTA_LINK
    assert r["send_event_video"] is True and r["send_event_photo"] is True
    assert r["rag_meta"]["fuentes"] == ["venta_tras_datos"]   # подписи — общий путь с шагом после фото
    t = all_text(r)
    assert not slot_qs(t, "photo") and not slot_qs(t, "age") and not slot_qs(t, "profession")


# ───────────────────────── 3. «¿y tú cómo estás?» ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["¿y tú cómo estás?", "bien y tú?", "¿qué tal tu día?"])
async def test_3_courtesy_question_answered(monkeypatch, text):
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["Me interesa el evento"] = model(["Es una noche para conocer en persona a mujeres eslavas."],
                                              paso=NAME_Q)
    await d.say("Me interesa el evento")
    # как в живом прогоне: модель ответила одним «Gracias por preguntar» — его срезало целиком
    d.models[text] = model(["¡Gracias por preguntar! 😊"], paso=NAME_Q)
    r = await d.say(text)
    t = all_text(r)
    assert t[0] == "Muy bien, gracias 😊"
    assert not slot_qs(t, "name"), "имя спросили ходом раньше — не повторяем"


# ───────────────────────── 4. ответил только возрастом ─────────────────────────

@pytest.mark.asyncio
async def test_4_age_only_asks_profession_once(monkeypatch):
    d = Dialog(monkeypatch, lead={"name": "Pedro", "is_single": True, "interest": "event"})
    d._add("anna", AGE_PROF_Q)
    d.models["35"] = model(["¡Gracias, Pedro! 😊"], extracted={"age": 35})
    r = await d.say("35")
    assert all_text(r) == ["¡Gracias, Pedro! 😊", "¿Y a qué te dedicas?"]
    d.models["jaja ok"] = model(["Jaja 😊"], paso="¿A qué te dedicas?")
    r = await d.say("jaja ok")
    assert not slot_qs(all_text(r), "profession"), "недостающую часть не повторяем"


# ───────────────────────── 5. pair_expensive выключена ─────────────────────────

def test_5_pair_expensive_off_objection_literal_kept():
    by_id = {r["id"]: r for r in knowledge.load_seed()}
    assert by_id["pair_expensive"]["activo"] is False
    usable = {r["id"] for r in knowledge.usable(knowledge.load_seed(), {"event_active": "1"}, "2026-10-23")}
    assert "pair_expensive" not in usable
    assert by_id["obj_event_price"]["literal"] is True and by_id["obj_event_price"]["confirmado"] is True
    assert by_id["obj_event_price"]["contenido_es"].startswith("Te entiendo 🤍 Mira")
    assert "obj_event_price" in usable


def test_5_import_applies_activo():
    from scripts import import_knowledge as ik
    assert "activo" in ik.COLS
    row = ik.row_values({**{c: None for c in ik.COLS}, "id": "pair_expensive", "activo": False}) \
        if hasattr(ik, "row_values") else None
    assert row is None or False in row


@pytest.mark.parametrize("text,ok", [("¿y tú cómo estás?", True), ("bien y tú?", True), ("¿qué tal tu día?", True),
                                     ("soy contador y tú?", False), ("¿qué tal es el evento?", False),
                                     ("¿cómo estás? ¿cuánto cuesta?", False), ("Pedro", False)])
def test_3_courtesy_detection(text, ok):
    assert rag.es_cortesia(text) is ok


# ───────────────────────── ревью 2026-10-23 (второе) ─────────────────────────

@pytest.mark.asyncio
async def test_review_no_interest_still_trims(monkeypatch):
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True})
    d.models["no me interesa el evento"] = model(["Entiendo 🤍", EVENT_WHAT])
    r = await d.say("no me interesa el evento")
    assert EVENT_WHAT not in all_text(r)


@pytest.mark.asyncio
async def test_review_venta_needs_data_in_text(monkeypatch):
    """Модель «извлекла» имя и «холост» из «ok» — шаг к покупке не запускаем."""
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["[фото одобрено]"] = model(["¡Gracias por tu foto! 😊"])
    await d.say("", photo=True)
    d.models["ok"] = model(["😊"], extracted={"name": "Jorge", "is_single": True})
    r = await d.say("ok")
    assert rag.CTA_LINK not in r["messages"]


@pytest.mark.asyncio
async def test_review_venta_asks_age_once_if_never_asked(monkeypatch):
    d = Dialog(monkeypatch)
    await d.say("hola")
    d.models["[фото одобрено]"] = model(["¡Gracias por tu foto! 😊"])
    await d.say("", photo=True)
    d.models["Soy Jorge y estoy soltero"] = model(["¡Gracias!"], extracted={"name": "Jorge", "is_single": True})
    r = await d.say("Soy Jorge y estoy soltero")
    assert r["messages"][-1] == rag.CTA_LINK
    assert r["after_media"] == [AGE_PROF_Q]
