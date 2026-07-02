"""Leak-guard: шаблоны bot_then_anna не должны выдавать механику передачи к Ане.

Лид не должен заметить подмену — для него это всё та же Anna. Проверяем ОБРАЗЦЫ
(template_es / messages), на которые опирается AI. Детерминированно, без БД/сети.
Реальный AI-выход дополнительно проверяется в eval (scripts/run_eval.py).
"""
from __future__ import annotations

import json
import re

# Слова, которые НЕ должны попадать в текст, видимый лидом (утечка механики).
BANNED_WORDS = [
    "anna real", "equipo", "asesor", "asesora", "transferir", "escalar", "escalo",
    "bot", "sistema", "robot", "команда", "передам", "передаю", "эскал",
]

SCENARIOS_FILE = "scenarios_49_final.json"


def _has_banned(text: str) -> list[str]:
    low = (text or "").lower()
    return [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", low)]


def test_bot_then_anna_templates_have_no_mechanics_leak():
    """Ни один шаблон bot_then_anna не содержит слов о передаче/боте/системе."""
    scenarios = json.load(open(SCENARIOS_FILE, encoding="utf-8"))
    leaks = {}
    for s in scenarios:
        if s.get("mode") == "bot_then_anna":
            hits = _has_banned(" ".join(s.get("messages", [])))
            if hits:
                leaks[s["id"]] = hits
    assert not leaks, f"Утечка механики в шаблонах bot_then_anna: {leaks}"


def test_no_mila_in_any_scenario():
    """Ни один сценарий не упоминает старое имя Mila."""
    scenarios = json.load(open(SCENARIOS_FILE, encoding="utf-8"))
    bad = [s["id"] for s in scenarios if "mila" in " ".join(s.get("messages", [])).lower()]
    assert not bad, f"Упоминание Mila в сценариях: {bad}"
