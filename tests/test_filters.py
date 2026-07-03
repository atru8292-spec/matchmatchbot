"""Тесты чистых функций filters.decide, is_escort_mention, is_aggression.

Без БД — только данные в dict. Проверяем бизнес-логику:
whitelist, do_not_contact, manual mode, escort (с границами слов), агрессия, возраст, is_single.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from filters import Decision, decide, is_aggression, is_escort_mention, is_russian_number, has_cyrillic


# ===========================================================================
# Прямые unit-тесты вспомогательных функций
# ===========================================================================


class TestIsEscortMention:
    """is_escort_mention — совпадения и ложные срабатывания на границах слов."""

    def test_explicit_escort(self):
        assert is_escort_mention("busco un escort") is True

    def test_escorts_plural(self):
        assert is_escort_mention("hay escorts disponibles") is True

    def test_sexo_standalone(self):
        assert is_escort_mention("quiero sexo") is True

    def test_sexual(self):
        assert is_escort_mention("servicio sexual") is True

    def test_prostitutas(self):
        assert is_escort_mention("son prostitutas") is True

    def test_servicio_sexual_phrase(self):
        assert is_escort_mention("ofrecen servicio sexual") is True

    def test_acompanante(self):
        assert is_escort_mention("busco acompanante") is True

    # Критичные тесты границ слов — sexo НЕ должен ловиться в sexto/sexta/sexteto

    def test_sexto_not_matched(self):
        """sexto — порядковое числительное, не сексуальный контекст."""
        assert is_escort_mention("el sexto piso") is False

    def test_sexta_not_matched(self):
        """sexta — порядковое числительное женского рода."""
        assert is_escort_mention("la sexta vez") is False

    def test_sexteto_not_matched(self):
        """sexteto — музыкальный термин (шесть исполнителей)."""
        assert is_escort_mention("el sexteto tocó muy bien") is False

    def test_empty_string_safe(self):
        assert is_escort_mention("") is False

    def test_none_safe(self):
        assert is_escort_mention(None) is False  # type: ignore[arg-type]

    def test_neutral_text_not_matched(self):
        assert is_escort_mention("hola quiero informacion sobre sus servicios") is False


class TestIsAggression:
    """is_aggression — оскорбления и агрессия."""

    def test_pendejo(self):
        assert is_aggression("eres un pendejo") is True

    def test_pendejos_plural(self):
        assert is_aggression("todos son pendejos") is True

    def test_estafa(self):
        assert is_aggression("que estafa") is True

    def test_estafador(self):
        assert is_aggression("eres un estafador") is True

    def test_idiota(self):
        assert is_aggression("idiota!") is True

    def test_mierda(self):
        assert is_aggression("esto es una mierda") is True

    def test_estupida_with_accent(self):
        assert is_aggression("eres estúpida") is True

    def test_estupido_without_accent(self):
        assert is_aggression("que estupido") is True

    def test_fraude(self):
        assert is_aggression("esto es fraude") is True

    def test_empty_string_safe(self):
        assert is_aggression("") is False

    def test_none_safe(self):
        assert is_aggression(None) is False  # type: ignore[arg-type]

    def test_neutral_text_not_matched(self):
        assert is_aggression("hola quiero mas informacion") is False


# ===========================================================================
# Whitelist и do_not_contact — наивысший приоритет
# ===========================================================================


class TestDecideWhitelist:

    def test_whitelisted_returns_silent_whitelist(self):
        d = decide({}, True, "hola")
        assert d.action == "silent_whitelist"

    def test_whitelisted_sets_alert_manager(self):
        d = decide({}, True, "hola")
        assert d.alert_manager is True

    def test_whitelist_overrides_escort_text(self):
        """Whitelist проверяется раньше escort — должно быть silent, не blocked."""
        d = decide({}, True, "quiero sexo")
        assert d.action == "silent_whitelist"

    def test_whitelist_overrides_aggression_text(self):
        d = decide({}, True, "eres un idiota")
        assert d.action == "silent_whitelist"

    def test_whitelist_escort_not_blocked(self):
        """VIP с escort-паттерном: молчим и эскалируем, НЕ блокируем (Аня разберётся)."""
        d = decide({}, True, "quiero sexo")
        assert d.action == "silent_whitelist"
        assert d.block_permanent is False
        assert d.is_escort is False
        assert d.alert_manager is True

    def test_do_not_contact_returns_silent_no_alert(self):
        """do_not_contact → silent БЕЗ VIP-алерта (заблокированный не спамит Аню)."""
        d = decide({"do_not_contact": True}, False, "hola")
        assert d.action == "silent"
        assert d.alert_manager is False

    def test_do_not_contact_false_no_effect(self):
        """do_not_contact=False (явно) → не тормозим, продолжаем."""
        d = decide({"do_not_contact": False, "age": 35, "is_single": True}, False, "hola")
        assert d.action == "needs_ai"


# ===========================================================================
# Manual mode
# ===========================================================================


class TestDecideManualMode:

    def test_manual_without_until_is_silent(self):
        """mode='manual', manual_until=None → активный manual → silent (без алерта)."""
        d = decide({"mode": "manual", "manual_until": None}, False, "hola")
        assert d.action == "silent"
        assert d.alert_manager is False

    def test_manual_with_future_until_is_silent(self):
        """manual_until в будущем → менеджер ведёт → бот молчит (silent)."""
        future = datetime.now(timezone.utc) + timedelta(days=30)
        d = decide({"mode": "manual", "manual_until": future}, False, "hola")
        assert d.action == "silent"

    def test_manual_with_past_until_continues(self):
        """manual_until в прошлом → срок истёк → бот продолжает обработку."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        d = decide({"mode": "manual", "manual_until": past, "age": 35, "is_single": True}, False, "hola")
        assert d.action == "needs_ai"

    def test_auto_mode_not_silent(self):
        """mode='auto' → не manual → обрабатываем нормально."""
        d = decide({"mode": "auto", "age": 35, "is_single": True}, False, "hola")
        assert d.action == "needs_ai"

    def test_no_mode_field_not_silent(self):
        """Нет поля mode → не manual → обрабатываем нормально."""
        d = decide({"age": 35, "is_single": True}, False, "hola")
        assert d.action == "needs_ai"


# ===========================================================================
# Escort / блок
# ===========================================================================


class TestDecideEscort:

    def test_busco_sexo_blocked(self):
        d = decide({}, False, "busco sexo")
        assert d.action == "blocked"

    def test_escort_blocked_with_permanent_flag(self):
        d = decide({}, False, "quiero un escort")
        assert d.block_permanent is True

    def test_escort_blocked_with_alert(self):
        d = decide({}, False, "quiero un escort")
        assert d.alert_manager is True

    def test_servicio_sexual_blocked(self):
        d = decide({}, False, "ofrecen servicio sexual")
        assert d.action == "blocked"

    def test_prostituta_blocked(self):
        d = decide({}, False, "es una prostituta")
        assert d.action == "blocked"

    # Граница слова — КЛЮЧЕВЫЕ тесты

    def test_sexto_piso_not_blocked(self):
        """el sexto piso — 'sexto' не содержит слово 'sexo' как целое слово."""
        d = decide({}, False, "vivo en el sexto piso")
        assert d.action == "needs_ai"

    def test_sexta_vez_not_blocked(self):
        """la sexta vez — 'sexta' не совпадает с 'sexo' по границе слова."""
        d = decide({}, False, "es la sexta vez que llamo")
        assert d.action == "needs_ai"

    def test_sexteto_not_blocked(self):
        """sexteto — музыкальный ансамбль, не ловится как 'sexo'."""
        d = decide({}, False, "escucho el sexteto de Mozart")
        assert d.action == "needs_ai"


# ===========================================================================
# Агрессия / блок
# ===========================================================================


class TestDecideAggression:

    def test_pendejo_blocked(self):
        d = decide({}, False, "eres un pendejo")
        assert d.action == "blocked"

    def test_aggression_sets_permanent(self):
        d = decide({}, False, "eres un pendejo")
        assert d.block_permanent is True

    def test_aggression_sets_alert(self):
        d = decide({}, False, "eres un pendejo")
        assert d.alert_manager is True

    def test_estafa_blocked(self):
        d = decide({}, False, "que estafa")
        assert d.action == "blocked"

    def test_idiota_blocked(self):
        d = decide({}, False, "idiota")
        assert d.action == "blocked"


# ===========================================================================
# Приоритет: whitelist > escort > aggression
# ===========================================================================


class TestDecidePriority:

    def test_whitelist_before_escort(self):
        """Whitelist → silent, не blocked, даже с escort-текстом."""
        d = decide({}, True, "busco sexo con escort")
        assert d.action == "silent_whitelist"

    def test_escort_before_age(self):
        """Escort → blocked, не rejected по возрасту (даже если age=25)."""
        d = decide({"age": 25}, False, "busco sexo")
        assert d.action == "blocked"

    def test_escort_before_is_single(self):
        """Escort → blocked, не rejected по is_single."""
        d = decide({"is_single": False}, False, "busco sexo")
        assert d.action == "blocked"

    def test_aggression_before_age(self):
        """Агрессия → blocked, не rejected по возрасту."""
        d = decide({"age": 20}, False, "idiota")
        assert d.action == "blocked"


# ===========================================================================
# Фильтр возраста
# ===========================================================================


class TestDecideAge:

    def test_age_27_rejected(self):
        """27 лет — меньше минимума (28) → rejected."""
        d = decide({"age": 27}, False, "hola")
        assert d.action == "rejected"

    def test_age_66_rejected(self):
        """66 лет — больше максимума (65) → rejected."""
        d = decide({"age": 66}, False, "hola")
        assert d.action == "rejected"

    def test_age_28_boundary_not_rejected(self):
        """Граница включительно: 28 → не отказ."""
        d = decide({"age": 28}, False, "hola")
        assert d.action != "rejected"

    def test_age_65_boundary_not_rejected(self):
        """Граница включительно: 65 → не отказ."""
        d = decide({"age": 65}, False, "hola")
        assert d.action != "rejected"

    def test_age_40_needs_ai(self):
        """Нормальный возраст + is_single=True → needs_ai."""
        d = decide({"age": 40, "is_single": True}, False, "hola")
        assert d.action == "needs_ai"

    def test_age_none_not_rejected(self):
        """age=None (неизвестен) → не дисквалифицируем по возрасту."""
        d = decide({"age": None}, False, "hola")
        assert d.action != "rejected"

    def test_age_string_not_rejected(self):
        """age не int (строка) → isinstance(age, int) = False → не отклоняем."""
        d = decide({"age": "35"}, False, "hola")
        assert d.action != "rejected"

    def test_age_0_rejected(self):
        """age=0 — явно вне диапазона → rejected."""
        d = decide({"age": 0}, False, "hola")
        assert d.action == "rejected"


# ===========================================================================
# Фильтр семейного положения
# ===========================================================================


class TestDecideIsSingle:

    def test_is_single_false_rejected(self):
        d = decide({"is_single": False}, False, "hola")
        assert d.action == "rejected"

    def test_is_single_false_reason_mentions_cold(self):
        """Причина отказа упоминает холост/not single."""
        d = decide({"is_single": False}, False, "hola")
        reason_lower = d.reason.lower()
        assert "холост" in reason_lower or "single" in reason_lower

    def test_is_single_true_not_rejected(self):
        d = decide({"is_single": True, "age": 35}, False, "hola")
        assert d.action == "needs_ai"

    def test_is_single_none_not_rejected(self):
        """is_single=None (неизвестно) → не дисквалифицируем."""
        d = decide({"is_single": None, "age": 35}, False, "hola")
        assert d.action == "needs_ai"

    def test_is_single_missing_not_rejected(self):
        """Нет поля is_single в dict → lead.get → None → не дисквалифицируем."""
        d = decide({"age": 35}, False, "hola")
        assert d.action == "needs_ai"


# ===========================================================================
# Нейтральные / пограничные кейсы
# ===========================================================================


class TestDecideNeutral:

    def test_clean_lead_needs_ai(self):
        d = decide({"age": 35, "is_single": True}, False, "hola quiero info")
        assert d.action == "needs_ai"

    def test_empty_lead_needs_ai(self):
        """Пустой dict (новый лид) + нейтральный текст → needs_ai."""
        d = decide({}, False, "hola quiero informacion")
        assert d.action == "needs_ai"

    def test_none_lead_safe(self):
        """lead=None → inside decide: lead or {} → {} → needs_ai."""
        d = decide(None, False, "hola")  # type: ignore[arg-type]
        assert d.action == "needs_ai"

    def test_empty_text_needs_ai(self):
        """Пустой текст → ни escort, ни агрессия → needs_ai."""
        d = decide({"age": 35, "is_single": True}, False, "")
        assert d.action == "needs_ai"

    def test_none_text_safe(self):
        """text=None → safe (text or '' в decide)."""
        d = decide({"age": 35, "is_single": True}, False, None)  # type: ignore[arg-type]
        assert d.action == "needs_ai"

    def test_decision_is_dataclass_with_correct_fields(self):
        """Decision — dataclass с полями action, reason, alert_manager, block_permanent."""
        d = decide({}, False, "hola")
        assert hasattr(d, "action")
        assert hasattr(d, "reason")
        assert hasattr(d, "alert_manager")
        assert hasattr(d, "block_permanent")

    def test_needs_ai_alert_manager_false(self):
        """needs_ai → alert_manager=False (Аню не беспокоим)."""
        d = decide({}, False, "hola")
        assert d.alert_manager is False

    def test_needs_ai_block_permanent_false(self):
        """needs_ai → block_permanent=False."""
        d = decide({}, False, "hola")
        assert d.block_permanent is False


# ===========================================================================
# Поле is_escort в Decision
# ===========================================================================


class TestDecisionIsEscortField:
    """Decision.is_escort — только escort-ветка возвращает True; остальные False."""

    def test_escort_branch_is_escort_true(self):
        """Escort-ветка decide() → is_escort=True."""
        d = decide({}, False, "busco sexo")
        assert d.is_escort is True

    def test_escort_branch_action_is_blocked(self):
        """Убеждаемся что это именно blocked (а не другое действие с is_escort)."""
        d = decide({}, False, "quiero un escort")
        assert d.action == "blocked"
        assert d.is_escort is True

    def test_aggression_branch_is_escort_false(self):
        """Агрессия → blocked, но is_escort=False (не escort-упоминание)."""
        d = decide({}, False, "eres un idiota")
        assert d.action == "blocked"
        assert d.is_escort is False

    def test_rejected_age_is_escort_false(self):
        """rejected по возрасту → is_escort=False."""
        d = decide({"age": 70}, False, "hola")
        assert d.action == "rejected"
        assert d.is_escort is False

    def test_rejected_is_single_is_escort_false(self):
        """rejected по is_single → is_escort=False."""
        d = decide({"is_single": False}, False, "hola")
        assert d.action == "rejected"
        assert d.is_escort is False

    def test_needs_ai_is_escort_false(self):
        """needs_ai → is_escort=False."""
        d = decide({}, False, "hola quiero info")
        assert d.action == "needs_ai"
        assert d.is_escort is False

    def test_silent_whitelist_is_escort_false(self):
        """silent_whitelist → is_escort=False."""
        d = decide({}, True, "hola")
        assert d.action == "silent_whitelist"
        assert d.is_escort is False

    def test_do_not_contact_is_escort_false(self):
        """do_not_contact → silent_whitelist → is_escort=False."""
        d = decide({"do_not_contact": True}, False, "hola")
        assert d.is_escort is False


# ===========================================================================
# Множественные формы и новые паттерны is_escort_mention
# ===========================================================================


class TestEscortMentionPluralsAndPhrases:
    """Дополнительные паттерны: множественные числа, варианты написания, граница слов."""

    def test_acompanantes_with_tilde_true(self):
        """acompañantes (с тильдой, мн.ч.) → True."""
        assert is_escort_mention("busco acompañantes") is True

    def test_necesito_acompanante_without_tilde_true(self):
        """necesito acompanante (без тильды) → True."""
        assert is_escort_mention("necesito acompanante") is True

    def test_servicios_sexuales_true(self):
        """servicios sexuales → True."""
        assert is_escort_mention("servicios sexuales") is True

    def test_quiero_escorts_true(self):
        """quiero escorts → True."""
        assert is_escort_mention("quiero escorts") is True

    def test_sexto_piso_false(self):
        """'el sexto piso' — sexto не содержит sexo как целое слово → False."""
        assert is_escort_mention("el sexto piso") is False

    def test_sexteto_de_jazz_false(self):
        """'sexteto de jazz' — sexteto не содержит sexo как целое слово → False."""
        assert is_escort_mention("sexteto de jazz") is False


# ===========================================================================
# Тесты нового фильтра "silent" — русский номер (+7) и кириллица
# ===========================================================================


class TestIsRussianNumber:
    """is_russian_number — только wa_7... считается русским/казахским."""

    def test_russian_mobile_true(self):
        """wa_79991234567 — очевидный РФ номер → True."""
        assert is_russian_number("wa_79991234567") is True

    def test_kazakhstan_7_true(self):
        """wa_77012345678 — Казахстан тоже +7 → True (не целевой регион)."""
        assert is_russian_number("wa_77012345678") is True

    def test_mexico_52_false(self):
        """wa_5215551234567 — Мексика +52, не +7 → False."""
        assert is_russian_number("wa_5215551234567") is False

    def test_mexico_52_with_7_inside_false(self):
        """wa_5271234567 — начинается на wa_52, цифра 7 внутри не считается → False."""
        assert is_russian_number("wa_5271234567") is False

    def test_empty_string_false(self):
        """Пустая строка → False."""
        assert is_russian_number("") is False

    def test_short_wa_1234_false(self):
        """wa_1234 — начинается на wa_1, не wa_7 → False."""
        assert is_russian_number("wa_1234") is False

    def test_none_safe(self):
        """None передан как phone (совместимость) → False, без исключения."""
        assert is_russian_number(None) is False  # type: ignore[arg-type]

    def test_us_number_false(self):
        """wa_15555550100 — США +1 → False."""
        assert is_russian_number("wa_15555550100") is False


class TestHasCyrillic:
    """has_cyrillic — кириллические символы в тексте."""

    def test_pure_russian_true(self):
        """привет — только кириллица → True."""
        assert has_cyrillic("привет") is True

    def test_pure_latin_false(self):
        """hola — только латиница → False."""
        assert has_cyrillic("hola") is False

    def test_mixed_cyrillic_latin_true(self):
        """спасибо guapo — смесь, есть кириллица → True."""
        assert has_cyrillic("спасибо guapo") is True

    def test_empty_string_false(self):
        """Пустая строка → False."""
        assert has_cyrillic("") is False

    def test_digits_special_chars_false(self):
        """123 !!! — только цифры и спецсимволы, кириллицы нет → False."""
        assert has_cyrillic("123 !!!") is False

    def test_capital_cyrillic_true(self):
        """ПРИВЕТ — заглавные кириллические → True."""
        assert has_cyrillic("ПРИВЕТ") is True

    def test_yo_letter_true(self):
        """Буква ё входит в [а-яёА-ЯЁ] → True."""
        assert has_cyrillic("ёлка") is True

    def test_none_safe(self):
        """None → False, без исключения."""
        assert has_cyrillic(None) is False  # type: ignore[arg-type]


class TestDecideSilentByPhone:
    """decide: русский номер (+7) → action='silent' независимо от текста."""

    def test_russian_phone_hola_silent(self):
        """Русский номер, испанский текст → всё равно silent (номер проверяется первым)."""
        d = decide({}, False, "hola", "wa_79991234567")
        assert d.action == "silent"

    def test_silent_no_alert_manager(self):
        """silent → alert_manager=False (Аню не беспокоим)."""
        d = decide({}, False, "hola", "wa_79991234567")
        assert d.alert_manager is False

    def test_silent_no_block_permanent(self):
        """silent → block_permanent=False (не дисквалификация, вдруг ошибка)."""
        d = decide({}, False, "hola", "wa_79991234567")
        assert d.block_permanent is False

    def test_silent_no_is_escort(self):
        """silent → is_escort=False."""
        d = decide({}, False, "hola", "wa_79991234567")
        assert d.is_escort is False

    def test_silent_reason_not_empty(self):
        """silent → reason содержит пояснение (не пустая строка)."""
        d = decide({}, False, "hola", "wa_79991234567")
        assert d.reason  # непустая строка


class TestDecideSilentByCyrillic:
    """decide: кириллица в тексте → action='silent'."""

    def test_cyrillic_text_any_phone_silent(self):
        """Кириллица + мексиканский номер → silent (текст нецелевой)."""
        d = decide({}, False, "привет как estas", "wa_5215551234567")
        assert d.action == "silent"

    def test_cyrillic_text_no_phone_silent(self):
        """phone дефолт '' (совместимость) + кириллица → silent."""
        d = decide({}, False, "привет")
        assert d.action == "silent"

    def test_cyrillic_mixed_text_silent(self):
        """Смешанный текст с кириллицей → silent."""
        d = decide({}, False, "hola меня зовут Ivan")
        assert d.action == "silent"


class TestDecideSilentPriority:
    """Проверка приоритета: whitelist/DNC/manual > silent."""

    def test_whitelist_before_silent_russian_phone(self):
        """Whitelist + русский номер + кириллица → silent_whitelist, НЕ silent."""
        d = decide({}, True, "привет", "wa_79991234567")
        assert d.action == "silent_whitelist"
        assert d.alert_manager is True

    def test_do_not_contact_before_silent(self):
        """do_not_contact проверяется РАНЬШЕ региона/кириллицы (обе ветки → silent,
        но причина должна быть do_not_contact, не «кириллица»)."""
        d = decide({"do_not_contact": True}, False, "привет", "wa_79991234567")
        assert d.action == "silent"
        assert "do_not_contact" in d.reason

    def test_whitelist_before_silent_cyrillic_only(self):
        """Whitelist + кириллица (нейтральный номер) → silent_whitelist."""
        d = decide({}, True, "привет как дела", "wa_5215551234567")
        assert d.action == "silent_whitelist"

    def test_escort_after_silent_not_triggered_on_russian_phone(self):
        """Русский номер → silent ДО escort-проверки (escort не выполняется)."""
        d = decide({}, False, "busco sexo", "wa_79991234567")
        # порядок: silent раньше escort → action='silent', не 'blocked'
        assert d.action == "silent"

    def test_escort_after_silent_not_triggered_on_cyrillic(self):
        """Кириллица → silent ДО escort-проверки."""
        d = decide({}, False, "ищу sexo", "wa_5215551234567")
        assert d.action == "silent"


class TestDecideSilentRegression:
    """Регрессия: фильтр silent не ломает нормальную логику на мексиканских номерах."""

    def test_mexico_spanish_text_needs_ai(self):
        """Мексиканский номер + испанский текст без кириллицы → needs_ai (не silent)."""
        d = decide({}, False, "hola quiero informacion", "wa_5215551234567")
        assert d.action == "needs_ai"

    def test_mexico_escort_text_blocked(self):
        """Мексиканский номер + escort-текст → blocked (не silent, silent не должен перехватить)."""
        d = decide({}, False, "busco sexo", "wa_5215551234567")
        assert d.action == "blocked"
        assert d.is_escort is True

    def test_mexico_young_age_rejected(self):
        """Мексиканский номер + age=24 + испанский текст → rejected (age-фильтр работает)."""
        d = decide({"age": 24}, False, "hola", "wa_5215551234567")
        assert d.action == "rejected"

    def test_mexico_old_age_rejected(self):
        """Мексиканский номер + age=70 → rejected (MAX_AGE=65)."""
        d = decide({"age": 70}, False, "hola", "wa_5215551234567")
        assert d.action == "rejected"

    def test_no_phone_spanish_text_needs_ai(self):
        """phone не передан (дефолт '') + испанский текст → needs_ai (обратная совместимость)."""
        d = decide({}, False, "hola quiero informacion")
        assert d.action == "needs_ai"

    def test_aggression_mexico_blocked(self):
        """Мексиканский номер + агрессия → blocked (aggressive-фильтр работает)."""
        d = decide({}, False, "idiota", "wa_5215551234567")
        assert d.action == "blocked"


class TestSilentBypass:
    """bypass_phones: номера-исключения silent-фильтра проходят дальше."""

    BYPASS = frozenset({"wa_79635378880", "wa_79635708880"})

    def test_bypass_ru_number_passes_to_ai(self):
        r = decide({}, False, "hola quiero info", "wa_79635378880", self.BYPASS)
        assert r.action == "needs_ai"

    def test_bypass_ru_number_cyrillic_passes(self):
        # bypass пропускает и кириллицу для этого номера
        r = decide({}, False, "привет", "wa_79635378880", self.BYPASS)
        assert r.action == "needs_ai"

    def test_non_bypass_ru_still_silent(self):
        r = decide({}, False, "hola", "wa_79001112233", self.BYPASS)
        assert r.action == "silent"

    def test_bypass_does_not_disable_cyrillic_for_others(self):
        r = decide({}, False, "привет", "wa_5215551234567", self.BYPASS)
        assert r.action == "silent"

    def test_empty_bypass_default_ru_silent(self):
        # без bypass (дефолт) — старое поведение
        r = decide({}, False, "hola", "wa_79991234567")
        assert r.action == "silent"

    def test_bypass_does_not_break_escort_for_bypassed(self):
        # bypass снимает только silent; escort по-прежнему блокирует
        r = decide({}, False, "busco sexo", "wa_79635378880", self.BYPASS)
        assert r.action == "blocked"


# ===========================================================================
# Заявление об оплате (блок 13)
# ===========================================================================

from filters import is_payment_claim


class TestIsPaymentClaim:
    def test_pague(self):
        assert is_payment_claim("ya pagué el evento") is True

    def test_pagado(self):
        assert is_payment_claim("listo, pagado") is True

    def test_deposite(self):
        assert is_payment_claim("deposité ayer") is True

    def test_transferi(self):
        assert is_payment_claim("te transferí el monto") is True

    def test_russian(self):
        assert is_payment_claim("оплатила уже") is True

    def test_question_about_payment_not_claim(self):
        """Вопрос про оплату — НЕ claim (ложное срабатывание не нужно)."""
        assert is_payment_claim("cómo es el pago?") is False
        assert is_payment_claim("dónde puedo pagar?") is False

    def test_empty(self):
        assert is_payment_claim("") is False


class TestDecidePaymentClaim:
    def test_payment_claim_action(self):
        d = decide({"age": 40, "is_single": True}, False, "ya pagué", "wa_52")
        assert d.action == "payment_claim"
        assert d.alert_manager is True

    def test_whitelist_beats_payment(self):
        """Whitelist приоритетнее — клиент из списка идёт к Ане, не в payment-флоу."""
        d = decide({}, True, "ya pagué", "wa_52")
        assert d.action == "silent_whitelist"

    def test_escort_beats_payment(self):
        """Escort-паттерн важнее (блок), даже если в тексте есть 'pagué'."""
        d = decide({}, False, "pagué por sexo", "wa_52")
        assert d.action == "blocked"
