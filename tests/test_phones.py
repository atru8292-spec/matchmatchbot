"""Тесты канонизации номеров (phones.py) и её применения в filters/config."""
import pytest

import filters
from config import Settings
from phones import canonical_phone, canonical_set, phone_variants, same_phone, sql_canonical


class TestCanonicalPhone:
    @pytest.mark.parametrize("raw", [
        "wa_5215512345678", "wa_525512345678", "5215512345678", "525512345678",
        "+52 1 55 1234 5678", "+52 55 1234-5678", "52 (55) 1234 5678",
    ])
    def test_mexican_both_forms_same_canon(self, raw):
        """С единицей и без, с префиксом/пробелами/скобками — один канон wa_521…"""
        assert canonical_phone(raw) == "wa_5215512345678"

    @pytest.mark.parametrize("raw,expected", [
        ("wa_79635708880", "wa_79635708880"),      # Россия
        ("wa_34612345678", "wa_34612345678"),      # Испания
        ("wa_5491123456789", "wa_5491123456789"),  # Аргентина
        ("wa_14056646694", "wa_14056646694"),      # США
        ("wa_918780743253", "wa_918780743253"),    # Индия
    ])
    def test_other_countries_untouched(self, raw, expected):
        assert canonical_phone(raw) == expected

    def test_52_followed_by_1_is_not_mexican_no_one(self):
        """'52' + '1…' (12 цифр) — это битый 521 + 9 цифр, единицу не добавляем."""
        assert canonical_phone("wa_521234567890") == "wa_521234567890"

    def test_bare_10_digits_not_guessed(self):
        """10 цифр без кода страны — страну не угадываем, оставляем как есть."""
        assert canonical_phone("5512345678") == "wa_5512345678"

    @pytest.mark.parametrize("raw", [None, "", "   ", "wa_", "abc", "+-()", "—"])
    def test_garbage_returns_empty(self, raw):
        assert canonical_phone(raw) == ""

    def test_int_input_does_not_crash(self):
        assert canonical_phone(5215512345678) == "wa_5215512345678"


class TestPhoneVariants:
    def test_mexican_gives_both_forms(self):
        assert phone_variants("wa_525512345678") == ["wa_5215512345678", "wa_525512345678"]
        assert phone_variants("wa_5215512345678") == ["wa_5215512345678", "wa_525512345678"]

    def test_other_country_single(self):
        assert phone_variants("wa_79635708880") == ["wa_79635708880"]

    def test_garbage_empty(self):
        assert phone_variants(None) == []
        assert phone_variants("") == []


class TestSamePhoneAndSet:
    def test_same_phone(self):
        assert same_phone("wa_525512345678", "wa_5215512345678")
        assert not same_phone("wa_525512345678", "wa_525512345679")
        assert not same_phone("", "")
        assert not same_phone(None, None)

    def test_canonical_set_drops_garbage(self):
        assert canonical_set(["525512345678", "", None, "wa_79635708880"]) == frozenset(
            {"wa_5215512345678", "wa_79635708880"})
        assert canonical_set(None) == frozenset()


class TestBypassUsesCanon:
    def test_env_bypass_set_canonical(self):
        s = Settings(silent_bypass_phones="525525098289, 79635708880,,")
        assert s.silent_bypass_set == frozenset({"wa_5215525098289", "wa_79635708880"})

    def test_decide_bypass_matches_other_form_on_pause(self):
        """Тестовый номер в ENV без единицы → пауза его НЕ глушит, хотя Wazzup шлёт с 1."""
        d = filters.decide({}, False, "hola", "wa_5215525098289",
                           frozenset({"wa_525525098289"}), bot_paused=True)
        assert d.action != "silent" or "пауз" not in d.reason

    def test_decide_non_bypass_silent_on_pause(self):
        d = filters.decide({}, False, "hola", "wa_5215511111111",
                           frozenset({"wa_525525098289"}), bot_paused=True)
        assert d.action == "silent" and "пауз" in d.reason

    def test_decide_empty_phone_not_bypass(self):
        """Пустой номер не должен случайно совпасть с пустым элементом bypass."""
        d = filters.decide({}, False, "hola", "", frozenset({""}), bot_paused=True)
        assert d.action == "silent"


class TestSqlCanonical:
    def test_expression_shape(self):
        sql = sql_canonical("w.phone")
        assert "w.phone" in sql and "wa_521" in sql and "CASE" in sql
        # регулярка с первой цифрой 2-9 (битый 521+9 цифр не трогаем)
        assert "^wa_52[2-9][0-9]{9}$" in sql
