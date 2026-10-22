"""Режим rag перед живыми лидами (задачи Арины 2026-10-23). Сквозные: настоящая память лида
(memory.build по переписке в памяти теста), база знаний (seed), все проверки; подменены
модель, проверяющая модель, эмбеддинги и БД (Dialog из test_rag_e2e).

1. Вопрос квалификации — один раз. Без ответа — повтор только после ответа лида на другую
   тему и не чаще раза за три хода; ответ уже есть (в том же сообщении, в другом порядке,
   «ya te dije») — не спрашиваем.
2. Не молчать из-за ошибок: (а) технические — повторы, запасная модель, потом молчание с
   причиной; (б) проверка не прошла — отправляем прошедшие пузыри, одна повторная попытка с
   другим поиском, потом молчание; (в) вне базы — молчание. Алерт говорит, какой случай.
3. «está muy caro» — утверждённый текст дословно один раз, второй раз — молча Ане.
"""
from __future__ import annotations

import json

import httpx
import pytest

import ai_conversation as rag
import knowledge
from tests.test_rag_e2e import Dialog, all_text, model

OBJ_TEXT = ("Te entiendo 🤍 Mira, no es solo una entrada: es una noche para conocer en persona a 30 "
            "mujeres solteras y platicar con cada una de ellas. Si te da pena acercarte, nosotros "
            "mismos te presentamos. Y en la agencia ya llevamos más de 180 parejas formadas.")
AGE_PROF_Q = "¿Cuántos años tienes y a qué te dedicas?"
NAME_Q = "¿Cómo te llamas y estás soltero?"


def slot_qs(texts, slot):
    return [t for t in texts if rag.memory.pregunta_slot(slot, t)]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def nop(*a, **k):
        return None
    monkeypatch.setattr(rag, "_sleep", nop, raising=False)


async def _to_name_question(d):
    await d.say("hola")
    d.models["Me interesa el evento"] = model(
        ["Es una noche para conocer en persona a mujeres eslavas solteras que buscan una relación seria."],
        paso=NAME_Q)
    r = await d.say("Me interesa el evento")
    assert all_text(r)[-1] == NAME_Q


# ───────────────────────── 1. вопрос один раз ─────────────────────────

@pytest.mark.asyncio
async def test_1a_photo_after_age_question_no_repeat(monkeypatch):
    """Прод: бот спросил возраст и профессию, лид прислал фото — бот снова спросил их."""
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["Me llamo Luis, soy soltero"] = model(["¡Gracias, Luis! 😊"],
                                                   extracted={"name": "Luis", "is_single": True})
    r = await d.say("Me llamo Luis, soy soltero")
    assert all_text(r)[-1] == AGE_PROF_Q
    r = await d.say("", photo=True)
    assert r["rag_meta"]["fuentes"] == ["venta_tras_foto"]
    assert not slot_qs(all_text(r), "age") and not slot_qs(all_text(r), "profession")


@pytest.mark.asyncio
async def test_1b_photo_before_name(monkeypatch):
    """Фото до имени: благодарим, имя не переспрашиваем сразу (спросили ходом раньше);
    через три хода после ответа лида на другую тему — можно один раз."""
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["[фото одобрено]"] = model(["¡Gracias por tu foto! 😊"], paso=NAME_Q)
    r = await d.say("", photo=True)
    assert r["action"] == "respond" and not slot_qs(all_text(r), "name")
    d.models["¿dónde es?"] = model(["Es en [event_address]."], paso=NAME_Q)
    r = await d.say("¿dónde es?")
    assert not slot_qs(all_text(r), "name")
    d.models["¿y qué incluye?"] = model(["Incluye bebida de bienvenida y botanas."], paso=NAME_Q)
    r = await d.say("¿y qué incluye?")
    assert slot_qs(all_text(r), "name"), "через три хода после ответа на другую тему — можно"


@pytest.mark.asyncio
async def test_1c_name_age_single_in_one_message(monkeypatch):
    """Имя, возраст и «холост» одним сообщением — не спрашиваем ни имя, ни возраст."""
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["Soy Luis, tengo 35 años y soy soltero"] = model(
        ["¡Gracias, Luis! 😊"], extracted={"name": "Luis", "is_single": True, "age": 35}, paso=AGE_PROF_Q)
    r = await d.say("Soy Luis, tengo 35 años y soy soltero")
    t = all_text(r)
    assert not slot_qs(t, "age") and not slot_qs(t, "name")
    assert t[-1] == "¿A qué te dedicas?"


@pytest.mark.asyncio
async def test_1d_age_in_text_model_forgot_to_extract(monkeypatch):
    """Возраст назван («35 años»), модель его не извлекла — возраст всё равно не спрашиваем."""
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["Luis, 35 años, soltero"] = model(
        ["¡Gracias, Luis! 😊"], extracted={"name": "Luis", "is_single": True}, paso=AGE_PROF_Q)
    r = await d.say("Luis, 35 años, soltero")
    assert not slot_qs(all_text(r), "age")


@pytest.mark.asyncio
async def test_1e_ya_te_dije(monkeypatch):
    """Лид ответил на возраст и профессию, модель не извлекла; бот не переспрашивает, а на
    «ya te dije» — тем более."""
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event"})
    d._add("anna", AGE_PROF_Q)
    d.models["35, abogado"] = model(["¡Gracias, Luis! 😊"], paso=AGE_PROF_Q)
    r = await d.say("35, abogado")
    assert not slot_qs(all_text(r), "age") and not slot_qs(all_text(r), "profession")
    d.models["ya te dije"] = model(["Tienes razón, perdón 🤍"], paso=AGE_PROF_Q)
    r = await d.say("ya te dije")
    assert not slot_qs(all_text(r), "age") and not slot_qs(all_text(r), "profession")


@pytest.mark.asyncio
async def test_1f_interrupt_with_question(monkeypatch):
    """Перебил вопросом посреди квалификации: два хода имя не спрашиваем, на третий — можно."""
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿cuánto cuesta?"] = model(["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."],
                                        paso=NAME_Q)
    r = await d.say("¿cuánto cuesta?")
    assert not slot_qs(all_text(r), "name")
    d.models["ok"] = model(["Perfecto 🤍"], paso=NAME_Q)
    r = await d.say("ok")
    assert not slot_qs(all_text(r), "name")


# ───────────────────────── 2. ошибки ─────────────────────────

def _fail_main(d, monkeypatch, errors, model_name=rag.MODEL):
    """Первые len(errors) вызовов model_name падают с этими ошибками."""
    real = rag._chat
    left = list(errors)
    calls = []

    async def chat(model_name_, system, user, cost, **kw):
        calls.append(model_name_)
        if model_name_ == model_name and left:
            raise left.pop(0)
        return await real(model_name_, system, user, cost, **kw)
    monkeypatch.setattr(rag, "_chat", chat)
    return calls


def _http(status):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return httpx.HTTPStatusError(f"{status}", request=req, response=httpx.Response(status, request=req))


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [
    [httpx.ReadTimeout("timeout")],
    [_http(500), _http(500)],
    [json.JSONDecodeError("bad", "{", 0)],
    [_http(429)],
])
async def test_2a_tech_retry_then_answer(monkeypatch, errors):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿dónde es?"] = model(["Es en [event_address]."])
    _fail_main(d, monkeypatch, errors)
    r = await d.say("¿dónde es?")
    assert r["action"] == "respond" and r["messages"][0] == "Es en [event_address]."


@pytest.mark.asyncio
async def test_2a_fallback_model(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿dónde es?"] = model(["Es en [event_address]."])
    calls = _fail_main(d, monkeypatch, [_http(500)] * 3)
    r = await d.say("¿dónde es?")
    assert r["action"] == "respond"
    assert getattr(rag, "FALLBACK_MODEL", "?") in calls


@pytest.mark.asyncio
async def test_2a_all_failed_silent_with_reason(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿dónde es?"] = model(["Es en [event_address]."])
    _fail_main(d, monkeypatch, [httpx.ReadTimeout("t")] * 3, rag.MODEL)
    _fail_main(d, monkeypatch, [httpx.ReadTimeout("t")] * 3, getattr(rag, "FALLBACK_MODEL", "x"))
    r = await d.say("¿dónde es?")
    assert r["action"] == "silent" and r["messages"] == []
    alert = rag.silent_alert(r["silent_reason"])
    assert alert.startswith("Техническая ошибка") and "таймаут" in alert


@pytest.mark.asyncio
async def test_2a_check_model_fails_then_recovers(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿dónde es?"] = model(["Es en [event_address]."])
    _fail_main(d, monkeypatch, [_http(503), httpx.ReadTimeout("t")], rag.CHECK_MODEL)
    r = await d.say("¿dónde es?")
    assert r["action"] == "respond"


@pytest.mark.asyncio
async def test_2a_check_model_down_silent_tech(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿dónde es?"] = model(["Es en [event_address]."])
    _fail_main(d, monkeypatch, [_http(500)] * 3, rag.CHECK_MODEL)
    _fail_main(d, monkeypatch, [_http(500)] * 3, getattr(rag, "CHECK_FALLBACK_MODEL", "x"))
    r = await d.say("¿dónde es?")
    assert r["action"] == "silent"
    alert = rag.silent_alert(r["silent_reason"])
    assert alert.startswith("Техническая ошибка") and "провер" in alert


def _check_says(monkeypatch, bad: list[str]):
    """Проверяющая модель отмечает эти фразы как утверждения без источника."""
    real = rag._chat

    async def chat(model_name, system, user, cost, **kw):
        if model_name in (rag.CHECK_MODEL, getattr(rag, "CHECK_FALLBACK_MODEL", None)):
            resp = user.get("RESPUESTA") if isinstance(user, dict) else []
            return {"afirmaciones": [{"texto": s, "respaldo": None}
                                     for s in bad if any(s in b for b in resp)]}
        return await real(model_name, system, user, cost, **kw)
    monkeypatch.setattr(rag, "_chat", chat)


@pytest.mark.asyncio
async def test_2b_send_verified_bubbles_only(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿dónde es?"] = model(["Es en [event_address].",
                                    "El lugar es muy seguro y tiene seguridad privada toda la noche."])
    _check_says(monkeypatch, ["El lugar es muy seguro y tiene seguridad privada toda la noche."])
    r = await d.say("¿dónde es?")
    assert r["action"] == "respond"
    assert r["messages"] == ["Es en [event_address]."]


@pytest.mark.asyncio
async def test_2b_nothing_verified_retry_then_silent(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    bad = "Van muchas mujeres de Rusia que ya encontraron pareja con nosotros gracias a la noche."
    d.models["¿quiénes van?"] = model([bad])
    _check_says(monkeypatch, [bad])
    r = await d.say("¿quiénes van?")
    assert d.model_calls.count("¿quiénes van?") == 2, "одна повторная попытка"
    assert r["action"] == "silent"
    assert rag.silent_alert(r["silent_reason"]).startswith("Проверка не пройдена")


@pytest.mark.asyncio
async def test_2c_out_of_base_silent(monkeypatch):
    d = Dialog(monkeypatch)
    await _to_name_question(d)
    d.models["¿hay estacionamiento?"] = model([], falta_dato=True)
    r = await d.say("¿hay estacionamiento?")
    assert r["action"] == "silent" and r["messages"] == []
    assert rag.silent_alert(r["silent_reason"]).startswith("Вопрос вне базы")


def test_2_reasons_counted_in_log(caplog):
    caplog.set_level("INFO")
    rag._silent("нет данных в базе знаний", kind="fuera_de_base")
    assert any("rag silencio" in r.message and "fuera_de_base" in r.message for r in caplog.records)


# ───────────────────────── 3. «está muy caro» ─────────────────────────

@pytest.mark.asyncio
async def test_3_price_objection_once_then_anya(monkeypatch):
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event",
                                  "age": 35, "profession": "abogado"})
    d._add("anna", "El precio de preventa es de 6000 MXN.")
    calls = len(d.model_calls)
    r = await d.say("está muy caro")
    assert len(d.model_calls) == calls, "дословно, без модели"
    assert r["messages"] == [OBJ_TEXT]
    r = await d.say("no sé, sigue siendo caro")
    assert len(d.model_calls) == calls
    assert r["action"] == "silent" and r["messages"] == []
    assert "цен" in rag.silent_alert(r["silent_reason"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["es caro", "uf, se me hace caro", "no me alcanza", "está muy costoso"])
async def test_3_objection_forms(monkeypatch, text):
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event"})
    d._add("anna", "El precio de preventa es de 6000 MXN.")
    r = await d.say(text)
    assert r["messages"] == [OBJ_TEXT]


def test_3_facts_in_seed():
    by_id = {r["id"]: r for r in knowledge.load_seed()}
    obj = by_id["obj_event_price"]
    assert obj["contenido_es"] == OBJ_TEXT and obj["literal"] and obj["confirmado"]
    facts = " ".join(r.get("contenido_es") or "" for r in by_id.values()
                     if r.get("confirmado") and "2026-10-23" in (r.get("fuente") or ""))
    for fact in ("platicar con cada una", "te presentamos", "180 parejas"):
        assert fact in facts, fact


# ───────────────────────── ревью 2026-10-23 ─────────────────────────

def test_review_detectors():
    m = rag.memory
    assert not m.es_objecion_precio("Soy Caro") and not m.es_objecion_precio("Caro")
    assert not m.es_objecion_precio("no es caro, me animo")
    assert m.es_objecion_precio("caro!") and m.es_objecion_precio("uf, qué caro")
    assert not m.responde_slot("name", "Buenas", True)
    assert not m.responde_slot("profession", "me interesa el evento", True)


@pytest.mark.asyncio
async def test_review_only_first_message_after_question_counts(monkeypatch):
    """Спросили профессию, лид ответил вопросом, потом «quiero información» — профессия не известна."""
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "agency", "age": 40})
    d._add("anna", "¿A qué te dedicas?")
    d._add("lead", "¿cuánto cuesta?")
    d._add("anna", "La inversión es desde $10,000 USD.")
    d._add("lead", "quiero información")
    ya = rag.memory.ya_hecho(d.history, d.lead, {}, None, d.t)
    assert "profession" not in ya["respondido"]


def test_review_question_in_two_bubbles_is_one_try():
    hist = [{"sender": "anna", "text": "¿Cómo te llamas?"}, {"sender": "anna", "text": "¿Cómo te llamas y estás soltero?"}]
    ya = rag.memory.ya_hecho(hist, {}, {}, None, None)
    assert ya["preguntas_calificacion"]["name"] == 1


@pytest.mark.asyncio
async def test_review_old_event_objection_does_not_count(monkeypatch):
    from datetime import timedelta
    d = Dialog(monkeypatch, lead={"name": "Luis", "is_single": True, "interest": "event"})
    d.history.append({"id": "old", "sender": "lead", "direction": "inbound", "text": "está muy caro",
                      "created_at": d.t - timedelta(days=60), "meta": {}})
    r = await d.say("está caro")
    assert r["messages"] == [OBJ_TEXT]


def test_gpt5_payload():
    b = rag.chat_payload("gpt-5", "S", "U", 700, 0.3)
    assert "temperature" not in b and "max_tokens" not in b
    assert b["max_completion_tokens"] >= 700 and b["reasoning_effort"] == rag.GPT5_REASONING_EFFORT
    b = rag.chat_payload("gpt-4.1", "S", "U", 700, 0.3)
    assert b["temperature"] == 0.3 and b["max_tokens"] == 700 and "reasoning_effort" not in b
