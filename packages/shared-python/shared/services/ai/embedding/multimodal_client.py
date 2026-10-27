"""Vercel AI Gateway client for Gemini Embedding 2 page-image and query vectors.

Page images and retrieval queries share one 3072-dimensional space. Images must
be sent as Gemini multimodal content; a data-URI string in ``input`` is treated
as text by the gateway and is not a visual embedding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import httpx
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import LLMServiceException
from shared.services.ai.token_tracking import (
    bind_token_tracker,
    get_current_token_tracker_root_id,
    record_tokens,
)
from shared.services.http.client_pool import get_sync_client
from shared.utils.security_utils import mask_api_key

DEFAULT_EMBEDDING_MODEL = "google/gemini-embedding-2"
DEFAULT_EMBEDDING_DIM = 3072
DEFAULT_EMBEDDING_ENDPOINT = "https://ai-gateway.vercel.sh/v1/embeddings"
_MAX_EMBEDDING_RETRIES = 3
_RETRY_BACKOFF_CAP_SECONDS = 8.0


def resolve_embedding_model(model: str | None = None) -> str:
    """Resolve the active multimodal embedding model."""
    candidate = str(
        model if model is not None else getattr(settings, "EMBEDDING_MODEL", "")
    ).strip()
    return candidate or DEFAULT_EMBEDDING_MODEL


def _resolve_embedding_dim() -> int:
    raw = int(getattr(settings, "EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM) or DEFAULT_EMBEDDING_DIM)
    return raw if raw > 0 else DEFAULT_EMBEDDING_DIM


def _resolve_embedding_endpoint() -> str:
    endpoint = str(getattr(settings, "EMBEDDING_ENDPOINT", "") or "").strip()
    return endpoint or DEFAULT_EMBEDDING_ENDPOINT


def _resolve_vercel_api_key() -> str:
    return str(getattr(settings, "VERCEL_KEY", "") or "").strip()


def _should_mock_embeddings() -> bool:
    return bool(getattr(settings, "LLM_MOCK_ENABLED", False))


def _build_deterministic_mock_vector(seed_bytes: bytes, *, dim: int) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        digest = hashlib.sha256(seed_bytes + counter.to_bytes(4, "big")).digest()
        for idx in range(0, len(digest), 2):
            if len(values) >= dim:
                break
            raw = int.from_bytes(digest[idx : idx + 2], "big", signed=True)
            values.append(raw / 32768.0)
        counter += 1
    norm = math.sqrt(sum(val * val for val in values)) or 1.0
    return [val / norm for val in values]


def _read_image_payload(image_input: str | Path | bytes) -> tuple[str, bytes]:
    if isinstance(image_input, bytes):
        return "image/png", image_input
    path = Path(image_input)
    raw_bytes = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg", raw_bytes
    if suffix == ".webp":
        return "image/webp", raw_bytes
    return "image/png", raw_bytes


def _build_embedding_request(
    *,
    model: str,
    text: str | None = None,
    image_mime: str | None = None,
    image_bytes: bytes | None = None,
) -> dict[str, Any]:
    if image_bytes is not None:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        mime_type = str(image_mime or "image/png").strip() or "image/png"
        return {
            "model": model,
            "input": "",
            "providerOptions": {
                "google": {
                    "content": [
                        [{"inlineData": {"mimeType": mime_type, "data": encoded}}]
                    ]
                }
            },
        }
    return {"model": model, "input": str(text or "")}


def _extract_embedding_vector(
    payload: dict[str, Any],
    *,
    expected_dim: int,
    model: str,
) -> list[float]:
    data = payload.get("data")
    first = data[0] if isinstance(data, list) and data else None
    raw_vec = first.get("embedding") if isinstance(first, dict) else None
    if not isinstance(raw_vec, list) or not raw_vec:
        raise LLMServiceException(
            internal_message=f"Vercel embedding returned no embeddings: {payload}",
            provider=model,
        )
    vector = [float(x) for x in raw_vec]
    if len(vector) != expected_dim:
        raise LLMServiceException(
            internal_message=(
                f"Vercel embedding dimension mismatch: "
                f"expected {expected_dim}, got {len(vector)}"
            ),
            provider=model,
        )
    return vector


def _extract_embedding_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": 0,
        "total_tokens": input_tokens,
    }


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _sleep_before_retry(attempt: int) -> None:
    delay = min(float(2**attempt), _RETRY_BACKOFF_CAP_SECONDS)
    time.sleep(delay)


def _call_gateway_embedding(
    *,
    text: str | None = None,
    image_mime: str | None = None,
    image_bytes: bytes | None = None,
    seed_bytes: bytes,
    model: str | None = None,
    usage_task: str = "embedding.multimodal",
) -> list[float]:
    resolved_model = resolve_embedding_model(model)
    expected_dim = _resolve_embedding_dim()

    if _should_mock_embeddings():
        record_tokens(
            {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            model=resolved_model,
            task=usage_task,
        )
        return _build_deterministic_mock_vector(seed_bytes, dim=expected_dim)

    api_key = _resolve_vercel_api_key()
    if not api_key:
        raise LLMServiceException(
            internal_message="VERCEL_KEY is required for embeddings",
            provider=resolved_model,
        )

    endpoint = _resolve_embedding_endpoint()
    timeout_seconds = float(getattr(settings, "OPENAI_CLIENT_TIMEOUT", 300) or 300)
    http_client = get_sync_client()
    request_body = _build_embedding_request(
        model=resolved_model,
        text=text,
        image_mime=image_mime,
        image_bytes=image_bytes,
    )

    last_error: Exception | None = None
    for attempt in range(_MAX_EMBEDDING_RETRIES):
        try:
            response = http_client.post(
                endpoint,
                json=request_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_seconds,
            )
            if _is_retryable_http_status(response.status_code):
                last_error = LLMServiceException(
                    internal_message=(
                        f"Vercel embedding HTTP {response.status_code}: {response.text}"
                    ),
                    provider=resolved_model,
                )
                logger.warning(
                    "Vercel embedding retryable HTTP {} (attempt {}/{})",
                    response.status_code,
                    attempt + 1,
                    _MAX_EMBEDDING_RETRIES,
                )
                if attempt < _MAX_EMBEDDING_RETRIES - 1:
                    _sleep_before_retry(attempt)
                    continue
                raise last_error

            if response.status_code >= 400:
                raise LLMServiceException(
                    internal_message=(
                        f"Vercel embedding HTTP {response.status_code}: {response.text}"
                    ),
                    provider=resolved_model,
                )

            payload = response.json()
            if not isinstance(payload, dict):
                raise LLMServiceException(
                    internal_message="Vercel embedding returned non-JSON object",
                    provider=resolved_model,
                )
            vector = _extract_embedding_vector(
                payload,
                expected_dim=expected_dim,
                model=resolved_model,
            )
            usage = _extract_embedding_usage(payload)
            record_tokens(usage, model=resolved_model, task=usage_task)
            return vector
        except LLMServiceException:
            raise
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            last_error = exc
            masked_key = mask_api_key(api_key)
            logger.warning(
                "Embedding request failed (attempt {}/{}): model={}, api_key={}, error={}",
                attempt + 1,
                _MAX_EMBEDDING_RETRIES,
                resolved_model,
                masked_key,
                exc,
            )
            if attempt < _MAX_EMBEDDING_RETRIES - 1:
                _sleep_before_retry(attempt)
                continue
            raise LLMServiceException(
                internal_message=(
                    f"Embedding failed after {_MAX_EMBEDDING_RETRIES} attempts "
                    f"(api_key={masked_key}): {exc}"
                ),
                provider=resolved_model,
                original_exception=exc,
            ) from exc

    raise LLMServiceException(
        internal_message=(
            f"Vercel embedding retry loop exited unexpectedly: {last_error}"
        ),
        provider=resolved_model,
        original_exception=last_error,
    )


def embed_query(
    text: str,
    *,
    model: str | None = None,
    usage_task: str = "retrieval.query_embed",
) -> list[float]:
    """Embed a query string into the shared multimodal vector space."""
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("embed_query requires non-empty query text")
    return _call_gateway_embedding(
        text=normalized,
        seed_bytes=normalized.encode("utf-8"),
        model=model,
        usage_task=usage_task,
    )


def embed_images(
    image_inputs: Sequence[str | Path | bytes],
    *,
    model: str | None = None,
    concurrency: int | None = None,
    usage_task: str = "page_memory.m0_embed",
) -> list[list[float] | None]:
    """Embed page images concurrently. Failed pages after retries are skipped."""
    items = list(image_inputs)
    if not items:
        return []

    default_concurrency = int(
        getattr(settings, "PAGE_MEMORY_EMBED_CONCURRENCY", 10) or 10
    )
    resolved_concurrency = max(int(concurrency or default_concurrency), 1)
    tracker_root_id = get_current_token_tracker_root_id()

    def _embed_one(index: int) -> tuple[int, list[float] | None]:
        with bind_token_tracker(tracker_root_id):
            try:
                mime_type, raw_bytes = _read_image_payload(items[index])
                vec = _call_gateway_embedding(
                    image_mime=mime_type,
                    image_bytes=raw_bytes,
                    seed_bytes=raw_bytes,
                    model=model,
                    usage_task=usage_task,
                )
                return index, vec
            except Exception as exc:
                logger.warning(
                    "Skipping page image embedding after retries: index={}, error={}",
                    index,
                    exc,
                )
                return index, None

    if len(items) == 1 or resolved_concurrency == 1:
        return [_embed_one(idx)[1] for idx in range(len(items))]

    import gevent
    from gevent.pool import Pool as GeventPool

    pool = GeventPool(size=min(resolved_concurrency, len(items)))
    greenlets = [pool.spawn(_embed_one, idx) for idx in range(len(items))]
    gevent.joinall(greenlets, raise_error=False)
    pairs = [
        cast(tuple[int, list[float] | None], greenlet.value)
        for greenlet in greenlets
    ]
    pairs.sort(key=lambda item: item[0])
    return [vec for _, vec in pairs]
