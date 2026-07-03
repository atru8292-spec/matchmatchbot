"""Стадии воронки: единый источник кодов и их русских названий (для TG/мини-аппа).

funnel_stage в БД хранит КОД. Здесь маппинг код→название и правила фоллоу-апов.
Каждая смена стадии пишется в funnel_events (см. db.set_funnel_stage).
"""
from __future__ import annotations

# Код стадии → человекочитаемое название (RU).
FUNNEL_STAGES: dict[str, str] = {
    # активные
    "new": "Новый",
    "qualifying": "Первичное общение",
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

# Активные стадии — лид ещё в работе (не клиент, не отказ). Для /leads менеджер-бота.
ACTIVE_STAGES: tuple[str, ...] = (
    "new", "qualifying", "photo_pending", "qualified", "pitched", "videocall_set",
)

# Первый догон: stage → задержка первого next_followup_at в ЧАСАХ от смены стадии.
# Дальше интервалы задаёт FOLLOWUP_LADDER (планировщик — scheduler.py, блок 13).
FOLLOWUP_FIRST_DELAY_HOURS: dict[str, int] = {
    # ранние стадии — контакт свежий, пингуем скорее
    "new": 48,             # написал, не втянулся: +2 дня
    "qualifying": 24,      # общались, замолк: +1 день
    "photo_pending": 24,   # не прислал фото: +1 день
    # поздние стадии
    "qualified": 48,       # замолчал после проверки: +2 дня
    "pitched": 48,         # думает над ценой: +2 дня
    "videocall_set": 24,   # записан, но не пришёл: +1 день
}


# Лестница фоллоу-апов (блок 13). Индекс = followup_sent_count (сколько уже слали):
# 0 → 1-я попытка и т.д. Кортеж (scenario_id, next_delay_days) — какой сценарий слать
# и через сколько дней ставить следующий next_followup_at (None → больше не догоняем).
# Первый догон ставится при смене стадии из FOLLOWUP_FIRST_DELAY_HOURS, дальше +5/+10.
# Цену/ценность ($1,400, сценарий 38) показываем на 2-й ступени (день ~7), не в конце.
FOLLOWUP_LADDER: tuple[tuple[int, int | None], ...] = (
    (36, 5),      # 1-я: мягко «ещё интересно?»
    (38, 10),     # 2-я: ценность пакета + $1,400 (раньше — не в конце)
    (33, None),   # 3-я (последняя): напоминание про анкету
)
MAX_FOLLOWUPS = len(FOLLOWUP_LADDER)

# Напоминания об ивенте НЕ шлём тем, кто уже в этих стадиях (отказ/не подошёл).
EVENT_REMINDER_EXCLUDE_STAGES: tuple[str, ...] = ("lost", "rejected")


def stage_label(code: str | None) -> str:
    """Название стадии по коду. Неизвестный/пустой код → название дефолтной стадии."""
    if not code:
        return FUNNEL_STAGES[DEFAULT_STAGE]
    return FUNNEL_STAGES.get(code, FUNNEL_STAGES[DEFAULT_STAGE])
