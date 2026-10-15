"""Канонизация телефонных номеров.

Wazzup присылает мексиканские мобильные как 'wa_521' + 10 цифр (старый формат WhatsApp
с «лишней» 1), а импорты (HubSpot и пр.) часто дают '52' + 10 цифр без единицы. Это ОДИН
номер. Чтобы whitelist/bypass/поиск лида не промахивались, сравниваем всегда по канону.

Канон для Мексики — 'wa_521XXXXXXXXXX' (как в leads/messages). Остальные страны —
'wa_' + цифры как есть. Хранимый формат в БД НЕ меняем, только сравниваем по канону.
"""
from __future__ import annotations

import re

# Мексиканский национальный номер (10 цифр) начинается с 2-9, никогда с 1 — так
# «52 + 10 цифр» не путается с битым «521 + 9 цифр».
_MX_NO_ONE = re.compile(r"^52([2-9]\d{9})$")  # 52 + 10 цифр (без единицы)
_MX_WITH_ONE = re.compile(r"^521([2-9]\d{9})$")  # 521 + 10 цифр (формат Wazzup)


def _digits(phone) -> str:
    """Только цифры из любого входа ('wa_', '+', пробелы, дефисы уходят). None → ''."""
    return re.sub(r"\D", "", str(phone)) if phone is not None else ""


def canonical_phone(phone) -> str:
    """Канонический ключ номера: 'wa_521XXXXXXXXXX' для Мексики, 'wa_<цифры>' для прочих.

    Пустой/бесцифровой/None вход → '' (не падает; '' ни с чем не совпадёт).
    """
    digits = _digits(phone)
    if not digits:
        return ""
    m = _MX_NO_ONE.match(digits)
    if m:
        return "wa_521" + m.group(1)
    return "wa_" + digits


def phone_variants(phone) -> list[str]:
    """Все формы, под которыми номер может лежать в БД (для WHERE phone = ANY(...)).

    Мексика → [канон 'wa_521…', 'wa_52…']; прочие → [канон]; мусор → [].
    """
    canon = canonical_phone(phone)
    if not canon:
        return []
    m = _MX_WITH_ONE.match(canon[3:])
    if m:
        return [canon, "wa_52" + m.group(1)]
    return [canon]


def same_phone(a, b) -> bool:
    """Один ли это номер (по канону). Пустые значения никогда не совпадают."""
    ca = canonical_phone(a)
    return bool(ca) and ca == canonical_phone(b)


def canonical_set(phones) -> frozenset[str]:
    """Множество канонов (пустые выкидываются) — для проверки 'номер in bypass'."""
    return frozenset(c for c in (canonical_phone(p) for p in (phones or ())) if c)


def sql_canonical(expr: str) -> str:
    """SQL-выражение канона для колонки с 'wa_…' номером (для JOIN по канону).

    'wa_52' + 10 цифр (первая 2-9) → 'wa_521' + те же 10; всё остальное без изменений.
    expr подставляется в SQL как есть — передавать ТОЛЬКО имена колонок из кода.
    """
    return (f"(CASE WHEN {expr} ~ '^wa_52[2-9][0-9]{{9}}$' "
            f"THEN 'wa_521' || substr({expr}, 6) ELSE {expr} END)")
