"""scripts/smoke_reply_mode: оценщик нарушений и данные прогона (без OpenAI)."""
from __future__ import annotations

from scripts import smoke_reply_mode as s


def test_judge_flags():
    assert s.judge(["¿Cómo te llamas?", "¿Estás soltero?"], "hola", False) == ["вопросов 2"]
    assert "5,500" in s.judge(["Son 5,500 MXN"], "¿y con amigo?", False)
    assert "повторная презентация" in s.judge(["Soy Anna, fundadora"], "hola", True)
    assert "ссылка без вопроса о покупке" in s.judge([s.LINK], "¿cuánto cuesta?", False)
    assert s.judge([s.LINK], "¿cómo compro el boleto?", False) == []
    assert s.judge(["Ahora cuesta 6000 MXN 🤍"], "¿cuánto cuesta?", False) == []


def test_plan_sizes():
    assert len(s.MESSAGES) == 13 and len(s.DIALOGS) == 10
    assert s.EVENT_SETTINGS["event_price_old"] == ""      # без зачёркнутой «было»
    assert s.EVENT_SETTINGS["event_price_next"] == "7000"
