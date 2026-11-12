from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.ai.token_costing import build_token_cost_estimate


def test_gemini_embedding_text_and_image_use_list_rates() -> None:
    estimate = build_token_cost_estimate(
        {
            "by_model": {
                "google/gemini-embedding-2": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "calls": 1,
                },
                "google/gemini-embedding-2-image": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "calls": 1,
                },
                "gemini-embedding-2": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "calls": 1,
                },
            }
        }
    )
    assert estimate["by_model"]["google/gemini-embedding-2"]["input_cost"] == 0.2
    assert "pricing_missing" not in estimate["by_model"]["google/gemini-embedding-2"]
    assert estimate["by_model"]["google/gemini-embedding-2-image"]["input_cost"] == 0.45
    assert estimate["by_model"]["gemini-embedding-2"]["input_cost"] == 0.2
    assert estimate["missing_pricing_models"] == []
    assert estimate["total_input_cost"] == 0.85


def test_fun_asr_audio_seconds_are_priced_per_second() -> None:
    estimate = build_token_cost_estimate(
        {
            "by_model": {
                "fun-asr": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "audio_seconds": 3600,
                    "calls": 1,
                },
                "deepseek-flash": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "calls": 1,
                },
            },
            "by_task": {
                "parser.media.asr": {
                    "audio_seconds": 3600,
                    "calls": 1,
                    "models": {"fun-asr": {"audio_seconds": 3600, "calls": 1}},
                },
            },
        }
    )
    assert estimate["by_model"]["fun-asr"]["audio_seconds"] == 3600
    assert estimate["by_model"]["fun-asr"]["audio_cost"] == 0.1152
    assert estimate["by_model"]["fun-asr"]["total_cost"] == 0.1152
    assert "audio_cost" not in estimate["by_model"]["deepseek-flash"]
    assert estimate["total_audio_cost"] == 0.1152
    assert estimate["total_cost"] == 0.2652
    assert estimate["by_task"]["parser.media.asr"]["audio_cost"] == 0.1152
    assert estimate["by_task"]["parser.media.asr"]["total_cost"] == 0.1152
    assert estimate["missing_pricing_models"] == []


def test_token_tracker_accumulates_audio_seconds_only_for_speech_calls() -> None:
    from shared.services.ai.token_tracking import (
        cleanup_token_tracker,
        init_token_tracker,
        record_tokens,
    )

    tracker = init_token_tracker()
    try:
        record_tokens({"audio_seconds": 30}, model="fun-asr", task="parser.media.asr")
        record_tokens({"audio_seconds": 12}, model="fun-asr", task="parser.media.asr")
        record_tokens(
            {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            model="deepseek-flash",
            task="parser.text_summary",
        )
        usage = dict(tracker)
    finally:
        cleanup_token_tracker()
    assert usage["audio_seconds"] == 42
    assert usage["by_model"]["fun-asr"]["audio_seconds"] == 42
    assert "audio_seconds" not in usage["by_model"]["deepseek-flash"]
    assert usage["by_task"]["parser.media.asr"]["calls"] == 2
