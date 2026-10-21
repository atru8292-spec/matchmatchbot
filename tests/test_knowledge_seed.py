"""Проверка knowledge/seed.json (база знаний режима rag): структура и правила владелицы.

Тексты, которые может увидеть лид или модель (contenido_es, bueno, frases_ejemplo,
como_responder), не содержат 5,500, сленга, запрещённых зачинов, длинного тире и больше
одного эмодзи. Поля malo (только для тестов) и ru (только для ревью) не проверяются на это.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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
          "[event_promo]", "[event_link]", "[course_link]", "[horario_llamada]"}
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
SLANG_RE = re.compile(r"\b(porfa|ahorita|checa|checar|jaja\w*|chance|te\s+late|padre|[óo]rale|"
                      r"oye|s[úu]per|guapo|perfil|embone)\b|¡va!|\bbase\s+de\b", re.IGNORECASE)
BANNED_START_RE = re.compile(r"^\W*(gracias\s+por\s+tu\s+inter[eé]s|excelente\s+pregunta|"
                             r"muy\s+buena\s+pregunta|qu[eé]\s+bueno\s+que|perfecto|claro|"
                             r"con\s+mucho\s+gusto)", re.IGNORECASE)


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
def test_no_slang_no_em_dash(r):
    for t in _lead_facing(r):
        assert not SLANG_RE.search(t), f"{r['id']}: {SLANG_RE.search(t).group(0)!r} в {t}"
        assert "—" not in t, f"{r['id']}: длинное тире"


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_no_banned_openers(r):
    for t in _lead_facing(r):
        for bubble in t.split("\n\n"):
            assert not BANNED_START_RE.search(bubble), f"{r['id']}: {bubble}"


@pytest.mark.parametrize("r", RECORDS, ids=lambda r: r["id"])
def test_only_known_tokens(r):
    for t in _model_facing(r):
        for tok in re.findall(r"\[[a-z_]+\]", t):
            assert tok in TOKENS, f"{r['id']}: неизвестный токен {tok}"


def test_link_only_in_purchase_records():
    """Ссылка на билет — только в записи «как купить» (и в паре-образце про покупку)."""
    with_link = {r["id"] for r in RECORDS if any("[event_link]" in t for t in _lead_facing(r))}
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
    assert unconfirmed == {"service_women_3000", "service_women_ages", "service_women_location",
                           "service_duration", "agency_events_28", "agency_clients_countries",
                           "agency_social_followers"}
    for r in RECORDS:
        if r["confirmado"]:
            for t in _lead_facing(r):
                assert not re.search(r"3,?000|28 eventos|seguidores|24 y 40", t), r["id"]


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
