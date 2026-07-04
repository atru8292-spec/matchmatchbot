"""Транскрибация голосовых сообщений лида через OpenAI Whisper.

Лиды иногда шлют голосовые вместо текста. Раньше бот всегда просил написать текстом
(сценарий №35). Теперь голосовое реально транскрибируется и обрабатывается как обычный
текст (RAG + генерация ответа). Если транскрибация не удалась — вызывающий откатывается
на плейсхолдер (сценарий №35 «me lo escribes?»), лид без ответа не остаётся.

Устойчивость: ретраи на 429/5xx/сеть; после исчерпания — пробрасываем (main ловит,
шлёт алерт и оставляет плейсхолдер).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from config import settings

logger = logging.getLogger("matchmatch.voice")

_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"

# WhatsApp-голосовые — ogg/opus. Имя файла нужно Whisper для определения формата.
_DEFAULT_FILENAME = "voice.ogg"
_DEFAULT_MIME = "audio/ogg"

# Ретраи (как в ai._openai_post): временный сбой не должен сразу ронять транскрибацию.
_MAX_RETRIES = 3
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_BACKOFF_BASE = 1.0  # сек; задержка attempt = base * 2**attempt (1, 2, 4)
_TIMEOUT = 60.0  # аудио может качаться/обрабатываться дольше текста


async def transcribe(audio_bytes: bytes, filename: str = _DEFAULT_FILENAME) -> str:
    """Транскрибировать аудио через Whisper. Вернуть распознанный текст (может быть пустым).

    Бросает при пустом аудио или после исчерпания ретраев (вызывающий откатится на №35).
    """
    if not audio_bytes:
        raise ValueError("пустое аудио — нечего транскрибировать")

    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    files = {"file": (filename, audio_bytes, _DEFAULT_MIME)}
    # language='es' закрепляем явно: лиды пишут на испанском. Без подсказки Whisper
    # может неверно определить язык на коротком/шумном аудио и выдать НЕ пустой, а
    # мусорный текст — он обошёл бы проверку на пустоту и ушёл в AI как реальный ввод.
    data = {"model": settings.openai_whisper_model, "language": "es"}

    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(_TRANSCRIBE_URL, headers=headers, files=files, data=data)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt >= _MAX_RETRIES:
                raise
            logger.warning("Whisper сеть %r — ретрай %d/%d", e, attempt + 1, _MAX_RETRIES)
            await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
            continue
        if r.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
            logger.warning("Whisper %d — ретрай %d/%d", r.status_code, attempt + 1, _MAX_RETRIES)
            await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
            continue
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
        logger.info("Whisper распознал %d символов", len(text))
        return text

    raise RuntimeError("Whisper: ретраи исчерпаны")  # недостижимо (loop выходит return/raise)
