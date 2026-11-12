from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from app.services.document_parser.formats.media.probe import (
    MEDIA_HAS_NO_AUDIO,
    admit_media_source,
    media_info_from_ffprobe_payload,
)
from app.services.document_parser.formats.media.toolchain import require_media_toolchain
from app.services.document_parser.formats.subtitle.parser import parse_subtitle
from shared.core.exceptions.domain_exceptions import (
    DependencyMissingException,
    ValidationException,
)

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def test_ffprobe_payload_with_audio_stream() -> None:
    info = media_info_from_ffprobe_payload(
        {
            "format": {"duration": "12.5"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2},
            ],
        }
    )
    assert info.has_audio is True
    assert info.duration_ms == 12500
    assert info.codec == "aac"
    assert info.channels == 2


def test_ffprobe_payload_without_audio_stream() -> None:
    info = media_info_from_ffprobe_payload(
        {
            "format": {"duration": "1.0"},
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
        }
    )
    assert info.has_audio is False
    assert info.codec is None
    assert info.channels is None
    assert info.duration_ms == 1000


def test_ffprobe_payload_rejects_non_finite_duration() -> None:
    info = media_info_from_ffprobe_payload(
        {
            "format": {"duration": "nan"},
            "streams": [{"codec_type": "audio", "codec_name": "mp3", "channels": 1}],
        }
    )
    assert info.has_audio is True
    assert info.duration_ms is None


def test_admit_media_source_rejects_payload_without_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_parser.formats.media import probe as probe_module

    monkeypatch.setattr(
        probe_module,
        "probe_media",
        lambda _path: media_info_from_ffprobe_payload(
            {"format": {"duration": "1"}, "streams": [{"codec_type": "video"}]}
        ),
    )
    with pytest.raises(ValidationException) as exc_info:
        admit_media_source("/tmp/silent.mp4")
    assert exc_info.value.internal_message == MEDIA_HAS_NO_AUDIO
    assert exc_info.value.details["violations"][0]["description"] == MEDIA_HAS_NO_AUDIO


def test_parse_subtitle_fails_closed_until_cue_parser_exists() -> None:
    with pytest.raises(ValidationException) as exc_info:
        parse_subtitle(
            "/tmp/captions.srt",
            filename="captions.srt",
            output_dir="/tmp/out",
            relative_root="captions.srt",
        )
    assert "cannot be converted to transcript chunks yet" in exc_info.value.user_message


def test_require_media_toolchain_fails_when_ffmpeg_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.document_parser.formats.media.toolchain.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(DependencyMissingException) as exc_info:
        require_media_toolchain()
    assert "ffmpeg" in exc_info.value.internal_message
    assert "ffprobe" in exc_info.value.internal_message


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg and ffprobe must be on PATH")
def test_admit_rejects_generated_video_without_audio(tmp_path: Path) -> None:
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:d=1",
            "-an",
            str(silent),
        ],
        check=True,
    )
    with pytest.raises(ValidationException) as exc_info:
        admit_media_source(str(silent))
    assert exc_info.value.internal_message == MEDIA_HAS_NO_AUDIO


