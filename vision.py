"""Фото-модерация: скачивание из Wazzup + Vision-классификация + загрузка в Storage.

Vision по base64 (не завязываемся на публичный Storage URL — надёжнее). Промпт —
из vision_prompt.md (единый одобренный источник). Вердикты: ok/retry/reject/manual.
Storage (Supabase) — для хранения/истории/менеджера, вердикт от него не зависит.

Устойчивость: сбой Vision → фолбэк manual (Аня решит); сбой Storage → (None,None),
вердикт не теряем; сбой скачивания → пробрасываем (вызывающий обработает).
"""
from __future__ import annotations

import base64
import json
import logging
import os

import httpx

from config import settings

logger = logging.getLogger("matchmatch.vision")

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "vision_prompt.md")
_vision_prompt_cache: str | None = None
VALID_VERDICTS = {"ok", "retry", "reject", "manual"}
MAX_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MB — защита от OOM (Wazzup может отдать видео)


def load_vision_prompt() -> str:
    """Промпт Vision из vision_prompt.md — берём блок инструкции внутри ```-фенса.

    В файле промпт лежит в тройных бэктиках; вырезаем его. Фолбэк — весь файл.
    """
    global _vision_prompt_cache
    if _vision_prompt_cache is None:
        with open(_PROMPT_PATH, encoding="utf-8") as f:
            content = f.read()
        # первый блок ```...``` — это сам промпт
        start = content.find("```")
        if start != -1:
            start = content.find("\n", start) + 1
            end = content.find("```", start)
            _vision_prompt_cache = content[start:end].strip() if end != -1 else content
        else:
            _vision_prompt_cache = content
    return _vision_prompt_cache


async def download_media(content_uri: str) -> bytes:
    """Скачать медиа по URL из вебхука Wazzup. Бросает при ошибке (вызывающий ловит).

    Защита от OOM: качаем СТРИМОМ с накопительным счётчиком байт и обрываем при
    превышении MAX_MEDIA_BYTES — не грузим в память видео на сотни МБ (Content-Length
    может отсутствовать/врать). follow_redirects=True: CDN Wazzup может отдать 30x.
    """
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        async with client.stream("GET", content_uri) as r:
            r.raise_for_status()
            cl = r.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > MAX_MEDIA_BYTES:
                raise ValueError(f"медиа слишком большое: {cl} байт (лимит {MAX_MEDIA_BYTES})")
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise ValueError(f"медиа слишком большое: >{MAX_MEDIA_BYTES} байт (лимит)")
                chunks.append(chunk)
            return b"".join(chunks)


async def analyze_photo(image_bytes: bytes) -> dict:
    """Классифицировать фото через Vision (base64). Вернуть {verdict, reason, ...}.

    Ошибка/таймаут/неверный ответ → фолбэк manual (безопасно: Аня посмотрит вручную).
    """
    try:
        b64 = base64.b64encode(image_bytes).decode()
        # mime по сигнатуре файла (WhatsApp/Wazzup могут отдать PNG, не только JPEG)
        mime = "image/png" if image_bytes[:4] == b"\x89PNG" else "image/jpeg"
        data_uri = f"data:{mime};base64,{b64}"
        payload = {
            "model": settings.openai_vision_model,
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": load_vision_prompt()},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                ],
            }],
        }
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
        verdict = data.get("verdict")
        if verdict not in VALID_VERDICTS:
            logger.warning("Vision вернул неизвестный verdict %r → manual", verdict)
            return {"verdict": "manual", "reason": f"неизвестный вердикт: {verdict}"}
        return {"verdict": verdict, "reason": data.get("reason", "")}
    except Exception:
        logger.exception("Vision-анализ упал → фолбэк manual")
        return {"verdict": "manual", "reason": "vision failed"}


_IMG_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
            "image/webp": "webp", "image/gif": "gif"}


async def upload_invitation(image_bytes: bytes, content_type: str = "image/jpeg") -> str | None:
    """Загрузить картинку-приглашение в Supabase Storage. Вернуть public_url или None.

    Переиспользуем ТОТ ЖЕ публичный bucket, что и фото лидов (supabase_storage_bucket),
    но под префиксом invitations/ — гарантированно публичный URL (его тянет Wazzup при
    отправке). Путь — по хешу содержимого (идемпотентно, без Date.now)."""
    if not settings.supabase_url or not settings.supabase_service_key:
        logger.debug("Storage не настроен — приглашение не загружено")
        return None
    try:
        import hashlib
        ext = _IMG_EXT.get((content_type or "").lower(), "jpg")
        digest = hashlib.sha1(image_bytes).hexdigest()[:16]
        path = f"invitations/{digest}.{ext}"
        base = settings.supabase_url.rstrip("/")
        bucket = settings.supabase_storage_bucket
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{base}/storage/v1/object/{bucket}/{path}",
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": content_type or "image/jpeg",
                    "x-upsert": "true",
                },
                content=image_bytes,
            )
            r.raise_for_status()
        return f"{base}/storage/v1/object/public/{bucket}/{path}"
    except Exception:
        logger.exception("upload_invitation упал")
        return None


async def upload_event_media(data: bytes, ext: str,
                             content_type: str) -> tuple[str | None, str | None]:
    """Загрузить медиа с ивента (фото/видео) в Storage под префиксом event-media/.

    Тот же bucket/паттерн, что upload_invitation (путь по хешу — идемпотентно). Вернуть
    (public_url, path) или (None, None) при сбое/ненастроенном Storage."""
    if not settings.supabase_url or not settings.supabase_service_key:
        logger.debug("Storage не настроен — event-media не загружено")
        return (None, None)
    try:
        import hashlib
        digest = hashlib.sha1(data).hexdigest()[:16]
        path = f"{digest}.{ext}"
        base = settings.supabase_url.rstrip("/")
        bucket = settings.supabase_event_media_bucket  # отдельный bucket (видео + 20 МБ)
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{base}/storage/v1/object/{bucket}/{path}",
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
                content=data,
            )
            r.raise_for_status()
        return (f"{base}/storage/v1/object/public/{bucket}/{path}", path)
    except Exception:
        logger.exception("upload_event_media упал")
        return (None, None)


async def upload_to_storage(phone: str, image_bytes: bytes) -> tuple[str | None, str | None]:
    """Загрузить фото в Supabase Storage (bucket lead-photos). Вернуть (public_url, path).

    Ошибка → (None, None) + лог: вердикт Vision важнее, хранение не критично.
    path строим детерминированно из phone (без времени в этом слое — уникальность по messageId
    добавит вызывающий при необходимости; здесь простой путь <phone>/<len>.jpg-подобный).
    """
    if not settings.supabase_url or not settings.supabase_service_key:
        logger.debug("Storage не настроен (нет url/service_key), фото не загружено")
        return (None, None)
    try:
        # уникальность пути — по хешу содержимого (без Date.now, недоступного тут стабильно)
        import hashlib
        digest = hashlib.sha1(image_bytes).hexdigest()[:16]
        path = f"{phone}/{digest}.jpg"
        base = settings.supabase_url.rstrip("/")
        bucket = settings.supabase_storage_bucket
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{base}/storage/v1/object/{bucket}/{path}",
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "image/jpeg",
                    "x-upsert": "true",
                },
                content=image_bytes,
            )
            r.raise_for_status()
        public_url = f"{base}/storage/v1/object/public/{bucket}/{path}"
        return (public_url, path)
    except Exception:
        logger.exception("upload_to_storage упал для %s", phone)
        return (None, None)
