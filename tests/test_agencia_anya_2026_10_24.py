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
    """Диалог для живого прогона: услуга → анкета → фото → для чего фото → цена ×2 → как
    работает → реально ли → дорого → не из CDMX → «sí, llámame» → имя и почта."""
    d = Dialog(monkeypatch, settings=EV)
    r = await d.say("Hola, busco conocer una mujer eslava para algo serio")
    assert all_text(r) == [servicio.BIENVENIDA, servicio.ANKETA]
    assert d.lead.get("interest") == "agency"
    r = await d.say("35, abogado, soltero")
    assert all_text(r) == [servicio.FOTOS]
    r = await d.say("¿para qué la foto?")
    assert all_text(r) == [servicio.PARA_QUE_FOTO]
    r = await d.say("", photo=True)
    assert all_text(r) == [servicio.GRACIAS, servicio.INVITACION]
    r = await d.say("¿cuánto cuesta?")
    assert all_text(r) == [servicio.PRECIO]
    r = await d.say("¿cuánto cuesta?")
    assert r["action"] == "silent" and r["silent_kind"] == rag.FUERA_DE_BASE
    r = await d.say("¿cómo funciona?")
    assert all_text(r) == servicio.COMO_FUNCIONA
    r = await d.say("¿es real?")
    assert r["action"] == "silent"
    r = await d.say("es muy caro")
    assert all_text(r) == [servicio.CARO]
    r = await d.say("no vivo en CDMX")
    assert all_text(r) == [servicio.FUERA_SIN_CIUDAD, servicio.FUERA_REGISTRO, servicio.SITE]
    r = await d.say("sí, llámame")
    assert all_text(r) == [servicio.NOMBRE_EMAIL]
    r = await d.say("Luis Pérez García, luis.perez@gmail.com")
    assert r["messages"] == [] and r["llamada_lista"] is True
    assert r["llamada_datos"] == {"nombre": "Luis Pérez García", "email": "luis.perez@gmail.com"}
    assert d.model_calls == [], "весь путь кодом, без модели"
    assert no_sums([m["text"] for m in d.history if m["sender"] == "anna"])


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
    assert all_text(r) == [servicio.CATALOGO]


@pytest.mark.asyncio
async def test_tipo(monkeypatch):
    d = started(Dialog(monkeypatch, lead=AGENCIA))
    r = await d.say("¿tienes judía en tu universo?")
    assert all_text(r) == ["muchas!", "con mucho gusto te podemos presentar con una judía",
                           "tienes tiempo para una llamada y platicamos?"]


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
    r = await d.say(text)
    assert all_text(r) == [first, servicio.FUERA_REGISTRO, servicio.SITE]


@pytest.mark.parametrize("text", ["no puedo los miércoles", "el miércoles se me complica", "ese día no puedo"])
@pytest.mark.asyncio
async def test_miercoles_three_messages(monkeypatch, text):
    d = started(Dialog(monkeypatch, lead={"interest": "event", "name": "Luis", "is_single": True}))
    r = await d.say(text)
    assert all_text(r) == servicio.MIERCOLES
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
    assert servicio.FUERA_REGISTRO not in all_text(r)


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
async def test_model_sum_retry_then_silent(monkeypatch):
    d = started(Dialog(monkeypatch, lead={**AGENCIA, "is_single": True, "age": 35, "profession": "abogado",
                                          "photo_received": True}))
    d._add("anna", servicio.INVITACION)
    d.models["¿y qué tan rápido encuentran a alguien?"] = model(["El plan de 3 matches son 3,000 USD."])
    r = await d.say("¿y qué tan rápido encuentran a alguien?")
    assert r["action"] == "silent" and r["messages"] == []
    assert len(d.model_calls) == 2 and rag.MONEY_REASON in r["silent_reason"]
