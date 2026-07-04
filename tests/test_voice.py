"""Тесты модуля voice.py (транскрибация голосовых через Whisper).

Мокаем httpx.AsyncClient через monkeypatch — никакой реальной сети.
asyncio_mode=auto (pytest.ini) — async-тесты без декоратора.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import voice
from config import settings as real_settings


def _make_post_client(*, response_json=None, post_exc=None, status_code=200,
                      raise_status_exc=None):
    """Фейк AsyncClient с методом post — для voice.transcribe."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if raise_status_exc is not None:
        mock_resp.raise_for_status = MagicMock(side_effect=raise_status_exc)
    else:
        mock_resp.raise_for_status = MagicMock()
    if response_json is not None:
        mock_resp.json = MagicMock(return_value=response_json)

    if post_exc is not None:
        _post = AsyncMock(side_effect=post_exc)
    else:
        _post = AsyncMock(return_value=mock_resp)

    class FakePostClient:
        def __init__(self, **kwargs):
            self.post = _post

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    FakePostClient._post = _post
    FakePostClient._response = mock_resp
    return FakePostClient


class TestTranscribe:

    async def test_success_returns_stripped_text(self, monkeypatch):
        """Whisper вернул текст → возвращаем его без окружающих пробелов."""
        cls = _make_post_client(response_json={"text": "  hola soy soltero  "})
        monkeypatch.setattr(voice.httpx, "AsyncClient", cls)

        result = await voice.transcribe(b"ogg_audio_bytes")

        assert result == "hola soy soltero"

    async def test_empty_audio_raises(self, monkeypatch):
        """Пустое аудио → ValueError, сеть не дёргаем."""
        cls = _make_post_client(response_json={"text": "x"})
        monkeypatch.setattr(voice.httpx, "AsyncClient", cls)

        with pytest.raises(ValueError):
            await voice.transcribe(b"")

        cls._post.assert_not_awaited()

    async def test_payload_has_file_model_and_bearer(self, monkeypatch):
        """POST содержит multipart file, model из settings и Authorization Bearer."""
        cls = _make_post_client(response_json={"text": "ok"})
        monkeypatch.setattr(voice.httpx, "AsyncClient", cls)

        await voice.transcribe(b"audio", filename="nota.ogg")

        kwargs = cls._post.call_args.kwargs
        # файл передан multipart-ом
        assert "file" in kwargs["files"]
        fname, fbytes, mime = kwargs["files"]["file"]
        assert fname == "nota.ogg" and fbytes == b"audio" and mime == "audio/ogg"
        # модель из settings
        assert kwargs["data"]["model"] == real_settings.openai_whisper_model
        # язык закреплён испанским (лиды пишут на испанском)
        assert kwargs["data"]["language"] == "es"
        # Bearer
        assert "Bearer" in kwargs["headers"].get("Authorization", "")

    async def test_empty_transcript_returns_empty_string(self, monkeypatch):
        """Whisper вернул пустой текст (тишина/шум) → пустая строка (не бросаем)."""
        cls = _make_post_client(response_json={"text": "   "})
        monkeypatch.setattr(voice.httpx, "AsyncClient", cls)

        result = await voice.transcribe(b"silence")

        assert result == ""

    async def test_retries_on_500_then_succeeds(self, monkeypatch):
        """500 на первой попытке → ретрай → на второй успех. sleep замокан."""
        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status = MagicMock()
        ok.json = MagicMock(return_value={"text": "listo"})
        fail = MagicMock()
        fail.status_code = 500
        fail.raise_for_status = MagicMock()

        _post = AsyncMock(side_effect=[fail, ok])

        class FakeClient:
            def __init__(self, **kwargs):
                self.post = _post

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        monkeypatch.setattr(voice.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(voice.asyncio, "sleep", AsyncMock())

        result = await voice.transcribe(b"audio")

        assert result == "listo"
        assert _post.await_count == 2

    async def test_http_400_raises(self, monkeypatch):
        """Неретраибельный 400 → raise_for_status бросает, пробрасываем наверх."""
        exc = httpx.HTTPStatusError(
            "400 Bad Request",
            request=httpx.Request("POST", voice._TRANSCRIBE_URL),
            response=httpx.Response(400),
        )
        cls = _make_post_client(status_code=400, raise_status_exc=exc)
        monkeypatch.setattr(voice.httpx, "AsyncClient", cls)

        with pytest.raises(httpx.HTTPStatusError):
            await voice.transcribe(b"audio")
