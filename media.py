"""Обработка медиа с ивентов: транскод видео под требования Wazzup.

Wazzup принимает видео СТРОГО: .mp4/.3gpp, кодек H.264 + AAC, одна аудиодорожка,
максимум 16 МБ (подтверждено в доках Wazzup). Телефоны часто снимают .mov/HEVC и
тяжелее 16 МБ → перекодируем КАЖДОЕ видео в mp4/H.264/AAC 720p и ужимаем под 16 МБ.

Если после сжатия видео всё равно >16 МБ (слишком длинное) — бросаем VideoTooLargeError
с понятным сообщением: Аня сама обрежет/сожмёт и попробует снова (по решению владельца —
без document/link-фолбэка).

ffmpeg/ffprobe — системные (apt install ffmpeg на сервере). Вызовы синхронные в
subprocess, оборачиваются в asyncio.to_thread вызывающим (эндпоинт загрузки).
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("matchmatch.media")

# Лимиты Wazzup (подтверждено в доках, НЕ общий WhatsApp):
VIDEO_MAX_BYTES = 16 * 1024 * 1024   # видео: 16 МБ
IMAGE_MAX_BYTES = 5 * 1024 * 1024    # фото: 5 МБ (как у приглашения)
UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # вход-кап (защита): не принимаем >200 МБ
_TARGET_BYTES = 15 * 1024 * 1024      # цель сжатия — 15 МБ (запас под 16)
_TARGET_HEIGHT = 720                  # даунскейл до 720p
_AUDIO_BITRATE_KBPS = 128
_FFMPEG_TIMEOUT = 300                 # сек на один прогон транскода

IMAGE_EXTS = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
              "image/webp": "webp"}
VIDEO_MIMES = ("video/mp4", "video/quicktime", "video/3gpp", "video/x-matroska",
               "video/webm", "video/x-msvideo")


class VideoTooLargeError(Exception):
    """Видео не влезло в лимит Wazzup даже после сжатия — Аня должна обрезать/сжать сама."""


class VideoProcessingError(Exception):
    """Сбой ffmpeg/ffprobe (битый файл, нет кодека и т.п.)."""


def _probe_duration(path: Path) -> float:
    """Длительность видео в секундах (ffprobe). 0.0 если не удалось определить."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, timeout=60, check=True,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        logger.warning("ffprobe: не смог определить длительность %s", path)
        return 0.0


def _target_video_bitrate_kbps(duration_s: float) -> int:
    """Битрейт видео (кбит/с), чтобы уложиться в _TARGET_BYTES с учётом аудио.

    size ≈ (v_bitrate + a_bitrate) * duration / 8. Решаем относительно v_bitrate.
    Минимум 300 кбит/с (ниже — каша). При неизвестной длительности — консервативно 1500.
    """
    if duration_s <= 0:
        return 1500
    total_kbits = _TARGET_BYTES * 8 / 1000
    v_kbps = int(total_kbits / duration_s) - _AUDIO_BITRATE_KBPS
    return max(v_kbps, 300)


def _run_ffmpeg(src: Path, dst: Path, v_kbps: int) -> None:
    """Транскод в mp4/H.264/AAC/одна аудиодорожка, 720p, целевой битрейт. Бросает на сбое."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale=-2:{_TARGET_HEIGHT}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.5)}k", "-bufsize", f"{v_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{_AUDIO_BITRATE_KBPS}k", "-ac", "2",
        "-map", "0:v:0", "-map", "0:a:0?",   # одна видео- + одна аудиодорожка (Wazzup требует)
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT, check=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", "ignore")[-500:]
        logger.error("ffmpeg упал: %s", stderr)
        raise VideoProcessingError("no se pudo procesar el video (formato no soportado)")
    except subprocess.TimeoutExpired:
        raise VideoProcessingError("el video tardó demasiado en procesarse")


def transcode_video(raw: bytes) -> bytes:
    """Перекодировать видео под Wazzup (mp4/H.264/AAC ≤16 МБ). Вернуть mp4-байты.

    Два прохода при необходимости: 1) по длительности рассчитываем битрейт; если итог
    всё равно >16 МБ — 2) повтор с половинным битрейтом. Если и это >16 МБ →
    VideoTooLargeError (Аня обрежет видео сама).
    """
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in"
        src.write_bytes(raw)
        dur = _probe_duration(src)
        for attempt, factor in enumerate((1.0, 0.5)):
            dst = Path(td) / f"out{attempt}.mp4"
            v_kbps = max(int(_target_video_bitrate_kbps(dur) * factor), 300)
            _run_ffmpeg(src, dst, v_kbps)
            size = dst.stat().st_size
            logger.info("транскод видео: попытка %d, %d кбит/с → %.1f МБ",
                        attempt + 1, v_kbps, size / 1024 / 1024)
            if size <= VIDEO_MAX_BYTES:
                return dst.read_bytes()
        # даже после второго прохода не влезло
        raise VideoTooLargeError(
            f"El video sigue pesando más de {VIDEO_MAX_BYTES // (1024 * 1024)} MB tras comprimir. "
            "Recórtalo o baja la calidad (Wazzup no acepta videos más pesados) e inténtalo de nuevo.")


def classify(content_type: str) -> str:
    """'image' | 'video' по MIME. Иначе '' (не поддерживаем)."""
    ct = (content_type or "").lower()
    if ct in IMAGE_EXTS:
        return "image"
    if ct in VIDEO_MIMES or ct.startswith("video/"):
        return "video"
    return ""
