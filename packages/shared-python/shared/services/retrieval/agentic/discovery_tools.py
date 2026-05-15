"""Agentic retrieval discovery tools.

This Module owns phase-1 retrieval: lexical bottom discovery and document
selection from the document-level knowledge map. The public tool adapter stays
in ``tools.py`` so orchestrator call sites keep a stable interface.
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document
from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.knowledge_map import build_knowledge_map_overview
from shared.services.retrieval.agentic.prompts import (
    FILE_SELECT_PROMPT,
    format_budget_block,
    parse_json_array,
)
from shared.services.retrieval.agentic.types import ToolResult
from shared.services.retrieval.channels import content_channel, path_channel, term_channel
from shared.services.retrieval.llm_adapter import LLMFn
from shared.services.retrieval.scoring import (
    merge_channels_rrf,
    merge_same_section_rows,
    normalize_row_scores,
)
from shared.services.retrieval.settings import (
    CHANNEL_WEIGHT_CONTENT,
    CHANNEL_WEIGHT_PATH,
    CHANNEL_WEIGHT_TERM,
    INTERNAL_RECALL_K_MULTIPLIER,
    resolve_allowed_chunk_types,
)


async def bottom_discovery(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    data_type: int = 1,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    internal_recall_k: int | None = None,
    **_kwargs: Any,
) -> ToolResult:
    """Run 3-channel BM25 discovery plus RRF fusion."""
    t0 = time.monotonic()
    try:
        allowed_chunk_types = resolve_allowed_chunk_types(data_type)
        effective_recall_k = (
            internal_recall_k
            if internal_recall_k is not None
            else top_k * INTERNAL_RECALL_K_MULTIPLIER
        )
        active_channels = set(channels) if channels else {"path", "content", "term"}

        path_rows: list[dict[str, Any]] = []
        content_rows: list[dict[str, Any]] = []
        term_rows: list[dict[str, Any]] = []

        if "path" in active_channels:
            path_rows = await path_channel(
                db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=effective_recall_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
            )

        if "content" in active_channels:
            content_rows = await content_channel(
                db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=effective_recall_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
            )

        if "term" in active_channels:
            term_rows = await term_channel(
                db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=effective_recall_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
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

        fused_rows = merge_channels_rrf(channel_lists, weight_list, top_k) if channel_lists else []
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
            f"  agentic.bottom_discovery: {len(fused_rows)} fused rows, "
            f"top_doc_ids={top_doc_ids}, {latency}ms"
        )
        return ToolResult(
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
        logger.error(f"  agentic.bottom_discovery failed: {exc}")
        return ToolResult(status="error", error=str(exc), latency_ms=latency)


async def kg_document_select(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    llm_fn: LLMFn | None,
    exclude_document_ids: list[str],
    revision_hint: str | None = None,
    **_kwargs: Any,
) -> ToolResult:
    """Select candidate documents from document-level KG."""
    t0 = time.monotonic()
    try:
        overview_text, doc_id_to_name = await build_knowledge_map_overview(
            db,
            user_id=user_id,
            namespace=namespace,
        )
        if overview_text == "(empty)":
            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                status="no_confident_doc",
                payload={"reason": "no active documents in namespace"},
                latency_ms=latency,
            )

        if llm_fn is None:
            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                status="no_confident_doc",
                payload={"reason": "LLM not available"},
                latency_ms=latency,
            )

        revision_context = ""
        if revision_hint:
            revision_context = (
                "\nIMPORTANT: This is a REVISION round. "
                "The previous search attempt failed because:\n"
                f'"{revision_hint}"\n'
                "Adjust your document selection accordingly. "
                "If no document can address this, return an EMPTY array [].\n"
            )

        file_prompt = FILE_SELECT_PROMPT.format(
            overview=overview_text,
            query=query,
            revision_context=revision_context,
            budget_block=format_budget_block(_kwargs.get("budget_snapshot")),
        )
        file_response = await llm_fn(file_prompt)
        selected_ids = parse_json_array(file_response)

        exclude_set = set(exclude_document_ids)
        valid_ids = [
            document_id
            for document_id in selected_ids
            if document_id in doc_id_to_name and document_id not in exclude_set
        ]

        if not valid_ids:
            latency = int((time.monotonic() - t0) * 1000)
            logger.info(
                f"  agentic.kg_document_select: LLM returned no valid docs, {latency}ms"
            )
            return ToolResult(
                status="no_confident_doc",
                payload={
                    "reason": "LLM returned no valid document IDs",
                    "raw_ids": selected_ids,
                },
                latency_ms=latency,
            )

        doc_job_map: dict[str, str] = {}
        doc_stmt = (
            select(Document.document_id, Document.current_job_result_id)
            .where(Document.document_id.in_(valid_ids))
        )
        doc_result = await db.execute(doc_stmt)
        for document_id, job_result_id in doc_result.all():
            if job_result_id:
                doc_job_map[document_id] = job_result_id

        candidate_docs = [
            {
                "document_id": document_id,
                "source_file_name": doc_id_to_name.get(document_id, ""),
                "confidence": 1.0,
                "reason": "LLM selected from KG overview",
                "source": "kg_llm_select",
            }
            for document_id in valid_ids
        ]

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"  agentic.kg_document_select: {len(candidate_docs)} docs selected, {latency}ms"
        )
        return ToolResult(
            status="selected_docs",
            payload={
                "candidate_docs": candidate_docs,
                "doc_id_to_name": doc_id_to_name,
                "doc_job_map": doc_job_map,
            },
            latency_ms=latency,
        )
    except BudgetExceeded:
        raise
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f"  agentic.kg_document_select failed: {exc}")
        return ToolResult(status="error", error=str(exc), latency_ms=latency)
