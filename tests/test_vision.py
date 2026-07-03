"""Тесты модуля vision.py (фото-модерация).

Мокаем httpx.AsyncClient через monkeypatch — никакой реальной сети.
Мокаем settings для upload_to_storage (supabase_url / service_key).
Кэш load_vision_prompt() изолируем фикстурой clear_cache.
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import vision
from config import settings as real_settings


# ---------------------------------------------------------------------------
# Вспомогательные фабрики фейк-клиентов
# ---------------------------------------------------------------------------

def _make_get_client(*, content: bytes = b"fake_image_bytes", raise_status_exc=None,
                     headers=None):
    """Фейк AsyncClient со stream('GET', ...) — для download_media (стриминг с cap)."""
    mock_resp = MagicMock()
    mock_resp.headers = headers or {}
    if raise_status_exc is not None:
        mock_resp.raise_for_status = MagicMock(side_effect=raise_status_exc)
    else:
        mock_resp.raise_for_status = MagicMock()

    async def _aiter():
        yield content
    mock_resp.aiter_bytes = lambda: _aiter()

    class _StreamCtx:
        async def __aenter__(self):
            return mock_resp

        async def __aexit__(self, *exc_info):
            return False

    class FakeGetClient:
        def __init__(self, **kwargs):
            pass

        def stream(self, method, url):
            return _StreamCtx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    FakeGetClient._response = mock_resp
    return FakeGetClient


def _make_post_client(*, response_json=None, post_exc=None, status_code=200):
    """Фейк AsyncClient с методом post — для analyze_photo и upload_to_storage."""
    mock_resp = MagicMock()
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


# ---------------------------------------------------------------------------
# 1. load_vision_prompt
# ---------------------------------------------------------------------------

class TestLoadVisionPrompt:

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Сброс кэша перед и после каждого теста — изоляция."""
        vision._vision_prompt_cache = None
        yield
        vision._vision_prompt_cache = None

    def test_not_empty(self):
        """Промпт загружается и не пуст."""
        prompt = vision.load_vision_prompt()
        assert prompt and len(prompt) > 0

    def test_contains_photo_moderator(self):
        """Промпт содержит 'photo moderator' (первая строка инструкции)."""
        prompt = vision.load_vision_prompt()
        assert "photo moderator" in prompt

    def test_not_contains_markdown_heading(self):
        """В промпте нет markdown-заголовка файла '# Vision' (только тело блока)."""
        prompt = vision.load_vision_prompt()
        assert "# Vision" not in prompt

    def test_cache_returns_same_object(self):
        """Второй вызов возвращает тот же объект (кэш, а не перечитывает файл)."""
        p1 = vision.load_vision_prompt()
        p2 = vision.load_vision_prompt()
        assert p1 is p2


# ---------------------------------------------------------------------------
# 2. download_media
# ---------------------------------------------------------------------------

class TestDownloadMedia:

    async def test_success_returns_bytes(self, monkeypatch):
        """GET 200 → возвращает r.content (bytes)."""
        cls = _make_get_client(content=b"jpeg_image_data")
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.download_media("https://wazzup.example.com/media/abc")

        assert result == b"jpeg_image_data"

    async def test_http_error_raises(self, monkeypatch):
        """raise_for_status бросает HTTPStatusError → download_media НЕ глотает, пробрасывает."""
        exc = httpx.HTTPStatusError(
            "404 Not Found",
            request=httpx.Request("GET", "https://wazzup.example.com/media/abc"),
            response=httpx.Response(404),
        )
        cls = _make_get_client(raise_status_exc=exc)
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        with pytest.raises(httpx.HTTPStatusError):
            await vision.download_media("https://wazzup.example.com/media/abc")


# ---------------------------------------------------------------------------
# 3–7. analyze_photo
# ---------------------------------------------------------------------------

def _openai_resp(content_str: str) -> dict:
    """Собрать фейк-ответ OpenAI с нужным content."""
    return {"choices": [{"message": {"content": content_str}}]}


class TestAnalyzePhoto:

    @pytest.fixture(autouse=True)
    def ensure_prompt_cached(self):
        """Гарантируем, что кэш промпта заполнен до тестов (не мешает мокам клиента)."""
        if vision._vision_prompt_cache is None:
            vision.load_vision_prompt()

    async def test_success_returns_verdict_and_reason(self, monkeypatch):
        """OpenAI вернул валидный JSON → возвращаем dict с verdict и reason."""
        openai_response = _openai_resp('{"verdict":"ok","reason":"чётко"}')
        cls = _make_post_client(response_json=openai_response)
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.analyze_photo(b"test_image")

        assert result == {"verdict": "ok", "reason": "чётко"}

    async def test_payload_contains_base64_data_uri(self, monkeypatch):
        """В payload есть data-URI с base64 изображением, detail=high,
        model из settings, temperature=0."""
        image_bytes = b"sample_image_bytes_for_test"
        expected_b64 = base64.b64encode(image_bytes).decode()
        openai_response = _openai_resp('{"verdict":"ok","reason":"test"}')
        cls = _make_post_client(response_json=openai_response)
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        await vision.analyze_photo(image_bytes)

        # Проверяем что post был вызван с нужным payload
        call_kwargs = cls._post.call_args.kwargs
        payload = call_kwargs["json"]

        # model и temperature
        assert payload["model"] == real_settings.openai_vision_model
        assert payload["temperature"] == 0

        # Сообщение содержит image_url с base64 data-URI и detail=high
        content_parts = payload["messages"][0]["content"]
        image_part = next(p for p in content_parts if p["type"] == "image_url")
        url = image_part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,"), f"URL не data-URI: {url[:50]}"
        assert expected_b64 in url, "base64 тела изображения не найден в URL"
        assert image_part["image_url"]["detail"] == "high"

    async def test_invalid_verdict_returns_manual(self, monkeypatch):
        """OpenAI вернул неизвестный verdict 'garbage' → verdict='manual'."""
        cls = _make_post_client(response_json=_openai_resp('{"verdict":"garbage"}'))
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.analyze_photo(b"img")

        assert result["verdict"] == "manual"

    async def test_missing_verdict_returns_manual(self, monkeypatch):
        """OpenAI вернул JSON без поля verdict → verdict='manual'."""
        cls = _make_post_client(response_json=_openai_resp('{"reason":"x"}'))
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.analyze_photo(b"img")

        assert result["verdict"] == "manual"

    async def test_openai_exception_returns_manual_no_raise(self, monkeypatch):
        """httpx.post бросает исключение → возвращаем manual, не пробрасываем."""
        cls = _make_post_client(post_exc=Exception("network timeout"))
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.analyze_photo(b"img")

        assert result == {"verdict": "manual", "reason": "vision failed"}

    async def test_bad_json_returns_manual_no_raise(self, monkeypatch):
        """OpenAI вернул не-JSON контент → json.loads падает → фолбэк manual (не бросает)."""
        cls = _make_post_client(response_json=_openai_resp("not json at all {{{{"))
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.analyze_photo(b"img")

        assert result["verdict"] == "manual"

    async def test_authorization_header_contains_bearer(self, monkeypatch):
        """POST-запрос к OpenAI содержит заголовок Authorization с Bearer."""
        cls = _make_post_client(response_json=_openai_resp('{"verdict":"ok","reason":"ok"}'))
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        await vision.analyze_photo(b"img")

        headers = cls._post.call_args.kwargs.get("headers", {})
        assert "Bearer" in headers.get("Authorization", ""), (
            f"Нет Bearer в Authorization: {headers}"
        )


# ---------------------------------------------------------------------------
# 8–10. upload_to_storage
# ---------------------------------------------------------------------------

class TestUploadToStorage:

    async def test_empty_supabase_url_returns_none_no_http(self, monkeypatch):
        """supabase_url='' → сразу (None, None), httpx НЕ вызван."""
        # Монтируем пустые настройки Storage
        monkeypatch.setattr(vision.settings, "supabase_url", "")
        monkeypatch.setattr(vision.settings, "supabase_service_key", "key")

        # Мок клиента, которого НЕ должны вызвать
        cls = _make_post_client()
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.upload_to_storage("wa_79635378880", b"img")

        assert result == (None, None)
        cls._post.assert_not_awaited()

    async def test_empty_service_key_returns_none_no_http(self, monkeypatch):
        """supabase_service_key='' → сразу (None, None), httpx НЕ вызван."""
        monkeypatch.setattr(vision.settings, "supabase_url", "https://x.supabase.co")
        monkeypatch.setattr(vision.settings, "supabase_service_key", "")

        cls = _make_post_client()
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.upload_to_storage("wa_79635378880", b"img")

        assert result == (None, None)
        cls._post.assert_not_awaited()

    async def test_success_returns_url_and_path(self, monkeypatch):
        """POST 200 → возвращает (public_url, path); проверяем формат path и url."""
        monkeypatch.setattr(vision.settings, "supabase_url", "https://x.supabase.co")
        monkeypatch.setattr(vision.settings, "supabase_service_key", "test-service-key")
        monkeypatch.setattr(vision.settings, "supabase_storage_bucket", "lead-photos")

        cls = _make_post_client()
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        phone = "wa_79635378880"
        url, path = await vision.upload_to_storage(phone, b"real_image_data")

        assert url is not None
        assert path is not None
        # path = <phone>/<sha1[:16]>.jpg
        assert path.endswith(".jpg"), f"path не заканчивается на .jpg: {path}"
        assert path.startswith("wa_79"), f"path не начинается с wa_79: {path}"
        # public_url содержит /public/
        assert "/public/" in url, f"/public/ не найден в url: {url}"

    async def test_success_headers_contain_bearer_and_content_type(self, monkeypatch):
        """POST-запрос содержит Authorization Bearer и Content-Type image/jpeg."""
        monkeypatch.setattr(vision.settings, "supabase_url", "https://x.supabase.co")
        monkeypatch.setattr(vision.settings, "supabase_service_key", "test-service-key")
        monkeypatch.setattr(vision.settings, "supabase_storage_bucket", "lead-photos")

        cls = _make_post_client()
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        await vision.upload_to_storage("wa_79635378880", b"img")

        headers = cls._post.call_args.kwargs.get("headers", {})
        assert "Bearer" in headers.get("Authorization", ""), (
            f"Нет Bearer: {headers}"
        )
        assert headers.get("Content-Type") == "image/jpeg", (
            f"Content-Type не image/jpeg: {headers}"
        )

    async def test_post_failure_returns_none_no_raise(self, monkeypatch):
        """POST бросает исключение → возвращает (None, None), не пробрасывает."""
        monkeypatch.setattr(vision.settings, "supabase_url", "https://x.supabase.co")
        monkeypatch.setattr(vision.settings, "supabase_service_key", "test-service-key")
        monkeypatch.setattr(vision.settings, "supabase_storage_bucket", "lead-photos")

        cls = _make_post_client(post_exc=Exception("storage unavailable"))
        monkeypatch.setattr(vision.httpx, "AsyncClient", cls)

        result = await vision.upload_to_storage("wa_79635378880", b"img")

        assert result == (None, None)
