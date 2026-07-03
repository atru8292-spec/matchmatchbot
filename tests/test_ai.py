"""Unit-тесты для ai.py — AI-ядро бота Anna.

Все внешние зависимости замоканы: OpenAI (_embed, _call_openai), БД (search_scenarios_by_vector).
Реальных сетевых вызовов нет.
"""
from __future__ import annotations

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

    def test_mode_to_anna_silent_gives_escalate(self):
        """mode='to_anna_silent' → action='escalate'."""
        scenario = _make_scenario(
            id=3,
            mode="to_anna_silent",
            blocks_lead=False,
            ai_allowed=False,
            template_es="",
        )
        result = ai._fixed_reply(scenario)
        assert result["action"] == "escalate"
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
        assert result["messages"] == ["Ahorita te contesto guapo 🤍"]

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


class TestGenerateReplyFallback:
    """Ветки 4 и 5: сбои → fallback, никогда не бросает."""

    async def test_openai_exception_returns_fallback(self, lead, history):
        """_call_openai падает → generate_reply возвращает _fallback_reply, не бросает."""
        with patch("ai.search_scenarios", new=AsyncMock(return_value=[])), \
             patch("ai._call_openai", AsyncMock(side_effect=Exception("OpenAI timeout"))):
            result = await ai.generate_reply(lead, history, "texto")

        assert result["action"] == "escalate"
        assert result["needs_escalation"] is True
        assert result["messages"] == ["Ahorita te contesto guapo 🤍"]
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
        assert result["messages"] == ["Ahorita te contesto guapo 🤍"]

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
        r = ai._validate_output({"messages": ["x"], "action": "respond", "funnel_stage": "client_starter"})
        assert r["funnel_stage"] == "client_starter"

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
