"""DashScope native multimodal-embedding-v1 client for shared image/text vectors.

Reuses ``AliQuotaManager`` token rotation, per-token RPM/daily quotas, 429
cooldown, and greenlet-safe ``record_tokens`` tracking. Unlike chat completions,
DashScope's multimodal embedding API uses its native HTTP endpoint rather than
the OpenAI compatible-mode path.
"""

from __future__ import annotations

import base64
import hashlib
import math
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

DEFAULT_MULTIMODAL_EMBEDDING_MODEL = "multimodal-embedding-v1"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_EMBEDDING_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)


def resolve_embedding_model(model: str | None = None) -> str:
    """Resolve the active multimodal embedding model."""
    candidate = str(
        model if model is not None else getattr(settings, "EMBEDDING_MODEL", "")
    ).strip()
    return candidate or DEFAULT_MULTIMODAL_EMBEDDING_MODEL


def _resolve_embedding_dim() -> int:
    raw = int(getattr(settings, "EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM) or DEFAULT_EMBEDDING_DIM)
    return raw if raw > 0 else DEFAULT_EMBEDDING_DIM


def _resolve_embedding_endpoint() -> str:
    endpoint = str(getattr(settings, "EMBEDDING_ENDPOINT", "") or "").strip()
    return endpoint or DEFAULT_EMBEDDING_ENDPOINT


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


def _encode_image_data_uri(image_input: str | Path | bytes) -> tuple[str, bytes]:
    if isinstance(image_input, bytes):
        raw_bytes = image_input
        mime_type = "image/png"
    else:
        path = Path(image_input)
        raw_bytes = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        elif suffix == ".webp":
            mime_type = "image/webp"
        else:
            mime_type = "image/png"
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", raw_bytes


def _parse_retry_after_header(response: httpx.Response | None) -> int | None:
    if response is None:
        return None
    raw_val = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not raw_val:
        return None
    try:
        parsed = int(float(raw_val))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_embedding_vector(
    payload: dict[str, Any],
    *,
    expected_dim: int,
    model: str,
) -> list[float]:
    output = payload.get("output")
    embeddings = output.get("embeddings") if isinstance(output, dict) else None
    if not isinstance(embeddings, list) or not embeddings:
        raise LLMServiceException(
            internal_message=f"DashScope multimodal embedding returned no embeddings: {payload}",
            provider=model,
        )
    first = embeddings[0]
    raw_vec = first.get("embedding") if isinstance(first, dict) else None
    if not isinstance(raw_vec, list) or not raw_vec:
        raise LLMServiceException(
            internal_message=f"DashScope multimodal embedding vector missing: {payload}",
            provider=model,
        )
    vector = [float(x) for x in raw_vec]
    if len(vector) != expected_dim:
        raise LLMServiceException(
            internal_message=(
                f"DashScope multimodal embedding dimension mismatch: "
                f"expected {expected_dim}, got {len(vector)}"
            ),
            provider=model,
        )
    return vector


def _extract_embedding_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("total_tokens")
        or usage.get("image_count")
        or 0
    )
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": 0,
        "total_tokens": input_tokens,
    }


def _call_dashscope_multimodal_embedding(
    *,
    content_item: dict[str, str],
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

    from shared.services.ai.ali_quota_manager import get_ali_quota_manager

    quota_manager = get_ali_quota_manager()
    endpoint = _resolve_embedding_endpoint()
    max_retries = max(int(getattr(settings, "ALI_INLINE_MAX_RETRIES", 3) or 3), 1)
    timeout_seconds = float(getattr(settings, "OPENAI_CLIENT_TIMEOUT", 300) or 300)
    http_client = get_sync_client()

    request_body = {
        "model": resolved_model,
        "input": {"contents": [content_item]},
    }

    for attempt in range(max_retries):
        lease = quota_manager.acquire_request(operation="multimodal_embedding")
        try:
            response = http_client.post(
                endpoint,
                json=request_body,
                headers={
                    "Authorization": f"Bearer {lease.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_seconds,
            )
            if response.status_code == 429:
                retry_after = _parse_retry_after_header(response)
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
                logger.warning(
                    "Ali token {} rate-limited on multimodal embedding (attempt {}/{}), rotating token",
                    lease.token_id,
                    attempt + 1,
                    max_retries,
                )
                if attempt == max_retries - 1:
                    raise LLMServiceException(
                        internal_message=(
                            f"All Ali tokens rate-limited after {max_retries} retries "
                            f"calling multimodal embedding ({resolved_model})"
                        ),
                        provider=resolved_model,
                    )
                continue

            if response.status_code >= 400:
                raise LLMServiceException(
                    internal_message=(
                        f"DashScope multimodal embedding HTTP {response.status_code}: "
                        f"{response.text}"
                    ),
                    provider=resolved_model,
                )

            payload = response.json()
            if not isinstance(payload, dict):
                raise LLMServiceException(
                    internal_message="DashScope multimodal embedding returned non-JSON object",
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
        except Exception as exc:
            masked_key = mask_api_key(lease.api_key)
            logger.error(
                "Multimodal embedding request failed: model={}, token_id={}, api_key={}, error={}",
                resolved_model,
                lease.token_id,
                masked_key,
                exc,
            )
            raise LLMServiceException(
                internal_message=(
                    f"Multimodal embedding failed "
                    f"(token_id={lease.token_id}, api_key={masked_key}): {exc}"
                ),
                provider=resolved_model,
                original_exception=exc,
            ) from exc

    raise LLMServiceException(
        internal_message="Ali token pool retry loop exited unexpectedly during multimodal embedding",
        provider=resolved_model,
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
    return _call_dashscope_multimodal_embedding(
        content_item={"text": normalized},
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
) -> list[list[float]]:
    """Embed a sequence of page images concurrently in order."""
    items = list(image_inputs)
    if not items:
        return []

    default_concurrency = int(
        getattr(settings, "PAGE_MEMORY_EMBED_CONCURRENCY", 8) or 8
    )
    resolved_concurrency = max(int(concurrency or default_concurrency), 1)
    tracker_root_id = get_current_token_tracker_root_id()

    def _embed_one(index: int) -> tuple[int, list[float]]:
        with bind_token_tracker(tracker_root_id):
            data_uri, raw_bytes = _encode_image_data_uri(items[index])
            vec = _call_dashscope_multimodal_embedding(
                content_item={"image": data_uri},
                seed_bytes=raw_bytes,
                model=model,
                usage_task=usage_task,
            )
            return index, vec

    if len(items) == 1 or resolved_concurrency == 1:
        return [_embed_one(idx)[1] for idx in range(len(items))]

    import gevent
    from gevent.pool import Pool as GeventPool

    pool = GeventPool(size=min(resolved_concurrency, len(items)))
    greenlets = [pool.spawn(_embed_one, idx) for idx in range(len(items))]
    gevent.joinall(greenlets, raise_error=True)
    pairs = [
        cast(tuple[int, list[float]], greenlet.value)
        for greenlet in greenlets
    ]
    pairs.sort(key=lambda item: item[0])
    return [vec for _, vec in pairs]
