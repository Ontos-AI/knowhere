from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.services.retrieval.execution.postgresql_snapshot import (
    open_postgresql_snapshot,
    validate_postgresql_snapshot,
)
from shared.services.retrieval.execution.reference_resolver import (
    resolve_workflow_references,
)
from shared.services.retrieval.execution.route_types import (
    RetrievalRouteContext,
    RetrievalRouteOutcome,
)
from shared.services.retrieval.execution.native_failure import NativeFailureState
from shared.services.retrieval.hydration.evidence_compose import (
    group_evidence_units,
)
from shared.services.retrieval.hydration.result_assembly import (
    RetrievalResultRows,
    assemble_retrieval_results,
    load_retrieval_result_rows,
)
from shared.services.retrieval.routing import config as query_router_config
from shared.services.retrieval.routing.classifier import classify_query_mode
from shared.services.retrieval.routing.types import QueryMode, RouterDecision
from shared.services.retrieval.search.ranking import rank_retrieval_candidates
from shared.services.retrieval.trace.types import DecisionTraceStep
from shared.services.retrieval.search.scoped_corpus import (
    count_scoped_chunks,
    load_all_scoped_chunks,
)
from shared.services.ai.retrieval_ledger import (
    cleanup_retrieval_ledger,
    get_current_retrieval_ledger,
    init_retrieval_ledger,
)
from shared.services.retrieval.search.visual_discovery import (
    VisualQueryEmbedding,
    has_visual_query_embeddings,
    hybrid_discovery,
)


@dataclass(frozen=True)
class _ClassicRead:
    rows: list[dict[str, Any]]
    router_used: Literal["classic_topk", "small_corpus_all"]
    has_unhydrated_hits: bool


def open_fresh_database_context() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a fresh session for final reference resolution after LLM waits."""
    from shared.core.database import get_db_context

    return get_db_context()


def open_agent_explore_database_context() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a tool-dispatch session whose timeouts match the episode wall clock."""
    from shared.core.database import get_db_context_with_timeouts
    from shared.services.retrieval.agent_explore.config import (
        AGENT_EXPLORE_WALL_CLOCK_SECONDS,
    )

    timeout_seconds = int(AGENT_EXPLORE_WALL_CLOCK_SECONDS)
    return get_db_context_with_timeouts(
        statement_timeout_ms=timeout_seconds * 1000,
        command_timeout_seconds=timeout_seconds,
    )


def _evidence_fields(rows: list[dict]) -> dict:
    # TODO: later use TypeSafe JEV for result rerank/filter. This step does not exist yet.
    evidence = group_evidence_units(
        [
            {
                "document_id": row["document_id"],
                "source_file_name": row["source_file_name"],
                "section_path": row["section_path"],
                "sort_order": row["sort_order"],
                "parts": row["composed"],
                "kind": "read",
            }
            for row in rows
            if row["composed"]
        ]
    )
    return {
        "evidence": evidence,
        "evidence_text": "",
    }


async def run_retrieval_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead

    init_retrieval_ledger()
    try:
        if settings.KNOWHERE_LEXICAL_BACKEND == "postgresql" and context.use_agentic is False:
            return await _run_postgresql_classic_route(context, try_small_corpus=True)

        if getattr(PublishedLexicalRead.current(), "manifestId", None) is None:
            small_corpus_outcome = await _try_run_small_corpus_route(context)
            if small_corpus_outcome is not None:
                return small_corpus_outcome

        use_agentic = context.use_agentic
        decision: RouterDecision | None = None
        if use_agentic is None:
            use_agentic, decision = await _resolve_auto_route(context)

        if use_agentic is False:
            outcome = await _run_classic_topk_route(context)
        else:
            outcome = await _run_agent_explore_route(context)

        if decision is not None:
            outcome.response["router_decision"] = decision.to_dict()
        return outcome
    finally:
        cleanup_retrieval_ledger()


async def _resolve_auto_route(
    context: RetrievalRouteContext,
) -> tuple[bool, RouterDecision]:
    if not query_router_config.QUERY_ROUTER_ENABLED:
        decision = RouterDecision(
            mode=QueryMode.AGENT,
            reason="",
            source="disabled",
            model_name="",
            tokens_used=0,
        )
    else:
        decision = await classify_query_mode(context.query)
    logger.info(
        f"retrieval query router mode={decision.mode.value} "
        f"source={decision.source} reason={decision.reason}"
    )
    return decision.mode == QueryMode.AGENT, decision


async def _try_run_small_corpus_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome | None:
    loaded_rows: RetrievalResultRows | None = None
    if settings.KNOWHERE_LEXICAL_BACKEND == "postgresql":
        async with _open_classic_snapshot(context) as snapshot:
            read = await _read_small_corpus_route(snapshot)
            if read is not None:
                loaded_rows = await _load_classic_rows(snapshot, read)
    else:
        read = await _read_small_corpus_route(context)
    if read is None:
        return None
    return await _finish_classic_route(context, read, loaded_rows=loaded_rows)


@asynccontextmanager
async def _open_classic_snapshot(
    context: RetrievalRouteContext,
) -> AsyncIterator[RetrievalRouteContext]:
    async with open_postgresql_snapshot(context.db) as snapshot:
        await validate_postgresql_snapshot(
            snapshot,
            user_id=context.user_id,
            namespace=context.namespace,
            pins=context.revision_pins,
        )
        yield replace(context, db=snapshot)


async def _read_small_corpus_route(
    context: RetrievalRouteContext,
) -> _ClassicRead | None:
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
    if len(all_rows) > context.top_k:
        return None
    return _ClassicRead(
        rows=all_rows,
        router_used="small_corpus_all",
        has_unhydrated_hits=total_chunk_count > 0 and not all_rows,
    )


async def _run_classic_topk_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    if settings.KNOWHERE_LEXICAL_BACKEND == "postgresql":
        return await _run_postgresql_classic_route(context, try_small_corpus=False)
    read: _ClassicRead = await _read_classic_topk_route(context)
    return await _finish_classic_route(context, read)


async def _run_postgresql_classic_route(
    context: RetrievalRouteContext,
    *,
    try_small_corpus: bool,
) -> RetrievalRouteOutcome:
    read: _ClassicRead | None = None
    loaded_rows: RetrievalResultRows
    async with _open_classic_snapshot(context) as snapshot:
        if try_small_corpus:
            read = await _read_small_corpus_route(snapshot)
        if read is None and not await has_visual_query_embeddings(
            snapshot.db,
            user_id=context.user_id,
            namespace=context.namespace,
            query=context.query,
            top_k=context.effective_recall_k,
            exclude_document_ids=context.exclude_document_ids,
            document_scope=context.document_scope,
            chunk_types=context.allowed_chunk_types,
            revision_pins=context.revision_pins,
        ):
            read = await _read_classic_topk_route(snapshot, use_visual=False)
        if read is not None:
            loaded_rows = await _load_classic_rows(snapshot, read)

    if read is None:
        # The first snapshot established the need for visual search. Resolve the
        # vector before reopening the same publication for the full read.
        embedding = VisualQueryEmbedding(context.query.strip())
        await embedding.get()
        async with _open_classic_snapshot(context) as snapshot:
            read = await _read_classic_topk_route(snapshot, query_embedding=embedding)
            loaded_rows = await _load_classic_rows(snapshot, read)

    return await _finish_classic_route(context, read, loaded_rows=loaded_rows)


async def _read_classic_topk_route(
    context: RetrievalRouteContext,
    *,
    use_visual: bool = True,
    query_embedding: VisualQueryEmbedding | None = None,
) -> _ClassicRead:
    discovery_result = await hybrid_discovery(
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
        use_visual=use_visual,
        query_embedding=query_embedding,
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

    return _ClassicRead(
        rows=ranked_rows,
        router_used="classic_topk",
        has_unhydrated_hits=discovery_result.has_unhydrated_hits,
    )


async def _load_classic_rows(
    context: RetrievalRouteContext, read: _ClassicRead,
) -> RetrievalResultRows:
    return await load_retrieval_result_rows(
        db=context.db,
        rows=read.rows,
        exclude_document_ids=context.exclude_document_ids,
        document_scope=context.document_scope,
        exclude_sections=context.exclude_sections,
        revision_pins=context.revision_pins,
    )


async def _finish_classic_route(
    context: RetrievalRouteContext,
    read: _ClassicRead,
    *,
    loaded_rows: RetrievalResultRows | None = None,
) -> RetrievalRouteOutcome:
    results = await assemble_retrieval_results(
        db=context.db,
        rows=read.rows,
        exclude_document_ids=context.exclude_document_ids,
        document_scope=context.document_scope,
        exclude_sections=context.exclude_sections,
        allowed_chunk_types=context.allowed_chunk_types,
        revision_pins=context.revision_pins,
        skip_locked_chunk_final=True,
        loaded_rows=loaded_rows,
    )
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": read.router_used,
        **_evidence_fields(results),
        "answer_text": "",
        "results": results,
    }
    await _persist_retrieval_usage_run(
        db=context.db,
        context=context,
        router_used=read.router_used,
        ranked_rows=results,
    )
    return RetrievalRouteOutcome(
        response=response,
        hit_stats_results=results,
        completion_label="Small corpus" if read.router_used == "small_corpus_all" else "CLASSIC TOP-K",
        completion_count=len(results),
        completion_detail="results",
        has_unhydrated_hits=read.has_unhydrated_hits,
    )


async def _run_agent_explore_route(
    context: RetrievalRouteContext,
) -> RetrievalRouteOutcome:
    """Default agentic path: in-process ``corpus.*`` tool-calling loop.

    Which provider actually runs the tool-calling loop is the
    ``AGENT_EXPLORE_HARNESS`` switch resolved by ``resolve_harness()``.
    """
    from shared.services.retrieval.agent_explore.bridge import (
        attach_evidence_pool,
        build_decision_trace,
    )
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget
    from shared.services.retrieval.agent_explore.evidence_pool import (
        attach_candidate_lit_pages,
        compose_pool_evidence,
        pool_chunk_refs,
    )
    from shared.services.retrieval.agent_explore.harness import resolve_harness

    # End any read transaction created by the route's pre-episode work before
    # handing control to the external agent/LLM. The episode can outlive
    # PostgreSQL's idle-in-transaction timeout, so the request session must not
    # be reused for post-episode database work.
    await context.db.rollback()

    harness = resolve_harness(cursor_model=context.agent_explore_model)
    episode_started = time.perf_counter()
    episode = await harness.run_episode(
        db_factory=open_agent_explore_database_context,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        budget=EpisodeBudget(),
        document_scope=context.document_scope,
    )
    NativeFailureState.raiseCurrent()
    logger.info(
        "retrieval agent_explore stage=episode seconds={:.3f} pool={} "
        "steps={} tokens={} stop_reason={}".format(
            time.perf_counter() - episode_started,
            len(episode.pool),
            len(episode.steps),
            episode.tokens_used,
            episode.stop_reason,
        )
    )

    decision_steps = build_decision_trace(episode.steps)
    chunk_refs = pool_chunk_refs(episode.pool)
    decision_steps = attach_evidence_pool(decision_steps, episode.pool)

    async with open_fresh_database_context() as final_db:
        resolved = await resolve_workflow_references(
            db=final_db,
            user_id=context.user_id,
            namespace=context.namespace,
            refs=chunk_refs,
            revision_pins=context.revision_pins,
        )
        attach_candidate_lit_pages(resolved.rows, episode.pool)
        assembled_rows = await assemble_retrieval_results(
            db=final_db,
            rows=resolved.rows,
            exclude_document_ids=context.exclude_document_ids,
            document_scope=context.document_scope,
            exclude_sections=context.exclude_sections,
            allowed_chunk_types=context.allowed_chunk_types,
            revision_pins=context.revision_pins,
            queried_tables=episode.queried_tables,
        )
        evidence = compose_pool_evidence(episode.pool, assembled_rows)
        evidence_fields = {
            "evidence": evidence,
            "evidence_text": "",
        }

        selected_doc_ids = list(
            {row.get("document_id", "") for row in resolved.rows if row.get("document_id")}
        )
        await _persist_retrieval_usage_run(
            db=final_db,
            context=context,
            router_used="agent_explore",
            ranked_rows=assembled_rows,
            decision_steps=decision_steps,
            budget_stop_reason=(
                episode.stop_reason.removeprefix("budget_")
                if episode.stop_reason.startswith("budget_")
                else None
            ),
            model_name=episode.model_name,
            selected_doc_ids=selected_doc_ids,
            policy_name="agent_explore_v1",
        )

    decision_trace = [step.to_dict() for step in decision_steps]
    response = {
        "namespace": context.namespace,
        "query": context.query,
        "router_used": "agent_explore",
        **evidence_fields,
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
            f"chunks | evidence={len(evidence)} parts | router=agent_explore"
        ),
    )


async def _persist_retrieval_usage_run(
    *,
    db: AsyncSession,
    context: RetrievalRouteContext,
    router_used: str,
    ranked_rows: list[dict],
    decision_steps: Sequence[DecisionTraceStep] | None = None,
    budget_stop_reason: str | None = None,
    model_name: str | None = None,
    selected_doc_ids: list[str] | None = None,
    policy_name: str = "retrieval_usage_v1",
) -> None:
    ledger = get_current_retrieval_ledger()
    if ledger is None:
        return

    from shared.services.retrieval.trace import TraceRecorder

    recorder = TraceRecorder(
        db,
        user_id=context.user_id,
        namespace=context.namespace,
        query=context.query,
        top_k=context.top_k,
        chunk_types=context.allowed_chunk_types,
        policy_name=policy_name,
        agentic_enabled=router_used == "agent_explore",
    )
    await recorder.create_run()
    for step in decision_steps or []:
        recorder.record_decision_trace_step(step)
    if budget_stop_reason:
        recorder.record_budget_stop(budget_stop_reason)
    for entry in ledger.entries:
        recorder.record_model_call(entry)
    token_count = ledger.total_tokens_or_none()
    latency_ms = ledger.wall_ms()
    await recorder.complete(
        ranked_rows,
        router_used,
        token_count=token_count,
        model_name=model_name,
        selected_doc_ids=selected_doc_ids,
        latency_ms=latency_ms,
    )
