from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.execution.reference_resolver import (
    resolve_workflow_references,
)
from shared.services.retrieval.execution.route_types import (
    RetrievalRouteContext,
    RetrievalRouteOutcome,
)
from shared.services.retrieval.hydration.evidence_text import render_evidence_blocks
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.retrieval.search.lexical_text import split_section_path
from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
from shared.services.retrieval.search.ranking import rank_retrieval_candidates
from shared.services.retrieval.search.scoped_corpus import (
    count_scoped_chunks,
    load_all_scoped_chunks,
)


def open_fresh_database_context() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a fresh session for final reference resolution after LLM waits."""
    from shared.core.database import get_db_context

    return get_db_context()


def _evidence_path_header(row: dict) -> str:
    source = row.get("source")
    if not isinstance(source, dict):
        source = row
    file_name = str(source.get("source_file_name") or "").strip()
    section_path = str(source.get("section_path") or "").strip()
    parts = split_section_path(section_path)
    if len(parts) > 1:
        section_path = " / ".join(parts[:-1])
    if file_name and section_path:
        return f"{file_name} / {section_path}"
    return file_name or section_path


def _render_rows_evidence(rows: list[dict]) -> str:
    groups: dict[str, list[str]] = {}
    for row in rows:
        header = _evidence_path_header(row)
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        groups.setdefault(header, []).append(content)
    return render_evidence_blocks(list(groups.items()))


async def run_retrieval_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    small_corpus_outcome = await _try_run_small_corpus_route(context)
    if small_corpus_outcome is not None:
        return small_corpus_outcome

    # Explicit False → classic map-unit BM25 top-K. None/True → agent_explore.
    if context.use_agentic is False:
        return await _run_classic_topk_route(context)
    return await _run_agent_explore_route(context)


async def _try_run_small_corpus_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome | None:
    total_chunk_count = await count_scoped_chunks(
        context.db,
        user_id=context.user_id,
        namespace=context.namespace,
        exclude_document_ids=context.exclude_document_ids,
        document_scope=context.document_scope,
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
        document_scope=context.document_scope,
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
        document_scope=context.document_scope,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        revision_pins=context.revision_pins,
    )
    results = assembled_rows
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": "small_corpus_all",
        "evidence_text": _render_rows_evidence(results),
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
        document_scope=context.document_scope,
        exclude_sections=context.exclude_sections,
        chunk_types=context.allowed_chunk_types,
        signal_paths=context.signal_paths,
        filter_mode=context.filter_mode,
        revision_pins=context.revision_pins,
    )

    fused_rows = list(discovery_result.payload.get("fused_rows") or [])

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
        document_scope=context.document_scope,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        revision_pins=context.revision_pins,
    )
    results = assembled_rows
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": "classic_topk",
        "evidence_text": _render_rows_evidence(results),
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


async def _run_agent_explore_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    """Default agentic path: in-process ``corpus.*`` tool-calling loop.

    Which provider actually runs the tool-calling loop is the
    ``AGENT_EXPLORE_HARNESS`` switch resolved by ``resolve_harness()``.
    """
    from shared.services.retrieval.agent_explore.bridge import build_decision_trace
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget
    from shared.services.retrieval.agent_explore.harness import resolve_harness
    from shared.services.retrieval.agent_explore.ref_resolution import (
        resolve_finish_refs,
    )
    from shared.services.retrieval.trace import TraceRecorder

    # End any read transaction created by the route's pre-episode work before
    # handing control to the external agent/LLM. The episode can outlive
    # PostgreSQL's idle-in-transaction timeout, so the request session must not
    # be reused for post-episode database work.
    await context.db.rollback()

    harness = resolve_harness()
    episode_started = time.perf_counter()
    episode = await harness.run_episode(
        db_factory=open_fresh_database_context,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        budget=EpisodeBudget(),
        document_scope=context.document_scope,
    )
    logger.info(
        "retrieval agent_explore stage=episode seconds={:.3f} refs={} "
        "steps={} tokens={} stop_reason={}".format(
            time.perf_counter() - episode_started,
            len(episode.refs),
            len(episode.steps),
            episode.tokens_used,
            episode.stop_reason,
        )
    )

    # episode.refs are document_id + section_path (what the agent actually
    # sees in tool text); resolve_workflow_references requires chunk_id —
    # see ref_resolution.py's module docstring for why this bridge exists.
    decision_steps = build_decision_trace(episode.steps)
    decision_trace = [step.to_dict() for step in decision_steps]

    async with open_fresh_database_context() as final_db:
        chunk_refs = await resolve_finish_refs(
            final_db,
            user_id=context.user_id,
            namespace=context.namespace,
            refs=episode.refs,
            document_scope=context.document_scope,
        )
        resolved = await resolve_workflow_references(
            db=final_db,
            user_id=context.user_id,
            namespace=context.namespace,
            refs=chunk_refs,
            revision_pins=context.revision_pins,
        )
        assembled_rows = await assemble_retrieval_results(
            db=final_db,
            rows=resolved.rows,
            exclude_document_ids=context.exclude_document_ids,
            document_scope=context.document_scope,
            exclude_sections=context.exclude_sections,
            allowed_chunk_types=context.allowed_chunk_types,
            revision_pins=context.revision_pins,
        )

        selected_doc_ids = list(
            {row.get("document_id", "") for row in resolved.rows if row.get("document_id")}
        )
        trace = TraceRecorder(
            final_db,
            user_id=context.user_id,
            namespace=context.namespace,
            query=context.query,
            top_k=context.top_k,
            chunk_types=context.allowed_chunk_types,
            policy_name="agent_explore_v1",
        )
        await trace.create_run()
        for step in decision_steps:
            trace.record_decision_trace_step(step)
        if episode.stop_reason.startswith("budget_"):
            trace.record_budget_stop(episode.stop_reason.removeprefix("budget_"))
        await trace.complete(
            assembled_rows,
            "agent_explore",
            token_count=episode.tokens_used,
            model_name=episode.model_name,
            selected_doc_ids=selected_doc_ids,
        )

    evidence_text = _render_rows_evidence(assembled_rows)
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": "agent_explore",
        "evidence_text": evidence_text,
        "answer_text": "",
        "referenced_chunks": resolved.refs,
        "results": assembled_rows,
        "stop_reason": episode.stop_reason,
        "decision_trace": decision_trace,
    }
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=resolved.refs,
        completion_label="AGENT EXPLORE RETRIEVAL",
        completion_count=len(resolved.refs),
        completion_detail=(
            f"chunks | evidence={len(evidence_text)} chars | router=agent_explore"
        ),
    )
