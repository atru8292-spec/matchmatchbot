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

# Возрастной фильтр (владелец поднял верхнюю границу 2026-07-26): 28-76 включительно.
MIN_AGE = 28
MAX_AGE = 76

# Явные дисквалификаторы по ключевым словам (испанский). \b — границы слова:
# 'sexo' не ловится в 'sexto'/'sexta'. Основа как в BLUEPRINT (force-escalate WF1).
_ESCORT_RE = re.compile(
    r"\b(escorts?|sexo|sexual(es)?|prostit\w*|acompañant\w*|acompanant\w*|"
    r"servicios?\s+sexual(es)?)\b",
    re.IGNORECASE,
)
# Excepciones (2026-08-06, encontrado en auditoría — mismo patrón que _SCAM_NEGATED_RE):
# - "¿puedo llevar acompañante al evento?" es pedir traer +1 (política de amigo al evento,
#   ver anna_prompt_v5.md) — NO es escort. acompañant\w* solo cazamos junto a sexo/servicio,
#   no en el contexto neutro "llevar/traer a alguien al evento".
# - "no busco sexo" / "no quiero nada sexual" es la NEGACIÓN — el lead confirmando que
#   busca algo serio, justo lo contrario de lo que el filtro intenta cazar.
_ESCORT_NEGATED_RE = re.compile(
    r"(llevar|traer|venir\s+con|puedo\s+llevar|puedo\s+traer)\s+(un[a]?\s+|mi\s+)?acompañant\w*|"
    r"no\s+(busco|quiero|me\s+interesa|es)\s+(solo\s+|nada\s+)?(el\s+)?(sexo|sexual)",
    re.IGNORECASE,
)
# 'estafa'/'fraude' sueltos NO están aquí (ver nota abajo) — 'estafador' sí se queda
# (acusación directa a una persona, más inequívoco que la palabra suelta).
_AGGRESSION_RE = re.compile(
    r"\b(idiota|est[uú]pid[oa]|pendej\w*|mierda|cabr[oó]n|estafador\w*)\b",
    re.IGNORECASE,
)
# Excepción (2026-08-06): "está cabrón el precio" es modismo mexicano ("está intenso/
# fuerte"), NO insulto — solo "cabrón" dirigido a una persona (eres/pinche cabrón) es
# agresión real.
_AGGRESSION_NEGATED_RE = re.compile(
    r"est[aá]\s+cabr[oó]n",
    re.IGNORECASE,
)
# 'estafa'/'fraude' sueltos SE QUITARON de _AGGRESSION_RE (auditoría 2026-08-06):
# incluso con la excepción de negación ("no es estafa"), seguían bloqueando NAVEGATE
# preguntas legítimas de duda sin "no" — "¿esto es estafa o real?", "me da miedo que
# sea una estafa" (escenario #22 "es seguro/no confío/suena raro" existe justo para
# esto vía AI, con matices que una regex no puede dar). Bloqueo permanente por duda
# razonable de un lead sobre un servicio de $10,000 USD es un costo de negocio mucho
# mayor que dejar pasar una rara queja real ("qué estafa!") al AI en vez de auto-block.
# Кириллица — признак нецелевого лида (агентство работает с мексиканцами по-испански).
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")

# Заявление об оплате (блок 13). Только claim-формы («я оплатил»), НЕ вопрос про оплату
# ("cómo es el pago?"): 'pago'/'pagar' без claim-контекста намеренно не ловим.
_PAYMENT_RE = re.compile(
    r"\b(pagu[eé]|ya\s+pagu[eé]|pagad[oa]s?|deposit[eé]|transfer[ií]|"
    r"hice\s+el\s+pago|оплати\w*)\b",
    re.IGNORECASE,
)
# Excepción (2026-08-06): negación ("aún no he pagado") — es un aviso de que TODAVÍA
# no pagó, lo contrario de un claim de pago. La regla del '?' en is_payment_claim cubre
# además preguntas sin negación ("¿ya están pagados los boletos?").
_PAYMENT_NEGATED_RE = re.compile(
    r"(no|a[uú]n\s+no|todav[ií]a\s+no)\s+(lo\s+|la\s+|los\s+|les\s+)?(he\s+|ha\s+|han\s+)?"
    r"(pagad[oa]s?|pagu[eé]|deposit[eé]|transfer[ií])",
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


# Opt-out / стоп-слово: ЯВНАЯ просьба прекратить контакт / удалить / оставить в покое.
# НЕ путать с мягким «no me interesa» (#17, лист ожидания — лид может вернуться).
# Требуем контакт-стоп-интент, поэтому «no me interesa / no gracias / paso» сюда НЕ попадают.
# Осторожно с ловушками: «no me molesta» (=«меня не смущает», ПОЗИТИВ) ≠ «no me molestes»
# (императив, opt-out) — ловим только императивные формы molest(-es/-en/-ar).
# Голое «baja»/«alto»/«stop» не берём (риск ложных) — только в связке.
# ИСПРАВЛЕНО (2026-08-06, аудит): 'escrib\w*'/'contact\w*' были жадными — ловили не
# только императив («no me escribas» = opt-out), но и прошедшее/жалобу («no me
# escribiste el link» = лид напоминает про обещанное, а его банили навсегда). Сузили
# до явных повелительных форм (escribas/escriban/contactes/contacten).
_OPTOUT_RE = re.compile(
    r"no\s+me\s+(escribas|escriban|contactes|contacten|vuelvas?\s+a\s+escribir|manden?\s+mensajes?)|"
    r"no\s+me\s+vuelv\w+\s+a\s+(escribir|contactar|molestar)|"
    r"dej[ae]n?\s+de\s+(escribir\w*|molestar\w*|mandar\w*)|"
    r"d[eé]jame\s+(en\s+paz|de\s+escribir\w*)|d[eé]jenme\s+en\s+paz|"
    r"no\s+me\s+molest(es|en|e\s+m[aá]s|ar)|"
    r"(quiero\s+)?(darme|dar)\s+de\s+baja|"
    r"b[oó]rrame|elim[ií]name|borra\s+mi\s+n[uú]mero|"
    r"(qu[ií]tame|s[aá]came)\s+de\s+(tu|la)\s+lista|"
    r"не\s+пиши\w*\s+мне|не\s+пишите\s+(мне|больше)|отпиш\w+|отписаться|"
    r"удалите\s+мой\s+номер|хватит\s+писать|не\s+беспоко\w+",
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
    """Явное упоминание интим-услуг. НЕ ловит «llevar acompañante al evento» или «no busco sexo»."""
    text = text or ""
    if not _ESCORT_RE.search(text):
        return False
    if _ESCORT_NEGATED_RE.search(text):
        return False
    return True


def is_aggression(text: str) -> bool:
    """Явная агрессия/оскорбление. НЕ ловит модизм "está cabrón" (=intenso, no insulto)."""
    text = text or ""
    if not _AGGRESSION_RE.search(text):
        return False
    if _AGGRESSION_NEGATED_RE.search(text):
        return False
    return True


def is_payment_claim(text: str) -> bool:
    """Лид заявляет, что оплатил (claim-форма). НЕ ловит отрицание или вопрос про оплату."""
    text = text or ""
    if not _PAYMENT_RE.search(text):
        return False
    if _PAYMENT_NEGATED_RE.search(text):
        return False
    if "?" in text:
        # Реальный claim об оплате почти никогда не вопрос — «¿ya están pagados?»
        # это сомнение/вопрос, а не заявление «я заплатил».
        return False
    return True


def is_optout(text: str) -> bool:
    """Лид явно просит прекратить контакт / отписаться (opt-out). НЕ мягкое «no me interesa»."""
    return bool(_OPTOUT_RE.search(text or ""))


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
           bypass_phones: frozenset = frozenset(),
           whitelist_no_alert: bool = False, bot_paused: bool = False) -> Decision:
    """Принять детерминированное решение по лиду и склеенному тексту залпа.

    lead — строка leads (dict) или {} для нового. Порядок приоритетов фиксирован.
    phone — 'wa_<digits>' (для проверки региона); по умолчанию '' (совместимость).
    bypass_phones — номера-исключения silent-фильтра (тестовые/доверенные).
    whitelist_no_alert — whitelist-запись это личный контакт Anna (reason personal_contact):
        бот молчит БЕЗ алерта Ане. VIP-клиент (False) — алерт остаётся.
    bot_paused — глобальная пауза (тумблер в CRM): бот молчит ВСЕМ, кроме bypass_phones
        (тестовые номера). Для тех-режима: подключаем/правим, отвечаем только на свой номер.
    """
    lead = lead or {}
    text = user_text or ""
    name = lead.get("whatsapp_name") or lead.get("name") or "лид"

    # 1) Whitelist → бот молчит. VIP-клиент: + алерт «написал клиент». personal_contact
    #    (личная база Anna): без алерта — она и так ведёт эти чаты в WhatsApp вручную.
    if is_whitelisted:
        return Decision("silent_whitelist", f"whitelist: написал {name}",
                        alert_manager=not whitelist_no_alert)
    # do_not_contact / manual → бот молчит БЕЗ алерта: заблокированный нарушитель не должен
    # спамить Аню VIP-уведомлением на каждое сообщение; в manual Аня и так в чате WhatsApp.
    if lead.get("do_not_contact"):
        return Decision("silent", f"do_not_contact — молчу: {name}")
    # Opt-out ДО manual/региона: явная просьба «не пиши» ставит do_not_contact навсегда,
    # даже если лида вёл менеджер (иначе при возврате в auto догоны возобновятся).
    if is_optout(text):
        return Decision("optout", "лид попросил не писать (opt-out)", alert_manager=True)
    # Глобальная пауза (тех. режим) — ПОСЛЕ whitelist/opt-out (согласие лида и VIP-алерты чтим
    #    даже на паузе), молчим всем остальным, кроме тестовых bypass-номеров. Без алерта.
    if bot_paused and phone not in bypass_phones:
        return Decision("silent", "бот на паузе (тех. режим) — молчу")
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
