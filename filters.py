"""Детерминированный слой решений (без AI): отвечать / молчать / блок / отказ.

Чистые функции — тестируются без БД. Данные (лид, флаг whitelist, текст) передаются
снаружи. Аналог Evaluate context + детерминированных веток Auto-action router из WF1.
Порядок проверок важен: whitelist раньше блокировок раньше квалификации.

AI-зависимые ветки (профессия, casual/несерьёзность, тон отказа) сюда НЕ входят —
для них возвращается action='needs_ai' (реальный вызов AI встанет в блоке 6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Возрастной фильтр (из CLAUDE.md): 28-65 включительно.
MIN_AGE = 28
MAX_AGE = 65

# Явные дисквалификаторы по ключевым словам (испанский). \b — границы слова:
# 'sexo' не ловится в 'sexto'/'sexta'. Основа как в BLUEPRINT (force-escalate WF1).
_ESCORT_RE = re.compile(
    r"\b(escorts?|sexo|sexual(es)?|prostit\w*|acompañant\w*|acompanant\w*|"
    r"servicios?\s+sexual(es)?)\b",
    re.IGNORECASE,
)
_AGGRESSION_RE = re.compile(
    r"\b(idiota|est[uú]pid[oa]|pendej\w*|mierda|cabr[oó]n|est[aá]fa|estafador\w*|fraude)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    """Результат детерминированного решения по залпу лида."""
    action: str                 # respond | silent_whitelist | blocked | rejected | needs_ai
    reason: str                 # краткая причина (для лога/алерта/эскалации)
    alert_manager: bool = False # нужно ли уведомить Аню (сам алерт — блок 8)
    block_permanent: bool = False  # блок навсегда (do_not_contact + manual надолго)
    is_escort: bool = False     # escort-блок (инкремент escort_mention_count); не завязываемся на текст reason


def is_escort_mention(text: str) -> bool:
    """Явное упоминание интим-услуг (по границам слова)."""
    return bool(_ESCORT_RE.search(text or ""))


def is_aggression(text: str) -> bool:
    """Явная агрессия/оскорбление."""
    return bool(_AGGRESSION_RE.search(text or ""))


def _manual_active(lead: dict) -> bool:
    """Лид в ручном режиме с активным manual_until (менеджер ведёт диалог)."""
    if lead.get("mode") != "manual":
        return False
    until = lead.get("manual_until")
    if until is None:
        return True  # manual без срока — считаем активным
    # manual_until может быть datetime (из asyncpg). Сравнение с now в БД-слое было бы
    # точнее, но здесь достаточно: если срок задан и в прошлом — уже не активен.
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > now
    except Exception:
        return True


def decide(lead: dict, is_whitelisted: bool, user_text: str) -> Decision:
    """Принять детерминированное решение по лиду и склеенному тексту залпа.

    lead — строка leads (dict) или {} для нового. Порядок приоритетов фиксирован.
    """
    lead = lead or {}
    text = user_text or ""
    name = lead.get("whatsapp_name") or lead.get("name") or "лид"

    # 1) Whitelist / manual / DNC → бот молчит, но уведомляем Аню.
    if is_whitelisted:
        return Decision("silent_whitelist", f"whitelist: написал {name}", alert_manager=True)
    if lead.get("do_not_contact"):
        return Decision("silent_whitelist", f"do_not_contact: {name}", alert_manager=True)
    if _manual_active(lead):
        return Decision("silent_whitelist", f"manual mode: менеджер ведёт {name}", alert_manager=True)

    # 2) Escort/секс-услуги → блок навсегда (с ПЕРВОГО упоминания).
    if is_escort_mention(text):
        return Decision("blocked", "Ищет интим-услуги", alert_manager=True,
                        block_permanent=True, is_escort=True)

    # 3) Явная агрессия → блок.
    if is_aggression(text):
        return Decision("blocked", "Агрессивное поведение", alert_manager=True, block_permanent=True)

    # 4) Жёсткая дисквалификация по УЖЕ известным полям (заполнит AI в блоке 6).
    age = lead.get("age")
    if isinstance(age, int) and (age < MIN_AGE or age > MAX_AGE):
        return Decision("rejected", f"Возраст {age} вне {MIN_AGE}-{MAX_AGE}")
    if lead.get("is_single") is False:
        return Decision("rejected", "Не холост")

    # 5) Остальное — решает AI (квалификация, профессия, casual, тон).
    return Decision("needs_ai", "нужен AI")
