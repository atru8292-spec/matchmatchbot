"""Unit-тесты для ai.py — AI-ядро бота Anna.

Все внешние зависимости замоканы: OpenAI (_embed, _call_openai), БД (search_scenarios_by_vector).
Реальных сетевых вызовов нет.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import ai


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _make_scenario(
    *,
    id: int = 1,
    template_es: str = "Hola!\n\nTe cuento más.",
    mode: str = "bot_auto",
    ai_allowed: bool = True,
    blocks_lead: bool = False,
    score: float = 0.75,
) -> dict:
    return {
        "id": id,
        "template_es": template_es,
        "mode": mode,
        "ai_allowed": ai_allowed,
        "blocks_lead": blocks_lead,
        "score": score,
    }


def _make_lead(**kwargs) -> dict:
    base = {
        "age": 40,
        "profession": "empresario",
        "is_single": True,
        "city": "CDMX",
        "interest": None,
        "funnel_stage": "new",
        "photo_received": False,
        "whatsapp_name": "Juan",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _split_template
# ---------------------------------------------------------------------------

class TestRelativeGapEs:
    """_relative_gap_es — разрыв времени между сообщениями в человеко-читаемом виде
    для AI (добавлено 2026-09-07, по просьбе пользователя: бот должен уметь отличить
    реальное возвращение лида после долгого молчания от мгновенного повтора)."""

    def test_small_gap_returns_none(self):
        assert ai._relative_gap_es(60) is None            # 1 минута
        assert ai._relative_gap_es(3 * 3600) is None       # 3 часа — ниже порога 6ч

    def test_hours_gap(self):
        assert ai._relative_gap_es(7 * 3600) == "unas horas"

    def test_one_day_gap(self):
        assert ai._relative_gap_es(30 * 3600) == "un día"

    def test_several_days_gap(self):
        assert ai._relative_gap_es(3 * 86400) == "3 días"

    def test_weeks_gap(self):
        assert ai._relative_gap_es(15 * 86400) == "2 semanas"
        assert ai._relative_gap_es(8 * 86400) == "1 semana"  # singular correcto

    def test_months_gap(self):
        assert ai._relative_gap_es(65 * 86400) == "2 meses"
        assert ai._relative_gap_es(31 * 86400) == "1 mes"  # singular correcto


class TestBuildUserContextGap:
    """_build_user_context выставляет tiempo_desde_ultimo_mensaje по времени
    ПОСЛЕДНЕГО СООБЩЕНИЯ ANNA в history (не history[-1] вообще!). ИСПРАВЛЕНО
    2026-09-07 на code-review: в реальном пайплайне main.py входящее сообщение лида
    уже лежит в БД (processed=True, не удалено) к моменту вызова _run_ai —
    db.get_conversation_history отдаёт его как САМУЮ СВЕЖУЮ запись, так что
    history[-1] почти всегда САМ ТЕКУЩИЙ user_text (created_at ≈ момент вебхука,
    не более debounce delay/max_wait ≈ 90-120с назад) — сравнение с ним всегда
    давало бы gap=None, фича была бы мертворождённой в проде. Реплика Anna не может
    быть частью необработанного залпа (её ещё нет в БД) — надёжный якорь."""

    def test_none_when_no_anna_message(self):
        """История без единой реплики Anna (совсем новый лид) — gap=None."""
        history = [{"sender": "lead", "text": "hola"}]
        ctx = ai._build_user_context({}, history, "hola", [])
        assert json.loads(ctx)["tiempo_desde_ultimo_mensaje"] is None

    def test_none_when_gap_small(self):
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc)
        history = [{"sender": "anna", "text": "hola", "created_at": recent}]
        ctx = ai._build_user_context({}, history, "hola", [])
        assert json.loads(ctx)["tiempo_desde_ultimo_mensaje"] is None

    def test_set_when_gap_large(self):
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(days=3)
        history = [{"sender": "anna", "text": "hola", "created_at": old}]
        ctx = ai._build_user_context({}, history, "hola", [])
        assert json.loads(ctx)["tiempo_desde_ultimo_mensaje"] == "3 días"

    def test_ignores_trailing_lead_burst_uses_anna_anchor(self):
        """Реалистичная форма history из реального пайплайна (см. докстринг класса):
        последняя реплика Anna была 4 дня назад, а ПОСЛЕ неё в history уже лежит
        текущий (ещё не отвеченный) залп лида, вставленный в БД до debounce —
        gap должен считаться от реплики Anna, а НЕ от history[-1] (который был бы
        "только что", раз это фактически тот же залп что и user_text)."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        history = [
            {"sender": "anna", "text": "Hola! eres soltero?", "created_at": now - timedelta(days=4)},
            # текущий залп — уже в БД (main.py вставляет ДО debounce), created_at свежий
            {"sender": "lead", "text": "hola, sigo aqui", "created_at": now - timedelta(seconds=90)},
        ]
        ctx = ai._build_user_context({}, history, "hola, sigo aqui", [])
        assert json.loads(ctx)["tiempo_desde_ultimo_mensaje"] == "4 días"

    def test_anna_reply_within_active_session_suppresses_gap(self):
        """Диалог активен (Anna ответила минуту назад) — gap=None, не повторяем
        "qué gusto que regresaste" на каждом ходу одной и той же сессии."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        history = [
            {"sender": "anna", "text": "Qué gusto que regresaste!", "created_at": now - timedelta(days=4)},
            {"sender": "lead", "text": "hola", "created_at": now - timedelta(minutes=2)},
            {"sender": "anna", "text": "Perfecto, gracias", "created_at": now - timedelta(minutes=1)},
        ]
        ctx = ai._build_user_context({}, history, "y ahora que?", [])
        assert json.loads(ctx)["tiempo_desde_ultimo_mensaje"] is None


class TestPlausibleName:
    """_plausible_name — фильтр whatsapp_name перед показом AI (см. ai.py)."""

    @pytest.mark.parametrize("name", [
        "Juan", "María José", "Jean-Paul", "O'Brien", "Carlos Ramírez",
        "Ana", "José Luis",
    ])
    def test_plausible_latin_names_pass(self, name):
        assert ai._plausible_name(name) == name

    @pytest.mark.parametrize("name", [
        "Арина",           # кириллица
        "田中",             # CJK
        "😎🔥",             # только эмодзи
        "Juan 😎",          # имя + эмодзи
        "12345",           # цифры
        "Juan123",         # буквы+цифры
        None,
        "",
        "   ",
        "a",               # 1 символ — слишком коротко
        "x" * 41,          # слишком длинно
    ])
    def test_implausible_names_rejected(self, name):
        assert ai._plausible_name(name) is None


class TestSplitTemplate:
    def test_basic_split(self):
        """Три части разделённые \\n\\n → список из трёх строк."""
        result = ai._split_template("a\n\nb\n\nc")
        assert result == ["a", "b", "c"]

    def test_more_than_4_parts_truncated(self):
        """Более 4 частей обрезаются до MAX_MESSAGES=4."""
        template = "p1\n\np2\n\np3\n\np4\n\np5\n\np6"
        result = ai._split_template(template)
        assert len(result) == 4
        assert result == ["p1", "p2", "p3", "p4"]

    def test_empty_parts_discarded(self):
        """Пустые части (пустые строки, только пробелы) отбрасываются."""
        result = ai._split_template("a\n\n\n\nb\n\n   \n\nc")
        assert result == ["a", "b", "c"]

    def test_empty_string(self):
        """Пустая строка → пустой список."""
        assert ai._split_template("") == []

    def test_none_input(self):
        """None-подобный ввод: функция принимает None через `or ''`."""
        # template_es может прийти как None из БД
        result = ai._split_template(None)
        assert result == []

    def test_single_part(self):
        """Нет разделителей → список из одного элемента."""
        assert ai._split_template("Hola!") == ["Hola!"]

    def test_exactly_4_parts(self):
        """Ровно 4 части — не обрезаем."""
        result = ai._split_template("a\n\nb\n\nc\n\nd")
        assert result == ["a", "b", "c", "d"]

    def test_whitespace_stripped(self):
        """Пробелы в начале/конце каждой части обрезаются."""
        result = ai._split_template("  hello  \n\n  world  ")
        assert result == ["hello", "world"]


# ---------------------------------------------------------------------------
# _fixed_reply
# ---------------------------------------------------------------------------

class TestFixedReply:
    def test_blocks_lead_true_gives_block_action(self):
        """blocks_lead=True → action='block', независимо от mode."""
        scenario = _make_scenario(
            id=5,
            mode="bot_then_block",
            blocks_lead=True,
            ai_allowed=False,
            template_es="Lo siento.",
        )
        result = ai._fixed_reply(scenario)
        assert result["action"] == "block"
        assert result["needs_escalation"] is False

    def test_mode_bot_then_anna_gives_escalate(self):
        """mode='bot_then_anna', blocks_lead=False → action='escalate', needs_escalation=True."""
        scenario = _make_scenario(
            id=10,
            mode="bot_then_anna",
            blocks_lead=False,
            ai_allowed=False,
            template_es="Te paso con Anna.\n\nElla te atiende.",
        )
        result = ai._fixed_reply(scenario)
        assert result["action"] == "escalate"
        assert result["needs_escalation"] is True

    def test_mode_bot_auto_gives_respond(self):
        """mode='bot_auto', blocks_lead=False → action='respond'."""
        scenario = _make_scenario(
            id=2,
            mode="bot_auto",
            blocks_lead=False,
            ai_allowed=False,
            template_es="Hola!\n\nEl precio es $1,400.",
        )
        result = ai._fixed_reply(scenario)
        assert result["action"] == "respond"
        assert result["needs_escalation"] is False

    def test_mode_to_anna_silent_gives_silent(self):
        """mode='to_anna_silent' → action='silent' (NO 'escalate' — no debe escribirle al lead)."""
        scenario = _make_scenario(
            id=3,
            mode="to_anna_silent",
            blocks_lead=False,
            ai_allowed=False,
            template_es="",
        )
        result = ai._fixed_reply(scenario)
        assert result["action"] == "silent"
        assert result["messages"] == []
        assert result["needs_escalation"] is True

    def test_mode_to_anna_silent_forces_empty_messages_even_with_template(self):
        """Aunque template_es tenga texto, 'silent' siempre manda messages=[]."""
        scenario = _make_scenario(
            id=37, mode="to_anna_silent", blocks_lead=False, ai_allowed=False,
            template_es="Este texto NUNCA debería llegar al lead.",
        )
        result = ai._fixed_reply(scenario)
        assert result["messages"] == []
        assert result["needs_escalation"] is True

    def test_used_scenario_id(self):
        """used_scenario_id равен id сценария."""
        scenario = _make_scenario(id=42)
        result = ai._fixed_reply(scenario)
        assert result["used_scenario_id"] == 42

    def test_messages_split_from_template(self):
        """messages берутся из template_es через _split_template."""
        scenario = _make_scenario(template_es="msg1\n\nmsg2")
        result = ai._fixed_reply(scenario)
        assert result["messages"] == ["msg1", "msg2"]

    def test_extracted_is_empty_dict(self):
        """extracted всегда пустой dict в фикс-ответе."""
        scenario = _make_scenario()
        result = ai._fixed_reply(scenario)
        assert result["extracted"] == {}

    def test_funnel_stage_is_none(self):
        """funnel_stage=None в фикс-ответе (не меняем воронку)."""
        scenario = _make_scenario()
        result = ai._fixed_reply(scenario)
        assert result["funnel_stage"] is None

    def test_scenario_17_and_10_funnel_stage_none_by_itself(self):
        """#10/#17 сами по себе funnel_stage не трогают — nurture проставляет
        _enforce_nurture_stage() отдельно (единая точка для фикс- И AI-ветки)."""
        assert ai._fixed_reply(_make_scenario(id=17))["funnel_stage"] is None
        assert ai._fixed_reply(_make_scenario(id=10))["funnel_stage"] is None


# ---------------------------------------------------------------------------
# _fallback_reply
# ---------------------------------------------------------------------------

class TestFallbackReply:
    def test_action_is_escalate(self):
        assert ai._fallback_reply()["action"] == "escalate"

    def test_needs_escalation_true(self):
        assert ai._fallback_reply()["needs_escalation"] is True

    def test_messages_content(self):
        result = ai._fallback_reply()
        assert result["messages"] == ["Ahorita te contesto 🤍"]

    def test_used_scenario_id_none(self):
        assert ai._fallback_reply()["used_scenario_id"] is None

    def test_extracted_empty(self):
        assert ai._fallback_reply()["extracted"] == {}


# ---------------------------------------------------------------------------
# _validate_output
# ---------------------------------------------------------------------------

class TestValidateOutput:
    def test_happy_path(self):
        """Валидный ответ AI проходит без изменений."""
        data = {
            "messages": ["Hola!", "Te cuento."],
            "action": "respond",
            "funnel_stage": "qualifying",
            "extracted": {"age": 35, "profession": "médico"},
            "needs_escalation": False,
            "used_scenario_id": None,
        }
        result = ai._validate_output(data)
        assert result["messages"] == ["Hola!", "Te cuento."]
        assert result["action"] == "respond"
        assert result["extracted"] == {"age": 35, "profession": "médico"}
        assert result["needs_escalation"] is False

    def test_messages_more_than_4_truncated(self):
        """5 сообщений → обрезка до 4."""
        data = {
            "messages": ["m1", "m2", "m3", "m4", "m5"],
            "action": "respond",
        }
        result = ai._validate_output(data)
        assert result["messages"] == ["m1", "m2", "m3", "m4"]

    def test_messages_empty_list_raises(self):
        """Пустой список messages → ValueError."""
        with pytest.raises(ValueError):
            ai._validate_output({"messages": [], "action": "respond"})

    def test_messages_not_list_raises(self):
        """messages — не список → ValueError."""
        with pytest.raises(ValueError):
            ai._validate_output({"messages": "hola", "action": "respond"})

    def test_messages_missing_raises(self):
        """Отсутствующий ключ messages → ValueError."""
        with pytest.raises(ValueError):
            ai._validate_output({"action": "respond"})

    def test_messages_all_blank_raises(self):
        """Список из пустых строк → ValueError после чистки."""
        with pytest.raises(ValueError):
            ai._validate_output({"messages": ["   ", ""], "action": "respond"})

    def test_invalid_action_replaced_with_respond(self):
        """Невалидный action → 'respond'."""
        data = {"messages": ["hi"], "action": "unknown_action"}
        result = ai._validate_output(data)
        assert result["action"] == "respond"

    def test_valid_actions_preserved(self):
        """Каждый из валидных action-ов сохраняется."""
        for action in ("respond", "block", "escalate"):
            data = {"messages": ["hi"], "action": action}
            assert ai._validate_output(data)["action"] == action

    def test_extracted_keeps_only_known_keys(self):
        """Лишние ключи в extracted отбрасываются."""
        data = {
            "messages": ["hi"],
            "action": "respond",
            "extracted": {
                "age": 40,
                "profession": "abogado",
                "unknown_field": "trash",
                "another_extra": 123,
            },
        }
        result = ai._validate_output(data)
        assert "unknown_field" not in result["extracted"]
        assert "another_extra" not in result["extracted"]
        assert result["extracted"]["age"] == 40
        assert result["extracted"]["profession"] == "abogado"

    def test_extracted_none_values_dropped(self):
        """None-значения в extracted не попадают в результат."""
        data = {
            "messages": ["hi"],
            "action": "respond",
            "extracted": {"age": None, "profession": "médico", "city": None},
        }
        result = ai._validate_output(data)
        assert "age" not in result["extracted"]
        assert "city" not in result["extracted"]
        assert result["extracted"]["profession"] == "médico"

    def test_needs_escalation_coerced_to_bool(self):
        """needs_escalation приводится к bool."""
        data = {"messages": ["hi"], "action": "respond", "needs_escalation": 1}
        assert ai._validate_output(data)["needs_escalation"] is True

        data2 = {"messages": ["hi"], "action": "respond", "needs_escalation": 0}
        assert ai._validate_output(data2)["needs_escalation"] is False

    def test_all_extracted_keys_accepted(self):
        """Все 5 допустимых ключей принимаются."""
        data = {
            "messages": ["hi"],
            "action": "respond",
            "extracted": {
                "age": 35,
                "profession": "médico",
                "is_single": True,
                "city": "CDMX",
                "interest": "seria",
            },
        }
        result = ai._validate_output(data)
        assert len(result["extracted"]) == 5

    def test_non_dict_input_raises(self):
        """Входной параметр — не dict → ValueError."""
        with pytest.raises(ValueError):
            ai._validate_output("not a dict")

    def test_silent_action_allows_empty_messages(self):
        """action='silent' — NO requiere messages no vacío (único caso)."""
        r = ai._validate_output({"messages": [], "action": "silent"})
        assert r["action"] == "silent"
        assert r["messages"] == []

    def test_silent_action_forces_empty_even_if_model_sent_text(self):
        """Si el modelo manda 'silent' pero igual escribió texto — igual forzamos []."""
        r = ai._validate_output({"messages": ["texto que no debería llegar"], "action": "silent"})
        assert r["messages"] == []

    def test_silent_action_missing_messages_key_ok(self):
        """action='silent' sin la clave 'messages' siquiera — no debe romper."""
        r = ai._validate_output({"action": "silent"})
        assert r["messages"] == []

    def test_other_actions_still_require_nonempty_messages(self):
        """No es una excepción general — respond/block/escalate siguen exigiendo messages."""
        with pytest.raises(ValueError):
            ai._validate_output({"messages": [], "action": "respond"})


# ---------------------------------------------------------------------------
# load_system_prompt
# ---------------------------------------------------------------------------

class TestLoadSystemPrompt:
    def setup_method(self):
        """Сбрасываем кэш перед каждым тестом."""
        ai._system_prompt_cache = None

    def test_returns_nonempty_string(self):
        """Промпт — непустая строка."""
        result = ai.load_system_prompt()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_anna(self):
        """Промпт содержит 'Anna' (имя бота)."""
        result = ai.load_system_prompt()
        assert "Anna" in result

    def test_cache_returns_same_object(self):
        """Повторный вызов возвращает тот же объект (кэш, не перечитывает файл)."""
        first = ai.load_system_prompt()
        second = ai.load_system_prompt()
        assert first is second  # именно один объект

    def teardown_method(self):
        """Восстанавливаем кэш после теста."""
        ai._system_prompt_cache = None


# ---------------------------------------------------------------------------
# generate_reply — тесты через мок search_scenarios + _call_openai
# ---------------------------------------------------------------------------

# Минимально валидный ответ OpenAI, который пройдёт _validate_output
_VALID_AI_RESPONSE = {
    "messages": ["Hola, guapo!"],
    "action": "respond",
    "funnel_stage": "qualifying",
    "extracted": {"age": 40},
    "needs_escalation": False,
    "used_scenario_id": None,
}

# Историал con la pregunta soltero/edad YA hecha por Anna — para tests de otros
# guardrails (link/video) que no quieren activar _enforce_event_qualification_gate.
_QUALIFIED_HISTORY = [
    {"sender": "lead", "text": "Hola"},
    {"sender": "anna", "text": "¡Hola! Eres soltero? Qué edad tienes?"},
    {"sender": "lead", "text": "Sí, tengo 30"},
]


@pytest.fixture()
def lead():
    return _make_lead()


@pytest.fixture()
def history():
    return [{"sender": "lead", "text": "Hola"}, {"sender": "bot", "text": "Hola!"}]


class TestGenerateReplyFixed:
    """Ветка 1: ai_allowed=False + score >= FALLBACK_SCORE → фикс-ответ, OpenAI не вызывается."""

    async def test_fixed_branch_no_openai_call(self, lead, history):
        """ai_allowed=False, score=0.7, blocks_lead=True → action=block, _call_openai НЕ вызван."""
        scenario = _make_scenario(
            id=7,
            ai_allowed=False,
            score=0.7,
            mode="bot_then_block",
            blocks_lead=True,
            template_es="msg1\n\nmsg2",
        )
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[scenario])) as mock_search, \
             patch("ai._call_openai", new=AsyncMock()) as mock_openai:
            result = await ai.generate_reply(lead, history, "texto")

        assert result["action"] == "block"
        assert result["used_scenario_id"] == 7
        assert result["messages"] == ["msg1", "msg2"]
        mock_openai.assert_not_awaited()

    async def test_fixed_branch_escalate_when_bot_then_anna(self, lead, history):
        """mode='bot_then_anna', blocks_lead=False → action=escalate."""
        scenario = _make_scenario(
            id=10,
            ai_allowed=False,
            score=0.65,
            mode="bot_then_anna",
            blocks_lead=False,
            template_es="Espera un momento.",
        )
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", new=AsyncMock()) as mock_openai:
            result = await ai.generate_reply(lead, history, "texto")

        assert result["action"] == "escalate"
        assert result["needs_escalation"] is True
        mock_openai.assert_not_awaited()


class TestContextFallback:
    """Контекст-фолбэк: bare<FALLBACK → перезапрос с последней репликой Anna."""

    def test_last_anna_text_returns_latest_bot(self):
        h = [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "eres soltero?"},
             {"sender": "lead", "text": "va"}]
        assert ai._last_anna_text(h) == "eres soltero?"

    def test_last_anna_text_none_without_bot(self):
        assert ai._last_anna_text([{"sender": "lead", "text": "hola"}]) is None
        assert ai._last_anna_text([]) is None

    async def test_fallback_reranks_when_bare_low(self, lead):
        """Низкий bare (0.21) + есть реплика Anna → перезапрос с контекстом, берём лучший."""
        history = [{"sender": "anna", "text": "me mandas una foto?"}]
        bare = [_make_scenario(id=39, ai_allowed=True, score=0.21)]
        ctx = [_make_scenario(id=6, ai_allowed=True, score=0.60)]
        mock_search = AsyncMock(side_effect=[bare, ctx])
        with patch("ai.search_scenarios", mock_search), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)):
            await ai.generate_reply(lead, history, "va")
        assert mock_search.await_count == 2
        assert "me mandas una foto?" in mock_search.await_args_list[1].args[0]

    async def test_no_fallback_when_bare_confident(self, lead):
        """Уверенный bare (0.72) → фолбэк НЕ срабатывает (здоровые сценарии не трогаем)."""
        history = [{"sender": "anna", "text": "hola"}]
        mock_search = AsyncMock(return_value=[_make_scenario(id=3, ai_allowed=True, score=0.72)])
        with patch("ai.search_scenarios", mock_search), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)):
            await ai.generate_reply(lead, history, "quiero conocer rusas")
        assert mock_search.await_count == 1


class TestAdKeywordForcesEventHook:
    """"novia rusa" — кодовая фраза рекламы → форс №2 (крючок про ивент), НЕ augment.

    Регресс найден 2026-09-01 живым тестом: augment-кандидата (score=0.5) проигрывал
    натуральному RAG-топу (напр. #3, общий питч агентства) — AI шёл за агентством,
    теряя весь смысл рекламной кодовой фразы. Синтетический маркетинговый триггер с
    ОДНИМ верным толкованием — форсим top напрямую (тот же принцип, что у "photo
    одобрено + interest=event", в отличие от органического текста лида)."""

    async def test_forces_scenario_2_even_when_natural_top_differs(self):
        wrong_top = _make_scenario(id=3, ai_allowed=True, score=0.75)
        n2_row = {"id": 2, "template_es": "Hola, te cuento del evento...",
                  "mode": "bot_auto", "ai_allowed": True, "blocks_lead": False}
        getrow = AsyncMock(return_value=n2_row)
        ai_response = {**_VALID_AI_RESPONSE, "used_scenario_id": 2}
        with patch("ai.search_scenarios", AsyncMock(return_value=[wrong_top])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply({"funnel_stage": "new"}, [], "novia rusa")
        getrow.assert_awaited_once_with(2)
        assert result["used_scenario_id"] == 2


class TestColdLeadEventGuard:
    """Холодный лид + ценовой/детальный вопрос → крючок или детали без цены."""

    async def test_cold_lead_service_price_question_no_longer_forced(self):
        """Холодный лид + ценовой вопрос про сервис (RAG=№16, ai_allowed=true) → форса
        больше нет (убран вместе с cold-lead router) — идёт обычная AI-ветка. Защиту от
        утечки $10k теперь обеспечивает _enforce_service_price_gate, не роутинг сюда."""
        lead = {"funnel_stage": "new"}  # is_single не задан → холодный
        n16 = _make_scenario(id=16, ai_allowed=True, score=0.55)
        getrow = AsyncMock()
        call_openai = AsyncMock(return_value=_VALID_AI_RESPONSE)
        with patch("ai.search_scenarios", AsyncMock(return_value=[n16])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", call_openai):
            await ai.generate_reply(lead, [], "cuánto sale entrar?")
        getrow.assert_not_awaited()          # роутинг убран — никакого форса
        call_openai.assert_awaited_once()

    async def test_cold_lead_event_price_via_natural_rag_no_force_needed(self):
        """Холодный лид спрашивает цену ИВЕНТА — раньше форсили №51 напрямую отдельным
        роутером, теперь не нужно: RAG сам находит №51 с высоким score (буквально пример
        в его trigger_es), обычный fixed-reply путь, без всякого форса."""
        lead = {"funnel_stage": "new"}
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.70,
                              template_es="Precio del evento.")
        getrow = AsyncMock()
        mock_openai = AsyncMock()
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", mock_openai):
            result = await ai.generate_reply(lead, [], "cuanto cuesta el evento")
        getrow.assert_not_awaited()
        mock_openai.assert_not_awaited()
        assert result["used_scenario_id"] == 51

    async def test_cold_lead_51_details_routed_to_52(self):
        """Холодный лид + RAG=N51 + не ценовой вопрос → №52 (детали без цены)."""
        lead = {"funnel_stage": "new"}  # is_single не задан → холодный
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.62)
        n52_row = {"id": 52, "template_es": "Detailes sin precio", "mode": "bot_auto",
                   "ai_allowed": False, "blocks_lead": False}
        getrow = AsyncMock(return_value=n52_row)
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)):
            await ai.generate_reply(lead, [], "info del evento?")
        getrow.assert_awaited_once_with(52)  # детали без цены → №52

    async def test_qualified_lead_51_not_routed(self):
        lead = {"funnel_stage": "qualified", "is_single": True}  # квалифицирован
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.62)
        getrow = AsyncMock()
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock()):
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        getrow.assert_not_awaited()          # №51 остался (не роутили)
        assert result["used_scenario_id"] == 51


class TestPostEventOnlyGate:
    """Сценарии, применимые ТОЛЬКО к лиду, который уже был на конкретном ивенте
    (funnel_stage='event_attended') — без этого гейта короткие/общие фразы от лида,
    никогда не бывшего на ивенте, могут матчить на сценарии, которые ГАЛЛЮЦИНИРУЮТ
    контекст "ты уже был на ивенте" (регресс 2026-08-24 для #24/#25/#57, найден
    заново для #26 2026-09-07: холодный лид "me pasas el numero de alguna chica?"
    матчил на #26 "Хочу контакт девушки С ИВЕНТА" — воспроизведено 3/3 живым тестом,
    бот отвечал "qué bueno que conectaste en el evento!" человеку, не бывшему там)."""

    async def test_scenario_26_filtered_for_non_event_attended_lead(self):
        lead = {"funnel_stage": "pitched", "is_single": True}  # НЕ event_attended
        n26 = _make_scenario(id=26, ai_allowed=True, score=0.75)
        with patch("ai.search_scenarios", AsyncMock(return_value=[n26])), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)) as mock_openai:
            await ai.generate_reply(lead, [], "me pasas el numero de alguna chica?")
        # confident-список для AI не должен содержать #26 — проверяем через контекст
        call_context = mock_openai.await_args.args[0]
        assert '"id": 26' not in call_context

    async def test_scenario_26_kept_for_event_attended_lead(self):
        lead = {"funnel_stage": "event_attended", "is_single": True}
        n26 = _make_scenario(id=26, ai_allowed=True, score=0.75)
        ai_response = {**_VALID_AI_RESPONSE, "used_scenario_id": 26}
        with patch("ai.search_scenarios", AsyncMock(return_value=[n26])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)) as mock_openai:
            result = await ai.generate_reply(lead, [], "me pasas el numero de la chica que conocí?")
        call_context = mock_openai.await_args.args[0]
        assert '"id": 26' in call_context
        assert result["used_scenario_id"] == 26


class TestWarmLeadEventPriceAugment:
    """Тёплый лид + смешанное сообщение (профиль + цена ивента), RAG-топ ненадёжен
    (регрессы 2026-08-21/26) → №51 ДОБАВЛЯЕТСЯ в кандидаты (не форсится top)."""

    async def test_augments_51_when_mismatched_top(self):
        lead = _make_lead(is_single=True, interest=None)
        wrong_top = _make_scenario(id=4, ai_allowed=True, score=0.55,
                                    template_es="Y cuántos años tienes?")
        n51_row = {"id": 51, "template_es": "Detalles+precio evento", "mode": "bot_auto",
                   "ai_allowed": False, "blocks_lead": False}
        getrow = AsyncMock(return_value=n51_row)
        ai_response = {**_VALID_AI_RESPONSE, "used_scenario_id": 51}
        with patch("ai.search_scenarios", AsyncMock(return_value=[wrong_top])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, [], "soy soltero, 35, cuanto cuesta el evento")
        getrow.assert_awaited_once_with(51)   # №51 добавлен как кандидат
        assert result["used_scenario_id"] == 51

    async def test_does_not_augment_when_explicit_service_question(self):
        """Явное "servicio" в тексте — приоритет над упоминанием evento/interest, №51
        не добавляем (регресс 2026-08-31)."""
        lead = _make_lead(is_single=True, interest="event")
        wrong_top = _make_scenario(id=4, ai_allowed=True, score=0.55)
        getrow = AsyncMock()
        with patch("ai.search_scenarios", AsyncMock(return_value=[wrong_top])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)):
            await ai.generate_reply(lead, [], "oye y el servicio de matchmaking cuanto cuesta")
        getrow.assert_not_awaited()

    async def test_does_not_duplicate_when_51_already_candidate(self):
        lead = _make_lead(is_single=True, interest=None)
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.40)
        getrow = AsyncMock()
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)):
            await ai.generate_reply(lead, [], "cuanto cuesta el evento")
        getrow.assert_not_awaited()   # уже в scenarios — второй раз не добавляем


class TestEventVideoAnnounce:
    """Анонс explainer-видео в #51/#52: дописываем в последний баббл, только если видео
    реально уйдёт (не слали + пул не пуст). Иначе текст кончается как есть (без обещания)."""

    # 4-абзацный шаблон #51 (как в проде — упирается в MAX_MESSAGES=4)
    _TMPL_51 = "Precio ...\n\nEs único ...\n\nTodos van ...\n\nAquí está el enlace: [event_link]"

    def _patches(self, *, already_sent: bool, pool_video: list):
        """Общие моки БД для ветки анонса."""
        return (
            patch("ai.db.get_settings", AsyncMock(return_value={"event_date": "2026-08-15"})),
            patch("ai.db.event_media_sent", AsyncMock(return_value=already_sent)),
            patch("ai.db.random_event_media", AsyncMock(return_value=pool_video)),
        )

    async def test_announce_added_when_not_sent_and_pool_nonempty(self):
        """Видео не слали + в пуле есть активное видео → подпись выставлена (video_caption),
        messages не трогаем — подпись идёт К видео, не отдельным бабблом."""
        lead = _make_lead(phone="wa_5215500000001")
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.62, template_es=self._TMPL_51)
        p_settings, p_sent, p_pool = self._patches(already_sent=False, pool_video=[{"storage_url": "u"}])
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock()) as mock_openai, \
             p_settings, p_sent, p_pool:
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        assert result["used_scenario_id"] == 51
        assert result["send_event_video"] is True
        assert result["video_caption"] == ai._EVENT_VIDEO_ANNOUNCE
        assert ai._EVENT_VIDEO_ANNOUNCE not in "\n".join(result["messages"])  # не в тексте
        assert len(result["messages"]) <= ai.MAX_MESSAGES                     # лимит не превышен
        mock_openai.assert_not_awaited()

    async def test_no_announce_when_already_sent(self):
        """Видео этому лиду на этот ивент уже слали → подписи нет, текст кончается на ссылке."""
        lead = _make_lead(phone="wa_5215500000002")
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.62, template_es=self._TMPL_51)
        p_settings, p_sent, p_pool = self._patches(already_sent=True, pool_video=[{"storage_url": "u"}])
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock()), \
             p_settings, p_sent, p_pool:
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        assert result.get("video_caption") is None
        assert result["messages"][-1] == "Aquí está el enlace: [event_link]"
        assert len(result["messages"]) <= ai.MAX_MESSAGES

    async def test_no_announce_when_pool_empty(self):
        """Пул видео пуст (удалили/сняли is_active) → подписи нет ДАЖЕ если маркера ещё нет."""
        lead = _make_lead(phone="wa_5215500000003")
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.62, template_es=self._TMPL_51)
        p_settings, p_sent, p_pool = self._patches(already_sent=False, pool_video=[])
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock()), \
             p_settings, p_sent, p_pool:
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        assert result.get("video_caption") is None
        assert result["messages"][-1] == "Aquí está el enlace: [event_link]"
        assert len(result["messages"]) <= ai.MAX_MESSAGES

    async def test_announce_also_for_52(self):
        """#52 (детали без цены) — та же ветка подписи при квалифицированном лиде."""
        lead = _make_lead(phone="wa_5215500000004")
        tmpl52 = "Incluye ...\n\nEs único ...\n\nTodos van ...\n\nSi quieres, te paso el enlace."
        n52 = _make_scenario(id=52, ai_allowed=False, score=0.62, template_es=tmpl52)
        p_settings, p_sent, p_pool = self._patches(already_sent=False, pool_video=[{"storage_url": "u"}])
        with patch("ai.search_scenarios", AsyncMock(return_value=[n52])), \
             patch("ai._call_openai", AsyncMock()), \
             p_settings, p_sent, p_pool:
            result = await ai.generate_reply(lead, [], "cuéntame del evento")
        assert result["video_caption"] == ai._EVENT_VIDEO_ANNOUNCE
        assert len(result["messages"]) <= ai.MAX_MESSAGES

    async def test_no_announce_when_block(self):
        """#51/#52 с blocks_lead=True (action=block) → анонса нет: main вернётся ДО отправки
        видео, значит анонс был бы ложью. Гард на action=='block' срабатывает до запросов БД."""
        lead = _make_lead(phone="wa_5215500000005")
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.70, blocks_lead=True,
                             mode="bot_then_block", template_es=self._TMPL_51)
        gs = AsyncMock(return_value={"event_date": "2026-08-15"})
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock()), \
             patch("ai.db.get_settings", gs):
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        assert result["action"] == "block"
        gs.assert_not_awaited()  # гард на block отработал до запроса event_date
        assert ai._EVENT_VIDEO_ANNOUNCE not in "\n".join(result["messages"])

    async def test_no_db_calls_when_no_phone(self):
        """Лид без phone → ранний выход, БД для анонса не дёргаем (обратная совместимость)."""
        lead = _make_lead()
        lead.pop("phone", None)
        n51 = _make_scenario(id=51, ai_allowed=False, score=0.62, template_es=self._TMPL_51)
        gs = AsyncMock(return_value={"event_date": "2026-08-15"})
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock()), \
             patch("ai.db.get_settings", gs):
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        gs.assert_not_awaited()  # без phone до запроса event_date не доходим
        assert ai._EVENT_VIDEO_ANNOUNCE not in "\n".join(result["messages"])

    async def test_ai_branch_51_gets_video_even_if_ai_forgot_flag(self):
        """№51 через AI-ветку (ai_allowed=true) — send_event_video гарантируется кодом,
        не полагаясь на то, что AI сам его выставит (найдено 2026-09-01: AI выставлял
        флаг верно только ~1 раз из 3). Регресс задачи #10 (перевод №51/52 на
        ai_allowed=true молча сломал старую гарантию _fixed_reply)."""
        lead = _make_lead(phone="wa_5215500000006", is_single=True)
        n51 = _make_scenario(id=51, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE,
                       "messages": ["El evento cuesta 6,000 MXN."],
                       "send_event_video": False,  # AI "забыл" выставить флаг
                       "used_scenario_id": 51}
        p_settings, p_sent, p_pool = self._patches(already_sent=False, pool_video=[{"storage_url": "u"}])
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)), \
             p_settings, p_sent, p_pool:
            result = await ai.generate_reply(lead, _QUALIFIED_HISTORY, "cuánto cuesta el evento?")
        assert result["send_event_video"] is True
        assert result["video_caption"] == ai._EVENT_VIDEO_ANNOUNCE

    async def test_ai_branch_no_video_when_already_sent(self):
        """AI-ветка + видео уже слали на этот ивент → дедуп всё равно срабатывает."""
        lead = _make_lead(phone="wa_5215500000007", is_single=True)
        n51 = _make_scenario(id=51, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE, "messages": ["El evento cuesta 6,000 MXN."],
                       "used_scenario_id": 51}
        p_settings, p_sent, p_pool = self._patches(already_sent=True, pool_video=[{"storage_url": "u"}])
        with patch("ai.search_scenarios", AsyncMock(return_value=[n51])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)), \
             p_settings, p_sent, p_pool:
            result = await ai.generate_reply(lead, [], "cuánto cuesta el evento?")
        assert ai._EVENT_VIDEO_ANNOUNCE not in "\n".join(result["messages"])


class TestEventPhotoAnnounce:
    """Подпись a la foto del evento (2026-09-12, feedback владелицы: las fotos no
    deben ir "peladas") — mismo principio que _maybe_announce_event_video pero para
    send_event_photo/photo_caption."""

    def _patches(self, *, already_sent: bool, pool_photo: list):
        return (
            patch("ai.db.get_settings", AsyncMock(return_value={"event_date": "2026-08-15"})),
            patch("ai.db.event_media_sent", AsyncMock(return_value=already_sent)),
            patch("ai.db.random_event_media", AsyncMock(return_value=pool_photo)),
        )

    async def test_caption_set_when_not_sent_and_pool_nonempty(self):
        reply = {"action": "respond", "messages": ["Tómate tu tiempo 🤍"], "send_event_photo": True}
        lead = _make_lead(phone="wa_5215500000010")
        p_settings, p_sent, p_pool = self._patches(already_sent=False, pool_photo=[{"storage_url": "u"}])
        with p_settings, p_sent, p_pool:
            await ai._maybe_announce_event_photo(reply, _make_scenario(id=2), lead)
        assert reply["photo_caption"] == ai._EVENT_PHOTO_ANNOUNCE

    async def test_no_caption_when_already_sent(self):
        reply = {"action": "respond", "messages": ["Tómate tu tiempo 🤍"], "send_event_photo": True}
        lead = _make_lead(phone="wa_5215500000011")
        p_settings, p_sent, p_pool = self._patches(already_sent=True, pool_photo=[{"storage_url": "u"}])
        with p_settings, p_sent, p_pool:
            await ai._maybe_announce_event_photo(reply, _make_scenario(id=2), lead)
        assert "photo_caption" not in reply

    async def test_no_caption_when_pool_empty(self):
        reply = {"action": "respond", "messages": ["Tómate tu tiempo 🤍"], "send_event_photo": True}
        lead = _make_lead(phone="wa_5215500000012")
        p_settings, p_sent, p_pool = self._patches(already_sent=False, pool_photo=[])
        with p_settings, p_sent, p_pool:
            await ai._maybe_announce_event_photo(reply, _make_scenario(id=2), lead)
        assert "photo_caption" not in reply

    async def test_no_caption_when_flag_false(self):
        reply = {"action": "respond", "messages": ["ok"], "send_event_photo": False}
        await ai._maybe_announce_event_photo(reply, _make_scenario(id=2), _make_lead())
        assert "photo_caption" not in reply

    async def test_no_caption_when_block(self):
        reply = {"action": "block", "messages": ["ok"], "send_event_photo": True}
        await ai._maybe_announce_event_photo(reply, _make_scenario(id=2), _make_lead())
        assert "photo_caption" not in reply


class TestGenerateReplyAI:
    """Ветка 2: ai_allowed=True (или нет уверенного матча) → OpenAI вызывается."""

    async def test_ai_branch_called_when_ai_allowed(self, lead, history):
        """ai_allowed=True → _call_openai вызван, результат провалидирован."""
        scenario = _make_scenario(id=1, ai_allowed=True, score=0.68)
        mock_openai = AsyncMock(return_value=_VALID_AI_RESPONSE)
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", mock_openai):
            result = await ai.generate_reply(lead, history, "cuéntame más")

        mock_openai.assert_awaited_once()
        assert result["action"] == "respond"
        assert result["messages"] == ["Hola, guapo!"]
        assert result["extracted"] == {"age": 40}

    async def test_low_score_goes_to_openai_even_if_not_ai_allowed(self, lead, history):
        """score < FALLBACK_SCORE при ai_allowed=False → НЕ fixed, идёт в OpenAI."""
        scenario = _make_scenario(id=3, ai_allowed=False, score=0.30)
        mock_openai = AsyncMock(return_value=_VALID_AI_RESPONSE)
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", mock_openai):
            result = await ai.generate_reply(lead, history, "algo")

        mock_openai.assert_awaited_once()
        assert result["action"] == "respond"

    async def test_ai_response_validated(self, lead, history):
        """_validate_output применяется: лишние extracted-поля отбрасываются."""
        ai_response = {
            "messages": ["Hola!"],
            "action": "respond",
            "funnel_stage": "qualifying",
            "extracted": {"age": 35, "unwanted_key": "trash"},
            "needs_escalation": False,
            "used_scenario_id": None,
        }
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "texto")

        assert "unwanted_key" not in result["extracted"]
        assert result["extracted"].get("age") == 35


class TestForceEscalateUsesUsedScenario:
    """Форс-эскалация bot_then_anna должна смотреть на used_scenario_id (реальный выбор
    LLM), а не на top (топ RAG-рейтинга) — регресс: №15 (event-only, bot_then_anna) не
    эскалировался, когда top по эмбеддингу был другой сценарий с mode=bot_auto."""

    async def test_llm_picks_non_top_bot_then_anna_scenario(self, lead, history):
        top_scenario = _make_scenario(id=2, mode="bot_auto", ai_allowed=True, score=0.66)
        handoff_scenario = _make_scenario(id=15, mode="bot_then_anna", ai_allowed=True,
                                          score=0.61)
        ai_response = {**_VALID_AI_RESPONSE, "action": "respond", "needs_escalation": False,
                       "used_scenario_id": 15}
        with patch("ai.search_scenarios",
                   new=AsyncMock(return_value=[top_scenario, handoff_scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "quiero ir al evento")

        assert result["action"] == "escalate"
        assert result["needs_escalation"] is True
        assert result["used_scenario_id"] == 15

    async def test_top_bot_auto_no_force_when_llm_stays_on_it(self, lead, history):
        """Без баг-кейса: LLM использует top (bot_auto) → эскалация НЕ форсится."""
        top_scenario = _make_scenario(id=2, mode="bot_auto", ai_allowed=True, score=0.66)
        ai_response = {**_VALID_AI_RESPONSE, "action": "respond", "needs_escalation": False,
                       "used_scenario_id": 2}
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[top_scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "hola")

        assert result["action"] == "respond"
        assert result["needs_escalation"] is False


class TestGenerateReplyFallback:
    """Ветки 4 и 5: сбои → fallback, никогда не бросает."""

    async def test_openai_exception_returns_fallback(self, lead, history):
        """_call_openai падает → generate_reply возвращает _fallback_reply, не бросает."""
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(side_effect=Exception("OpenAI timeout"))):
            result = await ai.generate_reply(lead, history, "texto")

        assert result["action"] == "escalate"
        assert result["needs_escalation"] is True
        assert result["messages"] == ["Ahorita te contesto 🤍"]
        assert result["used_scenario_id"] is None

    async def test_rag_exception_goes_to_openai(self, lead, history):
        """search_scenarios падает → идёт в OpenAI без сценариев, _call_openai вызван."""
        mock_openai = AsyncMock(return_value=_VALID_AI_RESPONSE)
        with patch("ai.search_scenarios", AsyncMock(side_effect=Exception("DB down"))), \
             patch("ai._call_openai", mock_openai):
            result = await ai.generate_reply(lead, history, "texto")

        mock_openai.assert_awaited_once()
        # контекст передан без сценариев — функция не упала
        assert result["action"] == "respond"

    async def test_invalid_openai_response_falls_back(self, lead, history):
        """_call_openai вернул невалидный ответ (messages пустой) → fallback."""
        bad_response = {"messages": [], "action": "respond"}
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(return_value=bad_response)):
            result = await ai.generate_reply(lead, history, "texto")

        assert result["action"] == "escalate"
        assert result["messages"] == ["Ahorita te contesto 🤍"]

    async def test_generate_reply_never_raises(self, lead, history):
        """При любых сбоях generate_reply НЕ бросает исключение."""
        with patch("ai.search_scenarios", AsyncMock(side_effect=RuntimeError("chaos"))), \
             patch("ai._call_openai", AsyncMock(side_effect=RuntimeError("more chaos"))):
            try:
                result = await ai.generate_reply(lead, history, "")
            except Exception as e:
                pytest.fail(f"generate_reply бросил исключение: {e}")
            assert result is not None


class TestGenerateReplyExtracted:
    """Проверка что extracted не выдумывается."""

    async def test_extracted_none_values_filtered(self, lead, history):
        """AI вернул None-значения в extracted → в результате их нет."""
        ai_response = {
            "messages": ["Hola!"],
            "action": "respond",
            "funnel_stage": "qualifying",
            "extracted": {"age": None, "profession": "médico", "city": None, "is_single": True},
            "needs_escalation": False,
        }
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "texto")

        assert "age" not in result["extracted"]
        assert "city" not in result["extracted"]
        assert result["extracted"]["profession"] == "médico"
        assert result["extracted"]["is_single"] is True

    async def test_extracted_unknown_keys_dropped(self, lead, history):
        """AI выдумал ключи — они выброшены, только известные остаются."""
        ai_response = {
            "messages": ["Hola!"],
            "action": "respond",
            "extracted": {
                "age": 45,
                "income": "alto",
                "marital_status": "casado",
                "interest": "seria",
            },
        }
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "texto")

        extracted = result["extracted"]
        assert "income" not in extracted
        assert "marital_status" not in extracted
        assert extracted.get("age") == 45
        assert extracted.get("interest") == "seria"


class TestGenerateReplyNoneLeadInput:
    """Граничный случай: None вместо lead."""

    async def test_none_lead_doesnt_crash(self):
        """lead=None обрабатывается корректно (заменяется на {})."""
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(return_value=_VALID_AI_RESPONSE)):
            result = await ai.generate_reply(None, [], "hola")
        assert result is not None


class TestFunnelStageValidation:
    """funnel_stage от AI валидируется против funnel.FUNNEL_STAGES (защита set_funnel_stage)."""

    def test_valid_stage_passes(self):
        r = ai._validate_output({"messages": ["x"], "action": "respond", "funnel_stage": "qualifying"})
        assert r["funnel_stage"] == "qualifying"

    def test_client_stage_passes(self):
        r = ai._validate_output({"messages": ["x"], "action": "respond", "funnel_stage": "client_agency"})
        assert r["funnel_stage"] == "client_agency"

    def test_invented_stage_becomes_none(self):
        r = ai._validate_output({"messages": ["x"], "action": "respond", "funnel_stage": "client_active"})
        assert r["funnel_stage"] is None

    def test_none_stage_stays_none(self):
        r = ai._validate_output({"messages": ["x"], "action": "respond", "funnel_stage": None})
        assert r["funnel_stage"] is None


class TestDualThreshold:
    """Двойной порог: блокировки требуют score>=0.60, обычный фикс — >=0.45."""

    async def test_block_scenario_below_060_goes_to_ai(self, monkeypatch):
        # блокирующий фикс с 0.50 (в зоне 0.45-0.60) → НЕ fixed, идёт в AI
        monkeypatch.setattr(ai, "search_scenarios", AsyncMock(return_value=[
            {"id": 9, "ai_allowed": False, "score": 0.50, "mode": "bot_then_block",
             "blocks_lead": True, "template_es": "bloqueo"}]))
        call = AsyncMock(return_value={"messages": ["respuesta ai"], "action": "respond"})
        monkeypatch.setattr(ai, "_call_openai", call)
        r = await ai.generate_reply({}, [], "pregunta ambigua")
        call.assert_awaited_once()
        assert r["messages"] == ["respuesta ai"]

    async def test_block_scenario_above_060_is_fixed(self, monkeypatch):
        # блокирующий фикс с 0.65 (>=0.60) → fixed, OpenAI НЕ вызван
        monkeypatch.setattr(ai, "search_scenarios", AsyncMock(return_value=[
            {"id": 7, "ai_allowed": False, "score": 0.65, "mode": "bot_then_block",
             "blocks_lead": True, "template_es": "bloqueo directo"}]))
        call = AsyncMock()
        monkeypatch.setattr(ai, "_call_openai", call)
        r = await ai.generate_reply({}, [], "tengo 24")
        call.assert_not_awaited()
        assert r["action"] == "block"
        assert r["used_scenario_id"] == 7

    async def test_nonblock_fixed_at_050_is_fixed(self, monkeypatch):
        # НЕ-блокирующий фикс (скидка) с 0.50 (>=0.45) → fixed без OpenAI
        monkeypatch.setattr(ai, "search_scenarios", AsyncMock(return_value=[
            {"id": 39, "ai_allowed": False, "score": 0.50, "mode": "bot_auto",
             "blocks_lead": False, "template_es": "no hay descuentos"}]))
        call = AsyncMock()
        monkeypatch.setattr(ai, "_call_openai", call)
        r = await ai.generate_reply({}, [], "descuento?")
        call.assert_not_awaited()
        assert r["used_scenario_id"] == 39
        assert r["action"] == "respond"

    async def test_nonblock_fixed_below_045_goes_to_ai(self, monkeypatch):
        # НЕ-блок фикс с 0.42 (<0.45) → в AI
        monkeypatch.setattr(ai, "search_scenarios", AsyncMock(return_value=[
            {"id": 40, "ai_allowed": False, "score": 0.42, "mode": "bot_auto",
             "blocks_lead": False, "template_es": "soy anna"}]))
        call = AsyncMock(return_value={"messages": ["ai resp"], "action": "respond"})
        monkeypatch.setattr(ai, "_call_openai", call)
        await ai.generate_reply({}, [], "algo")
        call.assert_awaited_once()


# ---------------------------------------------------------------------------
# _openai_post — ретраи на 429/5xx/сеть (блок 12, надёжность)
# ---------------------------------------------------------------------------

import httpx


def _resp(status=200, json_data=None, headers=None):
    """Заглушка httpx.Response: status_code, headers, json(), raise_for_status()."""
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = json_data if json_data is not None else {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def _client_factory(seq):
    """Фабрика AsyncClient-заглушки: post() отдаёт элементы seq по порядку через
    все переинстансы клиента (у _openai_post новый клиент на каждую попытку)."""
    shared = {"i": 0, "seq": list(seq)}

    class _C:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            item = shared["seq"][shared["i"]]
            shared["i"] += 1
            if isinstance(item, Exception):
                raise item
            return item

    _C.shared = shared
    return _C


class TestOpenAIRetry:
    async def test_success_first_try_no_sleep(self):
        cls = _client_factory([_resp(200, {"ok": 1})])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            r = await ai._openai_post("u", {}, 5)
        assert r.json() == {"ok": 1}
        sleep.assert_not_awaited()

    async def test_retries_on_429_then_succeeds(self):
        cls = _client_factory([_resp(429), _resp(200, {"ok": 2})])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            r = await ai._openai_post("u", {}, 5)
        assert r.json() == {"ok": 2}
        sleep.assert_awaited_once()

    async def test_gives_up_after_max_retries(self):
        # MAX_RETRIES+1 попыток → все 429 → пробрасывает HTTPStatusError
        cls = _client_factory([_resp(429)] * (ai.OPENAI_MAX_RETRIES + 1))
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            with pytest.raises(httpx.HTTPStatusError):
                await ai._openai_post("u", {}, 5)
        assert sleep.await_count == ai.OPENAI_MAX_RETRIES

    async def test_retry_after_header_respected(self):
        cls = _client_factory([_resp(429, headers={"retry-after": "2"}), _resp(200)])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            await ai._openai_post("u", {}, 5)
        assert sleep.await_args.args[0] == 2.0

    async def test_network_error_retried(self):
        cls = _client_factory([httpx.ConnectError("boom"), _resp(200, {"ok": 3})])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            r = await ai._openai_post("u", {}, 5)
        assert r.json() == {"ok": 3}
        sleep.assert_awaited_once()

    async def test_5xx_retried(self):
        cls = _client_factory([_resp(503), _resp(200, {"ok": 4})])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            r = await ai._openai_post("u", {}, 5)
        assert r.json() == {"ok": 4}

    async def test_retry_after_capped(self):
        """Огромный Retry-After обрезается до OPENAI_MAX_RETRY_AFTER (не sleep(3600))."""
        cls = _client_factory([_resp(429, headers={"retry-after": "3600"}), _resp(200)])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            await ai._openai_post("u", {}, 5)
        assert sleep.await_args.args[0] == ai.OPENAI_MAX_RETRY_AFTER

    async def test_retry_after_negative_ignored(self):
        """Отрицательный/нулевой Retry-After игнорируется → обычный backoff."""
        cls = _client_factory([_resp(429, headers={"retry-after": "-5"}), _resp(200)])
        sleep = AsyncMock()
        with patch("ai.httpx.AsyncClient", cls), patch("ai.asyncio.sleep", sleep):
            await ai._openai_post("u", {}, 5)
        assert sleep.await_args.args[0] == ai._backoff(0)


class TestSendInvitationFlag:
    def test_passthrough_true(self):
        out = ai._validate_output({"messages": ["hola"], "action": "respond",
                                   "send_invitation": True})
        assert out["send_invitation"] is True

    def test_default_false(self):
        out = ai._validate_output({"messages": ["hola"], "action": "respond"})
        assert out["send_invitation"] is False


class TestEnforceLinkPresence:
    """Guardrail: сценарий деталей ивента (№51/№52) без [event_link] в ответе AI →
    довешивается отдельным бабблом (регресс 2026-08-26 — AI терял ссылку)."""

    async def test_appends_link_when_missing(self, lead, history):
        scenario = _make_scenario(id=51, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE, "messages": ["El precio es 6000 MXN."],
                       "used_scenario_id": 51}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, _QUALIFIED_HISTORY, "cuanto cuesta el evento")
        assert any("[event_link]" in m for m in result["messages"])
        assert result["messages"][0] == "El precio es 6000 MXN."

    async def test_no_duplicate_when_link_present(self, lead, history):
        scenario = _make_scenario(id=51, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE,
                       "messages": ["Detalles del evento.", "Aquí tu boleto: [event_link] 🤍"],
                       "used_scenario_id": 51}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, _QUALIFIED_HISTORY, "cuanto cuesta el evento")
        assert result["messages"] == ["Detalles del evento.", "Aquí tu boleto: [event_link] 🤍"]

    async def test_no_duplicate_when_resolved_url_present(self, lead, history):
        """sender.py подставляет [event_link] в реальный URL ДО записи в историю — если
        AI естественно повторяет уже resolved-ссылку из контекста (не плейсхолдер), не
        считаем это "ссылки нет" и не довешиваем дубликат (найдено 2026-09-01, eval r5)."""
        scenario = _make_scenario(id=51, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE,
                       "messages": ["Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍"],
                       "used_scenario_id": 51}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, _QUALIFIED_HISTORY, "cuanto cuesta el evento")
        assert result["messages"] == ["Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍"]

    async def test_noop_for_unrelated_scenario(self, lead, history):
        """used_scenario_id no в {51,52} — гейт не трогает ответ вообще."""
        scenario = _make_scenario(id=16, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE, "messages": ["La inversión es $10,000 USD."],
                       "used_scenario_id": 16}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "cuanto cuesta el servicio")
        assert result["messages"] == ["La inversión es $10,000 USD."]

    async def test_replaces_last_bubble_at_max_messages(self, lead, history):
        """Уже MAX_MESSAGES бабблов без ссылки → заменяем последний, не превышаем лимит."""
        scenario = _make_scenario(id=51, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE, "messages": ["uno", "dos", "tres", "cuatro"],
                       "used_scenario_id": 51}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, _QUALIFIED_HISTORY, "cuanto cuesta el evento")
        assert len(result["messages"]) == ai.MAX_MESSAGES
        assert result["messages"][:3] == ["uno", "dos", "tres"]
        assert "[event_link]" in result["messages"][-1]

    def test_direct_noop_when_action_not_respond(self):
        """action != respond (напр. escalate) — не довешиваем ссылку, не наше дело."""
        used = _make_scenario(id=51)
        result = {"action": "escalate", "messages": ["ok"]}
        out = ai._enforce_link_presence(result, used)
        assert out["messages"] == ["ok"]


class TestEnforceNoEventQualificationGate:
    """Сценарии-ивента (#2/#15/#51/#52) не заменяют цену/ссылку редундантным
    вопросом-разрешением ("¿te gustaría que te mande todos los detalles?") — но
    ЛЕГИТИМНЫЙ вопрос "eres soltero?/qué edad tienes?" НЕ трогаем (испр. 2026-09-12:
    старая версия ошибочно гейтила и его тоже — дословный фидбек владелицы "это ок
    пусть спрашивает" был именно про soltero/edad, жалоба была только на редундантное
    "¿Te gustaría que te mande todos los detalles?")."""

    @pytest.mark.parametrize("scenario_id", [2, 15, 51, 52])
    def test_replaces_redundant_permission_without_content(self, scenario_id):
        used = _make_scenario(id=scenario_id)
        result = {"action": "respond",
                  "messages": ["¡Justo te iba a contar! ¿Te gustaría que te mande todos los detalles?"]}
        out = ai._enforce_no_event_qualification_gate(result, used)
        text = " ".join(out["messages"])
        assert "[event_link]" in text
        assert "mxn" in text.lower() or "MXN" in text

    def test_noop_when_content_already_present(self):
        """Модель спросила разрешение, НО заодно уже дала цену/ссылку — не блокирующе,
        не трогаем (избегаем ложных срабатываний)."""
        used = _make_scenario(id=51)
        result = {"action": "respond", "messages": [
            "El precio es 6000 MXN, aquí tu boleto: [event_link]. ¿Te gustaría que te cuente más?"]}
        out = ai._enforce_no_event_qualification_gate(result, used)
        assert out["messages"] == result["messages"]

    def test_noop_when_no_gate_phrase(self):
        used = _make_scenario(id=51)
        result = {"action": "respond", "messages": ["Con gusto te cuento del evento."]}
        out = ai._enforce_no_event_qualification_gate(result, used)
        assert out["messages"] == result["messages"]

    @pytest.mark.parametrize("scenario_id", [2, 15, 51, 52])
    def test_noop_on_legitimate_soltero_edad_question(self, scenario_id):
        """El propio "¿eres soltero?/qué edad tienes?" es LEGÍTIMO incluso para el
        embudo de evento — no es el patrón que este guardrail corrige, se deja pasar."""
        used = _make_scenario(id=scenario_id)
        result = {"action": "respond",
                  "messages": ["¡Perfecto! Antes de contarte más, ¿eres soltero? ¿Qué edad tienes?"]}
        out = ai._enforce_no_event_qualification_gate(result, used)
        assert out["messages"] == result["messages"]

    def test_noop_for_unrelated_scenario(self):
        used = _make_scenario(id=16)
        result = {"action": "respond", "messages": ["¿Te gustaría que te cuente los detalles?"]}
        out = ai._enforce_no_event_qualification_gate(result, used)
        assert out["messages"] == result["messages"]

    def test_noop_when_action_not_respond(self):
        used = _make_scenario(id=51)
        result = {"action": "escalate", "messages": ["¿Te gustaría que te mande los detalles?"]}
        out = ai._enforce_no_event_qualification_gate(result, used)
        assert out["messages"] == result["messages"]

    def test_noop_when_used_none(self):
        result = {"action": "respond", "messages": ["¿Te gustaría que te mande los detalles?"]}
        out = ai._enforce_no_event_qualification_gate(result, None)
        assert out["messages"] == result["messages"]


class TestEnforceEventQualificationGate:
    """(2026-09-12, revierte la decisión previa "sin gate") Precio/detalles del
    evento (#2/#15/#51/#52) no se dan sin haber preguntado ANTES soltero/edad al
    menos una vez en la conversación — pero solo la primera vez, no repetir."""

    @pytest.mark.parametrize("scenario_id", [2, 15, 51, 52])
    def test_replaces_content_with_question_first_time(self, scenario_id):
        used = _make_scenario(id=scenario_id)
        result = {"action": "respond",
                  "messages": ["El precio es 6,000 MXN, aquí tu boleto: [event_link]"],
                  "send_event_photo": True, "send_event_video": True}
        history = [{"sender": "lead", "text": "evento"}]
        out = ai._enforce_event_qualification_gate(result, used, history)
        text = " ".join(out["messages"])
        assert "soltero" in text.lower()
        assert "[event_link]" not in text
        assert out["send_event_photo"] is False
        assert out["send_event_video"] is False

    def test_noop_when_already_asked_before(self):
        """Ya se preguntó soltero/edad en un mensaje anterior de Anna — no repetir,
        aunque la respuesta del lead ("si") no se haya parseado a is_single/age
        (regresión documentada que motivó quitar el gate en 2026-09-09)."""
        used = _make_scenario(id=2)
        result = {"action": "respond",
                  "messages": ["El precio es 6,000 MXN, aquí tu boleto: [event_link]"]}
        history = [
            {"sender": "lead", "text": "evento"},
            {"sender": "anna", "text": "¡Perfecto! ¿Eres soltero? ¿Qué edad tienes?"},
            {"sender": "lead", "text": "si"},
        ]
        out = ai._enforce_event_qualification_gate(result, used, history)
        assert out["messages"] == result["messages"]

    def test_noop_when_no_content_yet(self):
        used = _make_scenario(id=2)
        result = {"action": "respond", "messages": ["¡Qué bueno que te interesa el evento!"]}
        out = ai._enforce_event_qualification_gate(result, used, [])
        assert out["messages"] == result["messages"]

    def test_noop_for_unrelated_scenario(self):
        used = _make_scenario(id=16)
        result = {"action": "respond", "messages": ["La inversión es de 6,000 MXN: [event_link]"]}
        out = ai._enforce_event_qualification_gate(result, used, [])
        assert out["messages"] == result["messages"]

    def test_noop_when_action_not_respond(self):
        used = _make_scenario(id=2)
        result = {"action": "escalate", "messages": ["El precio es 6,000 MXN: [event_link]"]}
        out = ai._enforce_event_qualification_gate(result, used, [])
        assert out["messages"] == result["messages"]

    def test_noop_when_used_none(self):
        result = {"action": "respond", "messages": ["El precio es 6,000 MXN: [event_link]"]}
        out = ai._enforce_event_qualification_gate(result, None, [])
        assert out["messages"] == result["messages"]


class TestEnforceEventQualificationFollowup:
    """Embudo de 2 pasos del evento: soltero/edad/profesión → pide foto del lead →
    (foto aprobada) → pitch. Reconoce el paso por el bubble EXACTO del turno anterior
    de Anna, no por a qué escenario matcheó este turno (encontrado 2026-09-12: la
    respuesta del lead a menudo matchea #4 genérico, que sigue el guion de SERVICIO)."""

    async def test_step1_to_2_asks_for_photo(self):
        """Turno anterior fue la pregunta soltero/edad/profesión → pedir foto, sin
        dar precio todavía (aunque el escenario matcheado sí lo hubiera dado)."""
        result = {"action": "respond", "messages": ["Va, gracias", "¿A qué te dedicas?"]}
        history = [
            {"sender": "lead", "text": "evento"},
            {"sender": "anna", "text": ai._EVENT_QUALIFY_BUBBLE},
        ]
        out = await ai._enforce_event_qualification_followup(
            result, history, _make_scenario(id=2), _make_lead(), "si soltero, 30, ingeniero")
        assert out["messages"] == [ai._EVENT_PHOTO_REQUEST_BUBBLE]
        assert out["send_event_photo"] is False
        assert out["send_event_video"] is False

    async def test_step1_noop_when_content_already_given(self):
        result = {"action": "respond", "messages": ["Va! El precio es 6,000 MXN: [event_link]"]}
        history = [{"sender": "anna", "text": ai._EVENT_QUALIFY_BUBBLE}]
        out = await ai._enforce_event_qualification_followup(
            result, history, _make_scenario(id=2), _make_lead(), "si soltero, 30")
        assert out["messages"] == result["messages"]

    async def test_recognizes_bubble_with_emoji_stripped(self):
        """Regresión 2026-09-13: _enforce_emoji_budget puede recortar el emoji de un
        bubble ANTES de que quede guardado en el historial (si la conversación ya
        viene cargada de emoji) — una comparación exacta con el emoji incluido en la
        constante dejaba de reconocer su propio bubble (_EVENT_PHOTO_REQUEST_BUBBLE
        termina en 😊) en el turno siguiente, y el embudo se quedaba atorado."""
        result = {"action": "respond", "messages": ["ok"]}
        stripped = ai._EVENT_PHOTO_REQUEST_BUBBLE.replace("😊", "").strip()
        assert stripped != ai._EVENT_PHOTO_REQUEST_BUBBLE  # confirma que el emoji SÍ se quitó
        history = [{"sender": "anna", "text": stripped}]
        used = _make_scenario(id=2)
        lead = _make_lead(phone="wa_5215500000098")
        with patch("ai.db.get_settings", AsyncMock(return_value={"event_date": "2026-08-15"})), \
             patch("ai.db.event_media_sent", AsyncMock(return_value=False)), \
             patch("ai.db.random_event_media", AsyncMock(return_value=[{"storage_url": "u"}])):
            out = await ai._enforce_event_qualification_followup(
                result, history, used, lead, "[фото одобрено]")
        assert "[event_link]" in " ".join(out["messages"])

    async def test_step2_without_photo_approved_is_noop(self):
        """Turno anterior fue pedir la foto, pero este turno NO trae "[фото одобрено]"
        (el lead no mandó foto o aún no pasó el filtro) — dejamos que el AI maneje
        este turno normalmente, no forzamos nada."""
        result = {"action": "respond", "messages": ["Mándamela cuando puedas 😊"]}
        history = [{"sender": "anna", "text": ai._EVENT_PHOTO_REQUEST_BUBBLE}]
        out = await ai._enforce_event_qualification_followup(
            result, history, _make_scenario(id=2), _make_lead(), "ahorita no tengo una a la mano")
        assert out["messages"] == result["messages"]

    async def test_step2_to_pitch_after_photo_approved(self):
        result = {"action": "respond", "messages": ["Ok"]}
        history = [{"sender": "anna", "text": ai._EVENT_PHOTO_REQUEST_BUBBLE}]
        used = _make_scenario(id=2)
        lead = _make_lead(phone="wa_5215500000099")
        with patch("ai.db.get_settings", AsyncMock(return_value={"event_date": "2026-08-15"})), \
             patch("ai.db.event_media_sent", AsyncMock(return_value=False)), \
             patch("ai.db.random_event_media", AsyncMock(return_value=[{"storage_url": "u"}])):
            out = await ai._enforce_event_qualification_followup(
                result, history, used, lead, "[фото одобрено]")
        text = " ".join(out["messages"])
        assert "[event_link]" in text
        assert "mxn" in text.lower() or "MXN" in text
        assert out["send_event_video"] is True

    async def test_step2_noop_when_content_already_given(self):
        result = {"action": "respond", "messages": ["Va! El precio es 6,000 MXN: [event_link]"]}
        history = [{"sender": "anna", "text": ai._EVENT_PHOTO_REQUEST_BUBBLE}]
        out = await ai._enforce_event_qualification_followup(
            result, history, _make_scenario(id=2), _make_lead(), "[фото одобрено]")
        assert out["messages"] == result["messages"]

    async def test_noop_when_last_anna_message_is_different(self):
        """El último mensaje de Anna no fue ninguno de los bubbles fijos del evento
        (p.ej. pregunta soltero del flujo de SERVICIO) — no es nuestro caso, no tocamos."""
        result = {"action": "respond", "messages": ["¿A qué te dedicas?"]}
        history = [{"sender": "anna", "text": "¿Eres soltero? ¿Qué edad tienes?"}]
        out = await ai._enforce_event_qualification_followup(
            result, history, _make_scenario(id=2), _make_lead(), "si")
        assert out["messages"] == result["messages"]

    async def test_noop_when_action_not_respond(self):
        result = {"action": "escalate", "messages": ["¿A qué te dedicas?"]}
        history = [{"sender": "anna", "text": ai._EVENT_QUALIFY_BUBBLE}]
        out = await ai._enforce_event_qualification_followup(
            result, history, _make_scenario(id=2), _make_lead(), "si, 30")
        assert out["messages"] == result["messages"]

    async def test_does_not_crash_when_used_is_none(self):
        """Regresión 2026-09-12: la respuesta del lead a la pregunta pendiente a veces
        no matchea NINGÚN escenario confiable (used=None, p.ej. RAG ambiguo) — antes
        esto crasheaba en _maybe_announce_event_video (scenario.get en None)."""
        result = {"action": "respond", "messages": ["Ok"]}
        history = [{"sender": "anna", "text": ai._EVENT_PHOTO_REQUEST_BUBBLE}]
        with patch("ai.db.get_settings", AsyncMock(return_value={"event_date": "2026-08-15"})), \
             patch("ai.db.event_media_sent", AsyncMock(return_value=False)), \
             patch("ai.db.random_event_media", AsyncMock(return_value=[{"storage_url": "u"}])):
            out = await ai._enforce_event_qualification_followup(
                result, history, None, _make_lead(), "[фото одобрено]")
        assert "[event_link]" in " ".join(out["messages"])
        assert out["send_event_video"] is True


class TestTagEventInterest:
    """extracted.interest='event' фиксируется единой пост-генерационной точкой для
    сценариев деталей ивента (№51/№52) — что для фикс-, что для AI-ветки."""

    def test_tags_interest_for_event_detail_scenario(self):
        used = _make_scenario(id=51)
        result = {"extracted": {"age": 30}}
        out = ai._tag_event_interest(result, used)
        assert out["extracted"] == {"age": 30, "interest": "event"}

    def test_noop_for_unrelated_scenario(self):
        used = _make_scenario(id=16)
        result = {"extracted": {"age": 30}}
        out = ai._tag_event_interest(result, used)
        assert out["extracted"] == {"age": 30}

    def test_noop_when_used_none(self):
        result = {"extracted": {}}
        out = ai._tag_event_interest(result, None)
        assert out["extracted"] == {}

    def test_prior_agency_interest_upgraded_to_both(self):
        """Лид раньше интересовался сервисом (interest='agency'), теперь заодно матчит
        сценарий-ивент — не стираем agency-интерес, повышаем до 'both' (найдено
        2026-09-03: слепое "= event" ломало event_recipients-таргетинг и путало, что
        лиду предлагать дальше)."""
        used = _make_scenario(id=51)
        result = {"extracted": {}}
        out = ai._tag_event_interest(result, used, lead={"interest": "agency"})
        assert out["extracted"] == {"interest": "both"}

    def test_prior_both_interest_not_downgraded(self):
        used = _make_scenario(id=51)
        result = {"extracted": {}}
        out = ai._tag_event_interest(result, used, lead={"interest": "both"})
        assert "interest" not in out["extracted"]

    def test_weak_single_candidate_does_not_tag_interest(self):
        """score < FALLBACK_SCORE (одиночный слабый RAG-матч) → не форсим interest —
        RAG тут явно не уверен (регресс-тест на guard по абсолютному score, найдено
        2026-09-03: ambiguous-проверка не ловит этот случай, она смотрит на разрыв
        топ-1/топ-2, а не на абсолютную уверенность единственного кандидата)."""
        used = _make_scenario(id=2, score=0.10)  # ниже FALLBACK_SCORE=0.40
        result = {"extracted": {}}
        out = ai._tag_event_interest(result, used, lead={"interest": None})
        assert "interest" not in out["extracted"]

    async def test_fixed_branch_tags_interest(self, lead, history):
        """№51 через детерминированную ветку (ai_allowed=false) → interest='event'
        всё равно проставляется, хотя OpenAI не вызывался."""
        scenario = _make_scenario(id=51, ai_allowed=False, score=0.70,
                                   template_es="Precio del evento.")
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock()) as mock_openai:
            result = await ai.generate_reply(lead, history, "cuanto cuesta el evento")
        mock_openai.assert_not_awaited()
        assert result["extracted"] == {"interest": "event"}

    async def test_scenario_2_tags_interest(self, history):
        """Регресс найден 2026-09-01 (живой тест в Telegram): "info del evento" матчит
        №2 (натуральный RAG-топ, не 51/52) — interest должен сохраниться, иначе форс
        "фото одобрено + interest=event" ниже не срабатывает и после фото уходит
        дефолтный питч сервиса без единого упоминания ивента."""
        lead = _make_lead(interest=None)
        scenario = _make_scenario(id=2, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE, "used_scenario_id": 2}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "hola, información de evento")
        assert result["extracted"]["interest"] == "event"


class TestMergeInterest:
    """_merge_interest — LLM может положить interest в extracted САМА (промпт это
    разрешает), в обход _tag_event_interest целиком. Тот же риск потери agency/both,
    что и у _event_interest_upgrade, но достижимый другим путём (найдено 2026-09-03,
    code-review). Единая пост-генерационная нормализация в generate_reply."""

    def test_no_new_value_returns_none(self):
        assert ai._merge_interest(None, "agency") is None

    def test_same_value_passthrough(self):
        assert ai._merge_interest("event", "event") == "event"

    def test_no_prior_takes_new(self):
        assert ai._merge_interest("event", None) == "event"

    def test_prior_both_never_narrowed(self):
        assert ai._merge_interest("event", "both") == "both"
        assert ai._merge_interest("agency", "both") == "both"

    def test_opposite_values_merge_to_both(self):
        assert ai._merge_interest("event", "agency") == "both"
        assert ai._merge_interest("agency", "event") == "both"

    async def test_llm_sets_interest_directly_does_not_erase_agency(self, history):
        """LLM сама вернула interest='event' в JSON (не через сценарий-ивент — top
        здесь #1, обычное приветствие, не в _EVENT_INTEREST_SCENARIOS) — lead.interest
        уже 'agency' → должно смерджиться в 'both', не затереться на голый 'event'."""
        lead = _make_lead(is_single=True, funnel_stage="qualifying", interest="agency")
        n1 = _make_scenario(id=1, ai_allowed=True, score=0.75)
        ai_response = {**_VALID_AI_RESPONSE, "extracted": {"interest": "event"}}
        with patch("ai.search_scenarios", AsyncMock(return_value=[n1])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, [], "hola")
        assert result["extracted"]["interest"] == "both"


class TestEnforceNurtureStage:
    """funnel_stage='nurture' для #10 (bajo ingreso) / #17 (no me interesa) — единая
    пост-генерационная точка, не полагаемся только на промпт (регресс 2026-08-06)."""

    def test_tags_nurture_for_scenario_10(self):
        used = _make_scenario(id=10)
        result = {"action": "respond", "funnel_stage": "qualifying"}
        out = ai._enforce_nurture_stage(result, used)
        assert out["funnel_stage"] == "nurture"

    def test_tags_nurture_for_scenario_17(self):
        used = _make_scenario(id=17)
        result = {"action": "respond", "funnel_stage": None}
        out = ai._enforce_nurture_stage(result, used)
        assert out["funnel_stage"] == "nurture"

    def test_noop_for_unrelated_scenario(self):
        used = _make_scenario(id=16)
        result = {"action": "respond", "funnel_stage": "qualifying"}
        out = ai._enforce_nurture_stage(result, used)
        assert out["funnel_stage"] == "qualifying"

    def test_noop_when_action_not_respond(self):
        used = _make_scenario(id=10)
        result = {"action": "block", "funnel_stage": "qualifying"}
        out = ai._enforce_nurture_stage(result, used)
        assert out["funnel_stage"] == "qualifying"

    def test_noop_when_ambiguous(self):
        """Найдено 2026-09-01 (smoke-test): "no me alcanza, mejor lo dejamos" после
        защиты цены сервиса матчил #17 с ambiguous score — по бизнес-правилу должен
        вести по лестнице сервис→ивент, не форсить nurture по шаткому RAG-топу."""
        used = _make_scenario(id=17)
        result = {"action": "respond", "funnel_stage": "pitched"}
        out = ai._enforce_nurture_stage(result, used, ambiguous=True)
        assert out["funnel_stage"] == "pitched"

    async def test_fixed_branch_sets_nurture(self, lead, history):
        """№10 через детерминированную ветку (ai_allowed=false) → nurture всё равно
        проставляется, хотя OpenAI не вызывался."""
        scenario = _make_scenario(id=10, ai_allowed=False, score=0.70,
                                   template_es="Lista de espera 6-12 meses.")
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock()) as mock_openai:
            result = await ai.generate_reply(lead, history, "trabajo de mesero")
        mock_openai.assert_not_awaited()
        assert result["funnel_stage"] == "nurture"


class TestEnforceServiceQualificationGate:
    """Питч сервиса не должен уходить с неполной анкетой (is_single/age/profession) —
    регресс найден 2026-09-01 (живой тест): лид пропустил вопрос про профессию, сразу
    прислал фото, бот всё равно дал полный питч. Гейт привязан к user_text=="[фото
    одобрено]" (не к used_scenario_id — AI не всегда его репортит). Только сервис —
    ивент (interest='event') НЕ гейтится (владелица подтвердила дважды)."""

    def test_missing_profession_replaces_pitch(self):
        lead = {"is_single": True, "age": 35, "profession": None}
        result = {"action": "respond", "messages": ["Pitch completo del servicio..."]}
        out = ai._enforce_service_qualification_gate(result, "[фото одобрено]", lead)
        assert out["messages"] == ["¡Gracias por tu foto! 😊", "Y antes de contarte más, ¿a qué te dedicas?"]

    def test_missing_age_asks_age(self):
        lead = {"is_single": True, "age": None, "profession": "abogado"}
        result = {"action": "respond", "messages": ["Pitch..."]}
        out = ai._enforce_service_qualification_gate(result, "[фото одобрено]", lead)
        assert "edad" in out["messages"][1]

    def test_missing_is_single_asks_is_single(self):
        lead = {"is_single": None, "age": 35, "profession": "abogado"}
        result = {"action": "respond", "messages": ["Pitch..."]}
        out = ai._enforce_service_qualification_gate(result, "[фото одобрено]", lead)
        assert "soltero" in out["messages"][1]

    def test_noop_when_qualification_complete(self):
        lead = {"is_single": True, "age": 35, "profession": "abogado"}
        result = {"action": "respond", "messages": ["Pitch completo del servicio..."]}
        out = ai._enforce_service_qualification_gate(result, "[фото одобрено]", lead)
        assert out["messages"] == ["Pitch completo del servicio..."]

    def test_noop_when_interest_is_event(self):
        """interest='event' NO se gatea — owner confirmó 2x que evento es libre."""
        lead = {"is_single": None, "age": None, "profession": None, "interest": "event"}
        result = {"action": "respond", "messages": ["Precio del evento..."]}
        out = ai._enforce_service_qualification_gate(result, "[фото одобрено]", lead)
        assert out["messages"] == ["Precio del evento..."]

    def test_noop_when_action_not_respond(self):
        lead = {"is_single": None, "age": None, "profession": None}
        result = {"action": "escalate", "messages": ["..."]}
        out = ai._enforce_service_qualification_gate(result, "[фото одобрено]", lead)
        assert out["messages"] == ["..."]

    def test_noop_when_not_photo_approved_trigger(self):
        """Гейт срабатывает только на "[фото одобрено]" — не на любое сообщение."""
        lead = {"is_single": None, "age": None, "profession": None}
        result = {"action": "respond", "messages": ["Pitch..."]}
        out = ai._enforce_service_qualification_gate(result, "cuanto cuesta el servicio", lead)
        assert out["messages"] == ["Pitch..."]

    def test_caption_before_marker_still_triggers_gate(self):
        """user_text="{caption}\\n\\n[фото одобрено]" (main.py _process_photos) — гейт
        срабатывает по "in", не по точному равенству (регресс 2026-09-01: подпись к
        фото раньше терялась целиком, что и привело к этому изменению)."""
        lead = {"is_single": True, "age": 35, "profession": None}
        result = {"action": "respond", "messages": ["Pitch..."]}
        out = ai._enforce_service_qualification_gate(
            result, "trabajo de algo raro\n\n[фото одобрено]", lead)
        assert out["messages"][1] == ai._QUALIFICATION_QUESTIONS["profession"]

    async def test_event_force_fires_with_caption_prefix(self):
        """Форс "фото одобрено + interest=event → №51" тоже проверяет "in", не точное
        равенство — иначе подпись к фото сломала бы форс ивента так же, как ломала гейт
        сервиса (регресс 2026-09-01)."""
        lead = {"funnel_stage": "qualified", "is_single": True, "age": 33,
                "profession": "abogado", "interest": "event"}
        n51_row = {"id": 51, "template_es": "Precio del evento...", "mode": "bot_auto",
                   "ai_allowed": True, "blocks_lead": False}
        getrow = AsyncMock(return_value=n51_row)
        ai_response = {**_VALID_AI_RESPONSE, "used_scenario_id": 51}
        with patch("ai.search_scenarios", AsyncMock(return_value=[])), \
             patch("ai.db.get_scenario_row", getrow), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, [], "voy solito\n\n[фото одобрено]")
        getrow.assert_awaited_once_with(51)
        assert result["used_scenario_id"] == 51

    def test_extracted_this_turn_counts_as_qualified(self):
        """Если AI ЭТИМ ЖЕ сообщением извлёк недостающее поле (из подписи к фото) —
        гейт не должен переспрашивать то, что только что пришло. lead (состояние ДО
        сообщения) сливается с result['extracted'] (это сообщение) перед проверкой."""
        lead = {"is_single": True, "age": 35, "profession": None}
        result = {"action": "respond", "messages": ["Pitch completo del servicio..."],
                  "extracted": {"profession": "abogado"}}
        out = ai._enforce_service_qualification_gate(
            result, "soy abogado\n\n[фото одобрено]", lead)
        assert out["messages"] == ["Pitch completo del servicio..."]

    async def test_integration_via_generate_reply(self, history):
        """Интеграционно: неполная анкета + [фото одобрено] через generate_reply целиком,
        даже когда AI не репортит used_scenario_id (найдено 2026-09-01 — сама причина,
        по которой гейт завязан на user_text, а не на used)."""
        lead = {"is_single": True, "age": 35, "profession": None}
        scenario = _make_scenario(id=6, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE,
                       "messages": ["¡Gracias por tu foto! 😊", "Mira, te cuento cómo funciona el servicio..."],
                       "used_scenario_id": None}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "[фото одобрено]")
        assert result["messages"] == ["¡Gracias por tu foto! 😊", "Y antes de contarte más, ¿a qué te dedicas?"]


class TestEnforceServicePriceGate:
    """Guardrail: холодному лиду (is_single != True) нельзя раскрывать цену сервиса
    ($10,000) — даже если AI ошибся вопреки промпту, заменяем весь ответ на крючок №2."""

    async def test_replaces_whole_reply_for_cold_lead(self, history):
        cold_lead = _make_lead(is_single=False)
        scenario = _make_scenario(id=16, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE,
                       "messages": ["La inversión es desde $10,000 USD."],
                       "used_scenario_id": 16}
        hook_row = {"id": 2, "template_es": "Te cuento del evento.\n\n¿Eres soltero?"}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)), \
             patch("db.get_scenario_row", AsyncMock(return_value=hook_row)):
            result = await ai.generate_reply(cold_lead, history, "cuanto cuesta el servicio")
        assert result["messages"] == ["Te cuento del evento.", "¿Eres soltero?"]
        assert result["used_scenario_id"] == 2
        assert not any("10,000" in m or "10000" in m for m in result["messages"])

    async def test_allows_price_for_qualified_lead(self, lead, history):
        """lead из фикстуры is_single=True → цену НЕ трогаем."""
        scenario = _make_scenario(id=16, ai_allowed=True, mode="bot_auto", score=0.80)
        ai_response = {**_VALID_AI_RESPONSE,
                       "messages": ["La inversión es desde $10,000 USD."],
                       "used_scenario_id": 16}
        with patch("ai.search_scenarios", AsyncMock(return_value=[scenario])), \
             patch("ai._call_openai", AsyncMock(return_value=ai_response)):
            result = await ai.generate_reply(lead, history, "cuanto cuesta el servicio")
        assert result["messages"] == ["La inversión es desde $10,000 USD."]

    async def test_noop_when_no_price_mentioned(self):
        result = {"action": "respond", "messages": ["Hola, cuéntame más de ti."]}
        out = await ai._enforce_service_price_gate(result, {"is_single": False})
        assert out["messages"] == ["Hola, cuéntame más de ti."]

    async def test_noop_when_action_not_respond(self):
        result = {"action": "escalate", "messages": ["La inversión es desde $10,000 USD."]}
        out = await ai._enforce_service_price_gate(result, {"is_single": False})
        assert out["messages"] == ["La inversión es desde $10,000 USD."]


class TestEnforceNoRegreetOnRepeat:
    """При повторе сообщения лида не здороваемся заново ("hola de nuevo") — промпт уже
    запрещает это (REGLAS ANTI-ALUCINACIÓN п.9), найдено smoke_test'ом 2026-09-01, что
    AI иногда всё равно так делает."""

    def test_is_repeated_detects_exact_match_case_insensitive(self):
        history = [{"sender": "lead", "text": "Evento"}, {"sender": "anna", "text": "..."}]
        assert ai._is_repeated_lead_message("evento", history) is True

    def test_is_repeated_false_when_different(self):
        history = [{"sender": "lead", "text": "Evento"}, {"sender": "anna", "text": "..."}]
        assert ai._is_repeated_lead_message("hola", history) is False

    def test_is_repeated_false_without_prior_lead_turn(self):
        assert ai._is_repeated_lead_message("hola", []) is False
        assert ai._is_repeated_lead_message("hola", [{"sender": "anna", "text": "hola"}]) is False

    def test_strips_regreet_prefix(self):
        history = [{"sender": "lead", "text": "Evento"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond",
                  "messages": ["¡Hola de nuevo! Ya te había contado del evento.", "¿Eres soltero?"]}
        out = ai._enforce_no_regreet_on_repeat(result, "Evento", history)
        assert out["messages"][0] == "Ya te había contado del evento."
        assert out["messages"][1] == "¿Eres soltero?"

    def test_noop_when_not_repeated(self):
        history = [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond", "messages": ["¡Hola de nuevo! Bla."]}
        out = ai._enforce_no_regreet_on_repeat(result, "Evento", history)
        assert out["messages"] == ["¡Hola de nuevo! Bla."]

    def test_noop_when_no_regreet_phrase_present(self):
        """Повтор есть, но ответ и так не содержит 'hola de nuevo' — не трогаем."""
        history = [{"sender": "lead", "text": "Evento"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond", "messages": ["Como te comentaba, es una noche especial."]}
        out = ai._enforce_no_regreet_on_repeat(result, "Evento", history)
        assert out["messages"] == ["Como te comentaba, es una noche especial."]

    def test_also_strips_on_escalate_action(self):
        """action='escalate' ТОЖЕ проверяем — лид получает текст и при эскалации
        (регресс найден 2026-09-01: smoke_test матчил bot_then_anna на повторе "Evento",
        guardrail изначально пропускал не-respond, поэтому баг не ловился)."""
        history = [{"sender": "lead", "text": "Evento"}, {"sender": "anna", "text": "..."}]
        result = {"action": "escalate", "messages": ["¡Hola de nuevo! Bla."]}
        out = ai._enforce_no_regreet_on_repeat(result, "Evento", history)
        assert out["messages"] == ["Bla."]

    def test_noop_when_action_block_or_silent(self):
        history = [{"sender": "lead", "text": "Evento"}, {"sender": "anna", "text": "..."}]
        for action in ("block", "silent"):
            result = {"action": action, "messages": ["¡Hola de nuevo!"]}
            out = ai._enforce_no_regreet_on_repeat(result, "Evento", history)
            assert out["messages"] == ["¡Hola de nuevo!"]

    def test_deletes_whole_bubble_when_it_is_only_regreet_phrase(self):
        """Найдено 2026-09-07 (live test): a veces el LLM manda "Hola de nuevo!"
        como bubble PROPIO, separado del resto ("Hola de nuevo! 😊", "Cuéntame,
        ¿eres soltero?"). Stripping deja el bubble vacío — antes el guardrail lo
        dejaba SIN TOCAR (defecto: "Hola de nuevo!" seguía visible); ahora BORRA
        el bubble entero (mismo principio que _enforce_no_self_narration)."""
        history = [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond",
                  "messages": ["Hola de nuevo! 😊", "Cuéntame, ¿eres soltero?"]}
        out = ai._enforce_no_regreet_on_repeat(result, "hola", history)
        assert out["messages"] == ["Cuéntame, ¿eres soltero?"]

    def test_keeps_lone_regreet_bubble_when_it_is_the_only_message(self):
        """Si "Hola de nuevo!" es el ÚNICO bubble — no lo borramos (dejaría messages
        vacío), mismo principio que _enforce_no_self_narration."""
        history = [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond", "messages": ["Hola de nuevo!"]}
        out = ai._enforce_no_regreet_on_repeat(result, "hola", history)
        assert out["messages"] == ["Hola de nuevo!"]

    def test_scans_all_bubbles_not_only_first(self):
        """Найдено на code-review 2026-09-07: el guardrail original solo miraba
        messages[0] — "hola de nuevo" en CUALQUIER otra posición (2do, 3er bubble)
        se colaba sin tocar. Ahora escanea TODOS los bubbles."""
        history = [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond", "messages": [
            "Perfecto, gracias.",
            "Hola de nuevo! 😊",
            "¿A qué te dedicas?",
        ]}
        out = ai._enforce_no_regreet_on_repeat(result, "hola", history)
        assert out["messages"] == ["Perfecto, gracias.", "¿A qué te dedicas?"]

    def test_strips_regreet_prefix_in_non_first_bubble(self):
        """Frase mezclada con más texto en un bubble que NO es el primero. (El "¿"
        se pierde junto con el prefijo — mismo comportamiento preexistente que ya
        tenía el regex para el bubble[0], no una regresión nueva.)"""
        history = [{"sender": "lead", "text": "hola"}, {"sender": "anna", "text": "..."}]
        result = {"action": "respond", "messages": [
            "Va, gracias.",
            "¡Hola de nuevo! Eres soltero?",
        ]}
        out = ai._enforce_no_regreet_on_repeat(result, "hola", history)
        assert out["messages"] == ["Va, gracias.", "Eres soltero?"]


class TestEnforceEmojiBudget:
    """Не более ~1 из 3-4 бабблов с эмодзи (REGLAS DE TONO) — если недавняя история уже
    перегружена, этот ответ идёт без эмодзи, не добавляя сверху (найдено smoke_test'ом)."""

    def test_ratio_computed_over_recent_window(self):
        history = [
            {"sender": "anna", "text": "Hola 😊"},
            {"sender": "anna", "text": "Perfecto 🤍"},
            {"sender": "anna", "text": "Va, gracias"},
            {"sender": "anna", "text": "Genial 😊"},
        ]
        assert ai._recent_anna_emoji_ratio(history) == 0.75

    def test_ratio_none_when_no_anna_history(self):
        assert ai._recent_anna_emoji_ratio([]) is None
        assert ai._recent_anna_emoji_ratio([{"sender": "lead", "text": "hola"}]) is None

    def test_strips_emoji_when_recent_ratio_high(self):
        history = [{"sender": "anna", "text": f"msg {i} 😊"} for i in range(4)]
        result = {"action": "respond", "messages": ["Perfecto, gracias 😊", "¿Y tu edad?"]}
        out = ai._enforce_emoji_budget(result, history)
        assert out["messages"] == ["Perfecto, gracias", "¿Y tu edad?"]

    def test_noop_when_recent_ratio_low(self):
        history = [{"sender": "anna", "text": "msg sin emoji"} for _ in range(4)]
        result = {"action": "respond", "messages": ["Perfecto, gracias 😊"]}
        out = ai._enforce_emoji_budget(result, history)
        assert out["messages"] == ["Perfecto, gracias 😊"]

    def test_noop_when_not_enough_history(self):
        result = {"action": "respond", "messages": ["Perfecto, gracias 😊"]}
        out = ai._enforce_emoji_budget(result, [])
        assert out["messages"] == ["Perfecto, gracias 😊"]

    def test_also_strips_on_escalate_action(self):
        """action='escalate' ТОЖЕ проверяем — лид получает текст и при эскалации."""
        history = [{"sender": "anna", "text": f"msg {i} 😊"} for i in range(4)]
        result = {"action": "escalate", "messages": ["Perfecto, gracias 😊"]}
        out = ai._enforce_emoji_budget(result, history)
        assert out["messages"] == ["Perfecto, gracias"]

    def test_noop_when_action_block_or_silent(self):
        history = [{"sender": "anna", "text": f"msg {i} 😊"} for i in range(4)]
        for action in ("block", "silent"):
            result = {"action": action, "messages": ["Perfecto, gracias 😊"]}
            out = ai._enforce_emoji_budget(result, history)
            assert out["messages"] == ["Perfecto, gracias 😊"]


class TestEnforceNoSelfNarration:
    """No narrar en voz alta el propio siguiente paso ("En cuanto los tenga, te
    pregunto...") — encontrado 2026-09-02, live test: instrucción de secuencia del
    prompt tomada literalmente como algo para decirle al lead."""

    def test_removes_whole_narration_bubble(self):
        result = {"action": "respond", "messages": [
            "¡Perfecto! ¿Me pasas tu nombre completo y correo?",
            "En cuanto los tenga, te pregunto qué día y hora te quedan bien 😊",
        ]}
        out = ai._enforce_no_self_narration(result)
        assert out["messages"] == ["¡Perfecto! ¿Me pasas tu nombre completo y correo?"]

    def test_case_insensitive_and_variant_wording(self):
        result = {"action": "respond", "messages": [
            "¿Me compartes tu correo?",
            "en cuanto lo tenga te pregunto cuándo te queda la llamada",
        ]}
        out = ai._enforce_no_self_narration(result)
        assert len(out["messages"]) == 1

    def test_never_leaves_messages_empty(self):
        """Si el bubble de narración es el ÚNICO — no lo quitamos (mejor una frase
        rara que una respuesta vacía)."""
        result = {"action": "respond",
                  "messages": ["En cuanto los tenga, te pregunto qué día te queda"]}
        out = ai._enforce_no_self_narration(result)
        assert out["messages"] == ["En cuanto los tenga, te pregunto qué día te queda"]

    def test_noop_when_no_narration_present(self):
        result = {"action": "respond", "messages": ["Perfecto, gracias.", "¿Y tu edad?"]}
        out = ai._enforce_no_self_narration(result)
        assert out["messages"] == ["Perfecto, gracias.", "¿Y tu edad?"]

    def test_noop_for_legitimate_future_mention_not_matching_pattern(self):
        """No debe activarse en menciones normales del futuro que no empiezan con
        'en cuanto' o no incluyen 'te pregunto' — ambigüedad evitada a propósito."""
        result = {"action": "respond",
                  "messages": ["Cuando nos llamemos, te cuento todo con calma 🤍"]}
        out = ai._enforce_no_self_narration(result)
        assert out["messages"] == ["Cuando nos llamemos, te cuento todo con calma 🤍"]

    def test_noop_when_action_block_or_silent(self):
        for action in ("block", "silent"):
            result = {"action": action, "messages": [
                "¿Nombre y correo?", "En cuanto los tenga, te pregunto qué día"]}
            out = ai._enforce_no_self_narration(result)
            assert len(out["messages"]) == 2


class TestIsPriceObjection:
    """_is_price_objection — не путать negación ("no es caro" = precio SÍ le
    funciona) con una objeción real (encontrado 2026-09-04, revisión propia)."""

    @pytest.mark.parametrize("text,expected", [
        ("está caro", True),
        ("no me alcanza", True),
        ("es mucho para mi", True),
        ("caro", True),
        ("no es caro", False),
        ("no está tan caro", False),
        ("no es muy caro", False),
        ("no me parece caro", False),
        ("esta bien de precio", False),
    ])
    def test_cases(self, text, expected):
        assert ai._is_price_objection(text) is expected


class TestEnforceCourseEscalation:
    """2ª objeción de precio SEGUIDA sobre el EVENTO (#51/#52) → garantiza mención de
    cursos (último escalón), sin depender solo del prompt (encontrado 2026-09-03/04,
    0/3 en test real: el LLM repetía indefinidamente la misma defensa de valor)."""

    def test_second_consecutive_objection_adds_courses(self):
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "este caro"}]
        result = {"action": "respond", "messages": ["Te entiendo, vale la pena."]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert any("curso" in m.lower() for m in out["messages"])
        assert any("[course_link]" in m for m in out["messages"])
        assert out["messages"][0] == "Te entiendo, vale la pena."  # no toca lo generado

    def test_first_objection_noop(self):
        """Primera objeción (el mensaje ANTERIOR del lead no era objeción) — no
        escalamos todavía, es la defensa de valor normal."""
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "y qué incluye el precio?"}]
        result = {"action": "respond", "messages": ["El precio incluye..."]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert out["messages"] == ["El precio incluye..."]

    def test_noop_when_current_message_not_objection(self):
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "está caro"}]
        result = {"action": "respond", "messages": ["Perfecto, gracias."]}
        out = ai._enforce_course_escalation(result, used, "y a qué hora empieza?", history)
        assert out["messages"] == ["Perfecto, gracias."]

    def test_noop_for_unrelated_scenario(self):
        """Objeción de precio pero NO estamos en el escalón evento (#51/#52) — este
        guardrail no es responsable (esa es la escalera del SERVICIO, ver prompt)."""
        used = _make_scenario(id=2)
        history = [{"sender": "lead", "text": "caro"}]
        result = {"action": "respond", "messages": ["Te entiendo."]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert out["messages"] == ["Te entiendo."]

    def test_noop_when_already_mentions_courses_with_link(self):
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "caro"}]
        result = {"action": "respond",
                  "messages": ["Te entiendo. También tengo cursos en línea: [course_link]"]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert out["messages"] == result["messages"]

    def test_replaces_bubble_when_courses_mentioned_without_token(self):
        """Encontrado 2026-09-05 en test real: el LLM mencionó "cursos en línea...
        te paso el link" en prosa pero SIN el placeholder [course_link] — el lead
        recibía la promesa del link sin el link. Mencionar la palabra "curso" NO
        basta para considerar el guardrail resuelto, solo el placeholder cuenta —
        pero REEMPLAZAMOS ese bubble en vez de añadir uno nuevo (encontrado
        2026-09-06: añadir uno nuevo dejaba DOS menciones de cursos seguidas,
        repetitivo/robótico)."""
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "caro"}]
        result = {"action": "respond", "messages": [
            "Te entiendo.",
            "También tengo cursos en línea donde te enseño esto. Te paso el link por si te interesa:",
        ]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert len(out["messages"]) == 2  # no se añadió un tercer bubble
        assert "[course_link]" in out["messages"][1]
        assert out["messages"][0] == "Te entiendo."  # el bubble sin mención no se toca

    def test_max_messages_replaces_link_only_last_bubble(self):
        """Al tope de MAX_MESSAGES, si el ÚLTIMO bubble es SOLO el link del boleto
        (forzado por _enforce_link_presence, sin importar el tipo de turno) — lo
        reemplazamos directo por cursos: si el link ya se había enviado, sender.py
        lo iba a dropear solo igual (Layer 2 dedup); no vale la pena sacrificar
        contenido real por él."""
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "caro"}]
        result = {"action": "respond",
                  "messages": ["uno", "dos", "tres", "Aquí tu boleto: [event_link] 🤍"]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert len(out["messages"]) == ai.MAX_MESSAGES
        assert out["messages"][:3] == ["uno", "dos", "tres"]
        assert "[course_link]" in out["messages"][-1]

    def test_max_messages_sacrifices_penultimate_when_last_is_real_content(self):
        """Si el ÚLTIMO bubble es contenido real (no un link) — lo conservamos y
        sacrificamos el penúltimo para hacer espacio a cursos."""
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "caro"}]
        result = {"action": "respond",
                  "messages": ["uno", "dos", "tres", "Créeme, vale la pena 🤍"]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert len(out["messages"]) == ai.MAX_MESSAGES
        assert out["messages"][-1] == "Créeme, vale la pena 🤍"
        assert "[course_link]" in out["messages"][-2]

    def test_noop_when_action_not_respond(self):
        used = _make_scenario(id=51)
        history = [{"sender": "lead", "text": "caro"}]
        result = {"action": "escalate", "messages": ["ok"]}
        out = ai._enforce_course_escalation(result, used, "caro", history)
        assert out["messages"] == ["ok"]


class TestEnforceNoLinkRepeat:
    """No repetir una URL que el LLM escribió como texto libre (copiada del
    historial, no el placeholder [event_link] — ese ya dedupica sender.py) si ya se
    le envió a este lead antes. Encontrado 2026-09-03/04: mismo link 2-3 veces
    seguidas en minutos ante objeciones repetidas."""

    async def test_removes_bubble_with_already_sent_url(self):
        lead = {"phone": "tg_test"}
        result = {"action": "respond", "messages": [
            "Te entiendo.",
            "Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍",
        ]}
        with patch("ai.db.link_already_sent", AsyncMock(return_value=True)):
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == ["Te entiendo."]

    async def test_strips_trailing_punctuation_before_comparing(self):
        """URL pegada a un punto sin espacio ("...aquí: https://url.") — \\S+ la
        capturaría con el punto incluido; sin normalizar, no coincidiría con la URL
        ya guardada (sin punto) y el dedup fallaría (найдено 2026-09-04, code-review).
        Verificamos que la URL pasada a link_already_sent NO tiene el punto final."""
        lead = {"phone": "tg_test"}
        result = {"action": "respond", "messages": [
            "Te entiendo.",
            "Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026.",
        ]}
        mock_sent = AsyncMock(return_value=True)
        with patch("ai.db.link_already_sent", mock_sent):
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == ["Te entiendo."]
        mock_sent.assert_awaited_once_with("tg_test", "https://www.rusaencdmx.com/09-09-2026")

    async def test_keeps_bubble_with_new_url(self):
        lead = {"phone": "tg_test"}
        result = {"action": "respond", "messages": [
            "Te entiendo.",
            "Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍",
        ]}
        with patch("ai.db.link_already_sent", AsyncMock(return_value=False)):
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == result["messages"]

    async def test_never_leaves_messages_empty(self):
        """Si el ÚNICO bubble tiene la URL repetida — lo dejamos (mejor repetir que
        quedarse sin respuesta), mismo principio que _enforce_no_self_narration."""
        lead = {"phone": "tg_test"}
        result = {"action": "respond",
                  "messages": ["Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍"]}
        with patch("ai.db.link_already_sent", AsyncMock(return_value=True)):
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == result["messages"]

    async def test_noop_when_action_not_respond(self):
        lead = {"phone": "tg_test"}
        result = {"action": "escalate",
                  "messages": ["Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍"]}
        with patch("ai.db.link_already_sent", AsyncMock(return_value=True)):
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == result["messages"]

    async def test_noop_without_phone(self):
        lead = {}
        result = {"action": "respond",
                  "messages": ["Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍"]}
        with patch("ai.db.link_already_sent", AsyncMock(return_value=True)) as mock_sent:
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == result["messages"]
        mock_sent.assert_not_awaited()

    async def test_db_failure_keeps_bubble(self):
        """Sбой БД → лучше отправить (не потерять ответ), чем ошибочно дропнуть."""
        lead = {"phone": "tg_test"}
        result = {"action": "respond",
                  "messages": ["Aquí tu boleto: https://www.rusaencdmx.com/09-09-2026 🤍"]}
        with patch("ai.db.link_already_sent", AsyncMock(side_effect=RuntimeError("db down"))):
            out = await ai._enforce_no_link_repeat(result, lead)
        assert out["messages"] == result["messages"]
