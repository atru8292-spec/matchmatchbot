"""Переключатель режима ответов: app_settings.reply_mode.

  legacy   — старый путь ai.generate_reply (по умолчанию: ключа нет, пусто, неизвестное
             значение, любая ошибка чтения);
  rag      — новый режим (ai_conversation) для всех лидов;
  rag_test — новый режим только для тестовых номеров (SILENT_BYPASS_PHONES ∪ тестовые
             номера мини-CRM), остальным legacy.

Ключ читается при каждом ответе без кэша (db.get_setting ходит в БД каждый раз):
переключение действует со следующего сообщения, без деплоя и рестарта.
"""
from __future__ import annotations

import logging

import db
from phones import canonical_phone

logger = logging.getLogger(__name__)

REPLY_MODE_KEY = "reply_mode"
LEGACY = "legacy"
RAG = "rag"
RAG_TEST = "rag_test"
_KNOWN = {LEGACY, RAG, RAG_TEST}


async def resolve(phone: str | None) -> str:
    """Какой режим применить к ответу этому лиду: 'legacy' или 'rag'. Никогда не бросает."""
    # get_setting (не get_settings): отдельный запрос одного ключа, не смешивается с
    # чтением настроек ивента в старом пути.
    try:
        raw = await db.get_setting(REPLY_MODE_KEY)
    except Exception as e:
        logger.warning("reply_mode: не прочитала app_settings (%r) → legacy", e)
        return LEGACY
    mode = (raw or "").strip().lower()
    if mode not in _KNOWN:
        if mode:
            logger.warning("reply_mode: неизвестное значение %r → legacy", raw)
        return LEGACY
    if mode == RAG:
        return RAG
    if mode == RAG_TEST:
        if not phone:
            return LEGACY
        try:
            bypass = await db.bypass_phone_set()
        except Exception:
            logger.exception("reply_mode: не прочитала тестовые номера → legacy")
            return LEGACY
        return RAG if canonical_phone(phone) in bypass else LEGACY
    return LEGACY
