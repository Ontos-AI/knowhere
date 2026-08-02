from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.discovery.tools import bottom_discovery
from shared.services.retrieval.execution.reference_resolver import resolve_workflow_references
from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results
from shared.services.retrieval.execution.response_projection import (
    attach_citation,
)
from shared.services.retrieval.hydration.legacy_evidence import render_legacy_evidence_text
from shared.services.retrieval.execution.route_types import (
    RetrievalRouteContext,
    RetrievalRouteOutcome,
)
from shared.services.retrieval.search.ranking import rank_retrieval_candidates
from shared.services.retrieval.search.scoped_corpus import (
    count_scoped_chunks,
    load_all_scoped_chunks,
)


def open_hydration_db_context() -> AbstractAsyncContextManager[AsyncSession]:
    from shared.core.database import get_db_context

    return get_db_context()


async def run_retrieval_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    small_corpus_outcome = await _try_run_small_corpus_route(context)
    if small_corpus_outcome is not None:
        return small_corpus_outcome

    if context.use_agentic is True:
        return await _run_agentic_route(context)

    return await _run_classic_topk_route(context)


async def _try_run_small_corpus_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome | None:
    total_chunk_count = await count_scoped_chunks(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        exclude_document_ids=context.exclude_document_ids,
        allowed_chunk_types=context.allowed_chunk_types,
    )

    logger.info(f"\n  Total chunks in scope: {total_chunk_count}")
    if total_chunk_count > context.top_k:
        return None

    logger.info(
        f"  Small corpus optimization: {total_chunk_count} chunks "
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
        f"  small_corpus load: loaded={len(all_rows)} rows after signal/exclude filters"
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
        "router_used": "small_corpus_all",
        "evidence_text": render_legacy_evidence_text(results),
        "answer_text": "",
        "results": results,
    }
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=results,
        completion_label="Small corpus",
        completion_count=len(results),
        completion_detail="results",
    )


async def _run_classic_topk_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    discovery_result = await bottom_discovery(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        top_k=context.effective_recall_k,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        chunk_types=context.allowed_chunk_types,
        signal_paths=context.signal_paths,
        filter_mode=context.filter_mode,
        channels=context.channels,
        channel_weights=context.channel_weights,
        internal_recall_k=context.internal_recall_k,
    )

    fused_rows = (
        discovery_result.payload.get("fused_rows", [])
        if discovery_result.status != "error"
        else []
    )

    ranked_rows = await rank_retrieval_candidates(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        discovery_rows=fused_rows,
        routed_rows=[],
        top_k=context.top_k,
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
        "router_used": "classic_topk",
        "evidence_text": render_legacy_evidence_text(results),
        "answer_text": "",
        "results": results,
    }
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=results,
        completion_label="CLASSIC TOP-K",
        completion_count=len(results),
        completion_detail="results",
    )


async def _run_agentic_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    from shared.services.retrieval.workflow.orchestrator import WorkflowOrchestrator
    from shared.services.retrieval.workflow.run_request import WorkflowRunRequest

    await context.db.rollback()

    workflow = WorkflowOrchestrator()
    workflow_result = await workflow.run_request(
        context.db,
        request=WorkflowRunRequest.from_route_context(context),
    )

    # Build a real score index for hydration:
    # 1. Discovery RRF score — available for chunks that surfaced in the
    #    3-channel BM25 pass (stored in each referenced_chunk as 'score').
    # 2. KG document confidence — LLM-assigned confidence per selected document
    #    (stored in decision_trace kg_select entry), used for navigation-only chunks.
    score_by_chunk_id: dict[str, float] = {}

    # Layer 1: referenced_chunk-level score (propagated from discovery rows)
    for ref in workflow_result.referenced_chunks:
        cid = ref.get('chunk_id', '')
        raw_score = ref.get('score')
        if cid and raw_score is not None:
            try:
                score_by_chunk_id[cid] = float(raw_score)
            except (TypeError, ValueError):
                pass

    # Layer 2: per-document KG confidence as default for navigation-only chunks.
    doc_confidence: dict[str, float] = {}
    for step in workflow_result.steps:
        for entry in step.decision_trace or []:
            if entry.get('phase') == 'kg_select':
                result = entry.get('result') or {}
                for doc_info in result.get('collected', []):
                    doc_id = doc_info.get('document_id', '')
                    conf = doc_info.get('confidence')
                    if doc_id and conf is not None:
                        try:
                            doc_confidence[doc_id] = float(conf)
                        except (TypeError, ValueError):
                            pass

    async with open_hydration_db_context() as hydration_db:
        resolved_references = await resolve_workflow_references(
            db=hydration_db,
            user_id=context.user_id,
            namespace=context.namespace,
            refs=workflow_result.referenced_chunks,
            score_by_chunk_id=score_by_chunk_id if score_by_chunk_id else None,
        )

        # Backfill doc-level confidence for chunks that have no discovery score
        if doc_confidence:
            for row in resolved_references.rows:
                cid = row.get('chunk_id', '')
                if cid and row.get('score') is None:
                    doc_id = row.get('document_id', '')
                    if doc_id in doc_confidence:
                        row['score'] = doc_confidence[doc_id]

        assembled_workflow_rows = await assemble_retrieval_results(
            db=hydration_db,
            rows=resolved_references.rows,
            exclude_document_ids=context.exclude_document_ids,
            exclude_sections=context.exclude_sections,
            allowed_chunk_types=context.allowed_chunk_types,
        )
    response = workflow_result.to_api_response()
    response["answer_text"] = ""
    response["referenced_chunks"] = resolved_references.refs
    response["results"] = [attach_citation(row) for row in assembled_workflow_rows]

    evidence_parts = [
        step.evidence_text
        for step in workflow_result.steps
        if step.evidence_text
    ]
    if evidence_parts:
        response["evidence_text"] = "\n\n".join(evidence_parts)

    last_retrieve = next(
        (s for s in reversed(workflow_result.steps) if s.step_kind == "retrieve"),
        None,
    )
    if last_retrieve:
        if last_retrieve.stop_reason:
            response["stop_reason"] = last_retrieve.stop_reason
        if last_retrieve.failure_reason:
            response["failure_reason"] = last_retrieve.failure_reason

    # Merge decision traces from all retrieve steps
    all_decision_trace: list[dict] = []
    for step in workflow_result.steps:
        if step.decision_trace:
            all_decision_trace.extend(step.decision_trace)

    # Embed stop/failure into decision_trace as a homogeneous terminal entry.
    stop_reason = response.get("stop_reason") or ""
    failure_reason = response.get("failure_reason") or ""
    has_terminal_trace = any(
        entry.get("phase") == "terminal"
        for entry in all_decision_trace
    )
    if (stop_reason or failure_reason) and not has_terminal_trace:
        terminal_index = len(all_decision_trace)
        all_decision_trace.append({
            "step_index": terminal_index,
            "agent": "workflow",
            "parent_step_index": None,
            "phase": "terminal",
            "document_id": None,
            "document": None,
            "scope": "workflow",
            "observation": {
                "router_used": workflow_result.router_used,
                "referenced_chunks": len(resolved_references.refs),
            },
            "decision": {
                "action": "complete",
                "args": {},
                "reason": stop_reason or failure_reason,
            },
            "result": {
                "status": "error" if failure_reason else "ok",
                "stop_reason": stop_reason,
                "failure_reason": failure_reason,
            },
        })

    if all_decision_trace:
        response["decision_trace"] = all_decision_trace

    completion_detail = (
        f"chunks | evidence={len(response.get('evidence_text') or '')} chars | "
        f"router={workflow_result.router_used}"
    )
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=resolved_references.refs,
        completion_label="AGENTIC RETRIEVAL",
        completion_count=len(resolved_references.refs),
        completion_detail=completion_detail,
    )
