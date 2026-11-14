"""Unit tests for the OpenSearch open-index limit and idle watermarks."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.config.publication import PublicationConfig


def test_open_index_limit_defaults_to_four_hundred_with_its_watermarks() -> None:
    config = PublicationConfig()
    assert (
        config.KNOWHERE_OPENSEARCH_OPEN_INDEX_LIMIT,
        config.KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK,
        config.KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK,
    ) == (400, 360, 320)


def test_watermarks_follow_a_smaller_open_index_limit() -> None:
    config = PublicationConfig(
        KNOWHERE_OPENSEARCH_OPEN_INDEX_LIMIT=100,
        KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK=90,
        KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK=80,
    )
    assert config.KNOWHERE_OPENSEARCH_OPEN_INDEX_LIMIT == 100


def test_high_watermark_above_the_open_index_limit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not exceed the open index limit"):
        PublicationConfig(KNOWHERE_OPENSEARCH_OPEN_INDEX_LIMIT=100)


@pytest.mark.parametrize("limit", [0, 1_001])
def test_open_index_limit_outside_the_node_shard_budget_is_rejected(limit: int) -> None:
    with pytest.raises(ValidationError):
        PublicationConfig(
            KNOWHERE_OPENSEARCH_OPEN_INDEX_LIMIT=limit,
            KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK=1,
            KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK=0,
        )
