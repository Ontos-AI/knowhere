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
