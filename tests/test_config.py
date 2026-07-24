"""Unit-тесты config.py — вычисляемые свойства Settings."""
from __future__ import annotations

from config import Settings


class TestTgManagerChatIds:
    def test_single_value_no_comma(self):
        s = Settings(tg_manager_chat_id="111")
        assert s.tg_manager_chat_ids == ("111",)

    def test_multiple_values_comma_separated(self):
        s = Settings(tg_manager_chat_id="111,222,333")
        assert s.tg_manager_chat_ids == ("111", "222", "333")

    def test_whitespace_around_commas_stripped(self):
        s = Settings(tg_manager_chat_id=" 111 , 222 ,333 ")
        assert s.tg_manager_chat_ids == ("111", "222", "333")

    def test_empty_value_returns_empty_tuple(self):
        s = Settings(tg_manager_chat_id="")
        assert s.tg_manager_chat_ids == ()

    def test_trailing_comma_no_empty_entries(self):
        s = Settings(tg_manager_chat_id="111,222,")
        assert s.tg_manager_chat_ids == ("111", "222")
