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

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_MINUTE_MS = 60_000


def _patch_media(
    monkeypatch: pytest.MonkeyPatch,
    *,
    duration_ms: int,
    user_tier: str,
) -> object:
    from app.services.document_ingestion import media_duration_policy as policy
    from app.services.document_parser.formats.media.probe import (
        media_info_from_ffprobe_payload,
    )
    from shared.core.config import settings

    monkeypatch.setattr(settings, "MEDIA_MAX_DURATION_FREE_SECONDS", 3600)
    monkeypatch.setattr(settings, "MEDIA_MAX_DURATION_PAID_SECONDS", 7200)
    monkeypatch.setattr(
        policy,
        "admit_media_source",
        lambda _path: media_info_from_ffprobe_payload(
            {
                "format": {"duration": str(duration_ms / 1000)},
                "streams": [{"codec_type": "audio"}],
            }
        ),
    )
    monkeypatch.setattr(policy, "_load_user_tier", lambda _user_id: user_tier)
    return policy


@pytest.mark.parametrize("user_tier", ["free", "guest"])
def test_free_tiers_reject_media_over_free_cap_with_upgrade_hint(
    monkeypatch: pytest.MonkeyPatch,
    user_tier: str,
) -> None:
    policy = _patch_media(
        monkeypatch, duration_ms=95 * _MINUTE_MS, user_tier=user_tier
    )
    rejection = policy.build_media_admission_rejection(
        file_extension=".mp3",
        local_file_path="/tmp/talk.mp3",
        job_user_id="user_1",
    )
    assert rejection is not None
    assert "95 minutes" in rejection.user_message
    assert "free plan supports up to 60 minutes" in rejection.user_message
    assert "up to 120 minutes" in rejection.user_message
    assert rejection.details["violations"][0]["field"] == "media_duration"


def test_paid_tier_accepts_media_between_free_and_paid_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _patch_media(monkeypatch, duration_ms=95 * _MINUTE_MS, user_tier="tier_1")
    assert (
        policy.build_media_admission_rejection(
            file_extension=".mp4",
            local_file_path="/tmp/talk.mp4",
            job_user_id="user_1",
        )
        is None
    )


def test_paid_tier_rejects_media_over_paid_cap_without_upgrade_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _patch_media(monkeypatch, duration_ms=121 * _MINUTE_MS, user_tier="tier_3")
    rejection = policy.build_media_admission_rejection(
        file_extension=".m4a",
        local_file_path="/tmp/talk.m4a",
        job_user_id="user_1",
    )
    assert rejection is not None
    assert "maximum is 120 minutes" in rejection.user_message
    assert "Upgrade" not in rejection.user_message


def test_media_exactly_at_free_cap_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _patch_media(monkeypatch, duration_ms=60 * _MINUTE_MS, user_tier="free")
    assert (
        policy.build_media_admission_rejection(
            file_extension=".wav",
            local_file_path="/tmp/talk.wav",
            job_user_id="user_1",
        )
        is None
    )


def test_non_media_sources_skip_media_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion import media_duration_policy as policy

    def _refuse(*_args: object) -> None:
        raise AssertionError("non-media files must not be probed")

    monkeypatch.setattr(policy, "admit_media_source", _refuse)
    monkeypatch.setattr(policy, "_load_user_tier", _refuse)
    assert (
        policy.build_media_admission_rejection(
            file_extension=".pdf",
            local_file_path="/tmp/report.pdf",
            job_user_id="user_1",
        )
        is None
    )


def test_media_without_audio_is_returned_as_rejection_before_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.document_ingestion import media_duration_policy as policy
    from app.services.document_parser.formats.media.probe import (
        MEDIA_HAS_NO_AUDIO,
        media_info_from_ffprobe_payload,
    )

    # Patch through the function globals: contract fixtures may reload modules.
    monkeypatch.setitem(
        policy.admit_media_source.__globals__,
        "probe_media",
        lambda _path: media_info_from_ffprobe_payload(
            {"format": {"duration": "1"}, "streams": [{"codec_type": "video"}]}
        ),
    )
    rejection = policy.build_media_admission_rejection(
        file_extension=".mp4",
        local_file_path="/tmp/silent.mp4",
        job_user_id="user_1",
    )
    assert rejection is not None
    assert rejection.internal_message == MEDIA_HAS_NO_AUDIO


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg and ffprobe must be on PATH")
def test_duration_is_measured_when_webm_header_has_no_duration(tmp_path: Path) -> None:
    from app.services.document_parser.formats.media.probe import (
        measure_media_duration_ms,
        probe_media,
    )

    streamed = tmp_path / "recording.webm"
    with streamed.open("wb") as handle:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-c:a",
                "libopus",
                "-f",
                "webm",
                "pipe:1",
            ],
            check=True,
            stdout=handle,
        )
    info = probe_media(str(streamed))
    assert info.duration_ms is None
    duration_ms = measure_media_duration_ms(str(streamed), info)
    assert 2900 <= duration_ms <= 3100
