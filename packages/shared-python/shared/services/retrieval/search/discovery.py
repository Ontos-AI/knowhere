"""Classic 3-channel bottom discovery (moved from agentic/discovery/tools).

Input (all keyword, via classic route context):
  db, user_id, namespace, query, top_k,
  exclude_document_ids, exclude_sections,
  chunk_types?, signal_paths?, filter_mode,
  channels?, channel_weights?, internal_recall_k?

Output:
  DiscoveryResult
    status: ``discovery_done`` | ``error``
    payload.fused_rows: RRF-merged scored rows (classic reads this)
    payload.top_doc_ids / channel_counts: diagnostics
    latency_ms, error?

Does not call LLM. Does not touch map-nav / wallet / agentic budget.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.search.channels import (
    content_channel,
    path_channel,
    term_channel,
)
from shared.services.retrieval.search.scoring import (
    merge_channels_rrf,
    merge_same_section_rows,
    normalize_row_scores,
)
from shared.services.retrieval.settings import (
    CHANNEL_WEIGHT_CONTENT,
    CHANNEL_WEIGHT_PATH,
    CHANNEL_WEIGHT_TERM,
    INTERNAL_RECALL_K_MULTIPLIER,
)


@dataclass
class DiscoveryResult:
    """Return shape for classic bottom discovery (replaces agentic ToolResult)."""

    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    error: str | None = None


async def bottom_discovery(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    internal_recall_k: int | None = None,
    revision_pins: Mapping[str, str] | None = None,
    **_kwargs: Any,
) -> DiscoveryResult:
    """Run 3-channel BM25 discovery plus RRF fusion."""
    t0 = time.monotonic()
    try:
        del db
        allowed_chunk_types = chunk_types
        effective_recall_k = (
            internal_recall_k
            if internal_recall_k is not None
            else top_k * INTERNAL_RECALL_K_MULTIPLIER
        )
        active_channels = set(channels) if channels else {"path", "content", "term"}

        path_rows, content_rows, term_rows = await asyncio.gather(
            _run_channel(
                path_channel,
                enabled="path" in active_channels,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=effective_recall_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
                revision_pins=revision_pins,
            ),
            _run_channel(
                content_channel,
                enabled="content" in active_channels,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=effective_recall_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
                revision_pins=revision_pins,
            ),
            _run_channel(
                term_channel,
                enabled="term" in active_channels,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=effective_recall_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
                revision_pins=revision_pins,
            ),
        )

        default_weights = {
            "path": CHANNEL_WEIGHT_PATH,
            "content": CHANNEL_WEIGHT_CONTENT,
            "term": CHANNEL_WEIGHT_TERM,
        }
        effective_weights = {**default_weights, **(channel_weights or {})}

        channel_lists: list[list[dict[str, Any]]] = []
        weight_list: list[float] = []
        if path_rows:
            channel_lists.append(path_rows)
            weight_list.append(effective_weights.get("path", CHANNEL_WEIGHT_PATH))
        if content_rows:
            channel_lists.append(content_rows)
            weight_list.append(effective_weights.get("content", CHANNEL_WEIGHT_CONTENT))
        if term_rows:
            channel_lists.append(term_rows)
            weight_list.append(effective_weights.get("term", CHANNEL_WEIGHT_TERM))

        fused_rows = (
            merge_channels_rrf(channel_lists, weight_list, effective_recall_k)
            if channel_lists
            else []
        )
        fused_rows = merge_same_section_rows(fused_rows)

        if fused_rows:
            normalize_row_scores(
                fused_rows,
                source_field="score",
                target_field="discovery_score",
                default=0.5,
            )

        doc_id_counts: dict[str, int] = {}
        for row in fused_rows:
            did = row.get("document_id", "")
            if did:
                doc_id_counts[did] = doc_id_counts.get(did, 0) + 1
        top_doc_ids = sorted(
            doc_id_counts,
            key=lambda document_id: doc_id_counts[document_id],
            reverse=True,
        )[:5]

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"  search.bottom_discovery: {len(fused_rows)} fused rows, "
            f"top_doc_ids={top_doc_ids}, {latency}ms"
        )
        return DiscoveryResult(
            status="discovery_done",
            payload={
                "fused_rows": fused_rows,
                "top_doc_ids": top_doc_ids,
                "channel_counts": {
                    "path": len(path_rows),
                    "content": len(content_rows),
                    "term": len(term_rows),
                },
            },
            latency_ms=latency,
        )
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f"  search.bottom_discovery failed: {exc}")
        return DiscoveryResult(status="error", error=str(exc), latency_ms=latency)


async def _run_channel(
    channel: Callable[..., Awaitable[list[dict[str, Any]]]],
    *,
    enabled: bool,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    if not enabled:
        return []

    # Import lazily so discovery remains usable by lightweight contract tests
    # without creating a database context until a channel is actually enabled.
    from shared.core.database import get_db_context

    async with get_db_context() as channel_db:
        return await channel(channel_db, **kwargs)
