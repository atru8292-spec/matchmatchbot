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
# Кириллица — признак нецелевого лида (агентство работает с мексиканцами по-испански).
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")

# Заявление об оплате (блок 13). Только claim-формы («я оплатил»), НЕ вопрос про оплату
# ("cómo es el pago?"): 'pago'/'pagar' без claim-контекста намеренно не ловим.
_PAYMENT_RE = re.compile(
    r"\b(pagu[eé]|ya\s+pagu[eé]|pagad[oa]s?|deposit[eé]|transfer[ií]|"
    r"hice\s+el\s+pago|оплати\w*)\b",
    re.IGNORECASE,
)

# Instagram вместо фото: ссылка ig, слово instagram/insta, или @хэндл. Ловим ТОЛЬКО
# на стадии photo_pending (см. decide) — чтобы «vi su Instagram» в первом сообщении
# (стадия new) не триггерило. Бот не умеет валидировать IG-профиль как фото (Vision),
# поэтому передаём Ане на ручную проверку.
_INSTAGRAM_RE = re.compile(
    # (?<![a-zA-Z0-9@]) перед @хэндлом — чтобы не ловить локальную часть email
    # (juan@hotmail.com: @ идёт после буквы → не совпадёт, а «@handle» после пробела/начала — да).
    r"(instagram\.com/|instagr\.am/|\binstagram\b|\binsta\b|(?<![a-zA-Z0-9@])@[a-zA-Z0-9._]{3,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    """Результат детерминированного решения по залпу лида."""
    action: str                 # respond | silent_whitelist | silent | blocked | rejected | needs_ai
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


def is_payment_claim(text: str) -> bool:
    """Лид заявляет, что оплатил (claim-форма, не вопрос про оплату)."""
    return bool(_PAYMENT_RE.search(text or ""))


def has_instagram(text: str) -> bool:
    """Лид дал Instagram (ссылку/@хэндл/слово). Гейтить стадией photo_pending в decide."""
    return bool(_INSTAGRAM_RE.search(text or ""))


def is_russian_number(phone: str) -> bool:
    """Номер с кодом страны +7 (Россия/Казахстан) — не целевой регион.

    Код +7 однозначный: только он начинается с 7 (нет других кодов стран на 7).
    Мексика — wa_52, поэтому wa_7... надёжно отделяется от других префиксов.
    """
    return (phone or "").startswith("wa_7")


def has_cyrillic(text: str) -> bool:
    """Текст содержит кириллицу (русский язык) — не целевой лид."""
    return bool(_CYRILLIC_RE.search(text or ""))


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


def decide(lead: dict, is_whitelisted: bool, user_text: str, phone: str = "",
           bypass_phones: frozenset = frozenset()) -> Decision:
    """Принять детерминированное решение по лиду и склеенному тексту залпа.

    lead — строка leads (dict) или {} для нового. Порядок приоритетов фиксирован.
    phone — 'wa_<digits>' (для проверки региона); по умолчанию '' (совместимость).
    bypass_phones — номера-исключения silent-фильтра (тестовые/доверенные).
    """
    lead = lead or {}
    text = user_text or ""
    name = lead.get("whatsapp_name") or lead.get("name") or "лид"

    # 1) Whitelist → бот молчит + алерт «написал клиент из списка» (Аня ведёт лично).
    if is_whitelisted:
        return Decision("silent_whitelist", f"whitelist: написал {name}", alert_manager=True)
    # do_not_contact / manual → бот молчит БЕЗ алерта: заблокированный нарушитель не должен
    # спамить Аню VIP-уведомлением на каждое сообщение; в manual Аня и так в чате WhatsApp.
    if lead.get("do_not_contact"):
        return Decision("silent", f"do_not_contact — молчу: {name}")
    if _manual_active(lead):
        return Decision("silent", f"manual mode — менеджер ведёт: {name}")

    # 1.5) Не целевой регион/язык (русский номер +7 или кириллица) → тихо молчим.
    #      НЕ блокируем (не дисквалификация, вдруг ошибка) — просто не тратим AI-вызов.
    #      Номера из bypass_phones (тестовые/доверенные) проверку пропускают.
    if phone not in bypass_phones:
        if is_russian_number(phone):
            return Decision("silent", "молчу — русский номер +7, не целевой регион")
        if has_cyrillic(text):
            return Decision("silent", "молчу — кириллица/русский язык, не целевой лид")

    # 2) Escort/секс-услуги → блок навсегда (с ПЕРВОГО упоминания).
    if is_escort_mention(text):
        return Decision("blocked", "Ищет интим-услуги", alert_manager=True,
                        block_permanent=True, is_escort=True)

    # 3) Явная агрессия → блок.
    if is_aggression(text):
        return Decision("blocked", "Агрессивное поведение", alert_manager=True, block_permanent=True)

    # 3.5) Заявление об оплате → ручное подтверждение Аней (блок 13). Бот НЕ меняет
    #      стадию сам: шлёт ack + эскалация с кнопкой «Подтвердить оплату».
    if is_payment_claim(text):
        return Decision("payment_claim", "лид сообщил об оплате", alert_manager=True)

    # 4) Жёсткая дисквалификация по УЖЕ известным полям (заполнит AI в блоке 6).
    age = lead.get("age")
    if isinstance(age, int) and (age < MIN_AGE or age > MAX_AGE):
        return Decision("rejected", f"Возраст {age} вне {MIN_AGE}-{MAX_AGE}")
    if lead.get("is_single") is False:
        return Decision("rejected", "Не холост")

    # 4.5) Instagram вместо фото (стадия photo_pending): бот не валидирует IG как фото —
    #      короткий бридж-ответ + ручной режим + алерт Ане (она смотрит профиль лично).
    if lead.get("funnel_stage") == "photo_pending" and has_instagram(text):
        return Decision("instagram_handoff", "Instagram вместо фото — на Аню", alert_manager=True)

    # 5) Остальное — решает AI (квалификация, профессия, casual, тон).
    return Decision("needs_ai", "нужен AI")
