"""Промпт режима rag (anna_prompt_conversation.md): без списков запрещённого, без 5,500,
с форматом ответа, который разбирает ai_conversation._parse_model."""
from __future__ import annotations

import re
from pathlib import Path

import ai_conversation
import conversation_rules

PROMPT = (Path(__file__).resolve().parent.parent / "anna_prompt_conversation.md").read_text(encoding="utf-8")


def test_prompt_loads():
    ai_conversation._prompt_cache = None
    assert ai_conversation.load_prompt() == PROMPT


def test_no_banned_words_or_slang():
    """Модель копирует слова из списков «так нельзя» — их в промпте нет."""
    assert conversation_rules.violations(PROMPT) == []


def test_never_mentions_5500_or_old_name():
    assert not re.search(r"5[.,]?500", PROMPT)
    assert "Slavic Latino Night" not in PROMPT
    assert "[event_promo]" not in PROMPT


def test_size_budget():
    assert len(PROMPT) // 4 < 6500          # ≈ токенов; v5 был ≈ 17 000


def test_output_contract():
    for key in ("bubbles", "accion", "fuentes", "falta_dato", "query_extra", "en_tema",
                "extracted", "send_event_photo", "funnel_stage", "proposed_videocall_at"):
        assert f'"{key}"' in PROMPT, key
    for tok in ("[event_date]", "[event_start]", "[event_address]", "[event_price_nonmember]",
                "[event_price_next]", "[event_link]", "[course_link]", "[horario_llamada]"):
        assert tok in PROMPT, tok


def test_memory_and_tone_rules_present():
    for needle in ("anya_manual", "notas_internas", "pasado", "regreso", "puede_preguntar",
                   "hasta 2 por respuesta", "como máximo 1 por burbuja", "desde $10,000 USD"):
        assert needle in PROMPT, needle
