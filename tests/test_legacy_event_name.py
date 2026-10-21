"""Старый путь (reply_mode=legacy): название ивента как на сайте — «Slavic-Latino Connect
Night» (решение владелицы 2026-10-21). Меняется только текст, логика не трогается."""
from __future__ import annotations

from pathlib import Path

import ai

ROOT = Path(__file__).resolve().parent.parent


def test_legacy_bubbles_use_site_name():
    assert "Slavic-Latino Connect Night" in ai._EVENT_GATE_OVERRIDE_BUBBLE
    assert "Slavic-Latino Connect Night" in ai._EVENT_OFFER_ON_SERVICE_OBJECTION_BUBBLE


def test_no_old_name_in_lead_facing_sources():
    for rel in ("ai.py", "anna_prompt_v5.md", "scripts/resume_lead_manual.py"):
        assert "Slavic Latino Night" not in (ROOT / rel).read_text(encoding="utf-8"), rel
