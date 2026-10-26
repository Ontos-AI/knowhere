"""I/O contract for Vercel Gemini Embedding 2 page-image and query vectors."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.exceptions.domain_exceptions import LLMServiceException
from shared.services.ai.embedding.multimodal_client import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    _build_embedding_request,
    _extract_embedding_vector,
)


def test_query_request_is_openai_text_input_without_dimensions() -> None:
    body = _build_embedding_request(
        model=DEFAULT_EMBEDDING_MODEL,
        text="table of contents",
    )
    assert body == {
        "model": DEFAULT_EMBEDDING_MODEL,
        "input": "table of contents",
    }


def test_image_request_uses_gemini_inline_image_not_data_uri_text() -> None:
    body = _build_embedding_request(
        model=DEFAULT_EMBEDDING_MODEL,
        image_mime="image/png",
        image_bytes=b"\x89PNG",
    )
    assert body["model"] == DEFAULT_EMBEDDING_MODEL
    assert body["input"] == ""
    assert "dimensions" not in body
    content = body["providerOptions"]["google"]["content"]
    part = content[0][0]["inlineData"]
    assert part["mimeType"] == "image/png"
    assert part["data"]
    assert not str(body["input"]).startswith("data:")


def test_extract_vector_requires_openai_shape_and_3072_dims() -> None:
    vector = _extract_embedding_vector(
        {"data": [{"embedding": [0.1] * DEFAULT_EMBEDDING_DIM}]},
        expected_dim=DEFAULT_EMBEDDING_DIM,
        model=DEFAULT_EMBEDDING_MODEL,
    )
    assert len(vector) == DEFAULT_EMBEDDING_DIM
    assert vector[0] == pytest.approx(0.1)

    with pytest.raises(LLMServiceException, match="dimension mismatch"):
        _extract_embedding_vector(
            {"data": [{"embedding": [0.1] * 1024}]},
            expected_dim=DEFAULT_EMBEDDING_DIM,
            model=DEFAULT_EMBEDDING_MODEL,
        )

    with pytest.raises(LLMServiceException, match="no embeddings"):
        _extract_embedding_vector(
            {"output": {"embeddings": [{"embedding": [0.1] * DEFAULT_EMBEDDING_DIM}]}},
            expected_dim=DEFAULT_EMBEDDING_DIM,
            model=DEFAULT_EMBEDDING_MODEL,
        )
