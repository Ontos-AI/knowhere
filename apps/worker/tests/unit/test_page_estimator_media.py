from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import importlib  # noqa: E402

import pytest  # noqa: E402

from app.services.document_ingestion.page_estimator import (  # noqa: E402
    MEDIA_MS_PER_PAGE,
    PageEstimator,
)
from shared.core.exceptions.domain_exceptions import ValidationException  # noqa: E402

_MINUTE_MS = 60_000


def _probe_module() -> object:
    # Resolve at patch time: contract fixtures may reload app modules.
    return importlib.import_module("app.services.document_parser.formats.media.probe")


def _patch_duration(monkeypatch: pytest.MonkeyPatch, duration_ms: int) -> None:
    probe_module = _probe_module()
    monkeypatch.setattr(
        probe_module,
        "probe_media",
        lambda _path: probe_module.media_info_from_ffprobe_payload(
            {
                "format": {"duration": str(duration_ms / 1000)},
                "streams": [{"codec_type": "audio"}],
            }
        ),
    )


def test_media_page_is_two_minutes() -> None:
    assert MEDIA_MS_PER_PAGE == 2 * _MINUTE_MS


@pytest.mark.parametrize(
    ("duration_ms", "expected_pages"),
    [
        (1_000, 1),
        (2 * _MINUTE_MS, 1),
        (2 * _MINUTE_MS + 1, 2),
        (60 * _MINUTE_MS, 30),
        (240 * _MINUTE_MS, 120),
    ],
)
@pytest.mark.parametrize("extension", [".mp3", ".mp4"])
def test_media_bills_by_audio_duration(
    monkeypatch: pytest.MonkeyPatch,
    duration_ms: int,
    expected_pages: int,
    extension: str,
) -> None:
    _patch_duration(monkeypatch, duration_ms)
    estimate = PageEstimator.estimate_workload(f"/tmp/talk{extension}")
    assert estimate.method == "media_duration"
    assert estimate.used_fallback is False
    assert estimate.page_count == expected_pages


def test_unreadable_media_falls_back_to_one_page_for_admission_to_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreadable(_path: str) -> object:
        raise ValidationException(
            user_message="Could not read this media file.",
            violations=[{"field": "file", "description": "unreadable"}],
        )

    monkeypatch.setattr(_probe_module(), "probe_media", _unreadable)
    estimate = PageEstimator.estimate_workload("/tmp/broken.mp3")
    assert estimate.method == "media_duration"
    assert estimate.page_count == 1
    assert estimate.fallback_reason == "media_duration_error:ValidationException"
