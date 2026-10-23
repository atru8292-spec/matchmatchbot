"""Ветка агентства словами Ани (решения Арины 2026-10-24, docs/agency_branch_vs_anya_2026-10-24.md).

Каждый шаг — её текст из переписок: первый ответ и регистрация, анкета без имени, один её
вопрос по недостающему, фото, «¿para qué la foto?», после фото приглашение на звонок без
времени, «¿cómo funciona?», цена (повтор — Ане), «es muy caro», не из CDMX, не может в среду,
согласие на звонок → имя и почта → ручной режим и алерт; контракт/документы/оплата/пакеты и
«¿es real?» — тишина и Аня. Суммы не звучат ни на одном шаге.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

import ai_conversation as rag
import db
import escalation
import knowledge
import main
import servicio
from tests.test_rag_e2e import SETTINGS, Dialog, all_text, model

THEME = "Esta vez es temática de Halloween 🎃"
EV = {**SETTINGS, "event_theme_phrase": THEME}
AGENCIA = {"interest": "agency"}


def started(d, lead_text="Hola"):
    d._add("lead", lead_text)
    d._add("anna", servicio.BIENVENIDA)
    d._add("anna", servicio.ANKETA)
    return d


def no_sums(texts):
    return not any(rag.suma_no_evento(t) for t in texts)


# ───────────────────────── тексты ─────────────────────────

def test_catalog_typo_fixed():
    assert "perfiles que te pueden funcionar" in servicio.CATALOGO
    assert not any("te puede funcionar" in t for t in servicio.TEXTOS)


@pytest.mark.parametrize("text", servicio.TEXTOS)
def test_texts_no_sums_no_invented_bits(text):
    assert no_sums([text])
    assert rag.emoji_count([text]) <= 2
    low = text.lower()
    for bad in ("porfavor", "aque ", "ayudae", "tal ves", "perfil ", "15 mujeres", "180 parejas",
                "paquete", "en un rato te escribo", "llamada corta"):
        assert bad not in low, (bad, text)
    assert not re.search(r"\b(?:lunes|martes|miércoles|jueves|viernes|mañana)\b.*\d", low), text


def test_old_invented_texts_gone():
    for name in ("PRECIO_A", "PRECIO_B", "FUERA", "LLAMADA_OK"):
        assert not hasattr(servicio, name), name
    code = open("ai_conversation.py", encoding="utf-8").read() + open("servicio.py", encoding="utf-8").read()
    for s in ("Entiendo, no te preocupes", "En un rato te escribo", "Te entiendo, es una decisión importante",
              "la inversión depende de lo que buscas"):
        assert s not in code, s


def test_seed_service_records_are_anya_or_off():
    by = {r["id"]: r for r in knowledge.load_seed()}
    assert by["greeting_service"]["contenido_es"] == servicio.BIENVENIDA + "\n\n" + servicio.ANKETA
    assert by["service_pitch"]["contenido_es"] == servicio.GRACIAS + "\n\n" + servicio.INVITACION
    assert by["service_what"]["contenido_es"] == servicio.COMO_FUNCIONA[0]
    assert by["tariff_service"]["contenido_es"] == servicio.PRECIO
    for rid in ("service_videocall", "service_vip", "service_international", "service_women_contact",
                "service_duration", "agency_trust", "obj_service_price", "obj_service_think", "obj_videocall"):
        assert by[rid]["activo"] is False, rid


# ───────────────────────── сквозной сценарий ─────────────────────────

@pytest.mark.asyncio
async def test_full_agency_dialog(monkeypatch):
    """Сухой прогон (2026-10-24, третья итерация): услуга → анкета → фото → для чего фото → цена →
    для чего звонок → как работает → Канкун → «sí, llámame» → имя и почта."""
    d = Dialog(monkeypatch, settings=EV)
    r = await d.say("Hola, busco conocer una mujer eslava para algo serio")
    assert all_text(r) == [servicio.BIENVENIDA, servicio.ANKETA]
    r = await d.say("35, abogado, soltero")
    assert all_text(r) == [servicio.FOTOS]
    r = await d.say("", photo=True)
    assert all_text(r) == [servicio.GRACIAS, servicio.INVITACION]
    r = await d.say("¿para qué la foto?")
    assert all_text(r) == [servicio.PARA_QUE_FOTO]
    r = await d.say("¿cuánto cuesta?")
    assert all_text(r) == [servicio.PRECIO], "приглашение было 2 ответа назад — без нового"
    r = await d.say("¿para qué la llamada?")
    assert all_text(r) == servicio.PARA_QUE_LLAMADA + [servicio.INVITACION_CORTA]
    r = await d.say("¿cómo funciona?")
    assert all_text(r) == servicio.COMO_FUNCIONA, "приглашение было в прошлом ответе"
    r = await d.say("no vivo en CDMX, estoy en Cancún")
    assert all_text(r) == ["en Cancún te podemos ayudar con matchmaking personalizado"]
    r = await d.say("sí, llámame")
    assert all_text(r) == [servicio.NOMBRE_EMAIL]
    r = await d.say("Luis Pérez García, luis.perez@gmail.com")
    assert r["messages"] == [servicio.GRACIAS] and r["llamada_lista"] is True
    assert r["llamada_datos"] == {"nombre": "Luis Pérez García", "email": "luis.perez@gmail.com"}
    assert d.model_calls == []
    sent = [m["text"] for m in d.history if m["sender"] == "anna"]
    assert no_sums(sent) and not any("registro" in t.lower() for t in sent)


@pytest.mark.asyncio
async def test_name_without_email_asks_email_then_alert(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    d._add("anna", servicio.INVITACION)
    r = await d.say("sí")
    assert all_text(r) == [servicio.NOMBRE_EMAIL]
    r = await d.say("Luis Pérez")
    assert all_text(r) == [servicio.CORREO]
    r = await d.say("luis@gmail.com")
    assert r["llamada_lista"] is True
    assert r["llamada_datos"] == {"nombre": "Luis Pérez", "email": "luis@gmail.com"}


@pytest.mark.asyncio
async def test_main_name_email_manual_and_alert_nothing_to_lead(monkeypatch):
    send = AsyncMock(return_value=1); monkeypatch.setattr(main.sender, "send", send)
    upd = AsyncMock(); monkeypatch.setattr(db, "update_lead_fields", upd)
    monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
    alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_call_ready", alert)
    result = rag._result([], llamada_lista=True, llamada_datos={"nombre": "Luis Pérez", "email": "l@x.com"},
                         extracted={"email": "l@x.com"}, funnel_stage="pitched", rag_meta={})
    await main._apply_ai_result("wa_5215512345678", {"phone": "wa_5215512345678"}, "Luis Pérez, l@x.com", result)
    upd.assert_any_await("wa_5215512345678", mode="manual")
    alert.assert_awaited_once()
    assert alert.call_args.kwargs == {"nombre": "Luis Pérez", "email": "l@x.com"}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_ready_alert_text(monkeypatch):
    monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "111")
    monkeypatch.setattr(escalation.settings, "tg_manager_chat_id", "222")
    send = AsyncMock(); monkeypatch.setattr(escalation, "_send_telegram", send)
    await escalation.notify_call_ready({"phone": "wa_5215512345678"}, "x", nombre="Luis Pérez", email="l@x.com")
    assert {c.args[1] for c in send.await_args_list} == {"111", "222"}
    text = send.await_args_list[0].args[2]
    assert "📞 Лид готов к созвону" in text and "Luis Pérez" in text and "l@x.com" in text
    assert "+5215512345678" in text


# ───────────────────────── анкета ─────────────────────────

@pytest.mark.parametrize("answer,expected", [
    ("soltero, 35", [servicio.P_PROFESION]),
    ("soy abogado, soltero", [servicio.P_EDAD]),
    ("soltero", [servicio.P_EDAD_PROFESION]),
    ("35, abogado", [servicio.P_SOLTERO]),
])
@pytest.mark.asyncio
async def test_missing_one_anya_question(monkeypatch, answer, expected):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say(answer)
    assert all_text(r) == expected


@pytest.mark.asyncio
async def test_missing_asked_once_then_photo(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    await d.say("soltero, 35")
    r = await d.say("ok")
    # «y a qué te dedicas?» уже спросили дважды (анкета + вопрос) — больше не спрашиваем
    assert all_text(r) == [servicio.FOTOS]


@pytest.mark.asyncio
async def test_no_name_question_in_service(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("35, abogado, soltero")
    assert not any("llamas" in t.lower() or "nombre" in t.lower() for t in all_text(r))


@pytest.mark.asyncio
async def test_how_many_photos(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    d._add("anna", servicio.FOTOS)
    r = await d.say("¿cuántas fotos?")
    assert all_text(r) == [servicio.CUANTAS_FOTOS]


# ───────────────────────── только Аня / тишина ─────────────────────────

@pytest.mark.parametrize("text", ["¿cómo es el contrato?", "te mando mi INE", "¿qué documento necesitas?",
                                  "¿cómo te pago?", "¿qué paquetes tienen?", "¿aceptan tarjeta?",
                                  "¿cuánto es el anticipo?", "¿me das factura?"])
@pytest.mark.asyncio
async def test_contract_docs_payment_silent(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead={**AGENCIA, "is_single": True, "age": 35, "profession": "abogado",
                                          "photo_received": True}))
    d._add("anna", servicio.INVITACION)
    r = await d.say(text)
    assert r["action"] == "silent" and r["messages"] == [] and d.model_calls == []


@pytest.mark.parametrize("text", ["¿es real?", "¿no es una estafa?", "¿esto es fraude?", "¿qué garantía tengo?"])
@pytest.mark.asyncio
async def test_es_real_silent(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say(text)
    assert r["action"] == "silent" and d.model_calls == []


@pytest.mark.asyncio
async def test_price_plus_discount_goes_silent(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("¿cuánto cuesta el servicio? ¿hay descuento?")
    assert r["action"] == "silent"


@pytest.mark.asyncio
async def test_caro_twice_silent(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("está muy caro")
    assert all_text(r) == [servicio.CARO]
    r = await d.say("sigue siendo caro")
    assert r["action"] == "silent"


# ───────────────────────── каталог, тип, уже встречается ─────────────────────────

@pytest.mark.asyncio
async def test_catalog(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("¿puedo ver a las chicas?")
    assert all_text(r) == [servicio.CATALOGO, servicio.CATALOGO_LLAMADA]


@pytest.mark.asyncio
async def test_tipo(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("¿tienes judía en tu universo?")
    assert all_text(r) == ["muchas!", "con mucho gusto te podemos presentar con una judía",
                           servicio.TIPO_LLAMADA]


@pytest.mark.asyncio
async def test_already_dating_suerte_and_manual(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("conocí a alguien y estoy saliendo con ella")
    assert all_text(r) == [servicio.SUERTE] and r.get("pasar_a_manual") is True


# ───────────────────────── не из CDMX / не в среду ─────────────────────────

@pytest.mark.parametrize("text,first", [
    ("vivo en Cancún", "en Cancún te podemos ayudar con matchmaking personalizado"),
    ("soy de Guadalajara", "en Guadalajara te podemos ayudar con matchmaking personalizado"),
    ("no vivo en CDMX", servicio.FUERA_SIN_CIUDAD)])
@pytest.mark.asyncio
async def test_fuera(monkeypatch, text, first):
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    d._add("lead", "ok")
    d._add("anna", "Te espero el 18 de noviembre.")
    r = await d.say(text)
    assert all_text(r) == [first, servicio.INVITACION]


@pytest.mark.parametrize("text", ["no puedo los miércoles", "el miércoles se me complica",
                                  "en miércoles es muy difícil para mí"])
@pytest.mark.asyncio
async def test_miercoles_three_messages(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    d._add("lead", "ok")
    d._add("anna", "Te espero el 18 de noviembre.")
    r = await d.say(text)
    assert all_text(r) == servicio.MIERCOLES + [servicio.MIERCOLES_LLAMADA]
    r = await d.say("sí")
    assert all_text(r) == [servicio.NOMBRE_EMAIL]


@pytest.mark.parametrize("text", ["vivo en la CDMX", "vivo en Polanco", "el miércoles sí puedo",
                                  "vivo en la Narvarte", "vivo en Santa Fe", "vivo en el D.F."])
def test_fuera_not_triggered(text):
    assert servicio.fuera_o_miercoles(text) is None


@pytest.mark.asyncio
async def test_fuera_with_age_goes_to_filters(monkeypatch):
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    d.models["tengo 80 años, vivo en Monterrey"] = model(["Gracias."], extracted={"age": 80})
    r = await d.say("tengo 80 años, vivo en Monterrey")
    assert not any("te podemos ayudar con matchmaking" in t for t in all_text(r))


# ───────────────────────── согласие на звонок ─────────────────────────

@pytest.mark.parametrize("text,ok", [
    ("sí", True), ("Sí, llámame", True), ("va", True), ("claro, ¿a qué hora?", True),
    ("no gracias", False), ("prefiero por aquí", False), ("¿cuánto cuesta?", False),
    ("Sí, soy Juan, 45 años, médico", False), ("Claro, soy soltero", False), ("sí pero cuánto cuesta", False)])
def test_acepta_llamada(text, ok):
    assert servicio.acepta_llamada(text) is ok


def test_yes_without_offer_needs_explicit_call():
    assert servicio.acepta_llamada("sí", ofrecida=False) is False
    assert servicio.acepta_llamada("sí, llámame", ofrecida=False) is True


@pytest.mark.asyncio
async def test_yes_to_anketa_is_not_call(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("sí")
    assert servicio.NOMBRE_EMAIL not in all_text(r)


@pytest.mark.parametrize("text,nombre,email", [
    ("Luis Pérez García, luis@gmail.com", "Luis Pérez García", "luis@gmail.com"),
    ("mi nombre es Luis Pérez y mi correo luis@x.mx", "Luis Pérez", "luis@x.mx"),
    ("Luis Pérez", "Luis Pérez", None)])
def test_nombre_y_email(text, nombre, email):
    assert servicio.nombre_y_email(text) == (nombre, email)


# ───────────────────────── догон (её цепочка) ─────────────────────────

@pytest.mark.asyncio
async def test_dogon_reply_no_gives_profiles_and_call(monkeypatch):
    d = started(Dialog(monkeypatch, lead={**AGENCIA, "is_single": True, "age": 35, "profession": "abogado"}))
    for b in servicio.DOGON:
        d._add("anna", b)
    r = await d.say("No, nada")
    assert all_text(r) == servicio.DOGON_NO
    r = await d.say("sí")
    assert all_text(r) == [servicio.NOMBRE_EMAIL]


@pytest.mark.asyncio
async def test_dogon_reply_yes_suerte(monkeypatch):
    d = started(Dialog(monkeypatch, lead={**AGENCIA, "is_single": True}))
    for b in servicio.DOGON:
        d._add("anna", b)
    r = await d.say("sí, estoy saliendo con alguien")
    assert all_text(r) == [servicio.SUERTE] and r.get("pasar_a_manual")


# ───────────────────────── сумма не звучит ─────────────────────────

@pytest.mark.asyncio
async def test_unknown_question_silent_no_model(monkeypatch):
    d = started(Dialog(monkeypatch, lead={**AGENCIA, "is_single": True, "age": 35, "profession": "abogado",
                                          "photo_received": True}))
    d._add("anna", servicio.INVITACION)
    # вопроса нет в её переписке — модель в ветке агентства не зовём: тишина и Аня
    r = await d.say("¿y qué tan rápido encuentran a alguien?")
    assert r["action"] == "silent" and r["messages"] == [] and d.model_calls == []


@pytest.mark.asyncio
async def test_anketa_answer_with_city_continues_anketa(monkeypatch):
    """«soy de Monterrey, abogado, soltero» на её анкету — анкета, а не ответ «не из CDMX»."""
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("soy de Monterrey, abogado, soltero")
    assert not any("te podemos ayudar con matchmaking" in t for t in all_text(r))


@pytest.mark.asyncio
async def test_photo_request_not_repeated(monkeypatch):
    """Её просьба о фото без «?» — считается вопросом; на «ok» второй раз не уходит (ревью)."""
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("35, abogado, soltero")
    assert all_text(r) == [servicio.FOTOS]
    r = await d.say("ok")
    assert servicio.FOTOS not in all_text(r)


@pytest.mark.parametrize("text", ["¿para qué quieres mi correo?", "no, mejor después", "mejor llámame tú"])
@pytest.mark.asyncio
async def test_name_email_step_not_data_goes_to_anya(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    d._add("anna", servicio.NOMBRE_EMAIL)
    r = await d.say(text)
    assert r["action"] == "silent" and not r.get("llamada_lista")


@pytest.mark.asyncio
async def test_caro_before_photo_does_not_block_invitation(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    await d.say("está muy caro")           # её фраза с приглашением
    await d.say("35, abogado, soltero")
    r = await d.say("", photo=True)
    # «es muy caro» не блокирует шаг после фото; приглашение было 2 ответа назад — только спасибо
    assert all_text(r) == [servicio.GRACIAS]
    r = await d.say("ok")
    assert servicio.GRACIAS not in all_text(r), "«Muchas gracias!» после фото — один раз"


@pytest.mark.asyncio
async def test_event_lead_after_fuera_keeps_event_route(monkeypatch):
    """После «no vivo en CDMX» интерес не меняется: вопрос про оплату билета — путь ивента."""
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    d._add("lead", "ok")
    d._add("anna", "Te espero el 18 de noviembre.")
    await d.say("no vivo en CDMX")
    assert d.lead["interest"] == "event"


@pytest.mark.asyncio
async def test_repeat_para_que_foto_goes_to_anya(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    await d.say("¿para qué la foto?")
    r = await d.say("¿para qué la foto?")
    assert r["action"] == "silent"


# ───────────────────────── третья итерация (2026-10-24) ─────────────────────────

def test_no_registration_ask_anywhere():
    for t in servicio.TEXTOS:
        assert "registro" not in t.lower(), t
    by = {r["id"]: r for r in knowledge.load_seed()}
    for r in by.values():
        if r.get("activo", True):
            assert "registro aqui" not in (r.get("contenido_es") or "").lower(), r["id"]
    prompt = open("anna_prompt_conversation.md", encoding="utf-8").read().lower()
    assert "registro aqui" not in prompt and "ayudame con tu registro" not in prompt


@pytest.mark.asyncio
async def test_invitation_counter_once_per_three_answers(monkeypatch):
    """Приглашение — не чаще раза за 3 ответа бота: после него два ответа без приглашения."""
    d = started(Dialog(monkeypatch, lead={**AGENCIA, "is_single": True, "age": 35, "profession": "abogado"}))
    r = await d.say("¿cuánto cuesta?")
    assert all_text(r)[-1] == servicio.INVITACION
    r = await d.say("¿para qué la foto?")
    assert not any(servicio.es_invitacion(t) for t in all_text(r))
    r = await d.say("¿puedo ver a las chicas?")
    assert all_text(r) == [servicio.CATALOGO], "приглашение было 2 ответа назад"
    r = await d.say("¿tienes rubia?")
    assert all_text(r)[-1] == servicio.TIPO_LLAMADA, "прошло 2 ответа без приглашения — можно"


@pytest.mark.asyncio
async def test_no_invitation_after_refusal(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    d._add("anna", servicio.INVITACION)
    d._add("lead", "no gracias, prefiero por aquí")
    d._add("anna", servicio.PARA_QUE_FOTO)
    d._add("lead", "ok")
    d._add("anna", servicio.CUANTAS_FOTOS)
    r = await d.say("¿cuánto cuesta?")
    assert all_text(r) == [servicio.PRECIO]


@pytest.mark.parametrize("text", ["¿para qué la llamada?", "¿y para qué es la videollamada?",
                                  "¿de qué se trata la llamada?"])
@pytest.mark.asyncio
async def test_para_que_la_llamada(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say(text)
    assert all_text(r)[:1] == servicio.PARA_QUE_LLAMADA and r["action"] == "respond"
    assert servicio.PARA_QUE_LLAMADA == ["En la llamada vemos qué buscas, cómo sería el proceso para ti "
                                         "y qué incluye, y te resuelvo todas tus dudas."]


@pytest.mark.asyncio
async def test_de_que_trata_after_invitation(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    d._add("lead", "ok")
    d._add("anna", servicio.INVITACION)
    r = await d.say("¿de qué trata?")
    assert all_text(r) == servicio.PARA_QUE_LLAMADA      # приглашение было только что — без нового


@pytest.mark.asyncio
async def test_after_email_one_thanks_then_manual(monkeypatch):
    send = AsyncMock(return_value=1); monkeypatch.setattr(main.sender, "send", send)
    upd = AsyncMock(); monkeypatch.setattr(db, "update_lead_fields", upd)
    monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
    alert = AsyncMock(); monkeypatch.setattr(escalation, "notify_call_ready", alert)
    result = rag._result([servicio.GRACIAS], llamada_lista=True,
                         llamada_datos={"nombre": "Luis", "email": "l@x.com"}, rag_meta={})
    await main._apply_ai_result("wa_5215512345678", {"phone": "wa_5215512345678"}, "Luis, l@x.com", result)
    send.assert_awaited_once_with("wa_5215512345678", [servicio.GRACIAS])
    upd.assert_any_await("wa_5215512345678", mode="manual")
    alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_ready_alert_urgent(monkeypatch):
    monkeypatch.setattr(escalation.settings, "tg_alerts_chat_id", "111")
    send = AsyncMock(); monkeypatch.setattr(escalation, "_send_telegram", send)
    await escalation.notify_call_ready({"phone": "wa_5215512345678"}, "x", nombre="Luis", email="l@x.com")
    assert "Ответь лиду, он ждёт времени звонка" in send.await_args_list[0].args[2]
