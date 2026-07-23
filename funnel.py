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
    "client_agency": "Клиент агентства",      # платит за персональный сервис (тариф — на звонке у Ани)
    "event_attended": "Гость ивента",         # купил билет на конкретный ивент
    # не сложилось
    "rejected": "Не подошёл",
    "lost": "Отказался",
    "nurture": "Лист ожидания",
}

DEFAULT_STAGE = "new"

# Стадии, для которых НЕ ставим next_followup_at (не догоняем фоллоу-апами).
NO_FOLLOWUP_STAGES: frozenset[str] = frozenset({
    "lost", "rejected", "nurture",
    "client_agency", "event_attended",
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


# Интервалы между догонами (дни до следующего next_followup_at), по номеру попытки.
# Индекс = followup_sent_count (0 → после 1-й попытки ждём 5 дней и т.д.). None → стоп.
# Первый next_followup_at ставится при смене стадии из FOLLOWUP_FIRST_DELAY_HOURS.
# КАКОЙ сценарий слать выбирает followup_scenario_for(lead) по стадии/анкете (не по позиции).
FOLLOWUP_INTERVALS: tuple[int | None, ...] = (5, 10, None)
MAX_FOLLOWUPS = len(FOLLOWUP_INTERVALS)

# Поля, собираемые ТОЛЬКО в анкете-в-чате (не в обычной квалификации) — по ним понимаем,
# начата/готова ли анкета, чтобы выбрать правильный догон.
ANKETA_CORE_FIELDS = ("email", "date_of_birth", "country", "desired_partner_age")


def anketa_complete(lead: dict) -> bool:
    """Все ключевые анкетные поля собраны."""
    return all(lead.get(f) for f in ANKETA_CORE_FIELDS)


def followup_scenario_for(lead: dict) -> int | None:
    """Какой сценарий-догон слать по состоянию лида (стадийная логика, не слепая лестница).

    None → не догоняем этим механизмом (звонок назначен — напоминает #49).
    - старый контакт (tag 'old_base') → #38 (сегмент ПУСТ сейчас — фактически не сработает)
    - звонок назначен (videocall_set) → None
    - анкета готова, звонок не назначен → #33 «agendemos la videollamada»
    - анкета начата, но не полна → #32 «faltan un par de datos»
    - холодный/ранний молчун (анкета не начата) → #36 «sigues buscando?» + мягкий опт-аут
    """
    if "old_base" in (lead.get("tags") or []):
        return 38
    if lead.get("funnel_stage") == "videocall_set":
        return None
    if anketa_complete(lead):
        return 33
    if any(lead.get(f) for f in ANKETA_CORE_FIELDS):
        return 32
    return 36

# Напоминания об ивенте НЕ шлём тем, кто уже в этих стадиях (отказ/не подошёл).
EVENT_REMINDER_EXCLUDE_STAGES: tuple[str, ...] = ("lost", "rejected")


def stage_label(code: str | None) -> str:
    """Название стадии по коду. Неизвестный/пустой код → название дефолтной стадии."""
    if not code:
        return FUNNEL_STAGES[DEFAULT_STAGE]
    return FUNNEL_STAGES.get(code, FUNNEL_STAGES[DEFAULT_STAGE])
