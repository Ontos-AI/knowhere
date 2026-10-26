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

import httpx

from shared.core.exceptions.domain_exceptions import LLMServiceException
from shared.services.ai.embedding import multimodal_client
from shared.services.ai.embedding.multimodal_client import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    _MAX_EMBEDDING_RETRIES,
    _build_embedding_request,
    _call_gateway_embedding,
    _extract_embedding_vector,
    embed_images,
)


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [{"embedding": [0.1] * DEFAULT_EMBEDDING_DIM}],
            "usage": {"prompt_tokens": 1},
        },
        request=httpx.Request("POST", "https://ai-gateway.vercel.sh/v1/embeddings"),
    )


def _status_response(status_code: int, text: str = "no") -> httpx.Response:
    return httpx.Response(
        status_code,
        text=text,
        request=httpx.Request("POST", "https://ai-gateway.vercel.sh/v1/embeddings"),
    )


def _ssl_error() -> httpx.ReadError:
    return httpx.ReadError(
        "[SSL] record layer failure",
        request=httpx.Request("POST", "https://ai-gateway.vercel.sh/v1/embeddings"),
    )


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, post_fn) -> None:
    class _Client:
        def post(self, *args, **kwargs):
            return post_fn(*args, **kwargs)

    monkeypatch.setattr(multimodal_client, "_resolve_vercel_api_key", lambda: "test-key")
    monkeypatch.setattr(multimodal_client, "get_sync_client", lambda: _Client())
    monkeypatch.setattr(multimodal_client.time, "sleep", lambda _seconds: None)


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


def test_ssl_blip_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def post_fn(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ssl_error()
        return _ok_response()

    _patch_gateway(monkeypatch, post_fn)
    vector = _call_gateway_embedding(text="query", seed_bytes=b"query")
    assert len(vector) == DEFAULT_EMBEDDING_DIM
    assert calls["n"] == 2


def test_http_500_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def post_fn(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _status_response(500, "upstream")
        return _ok_response()

    _patch_gateway(monkeypatch, post_fn)
    vector = _call_gateway_embedding(text="query", seed_bytes=b"query")
    assert len(vector) == DEFAULT_EMBEDDING_DIM
    assert calls["n"] == 2


def test_http_400_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def post_fn(*_args, **_kwargs):
        calls["n"] += 1
        return _status_response(400, "bad request")

    _patch_gateway(monkeypatch, post_fn)
    with pytest.raises(LLMServiceException, match="HTTP 400"):
        _call_gateway_embedding(text="query", seed_bytes=b"query")
    assert calls["n"] == 1


def test_ssl_exhausted_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def post_fn(*_args, **_kwargs):
        calls["n"] += 1
        raise _ssl_error()

    _patch_gateway(monkeypatch, post_fn)
    with pytest.raises(LLMServiceException, match="after 3 attempts"):
        _call_gateway_embedding(text="query", seed_bytes=b"query")
    assert calls["n"] == _MAX_EMBEDDING_RETRIES


def test_embed_images_keeps_other_pages_after_one_ssl_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def post_fn(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ssl_error()
        return _ok_response()

    _patch_gateway(monkeypatch, post_fn)
    vectors = embed_images([b"page-a", b"page-b"], concurrency=2)
    assert len(vectors) == 2
    assert all(item is not None and len(item) == DEFAULT_EMBEDDING_DIM for item in vectors)
    assert calls["n"] == 3


def test_embed_images_skips_page_after_retries_and_keeps_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def post_fn(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] <= _MAX_EMBEDDING_RETRIES:
            raise _ssl_error()
        return _ok_response()

    _patch_gateway(monkeypatch, post_fn)
    vectors = embed_images([b"page-a", b"page-b"], concurrency=1)
    assert vectors[0] is None
    assert vectors[1] is not None
    assert len(vectors[1]) == DEFAULT_EMBEDDING_DIM
    assert calls["n"] == _MAX_EMBEDDING_RETRIES + 1
