"""Режим rag (этап 2): память лида, база знаний, проверки ответа, конвейер.

OpenAI, БД и медиа замоканы: _chat отдаёт заранее заданные JSON, эмбеддинги фиктивные,
knowledge — из knowledge/seed.json.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import ai
import ai_conversation as rag
import db
import sender
import knowledge
import memory

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
EVENT_DESDE = datetime(2026, 10, 20, 17, 45, tzinfo=timezone.utc)
LINK = "https://www.rusaencdmx.com/18-11-26-slavic-latino-night-halloween"
SETTINGS = {"event_active": "1", "event_date": "2026-11-18", "event_start": "8:30 PM",
            "event_address": "Roma Norte, CDMX", "event_price_nonmember": "6000",
            "event_price_old": "9,000", "event_link": LINK}
PHONE = "wa_5215500000001"
SEED = knowledge.load_seed()
BY_ID = {r["id"]: r for r in SEED}


# Приветствие на «hola» — сценарий №1 дословно; при включённом и не прошедшем ивенте код
# добавляет фразу про Хеллоуин в конец абзаца про ивент (решение владелицы 2026-10-22).
S1 = BY_ID["greeting_hola"]["contenido_es"].split("\n\n")
S1_HALLOWEEN = [S1[0], S1[1] + " Esta vez es temática de Halloween 🎃", *S1[2:]]


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
        m = msg("anna", "Ahora cuesta 6000 MXN, es en Roma Norte, CDMX. Es temática de Halloween.", 60)
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
        # 30 и 30 подтверждены автоответом владелицы 2026-10-22; возрасты — нет
        assert "event_attendees" in ids and "service_women_ages" not in ids
        assert "event_end_time" not in ids and "event_unique" not in ids
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
        assert "todos solteros" not in dumped                    # старая формулировка «30 и 30»

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


def check(bubbles, user_text="¿cuánto cuesta?", lead=None, fichas=None, dio_algo=True, **mem_over):
    return rag.check_reply(bubbles, mem=_mem(**mem_over), fichas=fichas or FICHAS, dio_algo=dio_algo,
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
        b, reason, _ = check(["El precio de preventa es de 6000 MXN, antes 9,000 MXN."])
        assert reason is None
        # лестницы цен больше нет: «7000» без источника → молчание
        assert check(["Ahora cuesta 6000 MXN; después costará 7000 MXN."])[1]

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
        # нет имени и «холост ли»: ссылки нет, ответ по сути и вопрос (решение 2026-10-22)
        assert b == [rag.BUY_ONLINE_SENTENCE, "¿Cómo te llamas y estás soltero?"]

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
        b, _, _ = check(["¡Qué bien! Es en Roma Norte, CDMX."], user_text="¿dónde es?", inicios_recientes=["que bien", None])
        assert b == ["Es en Roma Norte, CDMX."]
        b, _, _ = check(["¡Qué bien! Es en Roma Norte, CDMX."], user_text="¿dónde es?", inicios_recientes=["jaja"])
        assert b == ["¡Qué bien! Es en Roma Norte, CDMX."]

    def test_laugh_allowed(self):
        b, reason, _ = check(["Jaja, ¡ojalá alguien llegue de vampiro! Si no tienes disfraz, te dan una máscara."],
                             user_text="jaja voy a ir de vampiro")
        assert reason is None and b[0].startswith("Jaja")

    def test_caption_within_budget(self):
        assert rag._caption_within_budget("Video 🎬", ["A 🤍", "B 😊"]) == "Video"
        assert rag._caption_within_budget("Video 🎬", ["A 🤍"]) == "Video 🎬"

    def test_banned_opener_stripped(self):
        b, _, _ = check(["¡Gracias por tu interés! Es en Roma Norte, CDMX."], user_text="¿dónde es?")
        assert b == ["Es en Roma Norte, CDMX."]
        b, _, _ = check(["¡Perfecto! Es en Roma Norte, CDMX. Te va a encantar."], user_text="¿dónde es?")
        assert b == ["Es en Roma Norte, CDMX."]

    def test_name_at_start_stripped(self):
        b, _, _ = check(["Carlos, es en Roma Norte, CDMX."])
        assert b == ["Es en Roma Norte, CDMX."]

    def test_no_reintroduction(self):
        ya = memory.ya_hecho([msg("anna", "Hola, soy Anna", 100)], {}, SETTINGS, None, NOW)
        b, _, _ = check(["Hola, soy Anna, fundadora de Match Match Agency. Es en Roma Norte, CDMX."],
                        ya_hecho=ya)
        assert b == ["Es en Roma Norte, CDMX."]

    def test_slang_sentence_removed(self):
        b, _, _ = check(["Es en Roma Norte, CDMX. Checa el link porfa."], user_text="¿dónde es?")
        assert b == ["Es en Roma Norte, CDMX."]

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
        assert r["messages"] == S1_HALLOWEEN
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
        assert ctx["evento_actual"]["precio"] == "[event_price_nonmember] MXN, precio de preventa"
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
        p = Pipe(monkeypatch, history=[msg("anna", "Es en Roma Norte, CDMX.", 5)])
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
        alert = AsyncMock()
        monkeypatch.setattr(ai.escalation, "notify_error", alert)
        r = await rag.generate_reply(p.lead, [], "¿dónde es?")
        # сбой нового режима: старый ответ не уходит — молчание, Аня и техалерт (2026-10-22)
        assert r["action"] == "silent" and r["messages"] == [] and "сбой" in r["silent_reason"]
        alert.assert_awaited()


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
            "messages": ["Es en Roma Norte, CDMX."], "action": "respond", "extracted": {},
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

    def test_patches_remove_5500_entrantes_keep_promo(self):
        from scripts import patch_rag_scenarios_2026_10 as p
        for _, old, new in p.PATCHES:
            assert "5,500" not in new and "entrantes" not in new and "único" not in new
            assert ("[event_promo]" in old) == ("[event_promo]" in new)   # зачёркнутая цена остаётся
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
        b, _, _ = check(["Es en Roma Norte, CDMX.", "Puedes comprar tu boleto aquí: [event_link]"],
                        user_text="¿cuánto cuesta el boleto?")
        assert b == ["Es en Roma Norte, CDMX."]

    def test_link_sentence_removed_rest_kept(self):
        b, _, _ = check(["Es en Roma Norte, CDMX. Puedes comprar tu boleto aquí: [event_link]"],
                        user_text="¿cuánto cuesta?")
        assert b == ["Es en Roma Norte, CDMX."]

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
        b, _, _ = check(["Con mucho gusto te cuento: el evento es en Roma Norte, CDMX."])
        assert b == ["El evento es en Roma Norte, CDMX."]

    def test_claro_reaction_allowed(self):
        """«¡Claro!» как реакция на слова лида разрешено (решение владелицы 2026-10-21)."""
        b, _, _ = check(["¡Claro! Es en Roma Norte, CDMX."], user_text="¿dónde es?")
        assert b == ["¡Claro! Es en Roma Norte, CDMX."]

    def test_multiline_bubble_preserved(self):
        b, _, _ = check(["Ahora cuesta 6000 MXN.\nIncluye bebida de bienvenida y botanas."],
                        user_text="¿cuánto cuesta y qué incluye?")
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
        assert len(recs) == len([r for r in SEED if r.get("activo", True)])
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
        assert cr.reaction_start("¡Es en Roma Norte!") is None
        assert cr.reaction_start("¡Hola Carlos!") is None
        assert cr.reaction_start("¡Qué bien, gracias!") == "que bien"
        b, _, _ = check(["¡Es en Roma Norte, CDMX!"], inicios_recientes=["es en roma norte cdmx"])
        assert b == ["¡Es en Roma Norte, CDMX!"]

    def test_mira_slang_and_base_de_datos(self):
        import conversation_rules as cr
        assert cr.violations("Mira, el evento es en Roma Norte.")
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

    async def test_buy_before_photo_gets_link(self, monkeypatch):
        """Решение 2026-10-22: на прямой вопрос о покупке ссылка сразу, фото не нужно."""
        p = Pipe(monkeypatch, lead_extra=self.NO_PHOTO)
        r = await rag.generate_reply(p.lead, [], "¿cómo compro el boleto?")
        assert p.model_calls() == []
        assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]
        assert r["send_event_video"] is True and not r["send_event_photo"]

    async def test_photo_approved_sends_link_with_video(self, monkeypatch):
        history = [msg("lead", "¿cómo compro el boleto?", 30),
                   msg("anna", "Se compra en línea en la página del evento. ¿Cómo te llamas y estás soltero?", 29),
                   msg("lead", "Carlos, soltero", 20),
                   msg("anna", "Gracias, Carlos. ¿Me mandas una foto tuya?", 19)]
        p = Pipe(monkeypatch, history=history, lead_extra={**self.NO_PHOTO, "photo_received": True})
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        assert p.model_calls() == []          # ход после фото собирает код (решение 2026-10-22)
        assert r["messages"][-1] == rag.CTA_LINK
        assert r["send_event_video"] is True and r["send_event_photo"] is True
        assert r["after_media"] == ["¿Cuántos años tienes y a qué te dedicas?"]


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
    """Сверка утверждений (решения владелицы 2026-10-22): источник — только подтверждённая
    база и карточка ивента; числа точно; отрицание должно совпадать; слову модели не верим."""

    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-21")

    async def _run(self, monkeypatch, items, bubbles=("x",)):
        async def chat(model, system, user, cost, **kw):
            self.payload = user
            return {"afirmaciones": items}
        monkeypatch.setattr(rag, "_chat", chat)
        return await rag.verify_claims(list(bubbles), self.RECS, memory.evento_actual(SETTINGS),
                                       ["¿cuánto cuesta?"], rag.Cost(), SETTINGS)

    @pytest.mark.parametrize("claim", [
        "incluye bebida de bienvenida y botanas",
        "Trabajo con más de 3,000 mujeres eslavas que buscan pareja.",
        "es una noche para conocer mujeres eslavas que buscan una relación seria",
        "Ahora cuesta 6000 MXN.",
        "Es en Roma Norte, CDMX.",
        "¡Gracias por tu foto, Javier! 🤍",
        "¿Cuántos años tienes y a qué te dedicas?",
    ])
    async def test_false_positives_dropped(self, monkeypatch, claim):
        assert await self._run(monkeypatch, [{"texto": claim, "respaldo": None}]) == []

    async def test_only_confirmed_records_are_sources(self, monkeypatch):
        await self._run(monkeypatch, [])
        fichas = self.payload["FUENTES"]["fichas"]
        assert len(fichas) > 20
        assert not any("30 mujeres y 30 hombres" in f for f in fichas)   # неподтверждённое
        assert "CONVERSACION" not in self.payload

    @pytest.mark.parametrize("claim", [
        "No incluye botanas.", "La entrada es gratis.", "Son 40 mujeres.", "Termina a la 1:00.",
        "Van 40 mujeres y 40 hombres.", "Hay valet parking gratis.",
        "Es el único evento así en América Latina.", "Puedes pagar en efectivo en OXXO.",
    ])
    async def test_inventions_kept_even_with_model_quote(self, monkeypatch, claim):
        """Выдумка не проходит ни без цитаты, ни с цитатой модели из настоящей фичи."""
        assert await self._run(monkeypatch, [{"texto": claim, "respaldo": None}]) == [claim]
        quote = "Incluye bebida de bienvenida y botanas."
        assert await self._run(monkeypatch, [{"texto": claim, "respaldo": quote}]) == [claim]

    async def test_paraphrase_with_real_quote_passes(self, monkeypatch):
        quote = "puedes venir disfrazado y, si no tienes disfraz, los organizadores te dan una máscara"
        assert await self._run(monkeypatch, [{"texto": "No es obligatorio ir disfrazado",
                                              "respaldo": quote}]) == []

    async def test_quote_from_old_bot_reply_not_trusted(self, monkeypatch):
        """Цитата из старого ответа бота — не источник."""
        claims = await self._run(monkeypatch, [{"texto": "Puedes pagar en OXXO",
                                                "respaldo": "puedes pagar en efectivo en Oxxo"}])
        assert claims == ["Puedes pagar en OXXO"]

    async def test_old_format(self, monkeypatch):
        async def chat(model, system, user, cost, **kw):
            return {"sin_fuente": ["hay valet parking"]}
        monkeypatch.setattr(rag, "_chat", chat)
        assert await rag.verify_claims(["x"], [], None, [], rag.Cost()) == ["hay valet parking"]

    def test_anya_promise_detected(self):
        hist = [{"autor": "anya_manual", "texto": "Te guardo un lugar hasta el viernes si me confirmas hoy."}]
        assert rag.promesa_de_anya(["te guardo el lugar hasta el viernes"], hist)
        assert not rag.promesa_de_anya(["hay valet parking"], hist)


def test_no_data_reply_goes_silent():
    assert rag._NO_DATA_RE.search("No puedo decirte cuántas personas van")
    assert not rag._NO_DATA_RE.search("No es obligatorio ir disfrazado")


def test_opener_with_name_stripped():
    assert rag.clean_style(["¡Perfecto, Luis! Gracias por compartirlo."]) == ["Gracias por compartirlo."]
    assert rag.clean_style(["¡Perfecto! Te cuento."]) == ["Te cuento."]


# ───────────────────────── решения владелицы 2026-10-22 ─────────────────────────

class TestToneAndSale:
    def test_ignored_slot_question_not_repeated(self):
        """Прошлый ответ спросил возраст, лид спросил о другом → вопрос не повторяем подряд."""
        last = "Es en Roma Norte, CDMX.\n\n¿Cuántos años tienes y a qué te dedicas?"
        b, _, notes = check(["Incluye bebida de bienvenida y botanas.", "¿Cuántos años tienes?"],
                            user_text="¿qué incluye?", ultimo_mensaje_anna=last)
        assert b == ["Incluye bebida de bienvenida y botanas."]
        assert "убрала повтор вопроса по слоту" in notes

    def test_other_slot_question_kept(self):
        b, _, _ = check(["Es en Roma Norte, CDMX.", "¿Me mandas una foto tuya?"], user_text="¿dónde es?",
                        ultimo_mensaje_anna="¿Cuántos años tienes?")
        assert b[-1] == "¿Me mandas una foto tuya?"

    def test_ignored_question_counts_as_attempt(self):
        prior = [msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 10), msg("lead", "¿dónde es?", 9)]
        ya = memory.ya_hecho(prior, {}, SETTINGS, None, NOW)
        assert ya["preguntas_calificacion"]["age"] == 1
        # «confirmas que estás soltero.» — утверждение, не вопрос: попыткой не считается
        ya = memory.ya_hecho([msg("anna", "Al comprar el boleto confirmas que estás soltero.", 5)],
                             {}, SETTINGS, None, NOW)
        assert ya["preguntas_calificacion"]["is_single"] == 0

    def test_last_reply_is_whole_group(self):
        prior = [msg("lead", "hola", 10), msg("anna", "Es en Roma Norte.", 9),
                 msg("anna", "¿Cuántos años tienes?", 8)]
        assert memory.ultima_respuesta(prior) == "Es en Roma Norte.\n\n¿Cuántos años tienes?"

    def test_thanks_only_when_lead_gave_something(self):
        b, _, _ = check(["¡Gracias, Diego! Qué gusto saber de ti.", "Es en Roma Norte, CDMX."],
                        user_text="hola, ¿dónde es?", dio_algo=False)
        assert b == ["Qué gusto saber de ti.", "Es en Roma Norte, CDMX."]
        b, _, _ = check(["¡Gracias por la foto!", "Es en Roma Norte, CDMX."], user_text="[фото одобрено]")
        assert b[0] == "¡Gracias por la foto!"
        assert rag.lead_dio_algo("me llamo Diego", {"name": "Diego"})
        assert rag.lead_dio_algo("[фото одобрено]", {})
        assert rag.lead_dio_algo("eres muy amable", {})
        assert not rag.lead_dio_algo("¿cuánto cuesta el evento?", {"interest": "event"})

    @pytest.mark.parametrize("given,expected", [
        # лестницы цен нет (автоответ владелицы 2026-10-22): ступень убираем, к цене — зачёркнутую
        (["La siguiente es de 7000 MXN."], []),
        (["Ahora cuesta 6000 MXN; la siguiente etapa es de 7000 MXN."],
         ["Ahora cuesta 6000 MXN[event_promo]."]),
        (["Ahora cuesta 6000 MXN. Más adelante el precio sube a [event_price_next] MXN.",
          "¿Cuántos años tienes?"],
         ["Ahora cuesta 6000 MXN[event_promo].", "¿Cuántos años tienes?"]),
        (["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."],
         ["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."]),
        (["La inversión es desde $10,000 USD."], ["La inversión es desde $10,000 USD."]),
    ])
    def test_whole_price(self, given, expected):
        assert rag.whole_price(given, SETTINGS) == expected

    def test_whole_price_service_price_untouched(self):
        assert rag.whole_price(["La inversión es desde $10,000 USD."], SETTINGS) == \
            ["La inversión es desde $10,000 USD."]

    def test_price_steps_record_off(self):
        by = {r["id"]: r for r in knowledge.usable(SEED, SETTINGS, "2026-10-21")}
        assert by["event_price"]["contenido_es"] == \
            "El precio de preventa es de [event_price_nonmember] MXN[event_promo]."
        assert "event_price_steps" not in by

    def test_facts_said_are_tracked(self):
        ya = memory.ya_hecho([msg("anna", "Es temática de Halloween.", 5),
                              msg("anna", "Es en Roma Norte, CDMX, el 18 de noviembre.", 4)],
                             {}, SETTINGS, EVENT_DESDE, NOW)
        assert {"halloween", "lugar", "fecha"} <= set(ya["hechos_dichos"])
        assert "evento_30" not in ya["hechos_dichos"]


OLD_LIES = [msg("lead", "¿cuántas van?", 60),
            msg("anna", "Van 50 mujeres y 50 hombres, todos solteros.", 59),
            msg("lead", "¿y cómo pago?", 58),
            msg("anna", "Puedes pagar en efectivo en OXXO o por transferencia.", 57)]


class TestOwnerRisks:
    async def test_old_bot_lies_not_confirmed(self, monkeypatch):
        """Старая переписка с выдумками legacy: новый бот их не подтверждает (пункт 6)."""
        p = Pipe(monkeypatch, history=OLD_LIES, lead_extra={"name": "Carlos", "is_single": True,
                                                             "interest": "event"})
        p.chat_replies = [reply(["Van 50 mujeres y 50 hombres, como te dije."], fuentes=["event_what"])]
        p.verify = {"afirmaciones": [{"texto": "Van 50 mujeres y 50 hombres",
                                      "respaldo": "Van 50 mujeres y 50 hombres, todos solteros."}]}
        r = await rag.generate_reply(p.lead, OLD_LIES, "¿al final cuántas mujeres van?")
        assert r["action"] == "silent" and r["messages"] == []
        # «50» нет в источниках — молчание уже на проверке чисел, до второй модели; если сверщик
        # всё же звали, в его источниках нет ни OXXO, ни старой выдумки
        for verify_payload in [c[1] for c in p.chat_calls if c[0] == rag.CHECK_MODEL]:
            assert "OXXO" not in json.dumps(verify_payload, ensure_ascii=False)
            assert "50 mujeres" not in json.dumps(verify_payload["FUENTES"], ensure_ascii=False)

    async def test_old_bot_payment_lie_not_confirmed(self, monkeypatch):
        p = Pipe(monkeypatch, history=OLD_LIES, lead_extra={"name": "Carlos", "is_single": True,
                                                             "interest": "event", "photo_received": True})
        p.chat_replies = [reply(["Puedes pagar en efectivo en OXXO, como te comenté."])]
        p.verify = {"afirmaciones": [{"texto": "Puedes pagar en efectivo en OXXO", "respaldo": None}]}
        r = await rag.generate_reply(p.lead, OLD_LIES, "¿entonces sigue en pie lo que me dijiste?")
        assert r["action"] == "silent" and r["messages"] == []
        # «cómo se paga» с 2026-10-22 — прямой вопрос о покупке: ссылка, OXXO не подтверждаем
        r = await rag.generate_reply(p.lead, OLD_LIES, "¿entonces cómo se paga al final?")
        assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]

    async def test_anya_promise_goes_to_anya(self, monkeypatch):
        hist = [msg("anna", "Te guardo un lugar hasta el viernes si me confirmas hoy.", 30, manual=True)]
        p = Pipe(monkeypatch, history=hist, lead_extra={"name": "Carlos", "is_single": True,
                                                         "interest": "event"})
        p.chat_replies = [reply(["Sí, te guardo el lugar hasta el viernes."])]
        p.verify = {"afirmaciones": [{"texto": "te guardo el lugar hasta el viernes", "respaldo": None}]}
        r = await rag.generate_reply(p.lead, hist, "¿me guardas el lugar hasta el viernes?")
        assert r["action"] == "silent" and "policy_anya_promise" in r["silent_reason"]

    async def test_photo_dead_end_buy_gets_link(self, monkeypatch):
        """Две просьбы фото без ответа, лид спрашивает, как купить: раньше — Аня (пункт 8),
        с решения 2026-10-22 фото для покупки не нужно — ссылка сразу."""
        hist = [msg("anna", "¿Me mandas una foto tuya?", 60), msg("lead", "¿y dónde es?", 50),
                msg("anna", "Es en Roma Norte, CDMX. ¿Me mandas una foto tuya?", 49)]
        p = Pipe(monkeypatch, history=hist, lead_extra={"name": "Carlos", "is_single": True,
                                                         "interest": "event", "age": 35,
                                                         "profession": "médico"})
        r = await rag.generate_reply(p.lead, hist, "¿cómo compro el boleto?")
        assert r["action"] == "respond" and r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]
        assert p.model_calls() == []

    async def test_photo_dead_end_other_question_still_anya(self, monkeypatch):
        hist = [msg("anna", "¿Me mandas una foto tuya?", 60), msg("lead", "¿y dónde es?", 50),
                msg("anna", "Es en Roma Norte, CDMX. ¿Me mandas una foto tuya?", 49)]
        p = Pipe(monkeypatch, history=hist, lead_extra={"name": "Carlos", "is_single": True,
                                                         "interest": "event"})
        r = await rag.generate_reply(p.lead, hist, "¿a qué hora empieza?")
        assert r["action"] == "silent" and "без фото" in r["silent_reason"]

    async def test_empty_presale_token_never_sent(self, monkeypatch):
        """Нет event_presale_until, а модель написала токен → не отправляем, Аня + техалерт (п. 10)."""
        settings = {**SETTINGS, "event_presale_until": ""}
        p = Pipe(monkeypatch, settings=settings, lead_extra={"name": "Carlos", "is_single": True,
                                                             "interest": "event"})
        alert = AsyncMock()
        monkeypatch.setattr(ai.escalation, "notify_error", alert)
        p.chat_replies = [reply(["El precio de preventa es de [event_price_nonmember] MXN, hasta el "
                                 "[event_presale_until]."], fuentes=["event_price"])]
        r = await rag.generate_reply(p.lead, [], "¿cuánto cuesta?")
        assert r["action"] == "silent" and "токен" in r["silent_reason"]
        alert.assert_awaited()


async def _silent_case(monkeypatch, case):
    """Пути молчания режима rag: каждый — action=silent с причиной и без текста лиду."""
    lead = {"name": "Carlos", "is_single": True, "interest": "event", "photo_received": True}
    p = Pipe(monkeypatch, lead_extra=lead)
    monkeypatch.setattr(ai.escalation, "notify_error", AsyncMock())
    text = "¿hay estacionamiento?"
    if case == "falta_dato":
        p.chat_replies = [reply([], falta_dato=True), reply([], falta_dato=True)]
    elif case == "modelo_silent":
        p.chat_replies = [reply([], accion="silent")]
    elif case == "politica":
        p.chat_replies = [reply(["x"], fuentes=["policy_companion"])]
        monkeypatch.setattr(knowledge, "search", AsyncMock(return_value=[BY_ID["policy_companion"]]))
    elif case == "fuera_de_tema":
        p.lead["rag_estado"] = {"fuera_de_tema_seguidos": 2}
        p.chat_replies = [reply(["Jaja."], en_tema=False)]
    elif case == "5500":
        p.chat_replies = [reply(["Son 5,500 MXN."])]
    elif case == "no_data":
        p.chat_replies = [reply(["No puedo decirte eso."])]
    elif case == "sverka":
        p.chat_replies = [reply(["Hay valet parking."])]
        p.verify = {"afirmaciones": [{"texto": "Hay valet parking.", "respaldo": None}]}
    elif case == "sverka_falla":
        p.chat_replies = [reply(["Es en [event_address]."])]

        async def chat(model, system, user, cost, **kw):
            if model == rag.CHECK_MODEL:
                raise RuntimeError("timeout")
            return p.chat_replies.pop(0)
        monkeypatch.setattr(rag, "_chat", chat)
    elif case == "openai_429":
        async def boom(*a, **k):
            raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(rag, "_chat", boom)
    elif case == "json_invalido":
        async def bad(*a, **k):
            raise json.JSONDecodeError("bad", "x", 0)
        monkeypatch.setattr(rag, "_chat", bad)
    elif case == "busqueda_falla":
        monkeypatch.setattr(knowledge, "search", AsyncMock(side_effect=RuntimeError("db")))
    elif case == "memoria_falla":
        monkeypatch.setattr(memory, "build", AsyncMock(side_effect=RuntimeError("db")))
    elif case == "tema_silencioso":
        text = "¿puedo pagar en efectivo?"
    return await rag.generate_reply(p.lead, [], text)


SILENT_CASES = ["falta_dato", "modelo_silent", "politica", "fuera_de_tema", "5500", "no_data",
                "sverka", "sverka_falla", "openai_429", "json_invalido", "busqueda_falla",
                "memoria_falla", "tema_silencioso"]


@pytest.mark.parametrize("case", SILENT_CASES)
async def test_every_silent_has_reason(monkeypatch, case):
    """Инвариант молчания (пункт 11): лиду ничего, причина для алерта Ане есть всегда."""
    r = await _silent_case(monkeypatch, case)
    assert r["action"] == "silent", case
    assert r["messages"] == [] and r.get("silent_reason"), case


class TestVerifierReview2:
    """Находки независимого ревью 2026-10-22: сверка по предложениям, fail-open сверщика."""

    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-21")

    async def _run(self, monkeypatch, items, bubbles, recs=None):
        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": items}
        monkeypatch.setattr(rag, "_chat", chat)
        return await rag.verify_claims(bubbles, recs or self.RECS, memory.evento_actual(SETTINGS),
                                       [], rag.Cost(), SETTINGS)

    def test_negation_compared_per_sentence(self):
        src = rag.fact_sources([{"contenido_es": "Incluye bebida y botanas. No necesitas disfraz."}],
                               None, SETTINGS)
        assert not rag.claim_supported("No incluye botanas", src)
        assert rag.claim_supported("Incluye bebida y botanas", src)

    def test_number_must_be_in_same_sentence(self):
        src = rag.fact_sources([{"contenido_es": "Cuesta [event_price_nonmember] MXN. Van 3000 personas."}],
                               None, SETTINGS)
        assert not rag.claim_supported("Cuesta 3000 MXN", src)

    @pytest.mark.parametrize("texto", ["Gracias, Luis. Puedes pagar en efectivo en OXXO.",
                                       "Incluye cena para 30 personas, ¿te late?"])
    async def test_thanks_or_question_tail_does_not_hide_claim(self, monkeypatch, texto):
        claims = await self._run(monkeypatch, [{"texto": texto, "respaldo": None}], [texto])
        assert claims

    @pytest.mark.parametrize("bubble", ["Puedes pagar en efectivo en OXXO.", "No incluye botanas.",
                                        "Hay valet parking.", "Van 40 mujeres y 40 hombres.",
                                        "La entrada es gratis.", "Incluye cena."])
    async def test_backstop_when_model_lists_nothing(self, monkeypatch, bubble):
        assert await self._run(monkeypatch, [], [bubble]) == [bubble]

    @pytest.mark.parametrize("bubble", ["Incluye bebida de bienvenida y botanas.",
                                        "Puedes pagar en la página del evento.",
                                        "El pago se hace en la página y el boleto te llega por correo.",
                                        "Trabajo con más de 3,000 mujeres eslavas.",
                                        "Solo trabajamos con hombres solteros.",
                                        "Ahora cuesta [event_price_nonmember] MXN.",
                                        "Gracias por tu foto, Carlos."])
    async def test_backstop_passes_real_facts(self, monkeypatch, bubble):
        assert await self._run(monkeypatch, [], [bubble]) == []


class TestPhotoQuestionReview2:
    @pytest.mark.parametrize("q", ["¿Me mandas una foto tuya?", "¿Puedes enviarme una foto?",
                                   "Para seguir, ¿que te mande una foto? No: ¿me mandas tu foto?",
                                   "¿Me pasas una foto tuya?", "¿Compárteme una foto tuya?",
                                   "¿Mándame una foto?", "¿Tienes una foto tuya?"])
    def test_photo_requests_counted(self, q):
        assert memory.pregunta_slot("photo", q)

    def test_event_photos_offer_not_counted(self):
        assert not memory.pregunta_slot("photo", "¿Quieres que te mande fotos del evento?")

    def test_photo_asked_before_current_event_not_counted(self):
        old = NOW - timedelta(days=60)
        prior = [dict(msg("anna", "¿Me mandas una foto tuya?", 0), created_at=old),
                 dict(msg("anna", "¿Me mandas una foto tuya?", 0), created_at=old)]
        ya = memory.ya_hecho(prior, {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["preguntas_calificacion"]["photo"] == 0
        lead = {"interest": "event", "name": "C", "is_single": True}
        assert not memory.calificacion(lead, ya, "hola", None)["foto_agotada"]


class TestNumbersReview2:
    def test_service_price_not_allowed_in_event_answer(self):
        _, reason, _ = check(["El servicio cuesta 10,000 USD."], fichas=[BY_ID["event_price"]])
        assert reason and "числа" in reason

    def test_lead_age_is_not_a_fact_source(self):
        _, reason, _ = check(["Van 30 mujeres."], fichas=[BY_ID["event_price"]],
                             lead={"name": "Carlos", "is_single": True, "age": 30, "photo_received": True})
        assert reason and "числа" in reason


class TestSummaryReview2:
    def _mem80(self):
        prior = [msg("lead" if i % 2 else "anna", f"t{i}", 1000 - i) for i in range(80)]
        return {"prior": prior, "resumen": None}

    async def test_write_failure_cached_not_recomputed(self, monkeypatch):
        rag._summary_cache.clear()
        calls = []

        async def chat(model, system, user, cost, **kw):
            calls.append(user)
            return {"resumen": "- Carlos, 38, médico"}
        monkeypatch.setattr(rag, "_chat", chat)
        monkeypatch.setattr(db, "update_memory_summary", AsyncMock(side_effect=RuntimeError("no col")))
        alert = AsyncMock()
        monkeypatch.setattr(ai.escalation, "notify_error", alert)
        for _ in range(3):
            lead = {"phone": PHONE}                      # из БД сводка не пришла
            await rag.maybe_refresh_summary(lead, self._mem80(), rag.Cost())
        assert len(calls) == 1 and alert.await_count == 1
        rag._summary_cache.clear()

    async def test_incremental_uses_previous_summary(self, monkeypatch):
        seen = {}

        async def chat(model, system, user, cost, **kw):
            seen.update(user)
            return {"resumen": "- nuevo"}
        monkeypatch.setattr(rag, "_chat", chat)
        monkeypatch.setattr(db, "update_memory_summary", AsyncMock())
        lead = {"phone": PHONE, "memoria_resumen": "- Carlos, médico", "memoria_mensajes": 10}
        await rag.maybe_refresh_summary(lead, self._mem80(), rag.Cost())
        assert seen["resumen_anterior"] == "- Carlos, médico"
        assert len(seen["mensajes"]) == 30            # только новые: 40 старых − 10 учтённых

    def test_summary_prompt_excludes_bot_facts(self):
        assert "anna_bot" in rag.SUMMARY_SYSTEM and "No copies datos" in rag.SUMMARY_SYSTEM


class TestKnowledgeFallbackReview2:
    async def test_db_error_keeps_last_db_records_and_alerts(self, monkeypatch):
        knowledge.reset_cache()
        knowledge._last_alert["at"] = 0.0
        alert = AsyncMock()
        monkeypatch.setattr(ai.escalation, "notify_error", alert)
        db_recs = [{"id": "solo_db", "activo": True}]
        monkeypatch.setattr(knowledge, "_load_from_db", AsyncMock(return_value=db_recs))
        assert await knowledge.records() == db_recs
        knowledge._cache["at"] = -1e9                      # TTL истёк
        monkeypatch.setattr(knowledge, "_load_from_db", AsyncMock(side_effect=RuntimeError("timeout")))
        assert await knowledge.records() == db_recs        # не seed.json
        knowledge._cache["at"] = -1e9
        await knowledge.records()
        assert alert.await_count == 1                      # не чаще раза в 10 минут
        knowledge.reset_cache()

    async def test_db_error_without_cache_uses_seed_and_alerts(self, monkeypatch):
        knowledge.reset_cache()
        knowledge._last_alert["at"] = 0.0
        alert = AsyncMock()
        monkeypatch.setattr(ai.escalation, "notify_error", alert)
        monkeypatch.setattr(knowledge, "_load_from_db", AsyncMock(side_effect=RuntimeError("timeout")))
        recs = await knowledge.records()
        assert len(recs) > 50 and alert.await_count == 1
        knowledge.reset_cache()


class TestReReview:
    """Повторное ревью 2026-10-22: ложные молчания сверки и мелочи тона."""

    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-21")
    SRC = rag.fact_sources(RECS, memory.evento_actual(SETTINGS), SETTINGS)

    @pytest.mark.parametrize("claim", [
        "Si no tienes disfraz, te dan una máscara",
        "Se compra en línea en la página del evento",
    ])
    def test_natural_paraphrases_supported(self, claim):
        assert rag.claim_supported(claim, self.SRC), claim

    @pytest.mark.parametrize("claim,quote", [
        ("Puedes ir disfrazado a la fiesta de Halloween", "puedes venir disfrazado"),
        ("Aceptamos hombres solteros", "Solo trabajamos con hombres solteros."),
    ])
    def test_paraphrases_with_model_quote_supported(self, claim, quote):
        """Пересказ другими словами проходит с цитатой сверщика (как в реальном потоке)."""
        assert rag.claim_supported(claim, self.SRC, quote), claim

    @pytest.mark.parametrize("sent", [
        "El boleto cuesta [event_price_nonmember] MXN y se paga en la página del evento.",
        "Puedes pagar en la página del evento.",
    ])
    def test_compound_hard_sentence_supported(self, sent):
        assert rag.hard_fact_supported(sent, self.SRC), sent

    @pytest.mark.parametrize("sent", ["Puedes pagar en efectivo en OXXO.", "No incluye botanas.",
                                      "Hay valet parking.", "Incluye cena y barra libre."])
    def test_hard_inventions_still_rejected(self, sent):
        assert not rag.hard_fact_supported(sent, self.SRC), sent

    def test_partial_answer_reask_allowed(self):
        last = "¿Cómo te llamas y estás soltero?"
        assert rag.drop_repeated_slot_question(["Mucho gusto, Luis.", "¿Estás soltero?"], last,
                                               {"name"}) == ["Mucho gusto, Luis.", "¿Estás soltero?"]

    def test_never_empties_reply(self):
        assert rag.drop_repeated_slot_question(["¿Cuántos años tienes?"], "¿Cuántos años tienes?") == \
            ["¿Cuántos años tienes?"]

    @pytest.mark.parametrize("q", ["¿Quieres que te mande fotos y videos de eventos pasados?",
                                   "¿Te mando fotos del lugar?"])
    def test_event_media_offers_not_photo_request(self, q):
        assert not memory.pregunta_slot("photo", q)

    def test_lead_age_echo_allowed_as_years(self):
        lead = {"name": "Carlos", "is_single": True, "age": 45, "photo_received": True}
        _, reason, _ = check(["Con 45 años estás dentro del rango."], fichas=[BY_ID["event_price"]], lead=lead)
        assert reason is None

    def test_whole_price_keeps_tail(self):
        out = rag.whole_price(["Cuesta [event_price_nonmember] MXN y se paga en la página."], SETTINGS)
        assert out == ["Cuesta [event_price_nonmember] MXN[event_promo] y se paga en la página."]


class TestLiveRun2Fixes:
    """Точные тексты из живого прогона 2026-10-22: ложные молчания и промахи тона."""

    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-21")

    async def _verify(self, monkeypatch, texto, quote=None, name="Carlos"):
        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": [{"texto": texto, "respaldo": quote}]}
        monkeypatch.setattr(rag, "_chat", chat)
        return await rag.verify_claims([texto], self.RECS, memory.evento_actual(SETTINGS), [],
                                       rag.Cost(), SETTINGS, name)

    @pytest.mark.parametrize("texto,quote", [
        ("La última vez platicamos del evento Slavic-Latino Connect Night, que será el 18 de "
         "noviembre en Roma Norte.", None),
        ("No es obligatorio ir disfrazado, pero puedes hacerlo si quieres.", None),
        ("No es obligatorio ir disfrazado, Carlos.", None),
        ("No es obligatorio ir disfrazado, pero puedes hacerlo si quieres.",
         "puedes venir disfrazado y, si no tienes disfraz, los organizadores te dan una máscara"),
    ])
    async def test_false_silences_fixed(self, monkeypatch, texto, quote):
        assert await self._verify(monkeypatch, texto, quote) == []

    @pytest.mark.parametrize("texto", [
        "El evento empieza a las [event_start], pero no hay una hora fija de cierre: suele terminar "
        "cuando la mayoría ya se va.",
        "Van 40 mujeres eslavas solteras y 40 hombres.",
    ])
    async def test_correct_silences_kept(self, monkeypatch, texto):
        assert await self._verify(monkeypatch, texto)

    def test_no_puedo_darte_is_no_data(self):
        assert rag._NO_DATA_RE.search("No puedo darte el número exacto, pero este es nuestro evento número 30")

    def _mem_buy(self, last=None):
        lead = {"interest": "event", "name": "Carlos", "is_single": True}
        ya = memory.ya_hecho([], {}, SETTINGS, None, NOW)
        cal = memory.calificacion(lead, ya, "¿cómo compro el boleto?", last, compra=True)
        return _mem(calificacion=cal, ultimo_mensaje_anna=last), lead

    def test_buy_before_photo_answer_and_photo_without_why(self):
        mem, lead = self._mem_buy()
        out = rag.purchase_before_photo(
            ["Solo me falta una foto tuya para completar tu registro. ¿Me la mandas por aquí?"],
            mem, "¿cómo compro el boleto?")
        assert out == [rag.BUY_ONLINE_SENTENCE, "¿Me mandas una foto tuya?"]

    def test_buy_before_photo_adds_photo_request(self):
        mem, lead = self._mem_buy("¿Cuántos años tienes y a qué te dedicas?")
        out = rag.purchase_before_photo(["El pago se hace en la página y el boleto te llega por correo."],
                                        mem, "ok, ¿cómo compro el boleto?")
        assert out == ["El pago se hace en la página y el boleto te llega por correo.",
                       rag.PHOTO_REQUEST]

    def test_orphan_me_la_fixed(self):
        mem, _ = self._mem_buy()
        mem["calificacion"] = {**mem["calificacion"], "etapa": "venta"}
        out = rag.purchase_before_photo(["Gracias por compartirlo.", "¿Me la puedes mandar aquí?"],
                                        mem, "tengo 41, soy abogado")
        assert out == ["Gracias por compartirlo.", "¿Me mandas una foto tuya?"]

    def test_why_removed_in_question(self):
        mem, _ = self._mem_buy()
        out = rag.purchase_before_photo(["Solo me falta una foto tuya para continuar, ¿me la puedes mandar?"],
                                        mem, "¿y cómo se paga el boleto?")
        assert "continuar" not in " ".join(out) and "foto" in " ".join(out)


# ───────── логика разговора (решение владелицы 2026-10-22, тексты живого прогона 2) ─────────

GREETING_EVENT = BY_ID["greeting_event"]["contenido_es"].split("\n\n")
RUN2_BASE = ([msg("lead", "hola, vi el evento", 30)] + [msg("anna", b, 29) for b in GREETING_EVENT]
             + [msg("lead", "Me llamo Carlos y sí, estoy soltero", 25),
                msg("anna", "¡Qué bien, Carlos! ¿Tienes alguna duda sobre el evento?", 24)])
CARLOS = {"name": "Carlos", "is_single": True, "interest": "event", "funnel_stage": "qualifying"}


def revisar(bubbles, user_text, lead, prior, extracted=None, paso=None, desde=EVENT_DESDE):
    """Как _generate: память из переписки, потом rag.revisar (paso_extra, вопрос по слоту, проверки)."""
    ya = memory.ya_hecho(prior, lead, SETTINGS, desde, NOW)
    last = memory.ultima_respuesta(prior)
    mem = {"settings": SETTINGS, "ya_hecho": ya, "regreso": memory.regreso(prior, NOW),
           "calificacion": memory.calificacion(lead, ya, user_text, last),
           "ultimo_mensaje_anna": last, "lead_texts": [user_text], "historial": [], "resumen": None,
           "compra_pendiente": False}
    out = {"bubbles": bubbles, "extracted": extracted or {},
           "paso_extra": paso or {"tipo": None, "texto": ""}}
    merged = {**lead, **(extracted or {})}
    return rag.revisar(out, mem, lead, merged, user_text, list(BY_ID.values()))


class TestConversationLogic:
    def test_d1_slot_answer_then_next_slot_not_fact(self):
        """«me llamo Javier, sí soltero» → «¡Gracias, Javier! 😊 ¿Cuántos años…?», не «evento número 30»."""
        prior = [msg("lead", "hola, vi el evento", 5)] + [msg("anna", b, 4) for b in GREETING_EVENT]
        b, reason, notes = revisar(["¡Gracias, Javier! 😊", "Este es nuestro evento número 30."],
                                   "me llamo Javier, sí soltero", {"interest": "event"}, prior,
                                   extracted={"name": "Javier", "is_single": True})
        assert reason is None
        assert b == ["¡Gracias, Javier! 😊", "¿Cuántos años tienes y a qué te dedicas?"]

    def test_d3_off_topic_fact_removed(self):
        b, reason, _ = revisar(
            ["Jajaja, no lo vi, Carlos, pero seguro estuvo emocionante.",
             "Por cierto, el evento Slavic-Latino Connect Night es el 18 de noviembre en Roma Norte. "
             "¿Cuántos años tienes y a qué te dedicas?"],
            "¿viste el partido del América ayer?", CARLOS, RUN2_BASE)
        assert reason is None
        assert b == ["Jajaja, no lo vi, Carlos, pero seguro estuvo emocionante.",
                     "¿Cuántos años tienes y a qué te dedicas?"]
        assert not any("Por cierto" in x for x in b)

    def test_d3_second_off_topic_networking_removed(self):
        prior = RUN2_BASE + [msg("lead", "¿viste el partido del América ayer?", 10),
                             msg("anna", "Jajaja, no lo vi, Carlos, pero seguro estuvo emocionante.", 9),
                             msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 9)]
        b, reason, _ = revisar(
            ["La verdad no le voy a ningún equipo, pero siempre disfruto el ambiente cuando hay partido.",
             "Por cierto, en el evento también suele haber muy buen ambiente y hasta se hacen amigos "
             "y contactos de negocio."],
            "jaja sí, estuvo buenísimo, ¿tú le vas a alguien?", CARLOS, prior)
        assert reason is None
        assert b == ["La verdad no le voy a ningún equipo, pero siempre disfruto el ambiente cuando hay partido."]

    def test_d7_diego_gave_data_keeps_thanks_drops_fact(self):
        """Диего через 3 дня назвал имя и «soltero» — это новые данные: «Gracias» уместно;
        рассказ про ивент без вопроса убран, следующий — вопрос по слоту."""
        prior = [msg("lead", "hola, vi el evento", 3 * 1440 + 40)] + \
            [msg("anna", b, 3 * 1440 + 39) for b in GREETING_EVENT]
        b, reason, _ = revisar(
            ["¡Gracias, Diego! Qué gusto tenerte por aquí.",
             "El evento Slavic-Latino Connect Night es el 18 de noviembre en Roma Norte, CDMX. Es temática "
             "de Halloween: puedes venir disfrazado y, si no tienes disfraz, los organizadores te dan una máscara."],
            "hola, perdón, me llamo Diego y sí estoy soltero", {"interest": "event"}, prior,
            extracted={"name": "Diego", "is_single": True}, desde=None)
        assert reason is None
        assert b == ["¡Gracias, Diego! Qué gusto tenerte por aquí.",
                     "¿Cuántos años tienes y a qué te dedicas?"]

    def test_d7_diego_returns_without_new_data_no_thanks(self):
        """Тот же Диего, но имя и «soltero» уже в профиле, а он просто вернулся — без «Gracias»."""
        lead = {"interest": "event", "name": "Diego", "is_single": True}
        prior = [msg("lead", "hola, vi el evento", 3 * 1440 + 40)] + \
            [msg("anna", b, 3 * 1440 + 39) for b in GREETING_EVENT]
        b, reason, notes = revisar(["¡Gracias, Diego! Qué gusto tenerte por aquí."],
                                   "hola, perdón, me llamo Diego y sí estoy soltero", lead, prior,
                                   extracted={"name": "Diego", "is_single": True}, desde=None)
        assert reason is None
        assert b[0] == "Qué gusto tenerte por aquí." and "убрала «Gracias» без повода" in notes
        assert not any("Gracias" in x for x in b)

    def test_name_without_thanks_is_fine(self):
        b, _, _ = revisar(["Diego, qué gusto tenerte por aquí."], "hola, ya volví",
                          {"interest": "event", "name": "Diego", "is_single": True}, RUN2_BASE)
        assert not any("Gracias" in x for x in b)

    def test_lead_thanks_allows_thanks(self):
        assert rag.lead_dio_algo("ok, muchas gracias", {})
        assert not rag.lead_dio_algo("hola", {"name": "Diego"}, {"name": "Diego"})
        assert rag.lead_dio_algo("soy Diego", {"name": "Diego"}, {"name": None})

    def test_d16_unasked_networking_removed(self):
        prior = RUN2_BASE + [msg("anna", "Es en Roma Norte, CDMX.", 10),
                             msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 10)]
        b, reason, _ = revisar(
            ["Incluye bebida de bienvenida y botanas.",
             "También es un buen espacio para conocer gente: además de pareja, muchos hacen amigos y "
             "contactos de negocio."], "¿qué incluye?", CARLOS, prior)
        assert reason is None and b == ["Incluye bebida de bienvenida y botanas."]

    @pytest.mark.parametrize("user_text,bubbles", [
        ("hola, ¿cuándo es el evento?",
         ["Ese evento ya pasó. El próximo es el [event_date], empieza a las [event_start] en [event_address]."]),
        ("¿hay que ir disfrazado?",
         ["No es obligatorio, Carlos. Puedes venir disfrazado si quieres, y si no tienes disfraz, "
          "los organizadores te dan una máscara."]),
        ("¿a qué hora empieza?", ["Empieza a las [event_start] el [event_date], en [event_address]."]),
        ("¿dónde es exactamente?", ["Es en [event_address]."]),
    ])
    def test_asked_fact_kept(self, user_text, bubbles):
        b, reason, _ = revisar(bubbles, user_text, CARLOS, RUN2_BASE)
        assert reason is None and b[0].startswith(bubbles[0][:12])
        assert memory.hechos_en(b[0], SETTINGS)

    def test_said_fact_not_repeated_on_broad_question(self):
        """«¿de qué se trata?» — рассказать можно, но Хеллоуин уже звучал в приветствии."""
        b, reason, _ = revisar(
            ["Es una noche para conocer en persona a mujeres eslavas solteras que buscan una relación "
             "seria. Esta vez es temática de Halloween."], "¿de qué se trata el evento?", CARLOS, RUN2_BASE)
        assert reason is None
        assert b == ["Es una noche para conocer en persona a mujeres eslavas solteras que buscan una "
                     "relación seria."]

    def test_said_fact_repeated_when_asked_again(self):
        b, _, _ = revisar(["Es temática de Halloween: puedes venir disfrazado."],
                          "¿otra vez, es de Halloween?", CARLOS, RUN2_BASE)
        assert "Halloween" in b[0]

    def test_paso_extra_fact_ignored(self):
        b, _, notes = revisar(["Incluye bebida de bienvenida y botanas."], "¿qué incluye?", CARLOS,
                              RUN2_BASE, paso={"tipo": None, "texto": "Este es nuestro evento número 30."})
        assert b == ["Incluye bebida de bienvenida y botanas."]
        assert "paso_extra не вопрос по слоту — убран" in notes

    def test_paso_extra_non_slot_question_ignored(self):
        b, _, _ = revisar(["Incluye bebida de bienvenida y botanas."], "¿qué incluye?", CARLOS,
                          RUN2_BASE, paso={"tipo": "pregunta", "texto": "¿Te gustaría ir?"})
        assert b == ["Incluye bebida de bienvenida y botanas."]

    def test_paso_extra_slot_question_added(self):
        b, _, _ = revisar(["Incluye bebida de bienvenida y botanas."], "¿qué incluye?", CARLOS,
                          RUN2_BASE, paso={"tipo": "pregunta",
                                           "texto": "¿Cuántos años tienes y a qué te dedicas?"})
        assert b == ["Incluye bebida de bienvenida y botanas.", "¿Cuántos años tienes y a qué te dedicas?"]

    def test_paso_extra_not_after_ignored_question(self):
        prior = RUN2_BASE + [msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 10)]
        b, _, _ = revisar(["Incluye bebida de bienvenida y botanas."], "¿qué incluye?", CARLOS,
                          prior, paso={"tipo": "pregunta",
                                       "texto": "¿Cuántos años tienes y a qué te dedicas?"})
        assert b == ["Incluye bebida de bienvenida y botanas."]

    def test_no_forced_question_when_lead_asked(self):
        b, _, _ = revisar(["Incluye bebida de bienvenida y botanas."], "¿qué incluye?", CARLOS, RUN2_BASE)
        assert b == ["Incluye bebida de bienvenida y botanas."]

    def test_no_forced_question_on_hesitation(self):
        b, _, _ = revisar(["Tómate tu tiempo, sin presión."], "lo voy a pensar", CARLOS, RUN2_BASE)
        assert b == ["Tómate tu tiempo, sin presión."]

    def test_partial_answer_asks_rest(self):
        prior = [msg("lead", "hola, quiero conocer mujeres rusas", 5),
                 msg("anna", "¿Cómo te llamas y estás soltero?", 4)]
        b, _, _ = revisar(["¡Gracias, Luis! 😊"], "Luis", {"interest": "agency"}, prior,
                          extracted={"name": "Luis"})
        assert b == ["¡Gracias, Luis! 😊", "¿Estás soltero?"]

    def test_after_age_photo_request(self):
        prior = RUN2_BASE + [msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 10)]
        b, _, _ = revisar(["Gracias por contarme."], "tengo 41, soy abogado", CARLOS, prior,
                          extracted={"age": 41, "profession": "abogado"})
        assert b == ["Gracias por contarme.", rag.PHOTO_REQUEST]

    def test_one_extra_step_only(self):
        """Факт и вопрос вдогонку: факт уходит, остаётся один вопрос."""
        b, _, _ = revisar(["¡Gracias, Carlos!", "Este es nuestro evento número 30.",
                           "¿Cuántos años tienes y a qué te dedicas?"],
                          "sí, estoy soltero", {"interest": "event", "name": "Carlos"}, RUN2_BASE[:4],
                          extracted={"is_single": True})
        assert b == ["¡Gracias, Carlos!", "¿Cuántos años tienes y a qué te dedicas?"]
        assert sum(x.count("?") for x in b) == 1

    def test_only_fact_kept_rather_than_silence(self):
        """Модель ответила только фактом не по теме вопроса — не молчим (ложное молчание хуже)."""
        b, reason, notes = revisar(["Es en [event_address]."], "¿cuánto cuesta?", CARLOS, RUN2_BASE)
        assert reason is None and b == ["Es en [event_address]."]
        assert "факт без вопроса лида оставлен: без него ответ пустой" in notes

    def test_run3_d3_dangling_lead_in_removed(self):
        """Прогон 3, Д3: модель оставила «Volviendo al evento,» в bubbles, вопрос — в paso_extra."""
        b, reason, _ = revisar(["Jaja, no lo vi, pero seguro estuvo emocionante. Volviendo al evento,"],
                               "¿viste el partido del América ayer?", CARLOS, RUN2_BASE,
                               paso={"tipo": "pregunta", "texto": "¿Cuántos años tienes y a qué te dedicas?"})
        assert reason is None
        assert b == ["Jaja, no lo vi, pero seguro estuvo emocionante.",
                     "¿Cuántos años tienes y a qué te dedicas?"]

    def test_run3_d3_offer_more_removed(self):
        prior = RUN2_BASE + [msg("lead", "¿viste el partido del América ayer?", 10),
                             msg("anna", "Jaja, no lo vi, pero seguro estuvo emocionante.", 9),
                             msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 9)]
        b, reason, _ = revisar(
            ["La verdad no le voy a ningún equipo, pero sí disfruto ver partidos de vez en cuando.",
             "Volviendo al evento, ¿te gustaría saber algo más sobre la Slavic-Latino Connect Night?"],
            "jaja sí, estuvo buenísimo, ¿tú le vas a alguien?", CARLOS, prior)
        assert reason is None
        assert b == ["La verdad no le voy a ningún equipo, pero sí disfruto ver partidos de vez en cuando."]


# ───────── ход после одобренного фото (решение владелицы 2026-10-22, Д1 и Д11 прогона 3) ─────────

D1_HISTORY = ([msg("lead", "hola, vi el evento", 40)] + [msg("anna", b, 39) for b in GREETING_EVENT]
              + [msg("lead", "me llamo Javier, sí soltero", 35),
                 msg("anna", "¡Gracias, Javier! 😊", 34),
                 msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 34),
                 msg("lead", "¿qué incluye?", 30),
                 msg("anna", "Incluye bebida de bienvenida y botanas.", 29),
                 msg("lead", "ok, ¿cómo compro el boleto?", 25),
                 msg("anna", "El pago se hace en la página y el boleto te llega por correo.", 24),
                 msg("anna", "¿Podrías mandarme una foto tuya, por favor?", 24)])
JAVIER = {"name": "Javier", "is_single": True, "interest": "event", "photo_received": True}


@pytest.mark.asyncio
class TestVentaTrasFoto:
    async def test_d11_photo_goes_straight_to_purchase(self, monkeypatch):
        """Д11: было «¡Gracias por tu foto, Carlos! 😊 ¿Cuántos años…?», ссылка только после
        «¿cómo compro?». Теперь сразу: спасибо, рассказ без уже сказанного, ссылка, медиа, вопрос."""
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra={**CARLOS, "photo_received": True})
        r = await rag.generate_reply(p.lead, RUN2_BASE, "[фото одобрено]")
        assert p.model_calls() == []
        assert r["action"] == "respond"
        # сводка одной фразой; Хеллоуин уже был в приветствии — его не добавляем
        assert r["messages"] == ["¡Gracias por tu foto, Carlos! 😊",
                                 "Te espero el [event_date] a las [event_start] en [event_address], con bebida de bienvenida y botanas.",
                                 "Aquí puedes apartar tu lugar: [event_link]"]
        assert not any("?" in m for m in r["messages"])
        assert r["send_event_video"] and r["send_event_photo"]
        assert r["after_media"] == ["¿Cuántos años tienes y a qué te dedicas?"]
        assert r["rag_meta"]["fuentes"] == ["venta_tras_foto"]

    async def test_d1_photo_after_purchase_question(self, monkeypatch):
        p = Pipe(monkeypatch, history=D1_HISTORY, lead_extra=JAVIER)
        r = await rag.generate_reply(p.lead, D1_HISTORY, "[фото одобрено]")
        assert p.model_calls() == []
        # прогон 4: было только «Empieza a las 8:30 PM.» — теперь сводка целиком, без повтора
        # Хеллоуина (он был в приветствии), хотя дата, место и «что входит» уже звучали
        assert r["messages"] == ["¡Gracias por tu foto, Javier! 😊",
                                 "Te espero el [event_date] a las [event_start] en [event_address], con bebida de bienvenida y botanas.",
                                 "Aquí puedes apartar tu lugar: [event_link]"]
        assert r["after_media"] == ["¿Cuántos años tienes y a qué te dedicas?"]

    async def test_full_story_when_nothing_said(self, monkeypatch):
        history = [msg("lead", "Carlos, soltero", 20), msg("anna", "¿Me mandas una foto tuya?", 19)]
        p = Pipe(monkeypatch, history=history, lead_extra={**CARLOS, "photo_received": True})
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        assert r["messages"][1] == ("Te espero el [event_date] a las [event_start] en [event_address], con bebida de bienvenida y botanas." + " "
                                    + BY_ID["event_halloween"]["contenido_es"])
        assert "medianoche" not in " ".join(r["messages"])

    async def test_no_question_when_age_known(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE,
                 lead_extra={**CARLOS, "photo_received": True, "age": 40, "profession": "médico"})
        r = await rag.generate_reply(p.lead, RUN2_BASE, "[фото одобрено]")
        assert r["after_media"] == []

    async def test_no_question_when_ignored_last_time(self, monkeypatch):
        history = RUN2_BASE + [msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 5)]
        p = Pipe(monkeypatch, history=history, lead_extra={**CARLOS, "photo_received": True})
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        assert r["after_media"] == []

    async def test_media_not_repeated(self, monkeypatch):
        history = RUN2_BASE + [msg("sistema", db.media_marker("video", SETTINGS["event_date"]), 5),
                               msg("sistema", db.media_marker("image", SETTINGS["event_date"]), 5)]
        history = [dict(m, sender="anna", direction="outbound", meta={"source": "system"})
                   if m["sender"] == "sistema" else m for m in history]
        p = Pipe(monkeypatch, history=history, lead_extra={**CARLOS, "photo_received": True})
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        assert r["send_event_video"] is False and r["send_event_photo"] is False

    async def test_no_photos_after_objection(self, monkeypatch):
        history = RUN2_BASE + [msg("lead", "se me hace caro", 6), msg("anna", "Te entiendo.", 5)]
        p = Pipe(monkeypatch, history=history, lead_extra={**CARLOS, "photo_received": True})
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        assert r["send_event_photo"] is False and r["send_event_video"] is True

    async def test_link_already_sent_goes_to_model(self, monkeypatch):
        history = RUN2_BASE + [msg("anna", f"Puedes comprar tu boleto aquí: {LINK}", 5)]
        p = Pipe(monkeypatch, history=history, lead_extra={**CARLOS, "photo_received": True})
        p.chat_replies = [reply(["¡Gracias por tu foto, Carlos! 😊"])]
        r = await rag.generate_reply(p.lead, history, "[фото одобрено]")
        assert len(p.model_calls()) == 1
        assert not any(LINK in m or "[event_link]" in m for m in r["messages"])

    async def test_question_with_photo_goes_to_model(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra={**CARLOS, "photo_received": True})
        p.chat_replies = [reply(["Ahora cuesta [event_price_nonmember] MXN."])]
        await rag.generate_reply(p.lead, RUN2_BASE, "[фото одобрено] ¿cuánto cuesta?")
        assert len(p.model_calls()) == 1

    async def test_service_lead_not_affected(self, monkeypatch):
        lead = {"name": "Luis", "is_single": True, "interest": "agency", "photo_received": True}
        hist = [msg("lead", "Luis, soltero", 20), msg("anna", "¿Me mandas una foto tuya?", 19)]
        p = Pipe(monkeypatch, history=hist, lead_extra=lead)
        p.chat_replies = [reply(["¡Gracias por tu foto, Luis! 😊"])]
        r = await rag.generate_reply(p.lead, hist, "[фото одобрено]")
        assert len(p.model_calls()) == 1 and not r.get("after_media")

    async def test_without_single_goes_to_model(self, monkeypatch):
        lead = {"name": "Carlos", "interest": "event", "photo_received": True}
        hist = [msg("lead", "me llamo Carlos", 20), msg("anna", "¿Me mandas una foto tuya?", 19)]
        p = Pipe(monkeypatch, history=hist, lead_extra=lead)
        p.chat_replies = [reply(["¡Gracias por tu foto, Carlos! ¿Estás soltero?"])]
        await rag.generate_reply(p.lead, hist, "[фото одобрено]")
        assert len(p.model_calls()) == 1

    async def test_emoji_budget_with_captions(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra={**CARLOS, "photo_received": True})

        async def video(reply, scenario, lead):
            reply["video_caption"] = "x 🎥"

        async def photo(reply, scenario, lead):
            reply["photo_caption"] = "x 📸"
        monkeypatch.setattr(ai, "_maybe_announce_event_video", video)
        monkeypatch.setattr(ai, "_maybe_announce_event_photo", photo)
        r = await rag.generate_reply(p.lead, RUN2_BASE, "[фото одобрено]")
        assert r["video_caption"] == rag.VIDEO_CAPTION and r["photo_caption"] == "Así fue el evento pasado."
        total = rag.emoji_count(r["messages"] + r["after_media"] + [r["video_caption"], r["photo_caption"]])
        assert total <= 2


@pytest.mark.asyncio
async def test_main_sends_video_photos_then_question(monkeypatch):
    import main
    order = []

    async def video(phone, caption=None):
        order.append(("video", caption))

    async def photos(phone, caption=None):
        order.append(("photos", caption))

    async def send(phone, msgs, **kw):
        order.append(("text", tuple(msgs)))
        return len(msgs)
    monkeypatch.setattr(main.actions, "send_event_video", video)
    monkeypatch.setattr(main.actions, "send_event_photos", photos)
    monkeypatch.setattr(main.sender, "send", send)
    await main._send_rag_media_and_tail(PHONE, {
        "send_event_video": True, "send_event_photo": True, "video_caption": "v",
        "photo_caption": "Así fue el evento pasado.", "after_media": ["¿Cuántos años tienes?"]})
    assert order == [("video", "v"), ("photos", "Así fue el evento pasado."),
                     ("text", ("¿Cuántos años tienes?",))]


class TestSlotOrderRun4:
    def test_d3_photo_question_out_of_order_removed(self):
        """Прогон 4, Д3 ход 2: вопрос о возрасте проигнорирован, модель перескочила на фото."""
        prior = RUN2_BASE + [msg("lead", "¿viste el partido del América ayer?", 10),
                             msg("anna", "Jaja, no lo vi, pero seguro estuvo emocionante.", 9),
                             msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 9)]
        b, reason, notes = revisar(
            ["La verdad no le voy a ningún equipo, pero me divierte ver cómo se emocionan todos. "
             "Volviendo al evento, ¿te gustaría mandarme una foto tuya?"],
            "jaja sí, estuvo buenísimo, ¿tú le vas a alguien?", CARLOS, prior)
        assert reason is None
        assert b == ["La verdad no le voy a ningún equipo, pero me divierte ver cómo se emocionan todos."]
        assert "убрала вопрос по слоту не по порядку" in notes

    def test_next_slot_question_kept(self):
        b, _, _ = revisar(["Incluye bebida de bienvenida y botanas. ¿Cuántos años tienes y a qué te dedicas?"],
                          "¿qué incluye?", CARLOS, RUN2_BASE)
        assert b == ["Incluye bebida de bienvenida y botanas. ¿Cuántos años tienes y a qué te dedicas?"]

    def test_photo_question_when_age_known(self):
        lead = {**CARLOS, "age": 40, "profession": "médico"}
        b, _, _ = revisar(["Incluye bebida de bienvenida y botanas.", "¿Me mandas una foto tuya?"],
                          "¿qué incluye?", lead, RUN2_BASE)
        assert b[-1] == "¿Me mandas una foto tuya?"

    def test_buy_before_photo_still_asks_photo(self):
        """Покупка до фото: возраст пропускаем, просьба фото остаётся (compra)."""
        mem_prior = RUN2_BASE
        ya = memory.ya_hecho(mem_prior, CARLOS, SETTINGS, EVENT_DESDE, NOW)
        last = memory.ultima_respuesta(mem_prior)
        mem = {"settings": SETTINGS, "ya_hecho": ya, "regreso": None,
               "calificacion": memory.calificacion(CARLOS, ya, "¿cómo compro?", last, compra=True),
               "ultimo_mensaje_anna": last, "lead_texts": ["¿cómo compro el boleto?"], "historial": [],
               "resumen": None, "compra_pendiente": True}
        out = {"bubbles": ["El pago se hace en la página.", "¿Me mandas una foto tuya?"], "extracted": {},
               "paso_extra": {"tipo": None, "texto": ""}}
        b, _, _ = rag.revisar(out, mem, CARLOS, CARLOS, "¿cómo compro el boleto?", list(BY_ID.values()))
        assert b[-1] == "¿Me mandas una foto tuya?"


def test_photo_summary_without_includes_record():
    by_id = {k: v for k, v in BY_ID.items() if k != "event_includes"}
    assert rag._story_after_photo(by_id, {"halloween"}) == \
        "Te espero el [event_date] a las [event_start] en [event_address]."


@pytest.mark.asyncio
class TestVerifierSmallTalkRun5:
    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-21")

    async def _verify(self, monkeypatch, texto):
        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": [{"texto": texto, "respaldo": None}]}
        monkeypatch.setattr(rag, "_chat", chat)
        return await rag.verify_claims([texto], self.RECS, memory.evento_actual(SETTINGS), [],
                                       rag.Cost(), SETTINGS, "Carlos")

    async def test_d3_personal_comment_not_a_claim(self, monkeypatch):
        """Прогон 5, Д3 ход 2: ложное молчание на реплике про футбол."""
        assert await self._verify(
            monkeypatch, "La verdad no le voy a ningún equipo, pero disfruto ver los partidos importantes.") == []

    @pytest.mark.parametrize("texto", [
        "Van 40 mujeres y 40 hombres.", "La entrada es gratis.", "Termina a la medianoche.",
        "Aquí nos enfocamos en ayudarte a conocer pareja.", "El evento tiene barra libre.",
        "Las chicas son todas modelos."])
    async def test_domain_claims_still_checked(self, monkeypatch, texto):
        assert await self._verify(monkeypatch, texto) == [texto]


class TestDateWithoutYear:
    def test_format(self):
        assert sender._fmt_date_es("2026-11-18") == "18 de noviembre"
        assert sender._fmt_date_es("18 de julio") == "18 de julio"

    @pytest.mark.asyncio
    async def test_greeting_and_photo_summary_rendered(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=SETTINGS))
        out = await sender.render_bubbles(
            GREETING_EVENT + [rag._story_after_photo(BY_ID, {"halloween"})], None, True)
        text = " ".join(out)
        assert "El 18 de noviembre tenemos Slavic-Latino Connect Night" in text
        assert "Te espero el 18 de noviembre a las 8:30 PM en Roma Norte, CDMX" in text
        assert "2026" not in text

    def test_year_typed_by_model_removed(self):
        b, reason, notes = check(["Es el 18 de noviembre de 2026, empieza a las [event_start]."],
                                 user_text="¿cuándo es?")
        assert reason is None and b == ["Es el 18 de noviembre, empieza a las [event_start]."]
        assert "убрала год из даты" in notes
        b, _, _ = check(["Ese evento ya pasó. El próximo es el 18 de noviembre del 2026."],
                        user_text="¿cuándo es el evento?")
        assert b == ["Ese evento ya pasó. El próximo es el 18 de noviembre."]


class TestRun6D3:
    def test_dangling_after_extra_question_removed(self):
        """Прогон 6, Д3 ход 1: вопрос в bubbles и в paso_extra — после удаления лишнего
        вопроса осталась подводка «Volviendo al evento,»."""
        b, reason, _ = revisar(
            ["Jaja, no lo vi, pero seguro estuvo emocionante. Volviendo al evento, "
             "¿cuántos años tienes y a qué te dedicas?"],
            "¿viste el partido del América ayer?", CARLOS, RUN2_BASE,
            paso={"tipo": "pregunta", "texto": "¿Cuántos años tienes y a qué te dedicas?"})
        assert reason is None
        assert b == ["Jaja, no lo vi, pero seguro estuvo emocionante.",
                     "¿Cuántos años tienes y a qué te dedicas?"]

    def test_requisito_explanation_removed(self):
        prior = RUN2_BASE + [msg("lead", "¿viste el partido del América ayer?", 10),
                             msg("anna", "Jaja, no lo vi, pero seguro estuvo emocionante.", 9),
                             msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 9)]
        b, reason, _ = revisar(
            ["La verdad no le voy a ningún equipo, pero me gusta ver los partidos.",
             "¿Me mandas una foto tuya? Es requisito para registrarte al evento."],
            "jaja sí, estuvo buenísimo, ¿tú le vas a alguien?", CARLOS, prior)
        assert reason is None
        assert b == ["La verdad no le voy a ningún equipo, pero me gusta ver los partidos."]

    def test_costume_fact_not_touched(self):
        b, _, _ = check(["Ir disfrazado no es obligatorio."], user_text="¿hay que ir disfrazado?")
        assert b == ["Ir disfrazado no es obligatorio."]


# ───────── прямой вопрос о покупке до фото (решение владелицы 2026-10-22) ─────────

D1_BEFORE_BUY = ([msg("lead", "hola, vi el evento", 40)] + [msg("anna", b, 39) for b in GREETING_EVENT]
                 + [msg("lead", "me llamo Javier, sí soltero", 35),
                    msg("anna", "¡Gracias, Javier! 😊", 34),
                    msg("anna", "¿Cuántos años tienes y a qué te dedicas?", 34),
                    msg("lead", "¿qué incluye?", 30),
                    msg("anna", "Incluye bebida de bienvenida y botanas.", 29)])


@pytest.mark.asyncio
class TestCompraDirecta:
    async def test_d1_buy_before_photo(self, monkeypatch):
        """Д1 «ok, ¿cómo compro el boleto?» до фото: было «El pago se hace… ¿Me mandas una foto
        tuya?» без ссылки. Теперь: ссылка, оплата на странице, видео; без фото и вопросов."""
        p = Pipe(monkeypatch, history=D1_BEFORE_BUY,
                 lead_extra={"name": "Javier", "is_single": True, "interest": "event"})

        async def video(reply, scenario, lead):
            reply["video_caption"] = "x"
        monkeypatch.setattr(ai, "_maybe_announce_event_video", video)
        r = await rag.generate_reply(p.lead, D1_BEFORE_BUY, "ok, ¿cómo compro el boleto?")
        assert p.model_calls() == []
        assert r["messages"] == ["Puedes comprar tu boleto aquí: [event_link]",
                                 "El pago se hace en la página y ahí mismo ves fotos de eventos "
                                 "pasados y todos los detalles."]
        assert r["send_event_video"] is True and r["video_caption"] == "Te dejo también un video del evento."
        assert r["send_event_photo"] is False and not r.get("after_media")
        assert not any("?" in m for m in r["messages"])

    async def test_no_name_asks_name_then_link(self, monkeypatch):
        """Лид без имени спрашивает, как купить: ответ по сути и «¿Cómo te llamas y estás
        soltero?», без ссылки; ответил — ссылка сразу."""
        hist = [msg("lead", "hola, vi el evento", 40)] + [msg("anna", b, 39) for b in GREETING_EVENT[:2]]
        p = Pipe(monkeypatch, history=hist, lead_extra={"interest": "event"})
        p.chat_replies = [reply(["Puedes comprar tu boleto aquí: [event_link]"])]
        r = await rag.generate_reply(p.lead, hist, "¿dónde compro el boleto?")
        assert r["messages"] == [rag.BUY_ONLINE_SENTENCE, "¿Cómo te llamas y estás soltero?"]
        assert not r["send_event_video"]

        hist2 = hist + [msg("lead", "¿dónde compro el boleto?", 5),
                        msg("anna", rag.BUY_ONLINE_SENTENCE, 4),
                        msg("anna", "¿Cómo te llamas y estás soltero?", 4)]
        p = Pipe(monkeypatch, history=hist2, lead_extra={"interest": "event"})
        p.chat_replies = [reply(["¡Gracias, Pedro!"], extracted={"name": "Pedro", "is_single": True})]
        r = await rag.generate_reply(p.lead, hist2, "Pedro, sí soltero")
        assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]
        assert r["extracted"] == {"name": "Pedro", "is_single": True}
        assert r["send_event_video"] is True

    async def test_repeat_buy_after_link_repeats_link_not_video(self, monkeypatch):
        hist = D1_BEFORE_BUY + [msg("lead", "ok, ¿cómo compro el boleto?", 10),
                                msg("anna", f"Puedes comprar tu boleto aquí: {LINK}", 9),
                                msg("anna", rag.BUY_PAGE, 9)]
        hist = hist + [dict(msg("anna", db.media_marker("video", SETTINGS["event_date"]), 8))]
        p = Pipe(monkeypatch, history=hist,
                 lead_extra={"name": "Javier", "is_single": True, "interest": "event"})
        r = await rag.generate_reply(p.lead, hist, "¿cómo compro?")
        assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]
        assert r["send_event_video"] is False

    async def test_cash_question_still_silent(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={"name": "Javier", "is_single": True, "interest": "event"})
        r = await rag.generate_reply(p.lead, [], "¿puedo pagar en efectivo en OXXO?")
        assert r["action"] == "silent"

    async def test_service_lead_not_affected(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={"name": "Luis", "is_single": True, "interest": "agency"})
        p.chat_replies = [reply(["La inversión es desde $10,000 USD."])]
        await rag.generate_reply(p.lead, [], "¿cómo pago el servicio?")
        assert len(p.model_calls()) == 1

    async def test_old_payment_question_does_not_trigger_link(self, monkeypatch):
        p = Pipe(monkeypatch, history=OLD_LIES, lead_extra={"name": "Carlos", "is_single": True,
                                                            "interest": "event"})
        p.chat_replies = [reply(["Incluye bebida de bienvenida y botanas."])]
        r = await rag.generate_reply(p.lead, OLD_LIES, "¿qué incluye?")
        assert not any("[event_link]" in m for m in r["messages"])


@pytest.mark.asyncio
async def test_run9_d12_como_se_paga_is_purchase(monkeypatch):
    """Прогон 9, Д12: «¿y cómo se paga el boleto, como me dijiste?» — прямой вопрос о покупке:
    ссылка и оплата на странице, а не «El pago se hace en la página…» без ссылки. OXXO из
    старой переписки не подтверждаем."""
    p = Pipe(monkeypatch, history=OLD_LIES, lead_extra={"name": "Carlos", "is_single": True,
                                                        "interest": "event"})
    r = await rag.generate_reply(p.lead, OLD_LIES, "¿y cómo se paga el boleto, como me dijiste?")
    assert p.model_calls() == []
    assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]
    assert "OXXO" not in " ".join(r["messages"])
    assert rag.wants_to_buy("¿cómo se paga?", None)


# ───────── автоприветствие WhatsApp в памяти режима rag ─────────

AUTO_GREETING_MSG = {"id": "ag", "sender": "anna", "direction": "outbound",
                     "text": "👉 ¡Bienvenido a Match Match Agency! ✨ Van 30 mujeres y 30 hombres. "
                             "Precio 9000 MXN, se paga en OXXO.",
                     "created_at": NOW - timedelta(minutes=1), "meta": {"source": "auto_greeting"}}


class TestAutoGreetingMemory:
    def test_not_anna_not_bot_and_marked(self):
        assert memory.autor(AUTO_GREETING_MSG) == "auto_saludo"
        ya = memory.ya_hecho([msg("lead", "Hola", 2), AUTO_GREETING_MSG], {}, SETTINGS, EVENT_DESDE, NOW)
        assert ya["auto_saludo"] and ya["presentacion"] is False and ya["hechos_dichos"] == []
        assert ya["ana_escribio_a_mano"] is None
        assert memory.ultima_respuesta([msg("lead", "Hola", 2), AUTO_GREETING_MSG]) is None

    def test_text_hidden_from_model(self):
        h = memory.historial([msg("lead", "Hola", 2), AUTO_GREETING_MSG])
        assert h[-1]["autor"] == "auto_saludo" and "OXXO" not in h[-1]["texto"]

    def test_not_a_fact_source(self):
        """Факты сверяются только с записями базы и карточкой — автоприветствия там нет."""
        sources = rag.fact_sources(knowledge.usable(SEED, SETTINGS, "2026-10-21"),
                                   memory.evento_actual(SETTINGS), SETTINGS)
        assert not any("OXXO" in s or "Bienvenido" in s for s in sources)


# ───────── факты ивента по автоответу владелицы (2026-10-22) ─────────

class TestOwnerAutoreplyFacts:
    def test_token_values(self):
        assert sender.event_token_value("event_weekday", SETTINGS) == "miércoles"
        assert sender.event_token_value("event_promo", SETTINGS) == " ~9,000 MXN~"
        assert sender.event_token_value("event_promo", {**SETTINGS, "event_price_old": ""}) == ""
        assert sender.event_token_value("event_date", SETTINGS) == "18 de noviembre"
        today = date(2026, 10, 22)
        assert sender.event_token_value("event_presale_until", {"event_presale_until": "2026-10-29"},
                                        today) == "29 de octubre"
        assert sender.event_token_value("event_presale_until", {"event_presale_until": "2026-10-20"},
                                        today) is None
        assert sender.event_token_value("event_presale_until", {}, today) is None

    def test_presale_record_only_with_future_date(self):
        ids = lambda st: {r["id"] for r in knowledge.usable(SEED, st, "2026-10-22")}
        assert "event_presale_until" not in ids(SETTINGS)
        assert "event_presale_until" in ids({**SETTINGS, "event_presale_until": "2026-10-29"})
        assert "event_presale_until" not in ids({**SETTINGS, "event_presale_until": "2026-10-21"})

    def test_card(self):
        card = memory.evento_actual(SETTINGS)
        assert card["lugar"] == "[event_address]" and card["dia"] == "[event_weekday]"
        assert card["precio"] == "[event_price_nonmember] MXN, precio de preventa"
        assert card["precio_tachado"] == "[event_promo]" and "siguiente_precio" not in card
        assert "preventa_hasta" not in card

    @pytest.mark.asyncio
    async def test_price_rendered_with_strikethrough_without_year(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=SETTINGS))
        out = await sender.render_bubbles(
            [BY_ID["event_price"]["contenido_es"],
             BY_ID["greeting_hola_evento_4p"]["contenido_es"].split("\n\n")[1]],
            None, True)
        assert out[0] == "El precio de preventa es de 6000 MXN ~9,000 MXN~."
        assert out[1].startswith("El miércoles 18 de noviembre tenemos Slavic-Latino Connect Night en "
                                 "Roma Norte, CDMX:")

    def test_attendees_answered_not_silent(self):
        b, reason, _ = check(["Van 30 mujeres eslavas solteras y 30 hombres solteros."],
                             user_text="¿cuántas personas van?", fichas=[BY_ID["event_attendees"]])
        assert reason is None and b == ["Van 30 mujeres eslavas solteras y 30 hombres solteros."]

    @pytest.mark.asyncio
    async def test_new_facts_verified(self, monkeypatch):
        recs = knowledge.usable(SEED, SETTINGS, "2026-10-22")

        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": []}
        monkeypatch.setattr(rag, "_chat", chat)
        for text in ("Van 30 mujeres eslavas solteras y 30 hombres solteros.",
                     "Las mujeres hablan español e inglés.",
                     "Todos van con la misma intención: conocer a alguien especial y construir una "
                     "relación seria."):
            assert await rag.verify_claims([text], recs, memory.evento_actual(SETTINGS), [],
                                           rag.Cost(), SETTINGS) == [], text

    def test_no_pressure_and_no_ladder_in_price(self):
        assert "sube" not in BY_ID["event_price"]["contenido_es"]
        assert not re.search(r"quedan\s+pocos", json.dumps(BY_ID["event_price"], ensure_ascii=False))


# ───────── приветствие на голое «hola» (решение владелицы 2026-10-22) ─────────

@pytest.mark.asyncio
class TestGreetingHola:
    async def test_bare_hola_sends_new_greeting_literal(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "Hola")
        assert p.model_calls() == []
        assert r["messages"] == S1_HALLOWEEN
        assert r["rag_meta"]["fuentes"] == ["greeting_hola"]

    async def test_hola_with_question_goes_to_model(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={})
        p.chat_replies = [reply(["Hola, soy Anna. El precio de preventa es de [event_price_nonmember] MXN."])]
        await rag.generate_reply(p.lead, [], "hola, ¿cuánto cuesta?")
        assert len(p.model_calls()) == 1

    async def test_after_auto_greeting_no_hola_script(self, monkeypatch):
        """В переписке уже автоприветствие WhatsApp: greeting_hola не шлём, отвечаем и
        переходим к квалификации."""
        hist = [msg("lead", "Hola", 2), AUTO_GREETING_MSG]
        p = Pipe(monkeypatch, history=hist, lead_extra={})
        r = await rag.generate_reply(p.lead, hist, "Hola")
        # готовый текст без модели (2026-10-22): «¡Hola! 🤍» и вопрос анкеты
        assert p.model_calls() == []
        assert r["messages"] == ["¡Hola! 🤍", "¿Cómo te llamas y estás soltero?"]



def test_greeting_hola_answer_el_evento_then_name_question():
    prior = [msg("lead", "Hola", 5)] + [msg("anna", b, 4) for b in
                                         BY_ID["greeting_hola"]["contenido_es"].split("\n\n")]
    b, reason, _ = revisar(["¡Qué bien!"], "el evento", {}, prior, extracted={"interest": "event"})
    assert reason is None and b[-1] == "¿Cómo te llamas y estás soltero?"


# ───────── независимое ревью 2026-10-22: числа в утверждениях ─────────

@pytest.mark.asyncio
class TestReviewNumbers:
    RECS = knowledge.usable(SEED, SETTINGS, "2026-10-22")

    async def _verify(self, monkeypatch, texto, respaldo=None):
        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": [{"texto": texto, "respaldo": respaldo}]}
        monkeypatch.setattr(rag, "_chat", chat)
        return await rag.verify_claims([texto], self.RECS, memory.evento_actual(SETTINGS), [],
                                       rag.Cost(), SETTINGS)

    @pytest.mark.parametrize("texto,respaldo", [
        # 4.1: склейка «evento número 30» + «3,000 mujeres» и запись услуги «15 mujeres»
        ("Van 15 mujeres al evento.", None),
        ("Van 15 mujeres al evento.", "te presento a 15 mujeres eslavas"),
        ("Van 40 mujeres.", None),
        ("Al evento van 3,000 mujeres.", "Trabajo con más de 3,000 mujeres eslavas."),
        # 4.6: числа словами
        ("Van unas cuarenta mujeres.", None),
        ("Habrá cien personas en el evento.", None),
    ])
    async def test_invented_counts_rejected(self, monkeypatch, texto, respaldo):
        assert await self._verify(monkeypatch, texto, respaldo) == [texto]

    async def test_attendees_only_from_event_attendees(self, monkeypatch):
        text = "Van 30 mujeres eslavas solteras y 30 hombres solteros."
        assert await self._verify(monkeypatch, text) == []
        no_att = [r for r in self.RECS if r["id"] not in ("event_attendees",)]

        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": [{"texto": text, "respaldo": None}]}
        monkeypatch.setattr(rag, "_chat", chat)
        assert await rag.verify_claims([text], no_att, memory.evento_actual(SETTINGS), [], rag.Cost(),
                                       SETTINGS) == [text]

    async def test_backstop_without_model_list(self, monkeypatch):
        """Страховка кодом: «N mujeres» не откатывается в склейку claim_supported."""
        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": []}
        monkeypatch.setattr(rag, "_chat", chat)
        claims = await rag.verify_claims(["Van 15 mujeres al evento."], self.RECS,
                                         memory.evento_actual(SETTINGS), [], rag.Cost(), SETTINGS)
        assert claims == ["Van 15 mujeres al evento."]


def test_word_numbers_and_alcohol():
    assert rag.numbers_in("Van unas cuarenta mujeres") == {"40"}
    assert rag.numbers_in("Docenas de personas ya encontraron pareja") == {"docenas"}
    assert rag.numbers_in("Hay dos formas de conocer gente") == set()
    assert rag.num_pairs("Van 30 mujeres eslavas") == {("30", "muje")}
    assert any(rx.search("Hay alcohol") for rx in rag._HARD_FACT_RES)
    assert any(rx.search("Hay alcohól") for rx in rag._HARD_FACT_RES)
    _, reason, _ = check(["Van unas cuarenta mujeres."], user_text="¿cuántas van?")
    assert reason and "числа" in reason


# ───────── ревью 2026-10-22: вопрос да/нет, фильтр тем сверщика ─────────

class TestYesNoAndDomain:
    @pytest.mark.parametrize("q", ["¿Aceptan tarjeta?", "¿Puedo pagar en la entrada?",
                                   "¿se paga en la puerta?", "¿tienen meses sin intereses?"])
    def test_new_silent_topics(self, q):
        assert rag._SILENT_TOPIC_RE.search(q), q

    def test_yes_no_claim_built(self):
        assert rag.yes_no_claim("¿Aceptan tarjeta?", ["¡Sí!"]) == "Aceptan tarjeta"
        assert rag.yes_no_claim("¿Hay estacionamiento?", ["Sí, hay."]) == "Hay estacionamiento"
        assert rag.yes_no_claim("¿Puedo pagar en la entrada?", ["Sí."]) == "Puedo pagar en la entrada"
        assert rag.yes_no_claim("¿Hay cena?", ["No."]) == "No Hay cena"
        assert rag.yes_no_claim("¿Cuánto cuesta?", ["Sí."]) is None          # не да/нет
        assert rag.yes_no_claim("¿Puedo ir solo?", ["¡Claro!"]) is None       # не тема фактов
        assert rag.yes_no_claim("¿Hay estacionamiento?",
                                ["Sí, hay estacionamiento gratuito en el lugar del evento."]) is None

    @pytest.mark.asyncio
    async def test_pipeline_short_yes_on_parking_silent(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
        p.chat_replies = [reply(["Sí, hay."])]
        p.verify = {"afirmaciones": []}
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿Hay estacionamiento?")
        assert r["action"] == "silent" and "вопрос да/нет" in r["silent_reason"]

    @pytest.mark.asyncio
    async def test_pipeline_card_in_tarjeta_question_silent_before_model(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿Aceptan tarjeta?")
        assert r["action"] == "silent" and p.model_calls() == []

    @pytest.mark.asyncio
    async def test_pipeline_confirmed_yes_passes(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
        p.chat_replies = [reply(["Sí."])]
        p.verify = {"afirmaciones": []}
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿Es en Roma Norte?")
        assert r["action"] == "respond"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("texto", ["No hay código de vestimenta, puedes ir como quieras.",
                                       "Sí, puedes ir en jeans."])
    async def test_dress_code_inventions_checked(self, monkeypatch, texto):
        async def chat(model, system, user, cost, **kw):
            return {"afirmaciones": [{"texto": texto, "respaldo": None}]}
        monkeypatch.setattr(rag, "_chat", chat)
        claims = await rag.verify_claims([texto], knowledge.usable(SEED, SETTINGS, "2026-10-22"),
                                         memory.evento_actual(SETTINGS), [], rag.Cost(), SETTINGS)
        assert claims

    @pytest.mark.asyncio
    async def test_personal_small_talk_still_skipped(self, monkeypatch):
        for texto in ("La verdad no le voy a ningún equipo, pero disfruto ver los partidos importantes.",
                      "Jaja, no lo vi, pero seguro estuvo emocionante."):
            async def chat(model, system, user, cost, **kw):
                return {"afirmaciones": [{"texto": texto, "respaldo": None}]}
            monkeypatch.setattr(rag, "_chat", chat)
            assert await rag.verify_claims([texto], knowledge.usable(SEED, SETTINGS, "2026-10-22"),
                                           memory.evento_actual(SETTINGS), [], rag.Cost(), SETTINGS) == []


@pytest.mark.parametrize("caption,ok", [
    ("", True), ("Carlos", True), ("soy Carlos, 35 años, ingeniero", True),
    ("¿cuánto cuesta?", False), ("voy con mi novia", False), ("¿cómo compro?", False),
    ("ya no me escribas", False)])
def test_photo_caption_only_self_data(caption, ok):
    assert rag.caption_only_self_data(caption) is ok


@pytest.mark.asyncio
async def test_photo_with_question_caption_goes_to_model(monkeypatch):
    p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra={**CARLOS, "photo_received": True})
    p.chat_replies = [reply(["¡Gracias por tu foto, Carlos! 😊", "Incluye bebida de bienvenida y botanas."])]
    await rag.generate_reply(p.lead, RUN2_BASE, "¿qué incluye?\n\n[фото одобрено]")
    assert len(p.model_calls()) == 1


# ───────── ревью 2026-10-22 (4.5): что не покупка ─────────

@pytest.mark.parametrize("text", ["Me gustaría ir pero está muy caro", "No puedo pagar ahorita",
                                  "ya me registré", "ya pagué", "lo voy a pensar, quiero ir",
                                  "no voy a comprar nada", "quiero ir, luego te aviso"])
def test_not_a_purchase(text):
    assert not rag.wants_to_buy(text, None), text


@pytest.mark.parametrize("text", ["¿cómo compro el boleto?", "quiero ir", "no sé cómo pagar",
                                  "pásame el link", "¿dónde compro?"])
def test_still_a_purchase(text):
    assert rag.wants_to_buy(text, None), text


@pytest.mark.asyncio
async def test_service_payment_without_interest_goes_to_model(monkeypatch):
    p = Pipe(monkeypatch, lead_extra={"name": "Luis", "is_single": True})
    p.chat_replies = [reply(["La inversión es desde $10,000 USD."])]
    r = await rag.generate_reply(p.lead, [], "¿Cómo se paga el servicio?")
    assert len(p.model_calls()) == 1 and not any("[event_link]" in m for m in r["messages"])


@pytest.mark.asyncio
async def test_purchase_plus_other_question_goes_to_model(monkeypatch):
    p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
    p.chat_replies = [reply(["Van 30 mujeres eslavas solteras y 30 hombres solteros."])]
    await rag.generate_reply(p.lead, RUN2_BASE, "¿Cómo pago? ¿Y cuántas mujeres van?")
    assert len(p.model_calls()) == 1


@pytest.mark.asyncio
async def test_objection_with_ir_not_link(monkeypatch):
    p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
    p.chat_replies = [reply(["Te entiendo, no es poco."])]
    r = await rag.generate_reply(p.lead, RUN2_BASE, "Me gustaría ir pero está muy caro")
    assert len(p.model_calls()) == 1 and not any("[event_link]" in m for m in r["messages"])


# ───────── ревью 2026-10-22: 4.11 купил по текущему ивенту, 4.12 токены, 4.13 лимит ─────────

class TestBoughtCurrentEvent:
    LEAD = {"funnel_stage": "event_attended"}

    def test_stage_from_past_event_is_not_bought(self):
        ev = [{"to_stage": "event_attended", "changed_at": EVENT_DESDE - timedelta(days=30), "meta": {}}]
        assert memory.boleto_del_evento_actual(self.LEAD, ev, SETTINGS, EVENT_DESDE) is False

    def test_bought_after_current_event_announced(self):
        ev = [{"to_stage": "event_attended", "changed_at": EVENT_DESDE + timedelta(hours=2), "meta": {}}]
        assert memory.boleto_del_evento_actual(self.LEAD, ev, SETTINGS, EVENT_DESDE) is True

    def test_meta_event_date_matches(self):
        ev = [{"to_stage": "event_attended", "changed_at": None, "meta": '{"event_date": "2026-11-18"}'}]
        assert memory.boleto_del_evento_actual(self.LEAD, ev, SETTINGS, None) is True

    def test_other_stage_never_bought(self):
        assert memory.boleto_del_evento_actual({"funnel_stage": "qualified"}, [], SETTINGS, EVENT_DESDE) is False

    @pytest.mark.asyncio
    async def test_past_attendee_can_buy_again(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra={**CARLOS, "funnel_stage": "event_attended"})
        monkeypatch.setattr(db, "get_funnel_events", AsyncMock(return_value=[
            {"to_stage": "event_attended", "changed_at": EVENT_DESDE - timedelta(days=40), "meta": {}}]))
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿cómo compro el boleto?")
        assert r["messages"] == [rag.BUY_LINK, rag.BUY_PAGE]


@pytest.mark.asyncio
class TestLiteralTokens:
    async def test_verbatim_scenario_with_unrendered_token_not_sent(self, monkeypatch):
        """Дословный сценарий из БД с токеном, которому нечего подставить, — ничего не шлём:
        молчание (main: ручной режим и Аня) и техалерт."""
        p = Pipe(monkeypatch, settings={**SETTINGS, "event_address": ""}, lead_extra=CARLOS)
        monkeypatch.setattr(db, "search_scenarios_by_vector", AsyncMock(return_value=[
            {"id": 40, "score": 0.99, "mode": "bot_auto", "blocks_lead": False,
             "template_es": "Jaja no, soy Anna 🤍\n\nNos vemos en [event_address]."}]))
        tech = AsyncMock()
        monkeypatch.setattr(rag, "_tech_alert", tech)
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿eres un bot?")
        assert r["action"] == "silent" and r["messages"] == [] and "токен" in r["silent_reason"]
        tech.assert_awaited()
        assert p.model_calls() == []

    async def test_literal_checked_passes_good_turn(self, monkeypatch):
        monkeypatch.setattr(db, "get_settings", AsyncMock(return_value=SETTINGS))
        res = rag._result([rag.BUY_LINK, rag.BUY_PAGE])
        assert await rag._literal_checked(res, CARLOS, "venta_por_compra", {}) is res

    async def test_empty_setting_drops_record_before_turn(self, monkeypatch):
        """Приветствие на «hola» (сценарий №1) токенов ивента не содержит: пустой адрес ему
        не мешает, уходит дословно без модели."""
        p = Pipe(monkeypatch, settings={**SETTINGS, "event_address": ""}, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "Hola")
        assert p.model_calls() == []
        assert r["messages"] == S1_HALLOWEEN


@pytest.mark.asyncio
class TestHardLimit:
    async def test_over_limit_no_openai_silent_alert_once(self, monkeypatch):
        p = Pipe(monkeypatch, settings={**SETTINGS, "rag_daily_hard_limit_usd": "15"}, lead_extra=CARLOS)
        monkeypatch.setattr(db, "get_daily_amount", AsyncMock(return_value=15.2))
        tech = AsyncMock()
        monkeypatch.setattr(rag, "_tech_alert", tech)
        rag._hard_limit_alerted["day"] = None
        r1 = await rag.generate_reply(p.lead, RUN2_BASE, "¿qué incluye?")
        r2 = await rag.generate_reply(p.lead, RUN2_BASE, "¿dónde es?")
        assert p.cost.await_count == 0          # расход не пишется: OpenAI не звали
        assert r1["action"] == r2["action"] == "silent" and "лимит" in r1["silent_reason"]
        assert p.chat_calls == []
        assert tech.await_count == 1

    async def test_under_limit_works(self, monkeypatch):
        p = Pipe(monkeypatch, settings=SETTINGS, lead_extra=CARLOS)
        monkeypatch.setattr(db, "get_daily_amount", AsyncMock(return_value=3.0))
        p.chat_replies = [reply(["Incluye bebida de bienvenida y botanas."])]
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿qué incluye?")
        assert r["action"] == "respond"



def test_scenarios_after_patch_have_no_5500_unico_end_time():
    """Ревью 2026-10-22 (4.8): по бэкапу всех прод-сценариев — после патча нет 5,500,
    «único en América Latina» и времени окончания; в промпте v5 нет 5,500."""
    from pathlib import Path
    from scripts import patch_rag_scenarios_2026_10 as p
    root = Path(__file__).resolve().parent.parent
    backup = root / "docs/backups/scenarios_all_before_rag_20261021T142906Z.json"
    if not backup.exists():
        pytest.skip("нет локального бэкапа сценариев (docs/backups не в git)")
    rows = {r["id"]: r["template_es"] for r in json.loads(backup.read_text("utf-8"))}
    # время окончания, как могло появиться в проде после бэкапа
    rows[51] = rows[51].replace("Comienza a las [event_start] (puedes venir más tarde)",
                                "Comienza a las [event_start] y termina alrededor de la medianoche. "
                                "Puedes venir más tarde")
    changes, errors = p.plan(rows, list(p.PATCHES) + p.optional_patches(rows))
    assert not errors
    after = {**rows, **changes}
    for sid, text in after.items():
        assert not re.search(r"5[.,]?500", text or ""), sid
    for sid in (2, 51):
        assert not re.search(r"[uú]nico\s+en|medianoche|\b1:00\b", after[sid]), sid
    assert "[event_promo]" in after[2] and "[event_promo]" in after[51]
    assert not re.search(r"5[.,]?500", (root / "anna_prompt_v5.md").read_text("utf-8"))


def test_import_knowledge_protects_manual_edits():
    """Ревью 2026-10-22 (4.9): без --force поля, правленные в мини-CRM, seed не перезаписывает."""
    from scripts import import_knowledge as ik
    sql = ik.build_sql()
    for c in ("activo", "confirmado", "contenido_es"):
        assert f"{c} = CASE WHEN COALESCE(knowledge.updated_by, '') = '' THEN EXCLUDED.{c} " \
               f"ELSE knowledge.{c} END" in sql
    assert "titulo = EXCLUDED.titulo" in sql
    forced = ik.build_sql(force=True)
    assert "CASE WHEN" not in forced and "contenido_es = EXCLUDED.contenido_es" in forced


@pytest.mark.asyncio
async def test_import_knowledge_backup(tmp_path, monkeypatch):
    from scripts import import_knowledge as ik
    monkeypatch.setattr(ik, "ROOT", tmp_path)
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[{"id": "event_price", "contenido_es": "x", "embedding": "[1]"}])
    path = await ik.backup_table(pool)
    data = json.loads(path.read_text("utf-8"))
    assert data == [{"id": "event_price", "contenido_es": "x"}] and "docs/backups" in str(path)


# ───────── живой прогон 2026-10-22 (59 ходов): дефекты и их исправления ─────────

class TestRun11Fixes:
    @pytest.mark.parametrize("given,expected", [
        # ходы 4 и 35: модель сама дописала «~9,000~» — была двойная зачёркнутая
        (["El precio de preventa es de [event_price_nonmember] MXN ~9,000~."],
         ["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."]),
        (["El precio de preventa es de 6000 MXN ~9,000 MXN~."],
         ["El precio de preventa es de 6000 MXN[event_promo]."]),
        # ход 56: «antes costaba 9,000 MXN» вручную — было ложное молчание на сверке
        (["El precio de preventa es de [event_price_nonmember] MXN[event_promo], antes costaba 9,000 MXN."],
         ["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."]),
    ])
    def test_price_strike_once(self, given, expected):
        assert rag.whole_price(given, SETTINGS) == expected

    @pytest.mark.asyncio
    async def test_run11_d19_price_not_silent(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
        p.chat_replies = [reply(["El precio de preventa es de [event_price_nonmember] MXN[event_promo], "
                                 "antes costaba 9,000 MXN."], fuentes=["event_price"])]
        p.verify = {"afirmaciones": [{"texto": "El precio de preventa es de 6000 MXN", "respaldo": None}]}
        monkeypatch.setattr(knowledge, "search", AsyncMock(return_value=[BY_ID["event_price"]]))
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿cuánto cuesta?")
        assert r["action"] == "respond"
        assert r["messages"] == ["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."]

    @pytest.mark.asyncio
    async def test_run11_end_time_evasion_silent(self, monkeypatch):
        """Ход 7: «¿a qué hora termina?» → «La hora de inicio es…» — уклонение, теперь Аня."""
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
        p.chat_replies = [reply(["La hora de inicio es a las [event_start], en [event_address]."])]
        r = await rag.generate_reply(p.lead, RUN2_BASE, "¿a qué hora termina?")
        assert r["action"] == "silent" and "окончания" in r["silent_reason"]

    def test_run11_photo_why_fragment_removed(self):
        """Ход 49: после вырезанного «para completar…» оставалось «Es.»"""
        b, _, _ = check(["Empieza a las [event_start], en [event_address].",
                         "Es para completar tu registro.",
                         "¿Podrías mandarme una foto tuya? Es para avanzar."],
                        user_text="¿a qué hora empieza?")
        assert not any(re.fullmatch(r"\W*Es\W*", x) or x.endswith(" Es.") for x in b)
        assert b[0] == "Empieza a las [event_start], en [event_address]."

    def test_run11_d3_dangling_without_period(self):
        """Ход 24: «Jajaja, no lo vi, volviendo al evento,» удалялось целиком."""
        assert rag.drop_dangling(["Jajaja, no lo vi, volviendo al evento,",
                                  "¿Cuántos años tienes y a qué te dedicas?"]) == \
            ["Jajaja, no lo vi.", "¿Cuántos años tienes y a qué te dedicas?"]

    def test_presale_end_question_not_end_time_silence(self):
        assert rag._END_TIME_Q_RE.search("¿cuándo termina la preventa?")
        assert rag._END_TIME_A_RE.search("Es por tiempo limitado, hasta el 29 de octubre.")



# ───────── 2026-10-22: «Hola» никогда не тишина, понятный алерт, алерты Арины только ей ─────────

@pytest.mark.asyncio
class TestBareGreetingNeverSilent:
    async def test_event_inactive_hola_literal(self, monkeypatch):
        """Прод 2026-10-22 10:07: event_active=0, на «Hola» модель сочинила описание агентства,
        проверка отбросила, лид остался без ответа. Теперь — готовое приветствие без ивента."""
        p = Pipe(monkeypatch, settings={**SETTINGS, "event_active": "0"}, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "Hola")
        assert p.model_calls() == []
        assert r["action"] == "respond"
        assert r["messages"] == S1                       # ивент выключен — без фразы про Хеллоуин
        assert "[" not in " ".join(r["messages"])

    async def test_event_active_hola_literal(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "buenos días")
        assert p.model_calls() == []
        assert r["messages"] == S1_HALLOWEEN

    async def test_hola_after_presentation_slot_question(self, monkeypatch):
        p = Pipe(monkeypatch, history=RUN2_BASE, lead_extra=CARLOS)
        r = await rag.generate_reply(p.lead, RUN2_BASE, "hola")
        assert p.model_calls() == []
        assert r["messages"] == ["¡Hola! 🤍", "¿Cuántos años tienes y a qué te dedicas?"]

    @pytest.mark.parametrize("text,bare", [("Hola", True), ("hola!!", True), ("Buenas tardes", True),
                                           ("hola, qué tal", True), ("hola, ¿cuánto cuesta?", False),
                                           ("hola, vi el evento", False), ("hola soy Pedro", False)])
    async def test_bare_greeting_detection(self, text, bare):
        assert rag.is_bare_greeting(text) is bare


def test_silent_alert_causes_cover_all_reasons():
    """Каждая причина молчания нового режима даёт понятную фразу; технических слов нет."""
    import pathlib
    src = pathlib.Path(rag.__file__).read_text("utf-8")
    reasons = re.findall(r'_silent\(f?"([^"{]+)', src)
    assert len(reasons) >= 15
    for r in reasons:
        phrase = rag.silent_alert("Режим rag: " + r)
        assert phrase and not re.search(r"rag|режим|стади|policy|fuentes", phrase, re.IGNORECASE), r
    assert rag.silent_alert("Режим rag: утверждения без источника: ['x']") == rag.UNVERIFIED
    assert rag.silent_alert("Режим rag: сбой OpenAI или ответа модели (RuntimeError)") == rag.FAILURE
    assert "скидку" in rag.silent_alert("Режим rag: скидка, способ оплаты или возврат (решение владелицы)")


def test_import_without_embeddings_keeps_vectors():
    from scripts import import_knowledge as ik
    sql = ik.build_sql(embeddings=False)
    assert "embedding" not in sql and "::vector" not in sql
    assert sql.count("$") == len(ik.COLS)
    assert "contenido_es = CASE WHEN" in sql


def test_httpx_does_not_log_token_urls():
    import logging
    import main  # noqa: F401
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING



# ───────── приветствие на «hola» = сценарий №1 (решение владелицы 2026-10-22) ─────────

def _scenario_1_backup():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    f = root / "docs/backups/scenarios_all_before_rag_20261022T040921Z.json"
    if not f.exists():
        pytest.skip("нет локального бэкапа сценариев (docs/backups не в git)")
    return {r["id"]: r for r in json.loads(f.read_text("utf-8"))}[1]["template_es"]


@pytest.mark.asyncio
class TestHolaScenario1:
    @pytest.mark.parametrize("text", ["hola", "Hola", "buenas", "buenos días", "hello", "hi",
                                      "привет", "Здравствуйте", "good morning"])
    async def test_bare_greeting_any_language_scenario_1(self, monkeypatch, text):
        p = Pipe(monkeypatch, lead_extra={})
        r = await rag.generate_reply(p.lead, [], text)
        assert p.model_calls() == []
        backup = _scenario_1_backup().split("\n\n")
        # посимвольно с бэкапом сценария №1; добавленная фраза — отдельно
        assert r["messages"][0] == backup[0] and r["messages"][2:] == backup[2:]
        assert r["messages"][1] == backup[1] + " " + rag.HALLOWEEN_PHRASE
        assert rag.HALLOWEEN_PHRASE == "Esta vez es temática de Halloween 🎃"
        assert "😉" in r["messages"][1]

    async def test_question_goes_to_model(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={})
        p.chat_replies = [reply(["El precio de preventa es de [event_price_nonmember] MXN[event_promo]."])]
        await rag.generate_reply(p.lead, [], "Hola, ¿cuánto cuesta?")
        assert len(p.model_calls()) == 1

    async def test_event_mention_greeting_event(self, monkeypatch):
        p = Pipe(monkeypatch, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "hola, vi lo del evento")
        assert p.model_calls() == []
        assert r["rag_meta"]["fuentes"] == ["greeting_event"]

    async def test_second_hola_in_a_row(self, monkeypatch):
        hist = [msg("lead", "hola", 5)] + [msg("anna", b, 4) for b in S1_HALLOWEEN]
        p = Pipe(monkeypatch, history=hist, lead_extra={})
        r = await rag.generate_reply(p.lead, hist, "hola")
        assert p.model_calls() == []
        assert r["messages"] == ["¡Hola! 🤍", "¿Cómo te llamas y estás soltero?"]

    async def test_after_whatsapp_auto_greeting(self, monkeypatch):
        hist = [msg("lead", "Hola", 2), AUTO_GREETING_MSG]
        p = Pipe(monkeypatch, history=hist, lead_extra={})
        r = await rag.generate_reply(p.lead, hist, "Hola")
        assert r["messages"] == ["¡Hola! 🤍", "¿Cómo te llamas y estás soltero?"]

    async def test_event_inactive_no_halloween(self, monkeypatch):
        p = Pipe(monkeypatch, settings={**SETTINGS, "event_active": "0"}, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "hola")
        assert r["messages"] == _scenario_1_backup().split("\n\n")
        assert "Halloween" not in " ".join(r["messages"])

    async def test_event_date_passed_no_halloween(self, monkeypatch):
        p = Pipe(monkeypatch, settings={**SETTINGS, "event_date": "2026-10-01"}, lead_extra={})
        r = await rag.generate_reply(p.lead, [], "hola")
        assert r["messages"] == _scenario_1_backup().split("\n\n")

    async def test_event_today_still_halloween(self, monkeypatch):
        monkeypatch.setattr(rag.knowledge, "today_cdmx", lambda now=None: "2026-11-18")
        assert rag.halloween_activo(SETTINGS)
        monkeypatch.setattr(rag.knowledge, "today_cdmx", lambda now=None: "2026-11-19")
        assert not rag.halloween_activo(SETTINGS)
