"""Воронка как у Ани (решения владелицы 2026-10-23). Сквозные: настоящая память лида, база
знаний (seed), все проверки; подменены модель, проверяющая модель, эмбеддинги, БД, отправка.

Анкета одним сообщением, разбор ответа кодом, недостающее одним сообщением один раз, отказ и
«casado» — молча Ане, «solo curiosidad» — тепло и один возврат, вопрос посреди анкеты, фото
первым сообщением, «ya te dije», оплата «tarjeta o transferencia», места по настройке, воронка
ивента без звонка, 🎃 вне лимита эмодзи. Ожидание ответа (скользящее окно, потолок, фото дольше),
одна обработка на лида и прерывание устаревшего ответа новым сообщением.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import ai_conversation as rag
import cuestionario as cq
import db
import main
import sender
from debounce import Debouncer
from tests.test_rag_e2e import SETTINGS, Dialog, all_text, model

THEME = "Esta vez es temática de Halloween 🎃"
EV = {**SETTINGS, "event_theme_phrase": THEME}
GRACIAS = ("¡Muchas gracias!", "¡Gracias, {n}!", "¡Qué bien, gracias!")


def thanks_ok(text: str, name: str | None = None) -> bool:
    return any(text.startswith(g.format(n=name or "")) for g in GRACIAS)


async def first_contact(d):
    r = await d.say("Hola, me interesa el evento")
    t = all_text(r)
    assert d.model_calls == [] and t[-1] == cq.COMPOUND_EVENT
    assert "[event_theme_phrase]" in t[1], "тема ивента из настроек (🎃 подставит отправка)"
    return r


# ───────────────────────── анкета одним сообщением ─────────────────────────

@pytest.mark.asyncio
async def test_yes_to_everything_in_one_message(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    r = await d.say("sí, eso busco. Luis, 35, abogado")
    assert d.model_calls == []
    assert d.lead["name"] == "Luis" and d.lead["is_single"] is True
    assert d.lead["age"] == 35 and d.lead["profession"] == "abogado"
    t = all_text(r)
    # анкета закрыта — отдельным сообщением фото (решение 2026-10-23)
    assert len(t) == 1 and t[0].endswith(cq.FOTO_PEDIDO)
    r = await d.say("", photo=True)
    assert thanks_ok(r["messages"][0], "Luis") and rag.CTA_LINK in r["messages"]
    assert r["send_event_video"] and r["after_media"] == []


@pytest.mark.asyncio
async def test_partial_answer_one_message_for_missing(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    r = await d.say("Pedro, 40")
    t = all_text(r)
    assert len(t) == 1 and t[0].count("Me falta saber") == 1
    assert "a qué te dedicas" in t[0] and "soltero" in t[0] and "fotos" not in t[0]
    assert "cómo te llamas" not in t[0] and "cuántos años" not in t[0]
    r = await d.say("ingeniero")
    t = all_text(r)
    assert d.lead["profession"] == "ingeniero"
    # ответил — теперь фото; недостающее («soltero») второй раз не спрашиваем
    assert len(t) == 1 and t[0].endswith(cq.FOTO_PEDIDO) and "falta" not in t[0]
    r = await d.say("", photo=True)
    assert rag.CTA_LINK in r["messages"], "после двух вопросов «холост» не блокирует покупку"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["no busco relación seria", "nada serio, algo casual", "casado, 40"])
async def test_refusal_or_married_silent_to_anya(monkeypatch, text):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    r = await d.say(text)
    assert r["action"] == "silent" and r["messages"] == [] and d.lead["mode"] == "manual"
    assert rag.silent_alert(r["silent_reason"]).startswith("Лид не прошёл первый вопрос: «")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["solo curiosidad", "no sé, tal vez", "no"])
async def test_curiosity_warm_reply_and_one_return(monkeypatch, text):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    r = await d.say(text)
    t = all_text(r)
    assert r["action"] == "respond" and d.model_calls == []
    assert t[0] == rag.DUDA_OPENER and "mujeres eslavas" in t[1]
    assert t[-1].startswith("Si te animas, me falta saber")
    r = await d.say("solo curiosidad")
    assert not any("falta" in x for x in all_text(r)), "мягкий возврат — один раз"


@pytest.mark.asyncio
async def test_question_instead_of_answer(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    d.models["¿cuánto cuesta?"] = model(
        ["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."],
        paso="¿Cómo te llamas y estás soltero?")
    r = await d.say("¿cuánto cuesta?")
    t = all_text(r)
    assert t[0].startswith("El precio de preventa")
    assert sum("Me falta saber" in x for x in t) == 1 and "¿Cómo te llamas y estás soltero?" not in t
    r = await d.say("sí busco, Pedro, 40, ingeniero")
    t = all_text(r)
    assert len(t) == 1 and "falta" not in t[0], "недостающее уже спрашивали"


@pytest.mark.asyncio
async def test_service_first_message_compound(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    r = await d.say("Hola, busco conocer una mujer eslava para algo serio")
    assert all_text(r)[-1] == cq.COMPOUND_SERVICE and d.lead["interest"] == "agency"


# ───────────────────────── фото первым, «ya te dije» ─────────────────────────

@pytest.mark.asyncio
async def test_photo_first_message(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    r = await d.say("", photo=True)
    assert r["messages"][0] == rag.PHOTO_FIRST_THANKS and r["messages"][1] == "¡Hola! 🤍"
    r = await d.say("me interesa el evento")
    t = all_text(r)
    assert t[-1] == cq.pregunta_compuesta("evento", foto_recibida=True)
    assert "fotos" not in t[-1]


@pytest.mark.asyncio
async def test_ya_te_dije_when_nothing_to_ask(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    await d.say("sí, eso busco. Luis, 35, abogado")
    await d.say("", photo=True)
    r = await d.say("ya te dije")
    assert all_text(r) == [cq.YA_TE_DIJE_REPLY]


# ───────────────────────── оплата и места ─────────────────────────

@pytest.mark.asyncio
async def test_payment_card_gives_link(monkeypatch):
    d = Dialog(monkeypatch, {"name": "Luis", "is_single": True, "interest": "event"}, settings=EV)
    r = await d.say("¿aceptan tarjeta?")
    assert all_text(r) == [rag.PAYMENT_QUESTION] and d.model_calls == []
    r = await d.say("con tarjeta")
    assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["transferencia", "¿puedo hacer transferencia?"])
async def test_payment_transfer_silent(monkeypatch, text):
    d = Dialog(monkeypatch, {"name": "Luis", "is_single": True, "interest": "event"}, settings=EV)
    d._add("anna", rag.PAYMENT_QUESTION)
    r = await d.say(text)
    assert r["action"] == "silent"
    assert rag.silent_alert(r["silent_reason"]).endswith("Лид хочет оплатить переводом, нужны реквизиты")


@pytest.mark.asyncio
@pytest.mark.parametrize("seats,silent", [("1", False), (None, True), ("0", True)])
async def test_seats_by_setting(monkeypatch, seats, silent):
    st = dict(EV)
    if seats is not None:
        st["event_seats_available"] = seats
    d = Dialog(monkeypatch, {"name": "Luis", "is_single": True, "interest": "event"}, settings=st)
    r = await d.say("¿quedan lugares?")
    if silent:
        assert r["action"] == "silent" and rag.silent_alert(r["silent_reason"]).endswith("Лид спрашивает про места")
    else:
        assert all_text(r) == [rag.SEATS_YES]


# ───────────────────────── без звонка, 🎃 вне лимита ─────────────────────────

@pytest.mark.asyncio
async def test_event_route_no_videocall(monkeypatch):
    d = Dialog(monkeypatch, {"name": "Luis", "is_single": True, "interest": "event"}, settings=EV)
    d.models["¿qué incluye?"] = model(["Incluye bebida de bienvenida y botanas.",
                                       "Si quieres, agendamos una videollamada y te cuento más."])
    r = await d.say("¿qué incluye?")
    assert not any("llamada" in x for x in all_text(r))


@pytest.mark.asyncio
async def test_theme_emoji_not_counted(monkeypatch):
    d = Dialog(monkeypatch, {"name": "Luis", "is_single": True, "interest": "event"}, settings=EV)
    d.models["¿de qué se trata?"] = model([f"Es una noche para conocer en persona a mujeres eslavas solteras. {THEME}",
                                          "Te espero 🤍", "Será increíble 😊"])
    r = await d.say("¿de qué se trata?")
    assert any("🎃" in x for x in all_text(r))


# ───────────────────────── ожидание, гонка, фото пачкой ─────────────────────────

@pytest.mark.asyncio
async def test_debounce_window_settings(monkeypatch):
    monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={}))
    assert await main._debounce_window("text") == (60.0, 180.0)
    assert await main._debounce_window("photo") == (90.0, 240.0)
    monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={"reply_debounce_seconds": "30"}))
    assert await main._debounce_window("text") == (30.0, 180.0)


@pytest.mark.asyncio
async def test_sliding_window_and_cap():
    """Масштаб 1:1000 (60 с → 0.06 с): каждое новое сообщение продлевает ожидание, но не
    дольше потолка от первого сообщения серии."""
    loop = asyncio.get_running_loop()
    flushes: list[float] = []

    async def on_flush(phone):
        flushes.append(loop.time())
    deb = Debouncer(on_flush, delay=0.06, max_wait=0.18)
    start = loop.time()
    await deb.trigger("wa_1", 0.06, 0.18)
    await asyncio.sleep(0.04)
    await deb.trigger("wa_1", 0.06, 0.18)       # окно продлилось
    await asyncio.sleep(0.04)
    assert flushes == []
    for _ in range(3):                          # пишет без пауз — сработает потолок
        await deb.trigger("wa_1", 0.06, 0.18)
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.1)
    assert len(flushes) == 1 and 0.17 <= flushes[0] - start <= 0.24


@pytest.mark.asyncio
async def test_photo_waits_longer():
    loop = asyncio.get_running_loop()
    flushes: list[float] = []

    async def on_flush(phone):
        flushes.append(loop.time())
    deb = Debouncer(on_flush, delay=0.06, max_wait=0.18)
    start = loop.time()
    await deb.trigger("wa_1", 0.09, 0.24)       # фото: окно 90 → 0.09
    await asyncio.sleep(0.07)
    assert flushes == []
    await asyncio.sleep(0.05)
    assert len(flushes) == 1 and flushes[0] - start >= 0.09


@pytest.mark.asyncio
async def test_race_new_message_stops_stale_reply(monkeypatch):
    """Бот отвечает тремя пузырями; после первого лид пишет ещё — остальные не уходят, затем
    новый проход разбирает новое сообщение (одна обработка на лида одновременно)."""
    sent: list[str] = []
    passes: list[int] = []
    monkeypatch.setattr(sender, "compute_delay", lambda t: 0)
    monkeypatch.setattr(sender, "render_bubbles", AsyncMock(side_effect=lambda m, p, a: list(m)))
    monkeypatch.setattr(db, "save_outbound", AsyncMock())

    async def send_one(chat_id, text):
        sent.append(text)
        if len(sent) == 1:
            await main.debouncer.trigger("wa_1", 0.01, 0.05)     # лид пишет во время ответа
        return True
    monkeypatch.setattr(sender, "send_one", send_one)

    async def impl(phone):
        passes.append(len(sent))
        if len(passes) == 1:
            await sender.send(phone, ["uno", "dos", "tres"])
    monkeypatch.setattr(main, "_process_burst_impl", impl)
    main.debouncer = Debouncer(main._process_burst, delay=0.01, max_wait=0.05)
    try:
        await main.debouncer.trigger("wa_1", 0.01, 0.05)
        await asyncio.sleep(0.1)
    finally:
        main.debouncer = None
    assert sent == ["uno"], "устаревшие пузыри не ушли"
    assert passes == [0, 1], "новое сообщение разобрано вторым проходом"


@pytest.mark.asyncio
async def test_one_processing_per_lead(monkeypatch):
    active, peak = 0, 0

    async def impl(phone):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
    monkeypatch.setattr(main, "_process_burst_impl", impl)
    await asyncio.gather(main._process_burst("wa_1"), main._process_burst("wa_1"))
    assert peak == 1


@pytest.mark.asyncio
async def test_three_photos_one_answer(monkeypatch):
    msgs = [{"id": i, "text": "[photo received]", "direction": "inbound",
             "meta": {"content_type": "photo", "content_uri": f"https://x/{i}.jpg"}} for i in (1, 2, 3)]
    monkeypatch.setattr(db, "get_unprocessed_inbound", AsyncMock(return_value=msgs))
    monkeypatch.setattr(db, "bypass_phone_set", AsyncMock(return_value=frozenset({"wa_1"})))
    monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": "wa_1"}))
    monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "mark_messages_processed", AsyncMock())
    monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={}))
    photos = AsyncMock()
    monkeypatch.setattr(main, "_process_photos", photos)
    await main._process_burst_impl("wa_1")
    photos.assert_awaited_once()
    assert photos.await_args.args[2] == ["https://x/1.jpg", "https://x/2.jpg", "https://x/3.jpg"]


# ───────────────────────── ревью 2026-10-23 ─────────────────────────

@pytest.mark.parametrize("text,want", [
    ("no tengo novia, Luis, 35, abogado", {"is_single": True, "name": "Luis", "age": 35, "profession": "abogado"}),
    ("no estoy casado", {"is_single": True}),
    ("no busco nada casual", {"seria": "si"}),
    ("soltero, sin compromisos", {"is_single": True, "seria": None}),
    ("Sí, quiero", {"profession": None}),
    ("¿es el 25 de octubre?", {"age": None}),
    ("a las 20 hrs", {"age": None}),
    ("Soltero, 45, trabajo en ventas", {"name": None, "profession": "ventas", "age": 45}),
    ("Sí, claro que sí, Luis", {"name": "Luis"}),
    ("soy mexicano", {"name": None}),
    ("soy muy serio", {"name": None}),
])
def test_parser_review_cases(text, want):
    got = cq.analizar(text)
    for k, v in want.items():
        assert got[k] == v, (text, k, got)


def test_yes_only_counts_after_questionnaire():
    assert cq.analizar("Sí, claro", responde_cuestionario=False)["seria"] is None


@pytest.mark.asyncio
async def test_card_without_data_no_link(monkeypatch):
    d = Dialog(monkeypatch, {"interest": "event"}, settings=EV)
    d._add("anna", rag.PAYMENT_QUESTION)
    r = await d.say("con tarjeta")
    assert rag.BUY_LINK not in r["messages"] and r["messages"][0] == rag.BUY_ONLINE_SENTENCE


@pytest.mark.asyncio
async def test_deposit_question_not_transfer(monkeypatch):
    assert rag.pago_paso({"interest": "event"}, {"ultimo_mensaje_anna": "", "settings": EV,
                                                 "ya_hecho": {}}, "¿cuánto es el depósito?") is None


@pytest.mark.asyncio
async def test_no_endless_thanks(monkeypatch):
    d = Dialog(monkeypatch, settings=EV)
    await first_contact(d)
    await d.say("Pedro, 40")                         # недостающее спросили
    r = await d.say("ok")                            # теперь фото — один раз
    assert all_text(r)[-1].endswith(cq.FOTO_PEDIDO)
    d.models["ok"] = model(["Perfecto, aquí estoy si tienes dudas."])
    r = await d.say("ok")                            # ничего нового — модель, не «gracias» по кругу
    assert d.model_calls[-1] == "ok"
    assert not any(cq.MARK in x or "falta" in x or "fotos" in x for x in all_text(r))


@pytest.mark.asyncio
async def test_lock_dict_cleaned(monkeypatch):
    monkeypatch.setattr(main, "_process_burst_impl", AsyncMock())
    await main._process_burst("wa_9")
    assert not any(k[1] == "wa_9" for k in main._burst_locks)
