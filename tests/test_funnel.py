"""Тесты funnel.py: stage_label, FUNNEL_STAGES, NO_FOLLOWUP_STAGES, FOLLOWUP_FIRST_DELAY_HOURS.

Чистые функции — без БД, без внешних зависимостей.
"""
from __future__ import annotations

import funnel
from funnel import (
    FOLLOWUP_FIRST_DELAY_HOURS,
    FUNNEL_STAGES,
    NO_FOLLOWUP_STAGES,
    stage_label,
)


# ===========================================================================
# FUNNEL_STAGES — полнота словаря
# ===========================================================================


class TestFunnelStagesDict:

    def test_stage_count_is_13(self):
        """FUNNEL_STAGES содержит ровно 13 стадий."""
        assert len(FUNNEL_STAGES) == 13

    def test_active_stages_present(self):
        """Все активные стадии воронки присутствуют."""
        for code in ("new", "qualifying", "photo_pending", "qualified", "pitched", "videocall_set"):
            assert code in FUNNEL_STAGES, f"Активная стадия {code!r} отсутствует"

    def test_client_stages_present(self):
        """Стадии клиентов присутствуют."""
        for code in ("client_starter", "client_standard", "client_vip", "event_attended"):
            assert code in FUNNEL_STAGES, f"Клиентская стадия {code!r} отсутствует"

    def test_terminal_stages_present(self):
        """Финальные стадии присутствуют."""
        for code in ("rejected", "lost", "nurture"):
            assert code in FUNNEL_STAGES, f"Финальная стадия {code!r} отсутствует"

    def test_values_are_strings(self):
        """Все значения — непустые строки."""
        for code, label in FUNNEL_STAGES.items():
            assert isinstance(label, str) and label, f"Пустое название для стадии {code!r}"


# ===========================================================================
# stage_label — каждый код маппится правильно
# ===========================================================================


class TestStageLabel:
    """Каждый из 13 кодов возвращает правильное название."""

    def test_new(self):
        assert stage_label("new") == "Новый"

    def test_qualifying(self):
        assert stage_label("qualifying") == "Первичное общение"

    def test_photo_pending(self):
        assert stage_label("photo_pending") == "Жду фото"

    def test_qualified(self):
        assert stage_label("qualified") == "Прошёл проверку"

    def test_pitched(self):
        assert stage_label("pitched") == "Показала цену"

    def test_videocall_set(self):
        assert stage_label("videocall_set") == "Записан на звонок"

    def test_client_starter(self):
        assert stage_label("client_starter") == "Клиент Starter"

    def test_client_standard(self):
        assert stage_label("client_standard") == "Клиент Standard"

    def test_client_vip(self):
        assert stage_label("client_vip") == "Клиент VIP"

    def test_event_attended(self):
        assert stage_label("event_attended") == "Пришёл на ивент"

    def test_rejected(self):
        assert stage_label("rejected") == "Не подошёл"

    def test_lost(self):
        assert stage_label("lost") == "Отказался"

    def test_nurture(self):
        assert stage_label("nurture") == "Лист ожидания"


class TestStageLabelFallback:
    """None и неизвестные коды возвращают дефолтное название ('Новый')."""

    def test_none_returns_default(self):
        assert stage_label(None) == "Новый"

    def test_empty_string_returns_default(self):
        assert stage_label("") == "Новый"

    def test_unknown_code_returns_default(self):
        assert stage_label("bogus") == "Новый"

    def test_uppercase_returns_default(self):
        """Коды регистрозависимы: 'NEW' ≠ 'new'."""
        assert stage_label("NEW") == "Новый"

    def test_whitespace_returns_default(self):
        assert stage_label("  ") == "Новый"

    def test_default_label_matches_new_stage(self):
        """Дефолт stage_label должен совпадать с явным 'new'."""
        assert stage_label("bogus") == stage_label("new")


# ===========================================================================
# NO_FOLLOWUP_STAGES
# ===========================================================================


class TestNoFollowupStages:

    def test_is_frozenset(self):
        assert isinstance(NO_FOLLOWUP_STAGES, frozenset)

    def test_lost_in_no_followup(self):
        assert "lost" in NO_FOLLOWUP_STAGES

    def test_rejected_in_no_followup(self):
        assert "rejected" in NO_FOLLOWUP_STAGES

    def test_nurture_in_no_followup(self):
        assert "nurture" in NO_FOLLOWUP_STAGES

    def test_client_starter_in_no_followup(self):
        assert "client_starter" in NO_FOLLOWUP_STAGES

    def test_client_standard_in_no_followup(self):
        assert "client_standard" in NO_FOLLOWUP_STAGES

    def test_client_vip_in_no_followup(self):
        assert "client_vip" in NO_FOLLOWUP_STAGES

    def test_event_attended_in_no_followup(self):
        assert "event_attended" in NO_FOLLOWUP_STAGES

    def test_active_qualifying_not_in_no_followup(self):
        assert "qualifying" not in NO_FOLLOWUP_STAGES

    def test_active_new_not_in_no_followup(self):
        assert "new" not in NO_FOLLOWUP_STAGES

    def test_active_photo_pending_not_in_no_followup(self):
        assert "photo_pending" not in NO_FOLLOWUP_STAGES

    def test_active_pitched_not_in_no_followup(self):
        assert "pitched" not in NO_FOLLOWUP_STAGES

    def test_active_videocall_set_not_in_no_followup(self):
        assert "videocall_set" not in NO_FOLLOWUP_STAGES

    def test_active_qualified_not_in_no_followup(self):
        assert "qualified" not in NO_FOLLOWUP_STAGES

    def test_all_no_followup_stages_exist_in_funnel_stages(self):
        """Каждый код из NO_FOLLOWUP_STAGES должен быть в FUNNEL_STAGES."""
        for code in NO_FOLLOWUP_STAGES:
            assert code in FUNNEL_STAGES, (
                f"Стадия {code!r} есть в NO_FOLLOWUP_STAGES, но отсутствует в FUNNEL_STAGES"
            )


# ===========================================================================
# FOLLOWUP_FIRST_DELAY_HOURS
# ===========================================================================


class TestFollowupFirstDelayHours:

    def test_qualified_48h(self):
        assert FOLLOWUP_FIRST_DELAY_HOURS["qualified"] == 48

    def test_pitched_48h(self):
        assert FOLLOWUP_FIRST_DELAY_HOURS["pitched"] == 48

    def test_videocall_set_24h(self):
        assert FOLLOWUP_FIRST_DELAY_HOURS["videocall_set"] == 24

    def test_early_stages_have_delay(self):
        """Ранние стадии тоже догоняются (блок 13): new/qualifying/photo_pending."""
        assert FOLLOWUP_FIRST_DELAY_HOURS["new"] == 48
        assert FOLLOWUP_FIRST_DELAY_HOURS["qualifying"] == 24
        assert FOLLOWUP_FIRST_DELAY_HOURS["photo_pending"] == 24

    def test_covers_all_active_stages(self):
        """Все активные стадии имеют задержку догона (иначе молчун на стадии не пингуется)."""
        for stage in funnel.ACTIVE_STAGES:
            assert stage in FOLLOWUP_FIRST_DELAY_HOURS, f"нет задержки для {stage}"

    def test_all_values_are_positive_int(self):
        """Все задержки — положительные целые числа (часы)."""
        for stage, hours in FOLLOWUP_FIRST_DELAY_HOURS.items():
            assert isinstance(hours, int) and hours > 0, (
                f"Задержка для {stage!r} должна быть положительным int, получили {hours!r}"
            )

    def test_followup_stages_not_in_no_followup(self):
        """Стадии с заданным фоллоу-апом не должны быть в NO_FOLLOWUP_STAGES."""
        for stage in FOLLOWUP_FIRST_DELAY_HOURS:
            assert stage not in NO_FOLLOWUP_STAGES, (
                f"Стадия {stage!r} одновременно в FOLLOWUP_FIRST_DELAY_HOURS и NO_FOLLOWUP_STAGES"
            )

    def test_followup_stages_exist_in_funnel_stages(self):
        """Все стадии с фоллоу-апом должны быть в FUNNEL_STAGES."""
        for stage in FOLLOWUP_FIRST_DELAY_HOURS:
            assert stage in FUNNEL_STAGES, f"Стадия {stage!r} нет в FUNNEL_STAGES"
