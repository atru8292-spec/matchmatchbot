"""Проверка knowledge/seed.json (база знаний режима rag): структура и правила владелицы.

Тексты, которые может увидеть лид или модель (contenido_es, bueno, frases_ejemplo,
como_responder), не содержат 5,500, сленга, запрещённых зачинов и фраз (conversation_rules),
длинного тире и больше одного эмодзи. Поля malo (только для тестов) и ru (только для ревью)
не проверяются на это. Факты ивента сверены с сайтом 21.10.2026.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import conversation_rules

SEED = json.loads((Path(__file__).resolve().parent.parent / "knowledge" / "seed.json")
                  .read_text(encoding="utf-8"))
RECORDS = SEED["records"]

FIELDS = ["id", "tipo", "titulo", "contenido_es", "cuando_usar", "como_responder", "notas_tono",
          "frases_ejemplo", "malo", "bueno", "por_que", "situacion", "literal", "accion",
          "escalar_despues", "escenario_literal", "confirmado", "evento_id", "valid_from",
          "valid_to", "activo", "fuente", "ru"]
TIPOS = {"event", "service", "tariff", "faq", "objection", "policy", "style", "style_pair",
         "base_script"}
TOKENS = {"[event_date]", "[event_start]", "[event_address]", "[event_price_nonmember]",
          "[event_price_next]", "[event_link]", "[course_link]", "[horario_llamada]"}
# Любая ссылка: токен билета, http(s), www., домен .com/.mx (code-review: www.… проходил мимо).
LINK_RE = re.compile(r"\[event_link\]|https?://|\bwww\.|\b[\w-]+\.(?:com|mx)\b", re.IGNORECASE)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
def _lead_facing(r: dict) -> list[str]:
    """Тексты, которые могут уйти лиду как есть. Записи tipo=style — инструкции модели со
    списками запрещённого (сами запрещённые слова там по назначению), их не считаем."""
    if r["tipo"] == "style":
        return []
    out = [r["contenido_es"] or "", r["bueno"] or ""] + list(r["frases_ejemplo"] or [])
    return [t for t in out if t]


def _model_facing(r: dict) -> list[str]:
    return _lead_facing(r) + [r["como_responder"] or "", r["notas_tono"] or "",
                              r["cuando_usar"] or ""]


def _instructions(r: dict) -> list[str]:
    """Инструкции модели (не слова лида): здесь не должно быть списков запрещённого."""
    out = [r["titulo"] or "", r["como_responder"] or "", r["notas_tono"] or "",
           r["por_que"] or ""]
    if r["tipo"] == "style":
        out.append(r["contenido_es"] or "")
    return [t for t in out if t]


def test_structure():
    ids = [r["id"] for r in RECORDS]
    assert len(ids) == len(set(ids))
    for r in RECORDS:
        assert list(r) == FIELDS, r["id"]
        assert r["tipo"] in TIPOS, r["id"]
        assert r["accion"] in ("respond", "silent_escalate"), r["id"]
        assert r["ru"], f"{r['id']}: нет перевода для ревью"
        assert r["cuando_usar"], f"{r['id']}: нет cuando_usar (по нему эмбеддинг)"


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_never_5500(r):
    for t in _model_facing(r):
        assert not re.search(r"5[,.]?500", t), f"{r['id']}: {t}"


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_one_emoji_max(r):
    for t in _lead_facing(r):
        assert len(EMOJI_RE.findall(t)) <= 1, f"{r['id']}: {t}"


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_lead_texts_clean(r):
    """Зачины, фразы, сленг (conversation_rules) и длинное тире — нигде в текстах для лида."""
    for t in _lead_facing(r):
        assert conversation_rules.violations(t) == [], f"{r['id']}: {t}"
        assert "—" not in t, f"{r['id']}: длинное тире"


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_no_banned_lists_for_model(r):
    """Решение владелицы: списки запрещённого не идут модели (она копирует из них слова)."""
    for t in _instructions(r):
        for rx in (conversation_rules.BANNED_PHRASE_RE, conversation_rules.SLANG_RE):
            m = rx.search(t)
            assert not m, f"{r['id']}: {m.group(0)!r} в инструкции: {t}"
        for q in re.findall(r"«([^»]+)»", t):
            assert not conversation_rules.violations(q), f"{r['id']}: цитата запрещённого «{q}»"


def test_no_style_banned_openers_record():
    assert not any(r["id"] == "style_banned_openers" for r in RECORDS)


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_only_known_tokens(r):
    for t in _model_facing(r):
        for tok in re.findall(r"\[[a-z_]+\]", t):
            assert tok in TOKENS, f"{r['id']}: неизвестный токен {tok}"
        assert "[event_promo]" not in t, f"{r['id']}: зачёркнутая «было» запрещена"


def test_link_only_in_purchase_records():
    """Ссылка на билет — только в записи «как купить» (и в паре-образце про покупку)."""
    with_link = {r["id"] for r in RECORDS if any(LINK_RE.search(t) for t in _lead_facing(r))}
    assert with_link == {"event_how_to_buy", "pair_opener_perfecto", "pair_slang"}


def test_base_scripts():
    for r in (r for r in RECORDS if r["tipo"] == "base_script"):
        assert r["literal"] and r["situacion"], r["id"]
        assert len(r["contenido_es"].split("\n\n")) <= 4, r["id"]


def test_hola_script_is_owner_text_without_binary_question():
    r = next(r for r in RECORDS if r["id"] == "greeting_hola")
    t = r["contenido_es"]
    assert "Soy Anna, fundadora de Match Match Agency" in t
    assert "Cuéntame qué estás buscando" in t
    assert t.endswith("La idea no es que conozcas a muchas mujeres, sino que conozcas a las correctas.")
    assert not re.search(r"dos\s+(caminos|opciones)|¿servicio o evento", t, re.IGNORECASE)
    assert "soltero" not in t and "llamas" not in t   # имя и «холост ли» — следующим ответом
    assert r["situacion"]["presentacion_hecha"] is False


def test_style_pairs():
    pairs = [r for r in RECORDS if r["tipo"] == "style_pair"]
    assert len(pairs) >= 8
    for r in pairs:
        assert r["malo"] and r["bueno"] and r["por_que"], r["id"]


def test_silent_policies():
    silent = {r["id"] for r in RECORDS if r["accion"] == "silent_escalate"}
    assert {"policy_companion", "policy_discount", "policy_cash_transfer", "policy_refund",
            "policy_exact_address_repeat", "policy_unknown_fact"} <= silent
    for r in RECORDS:
        if r["accion"] == "silent_escalate":
            assert not r["contenido_es"], f"{r['id']}: лиду ничего не отправляется"


def test_event_records_expire_with_event():
    for r in RECORDS:
        if r["evento_id"]:
            assert r["evento_id"] == SEED["evento_actual"]
            assert r["valid_to"] == "2026-11-18", r["id"]


def test_unconfirmed_numbers_not_served():
    unconfirmed = {r["id"] for r in RECORDS if not r["confirmado"]}
    assert unconfirmed == {"event_attendees", "event_end_time", "event_unique",
                           "service_women_ages", "service_women_location", "service_duration",
                           "agency_clients_countries", "agency_social_followers",
                           "pair_attendees", "pair_end_time"}
    for r in RECORDS:
        if r["confirmado"]:
            for t in _lead_facing(r) + [r["como_responder"] or ""]:
                assert not re.search(r"30\s+(?:mujeres|hombres)|30\s+y\s+30|[uú]nico\s+en\s+Am[eé]rica|"
                                     r"medianoche|24\s+y\s+40|seguidores|6\s+meses|28\s+eventos",
                                     t), f"{r['id']}: {t}"


def test_site_facts_2026_10_21():
    """Сверено с сайтом rusaencdmx.com/18-11-26-slavic-latino-night-halloween."""
    by_id = {r["id"]: r for r in RECORDS}
    assert by_id["event_name"]["contenido_es"] == "El evento se llama Slavic-Latino Connect Night."
    assert "Slavic-Latino Connect Night" in by_id["greeting_event"]["contenido_es"]
    assert "30" not in by_id["greeting_event"]["contenido_es"]
    assert by_id["event_single_confirmation"]["contenido_es"] == \
        "Al comprar el boleto confirmas que estás soltero."
    assert by_id["event_number_30"]["contenido_es"] == "Este es nuestro evento número 30."
    assert by_id["event_couples_found"]["contenido_es"] == \
        "Docenas de personas ya han encontrado a su pareja en nuestros eventos."
    assert by_id["service_women_3000"]["confirmado"] is True
    assert "[event_price_next]" in by_id["event_price_steps"]["contenido_es"]
    for r in RECORDS:
        for t in _model_facing(r):
            assert "Slavic Latino Night" not in t and "entrantes" not in t, r["id"]
    for rid in ("event_price", "event_includes", "obj_event_price"):
        assert "botanas" in by_id[rid]["contenido_es"], rid


def test_objections_without_praise():
    by_id = {r["id"]: r for r in RECORDS}
    assert by_id["obj_service_price"]["contenido_es"] == \
        "Cada mujer la elijo a mano pensando en ti, y te acompaño en todo el proceso."
    assert "vale la pena" not in by_id["obj_price_increase"]["contenido_es"]
    assert by_id["obj_event_price"]["contenido_es"].startswith("Te entiendo, no es poco.")


def test_greeting_service_asks_directly():
    t = next(r for r in RECORDS if r["id"] == "greeting_service")["contenido_es"]
    assert t.split("\n\n")[-1] == "¿Cómo te llamas y estás soltero?"


def test_end_time_only_approximate():
    r = next(r for r in RECORDS if r["id"] == "event_end_time")
    assert r["contenido_es"] == ("Empieza a las [event_start] y termina alrededor de la "
                                 "medianoche.")
    for x in RECORDS:
        for t in _lead_facing(x):
            assert not re.search(r"12:00|1:00\s*a|\[event_end\]", t), x["id"]


def test_review_doc_is_current():
    """docs/proposals/knowledge_review_ru.md собран из текущего seed.json (не устарел)."""
    from scripts import knowledge_review
    doc = (Path(__file__).resolve().parent.parent / "docs" / "proposals" / "knowledge_review_ru.md")
    assert doc.read_text(encoding="utf-8") == knowledge_review.render(SEED), \
        "перегенерируй: venv/bin/python -m scripts.knowledge_review"
