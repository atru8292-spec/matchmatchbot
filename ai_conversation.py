"""Режим ответов rag: разговор по базе знаний с памятью лида (app_settings.reply_mode).

Этап 0: только точка входа. Логика (память, поиск по knowledge, проверки) — этап 2.
Пока бросает NotImplementedError: ai.generate_reply ловит её и отвечает старым путём.
"""
from __future__ import annotations


async def generate_reply(lead: dict, history: list[dict], user_text: str) -> dict:
    """Ответ в режиме rag. Контракт результата тот же, что у ai.generate_reply."""
    raise NotImplementedError("режим rag ещё не реализован (этап 2)")
