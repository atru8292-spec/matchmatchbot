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
    assert len(s.MESSAGES) == 13 and len(s.DIALOGS) == 11
    assert s.EVENT_SETTINGS["event_price_old"] == ""      # без зачёркнутой «было»
    assert s.EVENT_SETTINGS["event_price_next"] == "7000"


def test_budget_is_base_exception():
    """Конвейеры ловят Exception — остановка бюджета не должна превращаться в заглушку."""
    assert issubclass(s.Budget, BaseException) and not issubclass(s.Budget, Exception)
    assert s.RESERVE_USD > 0


def test_judge_page_word_is_not_purchase_and_big_numbers():
    assert "ссылка без вопроса о покупке" in s.judge([s.LINK], "¿qué hay en la página?", False)
    assert "зачёркнутая/8000" not in s.judge(["Son 18,000 personas"], "x", False)


def test_judge_photo_gate():
    """Ссылка до одобренного фото — нарушение; после фото с прошлым вопросом о покупке — нет."""
    assert "ссылка до фото" in s.judge([s.LINK], "¿cómo compro el boleto?", False, photo=False)
    assert s.judge([s.LINK], s.PHOTO_OK, False, buying=True, photo=True) == []
    assert "ссылка без вопроса о покупке" in s.judge([s.LINK], s.PHOTO_OK, False, buying=False)


def test_photo_dialogs_present():
    texts = [t for _, _, _, turns in s.DIALOGS for t in turns]
    assert texts.count(s.PHOTO_OK) == 2
