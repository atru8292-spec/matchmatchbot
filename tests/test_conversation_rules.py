"""conversation_rules: проверка ответа режима rag кодом (зачины, фразы, сленг)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import conversation_rules as cr
import db
import sender

BAD = [
    "¡Gracias por tu interés, Carlos! El evento cuesta 6000 MXN.",
    "¡Excelente pregunta! Incluye bebida.",
    "¡Perfecto! El evento es ideal para ti.",
    "¡Perfecto! Te cuento.",
    "Con mucho gusto te cuento del evento.",
    "Qué bueno que preguntes: incluye botanas.",
    "Incluye botanas, te va a encantar.",
    "Así te confirmo si el evento es para ti.",
    "Cualquier duda, aquí estoy 🤍",
    "Va, checa el link porfa",
    "Mira, el evento es en Roma Norte.",
    "Eres un hombre muy interesante.",
    "Somos la mejor agencia de México.",
    "¡Wow, un médico! Me encanta, seguro eres un gran hombre.",
    "Ahorita te contesto",
    "Muchos me dicen que vale cada peso.",
    "Quedan pocos lugares, aprovecha.",
    "Es una inversión en algo tan importante como encontrar a tu pareja.",
    "Hola Carlos.\n\n¡Perfecto! Cuesta 6000 MXN.",   # зачин во втором пузыре тоже ловится
]
GOOD = [
    "Van 30 mujeres y 30 hombres, todos solteros.",
    "Ahora cuesta 6000 MXN e incluye bebida de bienvenida y botanas.",
    "Es en Roma Norte, CDMX.",
    "Te entiendo, no es poco. Es una noche pensada para conocer en persona a mujeres eslavas solteras.",
    "¿Cómo te llamas y estás soltero?",
    "Tómate tu tiempo, sin presión.",
    "Hola, soy Anna, fundadora de Match Match Agency 🤍",
    "Claramente es una noche tranquila.",
    # живые реакции на слова лида (решение владелицы 2026-10-21)
    "¡Claro! Te cuento.",
    "¡Qué bien! ¿Y a qué te dedicas?",
    "¡Me encanta! Es temática de Halloween.",
    "Qué gusto, Carlos. Es en Roma Norte, CDMX.",
    "Jaja, ¡ojalá alguien llegue de vampiro!",
]


@pytest.mark.parametrize("text,norm", [
    ("¡Qué bien! Es en Roma Norte.", "que bien"), ("Jajaja, sí.", "jaja"), ("Claro, te cuento.", "claro"),
    ("Es en Roma Norte, CDMX.", None), ("¡Me encanta! Ok.", "me encanta")])
def test_reaction_start(text, norm):
    assert cr.reaction_start(text) == norm


@pytest.mark.parametrize("text", BAD)
def test_bad_detected(text):
    assert cr.violations(text), text


@pytest.mark.parametrize("text", GOOD)
def test_good_clean(text):
    assert cr.violations(text) == [], text


async def test_price_next_token_substituted(monkeypatch):
    """Новый токен следующей ступени цены подставляется из app_settings."""
    monkeypatch.setattr(db, "get_settings", AsyncMock(
        return_value={"event_active": "1", "event_price_next": "7000"}))
    out = await sender._fill_event_vars("la siguiente es de [event_price_next] MXN.")
    assert out == "la siguiente es de 7000 MXN."


async def test_price_next_empty_drops_bubble(monkeypatch):
    """Ступень не задана в настройках → пузырь не уходит (а не «de  MXN»)."""
    monkeypatch.setattr(db, "get_settings", AsyncMock(
        return_value={"event_active": "1", "event_price_next": ""}))
    assert await sender._fill_event_vars("la siguiente es de [event_price_next] MXN.") is None
