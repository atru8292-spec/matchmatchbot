"""Стадии воронки: единый источник кодов и их русских названий (для TG/мини-аппа).

funnel_stage в БД хранит КОД. Здесь маппинг код→название и правила фоллоу-апов.
Каждая смена стадии пишется в funnel_events (см. db.set_funnel_stage).
"""
from __future__ import annotations

# Код стадии → человекочитаемое название (RU).
FUNNEL_STAGES: dict[str, str] = {
    # активные
    "new": "Новый",
    "qualifying": "Знакомлюсь",
    "photo_pending": "Жду фото",
    "qualified": "Прошёл проверку",
    "pitched": "Показала цену",
    "videocall_set": "Записан на звонок",
    # клиенты
    "client_starter": "Клиент Starter",       # $1,400/мес
    "client_standard": "Клиент Standard",     # $10k/6мес
    "client_vip": "Клиент VIP",               # $14k/год
    "event_attended": "Пришёл на ивент",      # 9k или 4k песо
    # не сложилось
    "rejected": "Не подошёл",
    "lost": "Отказался",
    "nurture": "Лист ожидания",
}

DEFAULT_STAGE = "new"

# Стадии, для которых НЕ ставим next_followup_at (не догоняем фоллоу-апами).
NO_FOLLOWUP_STAGES: frozenset[str] = frozenset({
    "lost", "rejected", "nurture",
    "client_starter", "client_standard", "client_vip", "event_attended",
})

# Задел под фоллоу-апы (планировщик — отдельный блок позже).
# stage → задержка первого догона в ЧАСАХ от смены стадии. Растущие интервалы и
# лимит попыток планировщик применит сам; здесь — момент первого next_followup_at.
FOLLOWUP_FIRST_DELAY_HOURS: dict[str, int] = {
    "qualified": 48,       # замолчал после проверки: +2 дня
    "pitched": 48,         # думает над ценой: +2 дня
    "videocall_set": 24,   # записан, но не пришёл: +1 день
}


def stage_label(code: str | None) -> str:
    """Название стадии по коду. Неизвестный/пустой код → название дефолтной стадии."""
    if not code:
        return FUNNEL_STAGES[DEFAULT_STAGE]
    return FUNNEL_STAGES.get(code, FUNNEL_STAGES[DEFAULT_STAGE])
