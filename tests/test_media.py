"""Тесты обработки медиа (media.py): классификация + транскод видео (ffmpeg замокан).

Реальный ffmpeg не требуется — мокаем subprocess. Один интеграционный тест с настоящим
ffmpeg помечен и пропускается, если ffmpeg не установлен.
"""
from __future__ import annotations

import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import media


class TestClassify:
    def test_image_types(self):
        assert media.classify("image/jpeg") == "image"
        assert media.classify("image/png") == "image"
        assert media.classify("image/webp") == "image"

    def test_video_types(self):
        assert media.classify("video/mp4") == "video"
        assert media.classify("video/quicktime") == "video"  # .mov с айфона
        assert media.classify("video/3gpp") == "video"

    def test_unsupported(self):
        assert media.classify("application/pdf") == ""
        assert media.classify("") == ""


class TestBitrateCalc:
    def test_unknown_duration_conservative(self):
        assert media._target_video_bitrate_kbps(0) == 1500

    def test_short_video_high_bitrate(self):
        # 10 сек под 15 МБ → высокий битрейт
        assert media._target_video_bitrate_kbps(10) > 1000

    def test_long_video_floored(self):
        # очень длинное → битрейт не ниже 300
        assert media._target_video_bitrate_kbps(100000) == 300


class TestTranscodeMocked:
    def _fake_size(self, size):
        """Патчим ffprobe (длительность) + ffmpeg (создаёт файл нужного размера)."""
        def fake_run(cmd, **kw):
            if cmd[0] == "ffprobe":
                return MagicMock(stdout=b'{"format":{"duration":"10.0"}}')
            # ffmpeg: пишем «выходной» файл нужного размера (последний арг — путь)
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * size)
            return MagicMock(returncode=0)
        return fake_run

    def test_fits_first_pass(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", self._fake_size(10 * 1024 * 1024))
        out = media.transcode_video(b"fake-video")
        assert len(out) == 10 * 1024 * 1024  # ≤16 МБ → принято с первого прохода

    def test_too_large_raises(self, monkeypatch):
        # оба прохода дают 20 МБ → VideoTooLargeError
        monkeypatch.setattr(subprocess, "run", self._fake_size(20 * 1024 * 1024))
        with pytest.raises(media.VideoTooLargeError) as e:
            media.transcode_video(b"fake-video")
        assert "16 MB" in str(e.value)  # понятное сообщение с лимитом

    def test_ffmpeg_failure_raises_processing(self, monkeypatch):
        def boom(cmd, **kw):
            if cmd[0] == "ffprobe":
                return MagicMock(stdout=b'{"format":{"duration":"10.0"}}')
            raise subprocess.CalledProcessError(1, cmd, stderr=b"bad codec")
        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(media.VideoProcessingError):
            media.transcode_video(b"fake-video")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg не установлен")
class TestTranscodeReal:
    def test_real_transcode_produces_h264_mp4(self, tmp_path):
        src = tmp_path / "in.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(src)],
            capture_output=True, check=True)
        out = media.transcode_video(src.read_bytes())
        assert len(out) <= media.VIDEO_MAX_BYTES and len(out) > 0
