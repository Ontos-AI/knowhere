from __future__ import annotations

import asyncio
import time
from contextlib import AbstractAsyncContextManager

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
from shared.services.retrieval.execution.reference_resolver import (
    resolve_workflow_references,
)
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.retrieval.hydration.legacy_evidence import (
    render_legacy_evidence_text,
)
from shared.services.retrieval.execution.route_types import (
    RetrievalRouteContext,
    RetrievalRouteOutcome,
)
from shared.services.retrieval.search.ranking import rank_retrieval_candidates
from shared.services.retrieval.search.scoped_corpus import (
    count_scoped_chunks,
    load_all_scoped_chunks,
)
from shared.services.retrieval.execution.revision_pins import (
    capture_revision_pins,
    is_revision_generation_stable,
)


def open_fresh_database_context() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a fresh session for final reference resolution after LLM waits."""
    from shared.core.database import get_db_context

    return get_db_context()


async def run_retrieval_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    small_corpus_outcome = await _try_run_small_corpus_route(context)
    if small_corpus_outcome is not None:
        return small_corpus_outcome

    # Explicit False → classic 3-channel top-K. None/True → map-nav (default).
    if context.use_agentic is False:
        return await _run_classic_topk_route(context)

    return await _run_mapnav_route(context)


async def _try_run_small_corpus_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome | None:
    total_chunk_count: int | None = None
    total_chunk_count = await count_scoped_chunks(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        exclude_document_ids=context.exclude_document_ids,
        allowed_chunk_types=context.allowed_chunk_types,
        revision_pins=context.revision_pins,
        max_count=context.top_k + 1,
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
        revision_pins=context.revision_pins,
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
        revision_pins=context.revision_pins,
    )
    results = assembled_rows
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
    discovery_result = await map_unit_discovery(
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
        revision_pins=context.revision_pins,
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
        revision_pins=context.revision_pins,
    )

    assembled_rows = await assemble_retrieval_results(
        db=context.db,
        rows=ranked_rows,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        revision_pins=context.revision_pins,
    )
    results = assembled_rows
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


async def _run_mapnav_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    """Default agentic path: PLANNER + HARVEST + CONTROL (checklist map-nav)."""
    from shared.services.retrieval import nav_llm_backend  # noqa: F401
    from shared.services.retrieval.nav import run_nav_episode
    from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
    from shared.services.retrieval.nav_bridge import build_referenced_chunks
    from shared.services.retrieval.nav_config import (
        MAPNAV_MODEL,
        build_nav_config,
        nav_evidence_chars,
    )
    from shared.services.retrieval.nav_snapshot import load_nav_snapshot
    from shared.services.retrieval.trace import (
        TraceRecorder,
        build_decision_trace,
        episode_selected_doc_ids,
        episode_selected_paths,
        episode_token_count,
        episode_workflow_plan,
    )

    snapshot_started = time.perf_counter()
    snapshot_pins = context.revision_pins
    snapshot = await load_nav_snapshot(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        exclude_document_ids=context.exclude_document_ids,
        exclude_sections=context.exclude_sections,
        lazy=True,
        revision_pins=snapshot_pins,
    )
    if snapshot_pins is not None and not await is_revision_generation_stable(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        pins=snapshot_pins,
    ):
        snapshot.close()
        snapshot_pins = await capture_revision_pins(
            context.db,
            user_id=context.user_id,
            namespace=context.namespace,
        )
        snapshot = await load_nav_snapshot(
            context.db,
            user_id=context.user_id,
            namespace=context.namespace,
            exclude_document_ids=context.exclude_document_ids,
            exclude_sections=context.exclude_sections,
            lazy=True,
            revision_pins=snapshot_pins,
        )
    snapshot_seconds = time.perf_counter() - snapshot_started
    logger.info(
        "retrieval mapnav stage=snapshot_load seconds={:.3f} documents={} refs={} "
        "conversation_id={}".format(
            snapshot_seconds,
            len(snapshot.document_ids),
            len(snapshot.chunk_ref_index),
            context.conversation_id or "",
        )
    )

    # Small-corpus count / snapshot reads may leave a checkout; drop it before
    # the sync LLM episode (same pattern as the retired workflow route).
    await context.db.rollback()

    budget = nav_evidence_chars()
    cfg = build_nav_config()
    toolspace = ProviderToolSpace(snapshot.provider)

    episode_started = time.perf_counter()
    try:
        episode = await asyncio.to_thread(
            run_nav_episode,
            None,
            context.query,
            corpus_doc_ids=list(snapshot.document_ids),
            budget_chars=budget,
            compose_answer=False,
            policy="llm",
            config=cfg,
            toolspace=toolspace,
        )

        refs, score_by_chunk_id = build_referenced_chunks(episode, snapshot)
        logger.info(
            "retrieval mapnav stage=episode seconds={:.3f} refs={}".format(
                time.perf_counter() - episode_started,
                len(refs),
            )
        )
    finally:
        snapshot.close()

    hydration_started = time.perf_counter()
    async with open_fresh_database_context() as final_db:
        resolved = await resolve_workflow_references(
            db=final_db,
            user_id=context.user_id,
            namespace=context.namespace,
            refs=refs,
            score_by_chunk_id=score_by_chunk_id or None,
            revision_pins=snapshot.document_revisions,
        )
        assembled_rows = await assemble_retrieval_results(
            db=final_db,
            rows=resolved.rows,
            exclude_document_ids=context.exclude_document_ids,
            exclude_sections=context.exclude_sections,
            allowed_chunk_types=context.allowed_chunk_types,
            revision_pins=snapshot.document_revisions,
        )

        decision_steps = build_decision_trace(
            episode,
            evidence_char_budget=budget,
            n_refs=len(resolved.refs),
        )
        decision_trace = [step.to_dict() for step in decision_steps]
        selected_paths = episode_selected_paths(episode, resolved.refs)
        selected_docs = episode_selected_doc_ids(resolved.refs)
        tokens_used = episode_token_count(episode)
        trace = TraceRecorder(
            final_db,
            user_id=context.user_id,
            namespace=context.namespace,
            query=context.query,
            top_k=context.top_k,
            chunk_types=context.allowed_chunk_types,
            workflow_plan=episode_workflow_plan(episode),
            policy_name="mapnav_checklist_v1",
        )
        await trace.create_run()
        for step in decision_steps:
            trace.record_decision_trace_step(step)
        await trace.complete(
            assembled_rows,
            "mapnav",
            token_count=tokens_used,
            model_name=MAPNAV_MODEL,
            selected_paths=selected_paths,
            selected_doc_ids=selected_docs,
        )
    logger.info(
        "retrieval mapnav stage=hydration seconds={:.3f} results={}".format(
            time.perf_counter() - hydration_started,
            len(assembled_rows),
        )
    )

    stop_reason = str(getattr(episode, "stop_reason", "") or "completed")
    evidence_text = str(getattr(episode, "evidence_text", "") or "")
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": "mapnav",
        "evidence_text": evidence_text,
        "answer_text": "",
        "referenced_chunks": resolved.refs,
        "results": assembled_rows,
        "stop_reason": stop_reason,
        "decision_trace": decision_trace,
    }

    completion_detail = f"chunks | evidence={len(evidence_text)} chars | router=mapnav"
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=resolved.refs,
        completion_label="MAPNAV RETRIEVAL",
        completion_count=len(resolved.refs),
        completion_detail=completion_detail,
    )
