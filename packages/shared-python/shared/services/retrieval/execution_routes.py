from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.channels import content_channel, path_channel, term_channel
from shared.services.retrieval.graph_service import GraphQueryService
from shared.services.retrieval.reference_hydration import hydrate_referenced_chunk_rows
from shared.services.retrieval.result_assembly import assemble_retrieval_results
from shared.services.retrieval.ranking import rank_retrieval_candidates
from shared.services.retrieval.response_projection import (
    attach_citation,
    enrich_referenced_chunks_with_asset_urls,
)
from shared.services.retrieval.scoped_corpus import (
    count_scoped_chunks,
    load_all_scoped_chunks,
)
from shared.services.retrieval.scoring import (
    get_row_path,
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


@dataclass(frozen=True)
class RetrievalRouteContext:
    db: AsyncSession
    user_id: str
    namespace: str
    query: str
    top_k: int
    exclude_document_ids: list[str]
    exclude_sections: list[dict[str, str]]
    allowed_chunk_types: set[str] | None
    data_type: int
    signal_paths: list[str] | None
    filter_mode: str
    channels: list[str] | None
    channel_weights: dict[str, float] | None
    threshold: float
    effective_recall_k: int
    use_agentic: bool | None


@dataclass(frozen=True)
class RetrievalRouteOutcome:
    response: dict[str, Any]
    hit_stats_results: list[dict[str, Any]]
    completion_label: str
    completion_count: int
    completion_detail: str


async def list_graph_routed_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    service = GraphQueryService()
    entry_document_ids = await service.find_entry_documents(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    return await service.collect_candidate_chunks(
        db,
        user_id=user_id,
        namespace=namespace,
        entry_document_ids=entry_document_ids,
        query=query,
        top_k=top_k * INTERNAL_RECALL_K_MULTIPLIER,
        exclude_sections=exclude_sections,
    )


async def run_retrieval_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    small_kb_outcome = await _try_run_small_kb_route(context)
    if small_kb_outcome is not None:
        return small_kb_outcome

    if _should_use_agentic_route(context.use_agentic):
        return await _run_agentic_route(context)

    return await _run_legacy_route(context)


async def _try_run_small_kb_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome | None:
    try:
        total_chunk_count = await count_scoped_chunks(
            context.db,
            user_id=context.user_id,
            namespace=context.namespace,
            exclude_document_ids=context.exclude_document_ids,
            allowed_chunk_types=context.allowed_chunk_types,
        )
    except Exception as exc:
        logger.warning(
            f"Failed to count scoped chunks, skipping small KB optimization: {exc}"
        )
        total_chunk_count = context.top_k + 1

    logger.info(f"\n  Total chunks in scope: {total_chunk_count}")
    if total_chunk_count > context.top_k:
        return None

    logger.info(
        f"  Small KB optimization: {total_chunk_count} chunks "
        f"<= top_k={context.top_k}, returning all"
    )
    all_rows = await load_all_scoped_chunks(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        signal_paths=context.signal_paths or [],
        filter_mode=context.filter_mode,
    )
    logger.info(
        f"  small_kb load: loaded={len(all_rows)} rows after signal/exclude filters"
    )
    assembled_rows = await assemble_retrieval_results(
        db=context.db,
        rows=all_rows,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
    )
    results = [attach_citation(row) for row in assembled_rows]
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": "small_kb_all",
        "results": results,
    }
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=results,
        completion_label="Small KB",
        completion_count=len(results),
        completion_detail="results",
    )


def _should_use_agentic_route(use_agentic: bool | None) -> bool:
    if use_agentic is not None:
        return use_agentic
    return os.environ.get("RETRIEVAL_AGENTIC_ENABLED", "true") == "true"


async def _run_agentic_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    from shared.services.retrieval.workflow.orchestrator import WorkflowOrchestrator

    workflow = WorkflowOrchestrator()
    workflow_result = await workflow.run(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        top_k=context.top_k,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        data_type=context.data_type,
        signal_paths=context.signal_paths,
        filter_mode=context.filter_mode,
        channels=context.channels,
        channel_weights=context.channel_weights,
    )

    enriched_refs = await enrich_referenced_chunks_with_asset_urls(
        workflow_result.referenced_chunks,
    )

    workflow_result_rows = await hydrate_referenced_chunk_rows(
        db=context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        refs=enriched_refs,
    )
    scoped_reference_keys = {
        (
            str(row.get("document_id") or "").strip(),
            str(row.get("chunk_id") or "").strip(),
        )
        for row in workflow_result_rows
    }
    enriched_refs = [
        ref
        for ref in enriched_refs
        if (
            str(ref.get("document_id") or "").strip(),
            str(ref.get("chunk_id") or "").strip(),
        )
        in scoped_reference_keys
    ]
    assembled_workflow_rows = await assemble_retrieval_results(
        db=context.db,
        rows=workflow_result_rows,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
    )
    response = workflow_result.to_api_response()
    response["referenced_chunks"] = enriched_refs
    response["results"] = [attach_citation(row) for row in assembled_workflow_rows]

    completion_detail = (
        f"chunks | answer={len(workflow_result.answer_text)} chars | "
        f"router={workflow_result.router_used}"
    )
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=enriched_refs,
        completion_label="AGENTIC RETRIEVAL",
        completion_count=len(enriched_refs),
        completion_detail=completion_detail,
    )


async def _run_legacy_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    active_channels = set(context.channels) if context.channels else {
        "path",
        "content",
        "term",
    }
    logger.info(
        f"\n  PHASE 1: Bottom-Layer Discovery "
        f"(channels={sorted(active_channels)})"
    )
    logger.info(f"  effective_recall_k={context.effective_recall_k}")

    path_rows = await _load_path_rows(context, active_channels)
    content_rows = await _load_content_rows(context, active_channels)
    term_rows = await _load_term_rows(context, active_channels)

    fused_rows = _fuse_legacy_rows(
        context=context,
        path_rows=path_rows,
        content_rows=content_rows,
        term_rows=term_rows,
    )
    router_used = "discovery_only"
    agent_rows: list[dict[str, Any]] = []

    logger.info("\n  PHASE 2: Legacy Graph Routing")
    try:
        agent_rows = await list_graph_routed_chunks(
            context.db,
            user_id=context.user_id,
            namespace=context.namespace,
            query=context.query,
            top_k=context.top_k,
            exclude_document_ids=context.exclude_document_ids,
            exclude_sections=context.exclude_sections,
        )
        if agent_rows:
            router_used = "discovery+graph"
            logger.info(f"  Graph routing: {len(agent_rows)} rows")
    except Exception as exc:
        logger.error(f"  Graph routing failed (ignored): {exc}")
        agent_rows = []

    if agent_rows:
        normalize_row_scores(
            agent_rows,
            source_field="score",
            target_field="agent_score",
            default=0.5,
        )

    ranked_rows = await rank_retrieval_candidates(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        discovery_rows=fused_rows,
        routed_rows=agent_rows,
        top_k=context.top_k,
    )
    if ranked_rows:
        logger.info(f"\n  Unified candidate ranking: {len(ranked_rows)} rows")
        for index, row in enumerate(ranked_rows[:10]):
            logger.info(
                "    "
                f"[{index}] evidence={row.get('evidence_score', 0.0):.4f} "
                f"discovery={row.get('discovery_score', 0.0):.4f} "
                f"agent={row.get('agent_score', 0.0):.4f} "
                f"path={get_row_path(row)}"
            )

    assembled_rows = await assemble_retrieval_results(
        db=context.db,
        rows=ranked_rows,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
    )
    results = [attach_citation(row) for row in assembled_rows]
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": router_used,
        "results": results,
    }
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=results,
        completion_label="RETRIEVAL",
        completion_count=len(results),
        completion_detail=f"results | router={router_used}",
    )


async def _load_path_rows(
    context: RetrievalRouteContext,
    active_channels: set[str],
) -> list[dict[str, Any]]:
    if "path" not in active_channels:
        return []

    start_time = time.monotonic()
    rows = await path_channel(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        top_k=context.effective_recall_k,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        signal_paths=context.signal_paths,
        filter_mode=context.filter_mode,
    )
    elapsed_ms = round((time.monotonic() - start_time) * 1000)
    logger.info(f"\n  path_channel: {len(rows)} rows in {elapsed_ms}ms")
    for index, row in enumerate(rows[:5]):
        logger.info(
            f"    [{index}] score={row.get('score', 0):.4f}  "
            f"path={row.get('section_path', '') or row.get('source_chunk_path', '')}  "
            f"type={row.get('chunk_type', '?')}"
        )
    if len(rows) > 5:
        logger.info(f"    ... and {len(rows) - 5} more")
    return rows


async def _load_content_rows(
    context: RetrievalRouteContext,
    active_channels: set[str],
) -> list[dict[str, Any]]:
    if "content" not in active_channels:
        return []

    start_time = time.monotonic()
    rows = await content_channel(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        top_k=context.effective_recall_k,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        signal_paths=context.signal_paths,
        filter_mode=context.filter_mode,
    )
    elapsed_ms = round((time.monotonic() - start_time) * 1000)
    logger.info(f"\n  content_channel: {len(rows)} rows in {elapsed_ms}ms")
    for index, row in enumerate(rows[:5]):
        logger.info(
            f"    [{index}] score={row.get('score', 0):.4f}  "
            f"path={row.get('section_path', '') or row.get('source_chunk_path', '')}  "
            f"content={str(row.get('content', ''))[:80]}"
        )
    if len(rows) > 5:
        logger.info(f"    ... and {len(rows) - 5} more")
    return rows


async def _load_term_rows(
    context: RetrievalRouteContext,
    active_channels: set[str],
) -> list[dict[str, Any]]:
    if "term" not in active_channels:
        return []

    start_time = time.monotonic()
    rows = await term_channel(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        top_k=context.effective_recall_k,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        signal_paths=context.signal_paths,
        filter_mode=context.filter_mode,
    )
    elapsed_ms = round((time.monotonic() - start_time) * 1000)
    logger.info(f"\n  term_channel: {len(rows)} rows in {elapsed_ms}ms")
    for index, row in enumerate(rows[:5]):
        logger.info(
            f"    [{index}] score={row.get('score', 0):.4f}  "
            f"path={row.get('section_path', '') or row.get('source_chunk_path', '')}  "
            f"type={row.get('chunk_type', '?')}"
        )
    if len(rows) > 5:
        logger.info(f"    ... and {len(rows) - 5} more")
    return rows


def _fuse_legacy_rows(
    *,
    context: RetrievalRouteContext,
    path_rows: list[dict[str, Any]],
    content_rows: list[dict[str, Any]],
    term_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    default_weights = {
        "path": CHANNEL_WEIGHT_PATH,
        "content": CHANNEL_WEIGHT_CONTENT,
        "term": CHANNEL_WEIGHT_TERM,
    }
    effective_weights = {**default_weights, **(context.channel_weights or {})}

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

    if channel_lists:
        fused_rows = merge_channels_rrf(
            channel_lists,
            weight_list,
            context.effective_recall_k,
        )
    else:
        fused_rows = []
    logger.info(
        f"\n  RRF Fusion: {len(fused_rows)} rows from "
        f"{len(channel_lists)} channels "
        f"(weights={dict(zip(['path', 'content', 'term'][:len(weight_list)], weight_list))})"
    )
    for index, row in enumerate(fused_rows[:5]):
        logger.info(
            f"    [{index}] rrf_score={row.get('score', 0):.4f}  "
            f"path={row.get('section_path', '') or row.get('source_chunk_path', '')}"
        )
    if len(fused_rows) > 5:
        logger.info(f"    ... and {len(fused_rows) - 5} more")

    pre_merge = len(fused_rows)
    fused_rows = merge_same_section_rows(fused_rows)
    if len(fused_rows) != pre_merge:
        logger.info(f"retrieval: section_merge={pre_merge}->{len(fused_rows)}")

    if context.channel_weights is not None:
        logger.debug(f"retrieval: channel_weights={context.channel_weights}")

    fused_rows = _filter_rows_by_threshold(fused_rows, context)
    if fused_rows:
        normalize_row_scores(
            fused_rows,
            source_field="score",
            target_field="discovery_score",
            default=0.5,
        )

    return fused_rows


def _filter_rows_by_threshold(
    rows: list[dict[str, Any]],
    context: RetrievalRouteContext,
) -> list[dict[str, Any]]:
    if context.threshold <= 0.0 or not rows:
        return rows

    pre_count = len(rows)
    filtered_rows = [
        row for row in rows if row.get("score", 0.0) >= context.threshold
    ]
    logger.info(
        f"retrieval: threshold_filter={pre_count}->{len(filtered_rows)} "
        f"(threshold={context.threshold})"
    )
    return filtered_rows
