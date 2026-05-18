from __future__ import annotations

import os

from loguru import logger

from shared.services.retrieval.legacy_route import run_legacy_retrieval_route
from shared.services.retrieval.reference_resolver import resolve_workflow_references
from shared.services.retrieval.result_assembly import assemble_retrieval_results
from shared.services.retrieval.response_projection import (
    attach_citation,
)
from shared.services.retrieval.route_types import (
    RetrievalRouteContext,
    RetrievalRouteOutcome,
)
from shared.services.retrieval.scoped_corpus import (
    count_scoped_chunks,
    load_all_scoped_chunks,
)


async def run_retrieval_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    small_kb_outcome = await _try_run_small_kb_route(context)
    if small_kb_outcome is not None:
        return small_kb_outcome

    if _should_use_agentic_route(context.use_agentic):
        return await _run_agentic_route(context)

    return await run_legacy_retrieval_route(context)


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

    resolved_references = await resolve_workflow_references(
        db=context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        refs=workflow_result.referenced_chunks,
    )
    assembled_workflow_rows = await assemble_retrieval_results(
        db=context.db,
        rows=resolved_references.rows,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
    )
    response = workflow_result.to_api_response()
    response["referenced_chunks"] = resolved_references.refs
    response["results"] = [attach_citation(row) for row in assembled_workflow_rows]

    completion_detail = (
        f"chunks | answer={len(workflow_result.answer_text)} chars | "
        f"router={workflow_result.router_used}"
    )
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=resolved_references.refs,
        completion_label="AGENTIC RETRIEVAL",
        completion_count=len(resolved_references.refs),
        completion_detail=completion_detail,
    )
