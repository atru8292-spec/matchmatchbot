"""Режим rag (этап 2): память лида, база знаний, проверки ответа, конвейер.

OpenAI, БД и медиа замоканы: _chat отдаёт заранее заданные JSON, эмбеддинги фиктивные,
knowledge — из knowledge/seed.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import ai
import ai_conversation as rag
import db
import knowledge
import memory

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
EVENT_DESDE = datetime(2026, 10, 20, 17, 45, tzinfo=timezone.utc)
LINK = "https://www.rusaencdmx.com/18-11-26-slavic-latino-night-halloween"
SETTINGS = {"event_active": "1", "event_date": "2026-11-18", "event_start": "8:30 PM",
            "event_address": "Polanco, CDMX", "event_price_nonmember": "6000",
            "event_price_next": "7000", "event_link": LINK}
PHONE = "wa_5215500000001"
SEED = knowledge.load_seed()
BY_ID = {r["id"]: r for r in SEED}


def msg(sender, text, minutes_ago, manual=False, source=None):
    meta = {}
    if manual:
        meta["manual"] = True
    if source:
        meta["source"] = source
    return {"id": f"m{minutes_ago}-{sender}", "sender": sender,
            "direction": "inbound" if sender == "lead" else "outbound", "text": text,
            "created_at": NOW - timedelta(minutes=minutes_ago), "meta": meta}


# ───────────────────────── memory ─────────────────────────

class TestAutor:
    def test_kinds(self):
        assert memory.autor(msg("lead", "hola", 1)) == "lead"
        assert memory.autor(msg("anna", "Hola", 1)) == "anna_bot"
        assert memory.autor(msg("anna", "ya te escribo", 1, manual=True)) == "anya_manual"
        assert memory.autor(msg("anna", "ya te escribo", 1, source="echo")) == "anya_manual"
        assert memory.autor(msg("anna", "[video ивента отправлено 2026-11-18]", 1)) == "sistema"


class TestHistorial:
    def test_window_and_gap_marks(self):
        prior = [msg("lead", f"m{i}", 10000 - i) for i in range(60)]
        prior.append(msg("anna", "respuesta", 3 * 24 * 60))   # пауза > 6 ч назад во времени
        prior.sort(key=lambda m: m["created_at"])
        h = memory.historial(prior)
        assert len([x for x in h if "texto" in x]) == memory.WINDOW
        assert all(set(x) == {"fecha", "autor", "texto"} or set(x) == {"pausa"} for x in h)

    def test_gap_text(self):
        assert memory.gap_text(7 * 3600) == "— 7 horas después —"
        assert memory.gap_text(3 * 86400 + 10) == "— 3 días después —"
        assert memory.gap_text(1 * 86400 + 10) == "— 1 día después —"
        assert memory.gap_text(21 * 86400) == "— 3 semanas después —"

    def test_split_current_burst(self):
        msgs = [msg("anna", "¿Cómo te llamas?", 10), msg("lead", "Carlos", 2),
                msg("lead", "y soy soltero", 1)]
        prior, burst = memory.split_current_burst(msgs, "Carlos\ny soy soltero")
        assert [m["text"] for m in prior] == ["¿Cómo te llamas?"]
        assert len(burst) == 2


class TestRegreso:
    @pytest.mark.parametrize("minutes,expected", [
        (30, None), (5 * 60, None), (7 * 60, "corto"), (2 * 24 * 60, "corto"),
        (3 * 24 * 60 + 1, "largo"), (7 * 24 * 60, "largo")])
    def test_modes(self, minutes, expected):
        assert memory.regreso([msg("anna", "x", minutes)], NOW) == expected

    def test_no_history(self):
        assert memory.regreso([], NOW) is None


class TestYaHecho:
    def test_link_sent_25_messages_ago_counts(self):
        prior = [msg("anna", f"Puedes comprar tu boleto aquí: {LINK}", 300)]
        prior += [msg("lead" if i % 2 else "anna", f"texto {i}", 299 - i) for i in range(25)]
        ya = memory.ya_hecho(prior, {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["enlace_enviado"] is not None
        assert ya["presentacion"] is True

    def test_past_event_marked_pasado(self):
        old = msg("anna", "El evento del 16 de octubre cuesta 5000 MXN", 3 * 24 * 60)
        ya = memory.ya_hecho([old], {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["precio_evento_dado"] is None
        assert ya["pasado"] and "5000" in ya["pasado"][0]["texto"]

    def test_current_price_link_address_halloween(self):
        m = msg("anna", "Ahora cuesta 6000 MXN, es en Polanco, CDMX. Es temática de Halloween.", 60)
        ya = memory.ya_hecho([m], {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["precio_evento_dado"] and ya["direccion_dada"] and ya["halloween_mencionado"]

    def test_slot_questions_counted_whole_history_by_bot_and_anya(self):
        prior = [msg("anna", "¿Cómo te llamas y estás soltero?", 5000),
                 msg("anna", "¿cómo te llamas?", 100, manual=True)]
        ya = memory.ya_hecho(prior, {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["preguntas_calificacion"]["name"] == 2
        assert ya["preguntas_calificacion"]["is_single"] == 1
        assert ya["ana_escribio_a_mano"]

    def test_media_markers_and_objections(self):
        prior = [msg("anna", "[video ивента отправлено 2026-11-18]", 50),
                 msg("lead", "se me hace caro", 40), msg("lead", "lo voy a pensar", 30)]
        ya = memory.ya_hecho(prior, {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["video_enviado"]
        assert [o["tipo"] for o in ya["objeciones"]] == ["precio", "lo_pensara"]


class TestCalificacion:
    def _ya(self, **tries):
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        ya["preguntas_calificacion"].update(tries)
        return ya

    def test_event_needs_name_and_single(self):
        c = memory.calificacion({"interest": "event"}, self._ya(), "¿cuánto cuesta?", None)
        assert c["ruta"] == "evento" and c["siguiente"] == "name" and c["etapa"] == "calificacion"

    def test_event_optional_age_does_not_block_venta(self):
        lead = {"interest": "event", "name": "Carlos", "is_single": True, "photo_received": True}
        c = memory.calificacion(lead, self._ya(), "ok", None)
        assert c["etapa"] == "venta" and c["siguiente"] == "age"

    def test_event_slot_skipped_after_two_tries(self):
        lead = {"interest": "event", "is_single": True, "photo_received": True}
        c = memory.calificacion(lead, self._ya(name=2), "ok", None)
        assert c["siguiente"] == "age" and c["etapa"] == "venta"

    def test_event_needs_photo_for_venta(self):
        """Ивент: имя и «холост ли» есть, фото нет → этап calificacion; порядок: возраст,
        профессия, потом фото (решение владелицы 2026-10-22)."""
        lead = {"interest": "event", "name": "Carlos", "is_single": True}
        c = memory.calificacion(lead, self._ya(), "ok", None)
        assert c["etapa"] == "calificacion" and c["siguiente"] == "age"
        lead.update(age=35, profession="médico")
        assert memory.calificacion(lead, self._ya(), "ok", None)["siguiente"] == "photo"

    def test_event_purchase_skips_age_goes_to_photo(self):
        lead = {"interest": "event", "name": "Carlos", "is_single": True}
        c = memory.calificacion(lead, self._ya(), "ok", None, compra=True)
        assert c["siguiente"] == "photo" and c["etapa"] == "calificacion"

    def test_event_photo_not_skipped_after_two_tries(self):
        """Фото не прислал после 2 просьб: больше не просим, но ссылка закрыта."""
        lead = {"interest": "event", "name": "Carlos", "is_single": True, "age": 35, "profession": "x"}
        c = memory.calificacion(lead, self._ya(photo=2), "ok", None)
        assert c["siguiente"] is None and c["etapa"] == "calificacion"

    def test_event_link_already_sent_is_venta(self):
        """Ссылку уже прислали (бот или Аня вручную) — ворота пройдены, фото не нужно."""
        lead = {"interest": "event", "name": "Carlos", "is_single": True}
        ya = self._ya()
        ya["enlace_enviado"] = "2026-10-20"
        assert memory.calificacion(lead, ya, "ok", None)["etapa"] == "venta"

    def test_service_after_two_tries_goes_to_call(self):
        lead = {"interest": "agency", "name": "C", "is_single": True}
        c = memory.calificacion(lead, self._ya(age=2), "ok", None)
        assert c["pasar_a_llamada"] and c["etapa"] == "venta" and c["siguiente"] is None

    def test_service_requires_photo(self):
        lead = {"interest": "agency", "name": "C", "is_single": True, "age": 35, "profession": "x"}
        assert memory.calificacion(lead, self._ya(), "ok", None)["siguiente"] == "photo"

    def test_no_question_on_objection_or_after_same_question(self):
        c = memory.calificacion({"interest": "event"}, self._ya(), "uf, se me hace caro", None)
        assert c["puede_preguntar"] is False
        c = memory.calificacion({"interest": "event"}, self._ya(), "ok",
                                "¿Cómo te llamas y estás soltero?")
        assert c["preguntado_en_ultimo_mensaje"] and not c["puede_preguntar"]

    def test_bought_ticket_is_client(self):
        lead = {"interest": "event", "funnel_stage": "event_attended"}
        ya = memory.ya_hecho([], lead, SETTINGS, None, NOW)
        assert memory.calificacion(lead, ya, "hola", None)["etapa"] == "cliente"


# ───────────────────────── knowledge ─────────────────────────

class TestKnowledgeUsable:
    def test_unconfirmed_never_served(self):
        ids = {r["id"] for r in knowledge.usable(SEED, SETTINGS, "2026-10-21")}
        assert "event_attendees" not in ids and "service_women_ages" not in ids
        assert "event_price" in ids and "service_women_3000" in ids

    def test_event_records_expire(self):
        ids = {r["id"] for r in knowledge.usable(SEED, SETTINGS, "2026-11-19")}
        assert "event_price" not in ids and "event_halloween" not in ids
        assert "service_what" in ids

    def test_event_inactive_hides_event_records(self):
        ids = {r["id"] for r in knowledge.usable(SEED, {**SETTINGS, "event_active": "0"}, "2026-10-21")}
        assert "event_price" not in ids and "event_how_to_buy" not in ids

    def test_empty_next_price_hides_steps(self):
        ids = {r["id"] for r in knowledge.usable(SEED, {**SETTINGS, "event_price_next": ""}, "2026-10-21")}
        assert "event_price_steps" not in ids and "event_price" in ids

    def test_always_in_context_has_no_malo(self):
        ctx = knowledge.always_in_context(knowledge.usable(SEED, SETTINGS, "2026-10-21"))
        dumped = json.dumps(ctx, ensure_ascii=False)
        assert "Gracias por tu interés, Carlos" not in dumped    # это «плохо», модель не видит
        assert ctx["ejemplos_buenos"] and ctx["estilo"]
        assert "30 mujeres" not in dumped                        # неподтверждённая пара

    async def test_search_ranks_by_cosine(self):
        recs = [BY_ID["event_price"], BY_ID["event_halloween"], BY_ID["style_tone"]]
        vecs = {"event_price": [1.0, 0.0], "event_halloween": [0.0, 1.0]}
        knowledge._seed_embeddings.clear()

        async def embed(texts):
            return [vecs["event_price"] if "cuesta" in t else vecs["event_halloween"] for t in texts]
        out = await knowledge.search([0.8, 0.6], recs, embed, top_k=5)
        assert [r["id"] for r in out] == ["event_price", "event_halloween"]   # style не ищется
        knowledge._seed_embeddings.clear()


# ───────────────────────── проверки ответа ─────────────────────────

def _mem(**over):
    ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
    cal = memory.calificacion({"interest": "event", "name": "Carlos", "is_single": True,
                               "photo_received": True}, ya, "x", None)
    base = {"settings": SETTINGS, "ya_hecho": ya, "calificacion": cal, "regreso": None,
            "lead_texts": ["hola"], "historial": [], "resumen": None}
    base.update(over)
    return base


FICHAS = [BY_ID["event_price"], BY_ID["event_how_to_buy"], BY_ID["event_price_steps"]]


def check(bubbles, user_text="¿cuánto cuesta?", lead=None, fichas=None, **mem_over):
    return rag.check_reply(bubbles, mem=_mem(**mem_over), fichas=fichas or FICHAS,
                           lead=lead or {"name": "Carlos", "is_single": True, "photo_received": True},
                           user_text=user_text)


class TestCheckReply:
    def test_clean_answer_passes(self):
        b, reason, _ = check(["Ahora cuesta [event_price_nonmember] MXN e incluye bebida de bienvenida y botanas."])
        assert reason is None and len(b) == 1

    def test_5500_never(self):
        assert check(["Con un amigo son 5,500 MXN."])[1]
        assert check(["Son 5500 por persona."])[1]

    def test_number_without_source_silent(self):
        _, reason, _ = check(["Cuesta 5000 MXN."])
        assert reason and "числа" in reason

    def test_numbers_from_card_and_lead_ok(self):
        b, reason, _ = check(["Ahora cuesta 6000 MXN; la siguiente etapa es de 7000 MXN."])
        assert reason is None

    def test_summary_is_not_a_source(self):
        """Цифра есть только в сводке памяти — не источник → silent."""
        _, reason, _ = check(["Son 30 mujeres y 30 hombres."], resumen="le dije 30 mujeres")
        assert reason

    def test_foreign_url_silent(self):
        assert check(["Mira aquí: https://otra-pagina.com/x"])[1]

    def test_link_only_when_buying(self):
        b, reason, notes = check(["Ahora cuesta [event_price_nonmember] MXN.",
                                  "Puedes comprar tu boleto aquí: [event_link]"])
        assert reason is None and len(b) == 1 and any("ссылк" in n for n in notes)
        b, _, _ = check(["Puedes comprar tu boleto aquí: [event_link]"],
                        user_text="¿cómo compro el boleto?")
        assert "[event_link]" in b[0]

    def test_link_not_repeated_unless_asked(self):
        ya = memory.ya_hecho([msg("anna", f"aquí: {LINK}", 500)], {}, SETTINGS, None, NOW)
        cal = memory.calificacion({"interest": "event", "name": "C", "is_single": True}, ya, "x", None)
        b, _, _ = check(["Ok.", "Aquí: [event_link]"], user_text="ok quiero ir",
                        ya_hecho=ya, calificacion=cal)
        assert all("[event_link]" not in x for x in b)
        b, _, _ = check(["Aquí: [event_link]"], user_text="pásame el link otra vez",
                        ya_hecho=ya, calificacion=cal)
        assert "[event_link]" in b[0]

    def test_link_blocked_in_calificacion(self):
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        cal = memory.calificacion({"interest": "event"}, ya, "x", None)
        b, _, notes = check(["Aquí: [event_link]", "¿Cómo te llamas y estás soltero?"],
                            user_text="¿cómo compro?", ya_hecho=ya, calificacion=cal)
        assert b == ["¿Cómo te llamas y estás soltero?"]

    def test_emoji_limit_and_one_question(self):
        b, _, _ = check(["Ahora cuesta 6000 MXN 🤍✨ ¿Te sirve?", "¿Cómo te llamas? 😊 ¿Estás soltero? 🎃"])
        text = " ".join(b)
        assert text.count("?") == 1 and "¿Estás soltero?" in text
        assert rag.emoji_count(b) == 2                     # до 2 на ответ
        assert all(rag.emoji_count([x]) <= 1 for x in b)   # не больше 1 в пузыре

    def test_three_bubbles_three_emojis_cut_to_two(self):
        b = rag.keep_emojis(["Uno 🤍", "Dos 😊", "Tres ✨"])
        assert b == ["Uno 🤍", "Dos 😊", "Tres"]

    def test_repeated_reaction_dropped(self):
        b, _, _ = check(["¡Qué bien! Es en Polanco, CDMX."], inicios_recientes=["que bien", None])
        assert b == ["Es en Polanco, CDMX."]
        b, _, _ = check(["¡Qué bien! Es en Polanco, CDMX."], inicios_recientes=["jaja"])
        assert b == ["¡Qué bien! Es en Polanco, CDMX."]

    def test_laugh_allowed(self):
        b, reason, _ = check(["Jaja, ¡ojalá alguien llegue de vampiro! Si no tienes disfraz, te dan una máscara."],
                             user_text="jaja voy a ir de vampiro")
        assert reason is None and b[0].startswith("Jaja")

    def test_caption_within_budget(self):
        assert rag._caption_within_budget("Video 🎬", ["A 🤍", "B 😊"]) == "Video"
        assert rag._caption_within_budget("Video 🎬", ["A 🤍"]) == "Video 🎬"

    def test_banned_opener_stripped(self):
        b, _, _ = check(["¡Gracias por tu interés! Ahora cuesta 6000 MXN."])
        assert b == ["Ahora cuesta 6000 MXN."]
        b, _, _ = check(["¡Perfecto! Ahora cuesta 6000 MXN. Te va a encantar."])
        assert b == ["Ahora cuesta 6000 MXN."]

    def test_name_at_start_stripped(self):
        b, _, _ = check(["Carlos, ahora cuesta 6000 MXN."])
        assert b == ["Ahora cuesta 6000 MXN."]

    def test_no_reintroduction(self):
        ya = memory.ya_hecho([msg("anna", "Hola, soy Anna", 100)], {}, SETTINGS, None, NOW)
        b, _, _ = check(["Hola, soy Anna, fundadora de Match Match Agency. Ahora cuesta 6000 MXN."],
                        ya_hecho=ya)
        assert b == ["Ahora cuesta 6000 MXN."]

    def test_slang_sentence_removed(self):
        b, _, _ = check(["Ahora cuesta 6000 MXN. Checa el link porfa."])
        assert b == ["Ahora cuesta 6000 MXN."]

    def test_service_price_blocked_in_calificacion(self):
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        cal = memory.calificacion({"interest": "agency"}, ya, "x", None)
        b, _, _ = check(["Te presento a 15 mujeres. La inversión es desde $10,000 USD."],
                        user_text="¿cómo funciona el servicio?", ya_hecho=ya, calificacion=cal,
                        fichas=[BY_ID["service_what"], BY_ID["tariff_service"]])
        assert b == ["Te presento a 15 mujeres."]

    def test_service_price_on_direct_question_allowed(self):
        """Вопрос лида не ждёт слотов: на прямой вопрос о цене услуги цену называем."""
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        cal = memory.calificacion({"interest": "agency"}, ya, "x", None)
        b, _, _ = check(["La inversión es desde $10,000 USD."], user_text="¿cuánto cuesta el servicio?",
                        ya_hecho=ya, calificacion=cal, fichas=[BY_ID["tariff_service"]])
        assert b == ["La inversión es desde $10,000 USD."]

    def test_number_not_in_given_fichas_is_silent(self):
        """15 есть только в карточке услуги; без неё в источниках → число без источника."""
        assert check(["Te presento a 15 mujeres."])[1]

    def test_empty_after_checks_silent(self):
        assert check(["¡Perfecto!"])[1]


# ───────────────────────── конвейер ─────────────────────────

class Pipe:
    """Моки БД/OpenAI для generate_reply."""

    def __init__(self, monkeypatch, history=None, lead_extra=None, settings=None):
        self.chat_replies: list[dict] = []
        self.chat_calls: list[tuple] = []
        self.verify = {"sin_fuente": []}
        self.cost = AsyncMock(return_value=0.01)
        knowledge.reset_cache()
        knowledge._seed_embeddings.clear()
        monkeypatch.setattr(knowledge, "_load_from_db", AsyncMock(return_value=None))
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=settings or SETTINGS))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=history or []))
        monkeypatch.setattr(db, "get_lead_notes", AsyncMock(return_value=[{"text": "VIP, trato especial"}]))
        monkeypatch.setattr(memory, "_evento_desde", AsyncMock(return_value=EVENT_DESDE))
        monkeypatch.setattr(db, "search_scenarios_by_vector", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "add_daily_amount", self.cost)
        monkeypatch.setattr(ai, "_maybe_announce_event_video", AsyncMock())
        monkeypatch.setattr(ai, "_maybe_announce_event_photo", AsyncMock())
        monkeypatch.setattr(rag, "load_prompt", lambda: "PROMPT")

        async def embed(texts, cost=None):
            return [[1.0, float(len(t) % 7)] for t in texts]
        monkeypatch.setattr(rag, "_embed_batch", embed)

        async def chat(model, system, user, cost, **kw):
            self.chat_calls.append((model, user))
            cost.add(model, {"prompt_tokens": 1000, "completion_tokens": 100})
            if model == rag.CHECK_MODEL:
                return self.verify
            return self.chat_replies.pop(0)
        monkeypatch.setattr(rag, "_chat", chat)
        self.lead = {"phone": PHONE, "whatsapp_name": "Carlos", **(lead_extra or {})}

    def model_calls(self):
        return [c for c in self.chat_calls if c[0] == rag.MODEL]


def reply(bubbles, **kw):
    return {"bubbles": bubbles, "accion": "respond", "fuentes": kw.pop("fuentes", []),
            "falta_dato": False, "en_tema": True, "extracted": {}, **kw}


class TestPipeline:
    async def test_hola_first_message_is_literal_no_model(self, monkeypatch):
        p = Pipe(monkeypatch)
        r = await rag.generate_reply(p.lead, [], "hola")
        assert r["messages"] == BY_ID["greeting_hola"]["contenido_es"].split("\n\n")
        assert p.model_calls() == []

    async def test_vi_el_evento_literal(self, monkeypatch):
        p = Pipe(monkeypatch)
        r = await rag.generate_reply(p.lead, [], "hola, vi el evento")
        assert r["messages"][-1] == "¿Cómo te llamas y estás soltero?"
        assert r["extracted"] == {"interest": "event"}
        assert p.model_calls() == []

    async def test_first_message_with_question_goes_to_model(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply(["Ahora cuesta [event_price_nonmember] MXN e incluye bebida de bienvenida y botanas."],
                                fuentes=["event_price"])]
        r = await rag.generate_reply(p.lead, [], "hola, ¿cuánto cuesta el evento?")
        assert r["action"] == "respond" and "[event_price_nonmember]" in r["messages"][0]
        assert len(p.model_calls()) == 1

    async def test_context_has_memory_layers_and_no_malo(self, monkeypatch):
        history = [msg("anna", "¿Cómo te llamas y estás soltero?", 60 * 24 * 4),
                   msg("lead", "Carlos, soltero", 60 * 24 * 4 - 1)]
        p = Pipe(monkeypatch, history=history)
        p.chat_replies = [reply(["Es en [event_address]."], fuentes=["event_location"])]
        await rag.generate_reply(p.lead, [], "¿dónde es?")
        ctx = p.model_calls()[0][1]
        assert ctx["regreso"] == "largo"
        assert ctx["memoria"]["notas_internas"]["notas"] == ["VIP, trato especial"]
        assert "no citar" in ctx["memoria"]["notas_internas"]["aviso"]
        assert ctx["historial"][0]["autor"] == "anna_bot" and ctx["historial"][0]["fecha"]
        assert ctx["evento_actual"]["precio"] == "[event_price_nonmember] MXN"
        assert "malo" not in json.dumps(ctx, ensure_ascii=False)

    async def test_falta_dato_retry_then_silent(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply([], falta_dato=True, query_extra="estacionamiento"),
                          reply([], falta_dato=True)]
        r = await rag.generate_reply(p.lead, [], "¿hay estacionamiento?")
        assert r["action"] == "silent" and r["messages"] == []
        assert "нет данных" in r["silent_reason"]
        assert len(p.model_calls()) == 2
        assert p.model_calls()[1][1].get("reintento")

    async def test_retry_finds_data(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply([], falta_dato=True, query_extra="cuándo termina"),
                          reply(["Puedes llegar un poco más tarde si lo necesitas."],
                                fuentes=["event_arrive_late"])]
        r = await rag.generate_reply(p.lead, [], "¿puedo llegar tarde?")
        assert r["action"] == "respond"

    async def test_silent_policy_source(self, monkeypatch):
        p = Pipe(monkeypatch, history=[msg("anna", "Es en Polanco, CDMX.", 5)])
        monkeypatch.setattr(knowledge, "search", AsyncMock(return_value=[BY_ID["policy_exact_address_repeat"]]))
        p.chat_replies = [reply(["Está en la calle Masaryk."], fuentes=["policy_exact_address_repeat"])]
        r = await rag.generate_reply(p.lead, [], "¿en qué calle exactamente?")
        assert r["action"] == "silent" and "политика" in r["silent_reason"]

    @pytest.mark.parametrize("text", ["¿aceptan efectivo?", "¿hay descuento?", "¿puedo pagar en OXXO?",
                                      "quiero mi reembolso", "¿puedo hacer transferencia?"])
    async def test_owner_silent_topics_without_model(self, monkeypatch, text):
        p = Pipe(monkeypatch, history=[msg("anna", "Hola", 5)])
        r = await rag.generate_reply(p.lead, [], text)
        assert r["action"] == "silent" and r["messages"] == [] and p.model_calls() == []

    async def test_second_model_rejects_unsourced_claim(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply(["Hay valet parking en el lugar."], fuentes=[])]
        p.verify = {"sin_fuente": ["hay valet parking"]}
        r = await rag.generate_reply(p.lead, [], "¿hay valet?")
        assert r["action"] == "silent" and "без источника" in r["silent_reason"]

    async def test_off_topic_counter(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={"rag_estado": {"fuera_de_tema_seguidos": 2, "regresos": 1}})
        p.chat_replies = [reply(["Te entiendo. Volviendo al evento, ¿te gustaría saber qué incluye?"],
                                en_tema=False)]
        r = await rag.generate_reply(p.lead, [], "¿viste el partido de ayer?")
        assert r["action"] == "silent" and r["rag_estado"]["fuera_de_tema_seguidos"] == 3

    async def test_off_topic_first_time_answers(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply(["Te entiendo. Volviendo al evento, ¿te gustaría saber qué incluye?"],
                                en_tema=False)]
        r = await rag.generate_reply(p.lead, [], "¿viste el partido?")
        assert r["action"] == "respond" and r["rag_estado"] == {"fuera_de_tema_seguidos": 1, "regresos": 1}

    async def test_on_topic_resets_counter(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={"rag_estado": {"fuera_de_tema_seguidos": 2, "regresos": 2}})
        p.chat_replies = [reply(["Es en [event_address]."])]
        r = await rag.generate_reply(p.lead, [], "¿dónde es?")
        assert r["rag_estado"]["fuera_de_tema_seguidos"] == 0

    async def test_age_out_of_range_verbatim_block(self, monkeypatch):
        p = Pipe(monkeypatch, history=[msg("anna", "¿Qué edad tienes?", 5)])
        monkeypatch.setattr(db, "get_scenario_row", AsyncMock(return_value={
            "id": 7, "template_es": "Texto fijo 7", "mode": "bot_then_block", "blocks_lead": True}))
        p.chat_replies = [reply(["Gracias."], extracted={"age": 24})]
        r = await rag.generate_reply(p.lead, [], "tengo 24 años")
        assert r["action"] == "block" and r["messages"] == ["Texto fijo 7"]

    async def test_verbatim_scenario_40(self, monkeypatch):
        p = Pipe(monkeypatch)
        monkeypatch.setattr(db, "search_scenarios_by_vector", AsyncMock(return_value=[
            {"id": 40, "score": 0.8, "mode": "bot_auto", "blocks_lead": False,
             "template_es": "Texto 40", "ai_allowed": False},
            {"id": 3, "score": 0.5}]))
        r = await rag.generate_reply(p.lead, [], "¿eres un bot?")
        assert r["messages"] == ["Texto 40"] and p.model_calls() == []

    async def test_video_once_with_first_link(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={"name": "Carlos", "is_single": True, "interest": "event",
                                          "photo_received": True})
        p.chat_replies = [reply(["Puedes comprar tu boleto aquí: [event_link]"], fuentes=["event_how_to_buy"])]
        r = await rag.generate_reply(p.lead, [], "¿cómo compro el boleto?")
        assert r["send_event_video"] is True

    async def test_cost_logged_and_recorded(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply(["Es en [event_address]."])]
        r = await rag.generate_reply(p.lead, [], "¿dónde es?")
        assert r["rag_meta"]["usd"] > 0
        p.cost.assert_awaited()

    async def test_openai_failure_fallback(self, monkeypatch):
        p = Pipe(monkeypatch)

        async def boom(*a, **k):
            raise RuntimeError("429")
        monkeypatch.setattr(rag, "_chat", boom)
        monkeypatch.setattr(ai.escalation, "notify_error", AsyncMock())
        r = await rag.generate_reply(p.lead, [], "¿dónde es?")
        assert r["action"] == "escalate"


class TestSummary:
    async def test_summary_built_when_lagging_and_not_source(self, monkeypatch):
        history = [msg("lead" if i % 2 else "anna", f"t{i}", 1000 - i) for i in range(80)]
        p = Pipe(monkeypatch, history=history)
        saved = AsyncMock()
        monkeypatch.setattr(db, "update_memory_summary", saved)

        async def chat(model, system, user, cost, **kw):
            p.chat_calls.append((model, user))
            if system == rag.SUMMARY_SYSTEM:
                return {"resumen": "- dijo que tiene 35 años"}
            if model == rag.CHECK_MODEL:
                return {"sin_fuente": []}
            return reply(["Es en [event_address]."])
        monkeypatch.setattr(rag, "_chat", chat)
        await rag.generate_reply(p.lead, [], "¿dónde es?")
        saved.assert_awaited_once()
        ctx = next(c[1] for c in p.chat_calls if c[0] == rag.MODEL)
        assert ctx["memoria"]["resumen"] == "- dijo que tiene 35 años"
        assert "no es fuente" in ctx["memoria"]["aviso_resumen"]


# ───────────────────────── интеграция ─────────────────────────

class TestMainIntegration:
    @pytest.fixture
    def run(self, monkeypatch):
        import escalation
        import main
        m = {"update_rag_state": AsyncMock(), "notify_escalation": AsyncMock(),
             "update_lead_fields": AsyncMock(), "send": AsyncMock(return_value=1)}
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "update_rag_state", m["update_rag_state"])
        monkeypatch.setattr(db, "update_lead_fields", m["update_lead_fields"])
        monkeypatch.setattr(db, "get_scenario_title", AsyncMock(return_value=None))
        monkeypatch.setattr(escalation, "notify_escalation", m["notify_escalation"])
        monkeypatch.setattr(main.sender, "send", m["send"])
        monkeypatch.setattr(main.actions, "save_anketa_if_complete", AsyncMock())
        m["main"] = main
        return m

    async def test_silent_reason_in_alert_and_state_saved(self, monkeypatch, run):
        monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value={
            "messages": [], "action": "silent", "extracted": {}, "funnel_stage": None,
            "used_scenario_id": None, "silent_reason": "Режим rag: нет данных в базе знаний",
            "rag_estado": {"fuera_de_tema_seguidos": 0}}))
        await run["main"]._run_ai(PHONE, {"phone": PHONE}, "¿hay estacionamiento?")
        run["update_rag_state"].assert_awaited_once_with(PHONE, {"fuera_de_tema_seguidos": 0})
        assert run["notify_escalation"].await_args.args[1] == "Режим rag: нет данных в базе знаний"
        run["send"].assert_not_awaited()

    async def test_state_save_failure_does_not_break_reply(self, monkeypatch, run):
        run["update_rag_state"].side_effect = RuntimeError("column rag_estado does not exist")
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock())
        monkeypatch.setattr(db, "reset_followup_timer", AsyncMock())
        monkeypatch.setattr(ai, "generate_reply", AsyncMock(return_value={
            "messages": ["Es en Polanco, CDMX."], "action": "respond", "extracted": {},
            "funnel_stage": None, "used_scenario_id": None, "rag_estado": {}}))
        await run["main"]._run_ai(PHONE, {"phone": PHONE}, "¿dónde es?")
        run["send"].assert_awaited_once()


class TestStatusAndScheduler:
    async def test_status_shows_cost_and_warning(self, monkeypatch):
        import manager_bot
        sent = AsyncMock()
        monkeypatch.setattr(manager_bot, "_reply", sent)
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value={
            "bot_paused": "1", "scheduler_enabled": "0", "event_active": "0", "reply_mode": "rag_test"}))
        monkeypatch.setattr(db, "count_manual_leads", AsyncMock(return_value=2))
        monkeypatch.setattr(db, "get_daily_amount", AsyncMock(return_value=6.5))
        await manager_bot._bot_status(1, {})
        text = sent.await_args.args[1]
        assert "reply_mode = rag_test" in text and "$6.50" in text and "больше $5" in text

    async def test_status_cost_under_limit(self):
        import manager_bot
        assert manager_bot._cost_line(1.234) == "Расход rag сегодня: $1.23"

    @pytest.mark.parametrize("mode,called", [("rag", True), ("rag_test", False), (None, False)])
    async def test_summaries_only_in_rag(self, monkeypatch, mode, called):
        import scheduler
        monkeypatch.setattr(db, "get_setting", AsyncMock(return_value=mode))
        refresh = AsyncMock(return_value=3)
        monkeypatch.setattr(rag, "refresh_stale_summaries", refresh)
        await scheduler.run_memory_summaries()
        assert refresh.await_count == (1 if called else 0)


class TestPatchScript:
    def test_plan_exact_single_match_only(self):
        from scripts import patch_rag_scenarios_2026_10 as p
        rows = {2: "Slavic Latino Night y Slavic Latino Night", 39: "Hola"}
        changes, errors = p.plan(rows, [(2, "Slavic Latino Night", "X"), (39, "Hola", "Buenas")])
        assert changes == {39: "Buenas"}
        assert errors and "вхождений 2" in errors[0]

    def test_patches_remove_5500_promo_entrantes(self):
        from scripts import patch_rag_scenarios_2026_10 as p
        for _, old, new in p.PATCHES:
            assert "5,500" not in new and "[event_promo]" not in new and "entrantes" not in new
        assert all(sid != 40 for sid, _, _ in p.PATCHES)   # №40 не меняем (решение владелицы)
        assert not hasattr(p, "PATCH_40")


class TestImportKnowledge:
    def test_sql_and_params(self):
        from datetime import date
        from scripts import import_knowledge as ik
        sql = ik.build_sql()
        assert sql.count("$") == len(ik.COLS) + 1 and sql.endswith("updated_at = now()")
        assert f"${len(ik.COLS) + 1}::vector" in sql and "ON CONFLICT (id)" in sql
        params = ik.row_params(BY_ID["event_price"])
        assert params[0] == "event_price" and len(params) == len(ik.COLS)
        assert params[ik.COLS.index("valid_to")] == date(2026, 11, 18)


class TestReviewFixes:
    @pytest.mark.parametrize("text", ["Hola buenas tardes\ncuanto cuesta el boleto",
                                      "Hola cuando es el evento", "hola, que incluye",
                                      "buenas, donde es"])
    def test_question_without_mark_detected(self, text):
        assert rag.has_question(text)

    @pytest.mark.parametrize("text", ["hola", "hola, vi el evento", "info del evento",
                                      "buenas noches", "quiero información"])
    def test_no_question(self, text):
        assert not rag.has_question(text)

    async def test_first_message_question_without_mark_goes_to_model(self, monkeypatch):
        p = Pipe(monkeypatch)
        p.chat_replies = [reply(["Ahora cuesta [event_price_nonmember] MXN."])]
        r = await rag.generate_reply(p.lead, [], "Hola buenas tardes\ncuanto cuesta el boleto")
        assert len(p.model_calls()) == 1

    async def test_verbatim_search_uses_only_lead_text(self, monkeypatch):
        p = Pipe(monkeypatch, history=[msg("anna", "¿Qué edad tienes?", 5)])
        seen = []

        async def embed(texts, cost=None):
            seen.append(list(texts))
            return [[1.0, float(i)] for i, _ in enumerate(texts)]
        monkeypatch.setattr(rag, "_embed_batch", embed)
        search = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "search_scenarios_by_vector", search)
        p.chat_replies = [reply(["Gracias."], extracted={"age": 45})]
        await rag.generate_reply(p.lead, [], "45")
        assert seen[0] == ["45", "¿Qué edad tienes? 45"]
        assert search.await_args.args[0] == "[1.0,0.0]"      # вектор только текста лида

    async def test_age_block_without_scenario_row(self, monkeypatch):
        p = Pipe(monkeypatch, history=[msg("anna", "¿Qué edad tienes?", 5)])
        monkeypatch.setattr(db, "get_scenario_row", AsyncMock(return_value=None))
        p.chat_replies = [reply(["Gracias."], extracted={"age": "80"})]
        r = await rag.generate_reply(p.lead, [], "tengo 80 años")
        assert r["action"] == "block" and r["messages"] == list(ai._AGE_OUT_OF_RANGE_BUBBLES)

    def test_buenas_noches_not_event(self):
        assert memory.ruta({}, "buenas noches") == "sin_definir"

    async def test_regresos_reset_on_topic(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={"rag_estado": {"fuera_de_tema_seguidos": 0, "regresos": 2}})
        p.chat_replies = [reply(["Es en [event_address]."])]
        r = await rag.generate_reply(p.lead, [], "¿dónde es?")
        assert r["rag_estado"]["regresos"] == 0

    def test_link_after_offer_and_yes(self):
        mem_over = {"ultimo_mensaje_anna": "¿Quieres que te pase el enlace para apartar tu lugar?"}
        b, _, _ = check(["Aquí está: [event_link]"], user_text="sí, por favor", **mem_over)
        assert "[event_link]" in b[0]

    def test_price_question_is_not_purchase(self):
        b, _, _ = check(["Ahora cuesta 6000 MXN.", "Puedes comprar tu boleto aquí: [event_link]"],
                        user_text="¿cuánto cuesta el boleto?")
        assert b == ["Ahora cuesta 6000 MXN."]

    def test_link_sentence_removed_rest_kept(self):
        b, _, _ = check(["Ahora cuesta 6000 MXN. Puedes comprar tu boleto aquí: [event_link]"],
                        user_text="¿cuánto cuesta?")
        assert b == ["Ahora cuesta 6000 MXN."]

    def test_url_with_trailing_punct_and_capital(self):
        b, reason, _ = check([f"{LINK}?"], user_text="pásame el link")
        assert reason is None and b[0].startswith("https://")

    def test_bare_foreign_domain_silent(self):
        assert check(["Más info en matchmatchagency.com/es"])[1]

    def test_numbers_formats(self):
        assert rag.numbers_in("6,000.00 MXN") == {"6000"}
        assert check(["Cuesta 6,000.00 MXN."])[1] is None
        assert check(["Son cinco mil quinientos por persona."])[1]
        assert check(["Son 15 500 personas."])[1]                # число без источника, не «5,500»

    def test_old_lead_numbers_not_a_source(self):
        """«30» из старого сообщения лида («tengo 30») не разрешает «30 mujeres»."""
        _, reason, _ = check(["Van 30 mujeres."], user_text="¿cuántas van?", lead_texts=["tengo 30"])
        assert reason

    def test_opener_cut_whole_clause(self):
        b, _, _ = check(["Con mucho gusto te cuento: el evento es en Polanco, CDMX."])
        assert b == ["El evento es en Polanco, CDMX."]

    def test_claro_reaction_allowed(self):
        """«¡Claro!» как реакция на слова лида разрешено (решение владелицы 2026-10-21)."""
        b, _, _ = check(["¡Claro! Es en Polanco, CDMX."])
        assert b == ["¡Claro! Es en Polanco, CDMX."]

    def test_multiline_bubble_preserved(self):
        b, _, _ = check(["Ahora cuesta 6000 MXN.\nIncluye bebida de bienvenida y botanas."])
        assert "\n" in b[0]

    def test_variation_selector_removed(self):
        assert rag.keep_emojis(["Hola 🤍", "Listo ❤️ ya", "Y ✨"]) == ["Hola 🤍", "Listo ❤️ ya", "Y"]
        assert rag.keep_emojis(["Hola 🤍❤️ ya"]) == ["Hola 🤍 ya"]

    def test_inbound_bracket_is_lead_and_in_burst(self):
        msgs = [msg("lead", "[voice message]", 2), msg("lead", "hola", 1)]
        assert memory.autor(msgs[0]) == "lead"
        prior, burst = memory.split_current_burst(msgs, "hola")
        assert prior == [] and len(burst) == 2

    async def test_announced_at_preferred(self, monkeypatch):
        monkeypatch.setattr(db, "get_setting", AsyncMock(return_value="2026-10-20T17:45:22+00:00"))
        assert await memory._evento_desde() == datetime(2026, 10, 20, 17, 45, 22, tzinfo=timezone.utc)

    async def test_empty_knowledge_table_falls_back_to_seed(self, monkeypatch):
        knowledge.reset_cache()
        monkeypatch.setattr(knowledge, "_load_from_db", AsyncMock(return_value=[]))
        recs = await knowledge.records()
        assert len(recs) == len(SEED)
        knowledge.reset_cache()


class TestPromoTrigger:
    @pytest.mark.parametrize("text", ["¿hay promo?", "¿tienen alguna promoción?", "¿y la promo?",
                                      "¿hay descuento?", "¿tienen cupón?", "¿tienes algún código?",
                                      "¿manejan promociones?"])
    def test_discount_questions_silent(self, text):
        assert rag._SILENT_TOPIC_RE.search(text), text

    @pytest.mark.parametrize("text", ["vi la promo en Instagram", "vi su promoción",
                                      "vi el anuncio", "hola, vi la promo del evento",
                                      "mi código postal es 11560"])
    def test_ad_mentions_not_silent(self, text):
        assert not rag._SILENT_TOPIC_RE.search(text), text


class TestReview3:
    def test_link_after_pending_purchase_and_name(self):
        """«¿cómo compro?» до имени → ссылку придержали; «Carlos, soltero» → ссылка уходит."""
        mem_over = {"lead_texts": ["hola, vi el evento", "¿cómo compro el boleto?", "Carlos, soltero"],
                    "ultimo_mensaje_anna": "Se compra en línea. ¿Cómo te llamas y estás soltero?"}
        b, _, _ = check(["Puedes comprar tu boleto aquí: [event_link]"], user_text="Carlos, soltero",
                        **mem_over)
        assert "[event_link]" in b[0]

    @pytest.mark.parametrize("text", ["quiero asistir", "me gustaría ir", "quiero 2 boletos",
                                      "¿dónde consigo los boletos?"])
    def test_purchase_phrases(self, text):
        assert rag.wants_to_buy(text, None)

    def test_offer_variants(self):
        assert rag.wants_to_buy("sí", "¿Te lo mando?")
        assert rag.wants_to_buy("claro", "¿Te gustaría el enlace?")

    @pytest.mark.parametrize("text", ["¿cuál es el código de vestimenta?", "mi código postal es 11560"])
    def test_codigo_not_silent(self, text):
        assert not rag._SILENT_TOPIC_RE.search(text)

    @pytest.mark.parametrize("text", ["quiero la promo", "¿la promo sigue vigente?",
                                      "¿tienen código de descuento?", "¿algún código?"])
    def test_promo_requests_silent(self, text):
        assert rag._SILENT_TOPIC_RE.search(text)

    def test_informative_exclamation_not_reaction(self):
        import conversation_rules as cr
        assert cr.reaction_start("¡Es en Polanco!") is None
        assert cr.reaction_start("¡Hola Carlos!") is None
        assert cr.reaction_start("¡Qué bien, gracias!") == "que bien"
        b, _, _ = check(["¡Es en Polanco, CDMX!"], inicios_recientes=["es en polanco cdmx"])
        assert b == ["¡Es en Polanco, CDMX!"]

    def test_mira_slang_and_base_de_datos(self):
        import conversation_rules as cr
        assert cr.violations("Mira, el evento es en Polanco.")
        assert cr.violations("Te cuento. Mira este video.")
        assert not cr.violations("Tenemos una base de datos de eventos.")
        assert not cr.violations("Puedes aprovechar para conocer gente.")


class TestEventPhotoGate:
    """Ссылка на ивент — только после одобренного фото (решение владелицы 2026-10-22)."""

    NO_PHOTO = {"name": "Carlos", "is_single": True, "interest": "event"}

    def test_link_removed_without_photo(self):
        lead = dict(self.NO_PHOTO)
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        cal = memory.calificacion(lead, ya, "¿cómo compro?", None, compra=True)
        b, reason, notes = check(["Se compra en línea en la página del evento: [event_link]",
                                  "¿Me mandas una foto tuya?"],
                                 user_text="¿cómo compro el boleto?", lead=lead, calificacion=cal)
        assert reason is None and all("[event_link]" not in x for x in b)
        assert any("calificacion" in n for n in notes)

    def test_pending_purchase_link_after_photo(self):
        """«¿cómo compro?» давно (дальше 3 сообщений), фото одобрено → ссылка уходит."""
        lead_texts = ["¿cómo compro el boleto?", "Carlos, soltero", "tengo 35", "soy médico",
                      "ok", "[фото одобрено]"]
        b, reason, _ = check(["¡Gracias por la foto! Puedes comprar tu boleto aquí: [event_link]"],
                             user_text="[фото одобрено]", lead_texts=lead_texts,
                             compra_pendiente=True)
        assert reason is None and "[event_link]" in b[0]

    def test_compra_pendiente_flag(self):
        mem = _mem(lead_texts=["¿cómo compro el boleto?", "Carlos, soltero", "[фото одобрено]"],
                   ultimo_mensaje_anna="¿Me mandas una foto tuya?")
        assert rag.compra_pendiente(mem, "[фото одобрено]") is True
        mem["ya_hecho"]["enlace_enviado"] = "2026-10-21"
        assert rag.compra_pendiente(mem, "[фото одобрено]") is False
        assert rag.compra_pendiente(_mem(lead_texts=["hola", "¿dónde es?"]), "¿dónde es?") is False

    async def test_buy_before_photo_asks_photo(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra=self.NO_PHOTO)
        p.chat_replies = [reply(["Se compra en línea en la página del evento: [event_link]",
                                 "¿Me mandas una foto tuya?"], fuentes=["event_how_to_buy"])]
        r = await rag.generate_reply(p.lead, [], "¿cómo compro el boleto?")
        ctx = p.model_calls()[0][1]
        assert ctx["calificacion"]["siguiente"] == "photo"
        assert ctx["calificacion"]["compra_pendiente"] is True
        assert ctx["calificacion"]["etapa"] == "calificacion"
        assert all("[event_link]" not in m for m in r["messages"])
        assert not r.get("send_event_video")

    async def test_photo_approved_sends_link_with_video(self, monkeypatch):
        history = [msg("lead", "¿cómo compro el boleto?", 30),
                   msg("anna", "Se compra en línea en la página del evento. ¿Cómo te llamas y estás soltero?", 29),
                   msg("lead", "Carlos, soltero", 20),
                   msg("anna", "Gracias, Carlos. ¿Me mandas una foto tuya?", 19)]
        p = Pipe(monkeypatch, history=history, lead_extra={**self.NO_PHOTO, "photo_received": True})
        p.chat_replies = [reply(["Gracias por la foto, Carlos.", "Puedes comprar tu boleto aquí: [event_link]"],
                                fuentes=["event_how_to_buy"])]
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        ctx = p.model_calls()[0][1]
        assert ctx["calificacion"]["etapa"] == "venta" and ctx["calificacion"]["compra_pendiente"]
        assert any("[event_link]" in m for m in r["messages"])
        assert r["send_event_video"] is True


class TestPhotoGateReview:
    def test_marker_counts_without_flag(self):
        """Флаг в словаре лида не дошёл, но маркер одобрения в этом ходе — фото есть."""
        lead = {"interest": "event", "name": "Carlos", "is_single": True}
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        assert memory.calificacion(lead, ya, "[фото одобрено]", None)["etapa"] == "venta"
        assert memory.calificacion(lead, ya, "[фото одобрено вручную]", None)["etapa"] == "venta"

    def test_old_registro_mention_is_not_pending_purchase(self):
        mem = _mem(lead_texts=["me registré en Instagram para ver sus fotos", "Carlos", "ok",
                               "soy médico", "[фото одобрено]"])
        assert rag.compra_pendiente(mem, "[фото одобрено]") is False
        mem = _mem(lead_texts=["¿cómo compro el boleto?", "Carlos", "ok", "soy médico",
                               "[фото одобрено]"])
        assert rag.compra_pendiente(mem, "[фото одобрено]") is True


class TestVerifierFromSmoke:
    """Ложные «без источника» из платного смоука 2026-10-22: gpt-4.1-mini помечал фразы,
    которые почти дословно есть в фичах, благодарности и вопросы."""

    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-21")

    async def _run(self, monkeypatch, items, bubbles=("x",), historial=None, anna=None):
        async def chat(model, system, user, cost, **kw):
            self.payload = user
            return {"afirmaciones": items}
        monkeypatch.setattr(rag, "_chat", chat)
        return await rag.verify_claims(list(bubbles), [], memory.evento_actual(SETTINGS),
                                       ["¿cuánto cuesta?"], rag.Cost(), anna or [],
                                       recs=self.RECS, historial=historial or [])

    @pytest.mark.parametrize("claim", [
        "incluye bebida de bienvenida y botanas",
        "Trabajo con más de 3,000 mujeres eslavas que buscan pareja.",
        "es una noche para conocer mujeres eslavas que buscan una relación seria",
        "¡Gracias por tu foto, Javier! 🤍",
        "¿Cuántos años tienes y a qué te dedicas?",
    ])
    async def test_false_positives_dropped(self, monkeypatch, claim):
        assert await self._run(monkeypatch, [{"texto": claim, "respaldo": None}]) == []

    async def test_all_usable_records_are_sources(self, monkeypatch):
        await self._run(monkeypatch, [])
        assert len(self.payload["FUENTES"]["fichas"]) > 20

    async def test_manual_promise_from_conversation(self, monkeypatch):
        hist = [{"autor": "anya_manual", "texto": "Te guardo un lugar hasta el viernes si me confirmas hoy."}]
        claims = await self._run(monkeypatch, [{"texto": "te guardo el lugar hasta el viernes",
                                                "respaldo": None}], historial=hist)
        assert claims == []

    @pytest.mark.parametrize("claim", ["Van 30 mujeres y 30 hombres.", "Hay valet parking gratis.",
                                       "Es el único evento así en América Latina."])
    async def test_real_inventions_kept(self, monkeypatch, claim):
        assert await self._run(monkeypatch, [{"texto": claim, "respaldo": None}]) == [claim]

    async def test_quoted_support_trusted_and_old_format(self, monkeypatch):
        assert await self._run(monkeypatch, [{"texto": "Hay valet parking", "respaldo": "x"}]) == []

        async def chat(model, system, user, cost, **kw):
            return {"sin_fuente": ["hay valet parking"]}
        monkeypatch.setattr(rag, "_chat", chat)
        assert await rag.verify_claims(["x"], [], None, [], rag.Cost()) == ["hay valet parking"]


def test_no_data_reply_goes_silent():
    assert rag._NO_DATA_RE.search("No puedo decirte cuántas personas van")
    assert not rag._NO_DATA_RE.search("No es obligatorio ir disfrazado")


def test_opener_with_name_stripped():
    assert rag.clean_style(["¡Perfecto, Luis! Gracias por compartirlo."]) == ["Gracias por compartirlo."]
    assert rag.clean_style(["¡Perfecto! Te cuento."]) == ["Te cuento."]
