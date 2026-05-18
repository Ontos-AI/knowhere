"""Discovery and document selection phase for agentic retrieval."""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document
from shared.services.retrieval.agentic import tools
from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.trace import TraceRecorder
from shared.services.retrieval.agentic.types import AgentState, CandidateDoc, ToolResult
from shared.services.retrieval.llm_adapter import LLMFn


async def run_initial_discovery(
    db: AsyncSession,
    *,
    state: AgentState,
    trace: TraceRecorder,
    trace_enabled: bool,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    data_type: int,
    signal_paths: list[str] | None,
    filter_mode: str,
    channels: list[str] | None,
    channel_weights: dict[str, float] | None,
    internal_recall_k: int | None,
    bootstrap_llm_fn: LLMFn | None,
) -> list[dict[str, Any]]:
    discovery_kwargs: dict[str, Any] = {
        "user_id": user_id,
        "namespace": namespace,
        "query": query,
        "top_k": top_k,
        "exclude_document_ids": exclude_document_ids,
        "exclude_sections": exclude_sections,
        "data_type": data_type,
        "signal_paths": signal_paths,
        "filter_mode": filter_mode,
        "channels": channels,
        "channel_weights": channel_weights,
        "internal_recall_k": internal_recall_k,
    }

    logger.info("  agentic: Phase 1 — discovery + document selection")
    discovery_result = await tools.bottom_discovery(db, **discovery_kwargs)
    state.step_count += 1
    discovery_rows = (
        discovery_result.payload.get("fused_rows", [])
        if discovery_result.status != "error"
        else []
    )
    state.discovery_top_doc_ids = (
        discovery_result.payload.get("top_doc_ids", [])
        if discovery_result.status != "error"
        else []
    )

    if trace_enabled:
        trace.record_step(
            "bottom_discovery",
            discovery_result,
            decision_reason="phase_1_mandatory",
        )

    logger.info(
        f"  agentic step {state.step_count}: bottom_discovery "
        f"status={discovery_result.status} latency={discovery_result.latency_ms}ms"
    )

    if bootstrap_llm_fn is not None:
        await _select_documents(
            db,
            state=state,
            trace=trace,
            trace_enabled=trace_enabled,
            user_id=user_id,
            namespace=namespace,
            query=query,
            exclude_document_ids=exclude_document_ids,
            bootstrap_llm_fn=bootstrap_llm_fn,
        )

    return discovery_rows


async def register_discovery_documents(
    db: AsyncSession,
    *,
    state: AgentState,
    discovery_by_doc: dict[str, list[dict[str, Any]]],
) -> None:
    selected_doc_ids = {doc.document_id for doc in state.selected_docs}
    for doc_id in discovery_by_doc:
        if doc_id in selected_doc_ids or doc_id in state.ever_explored_doc_ids:
            continue
        doc_stmt = (
            select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
            .where(Document.document_id == doc_id)
        )
        doc_result = await db.execute(doc_stmt)
        row_data = doc_result.first()
        if row_data is None:
            continue
        did, fname, job_result_id = row_data
        state.selected_docs.append(
            CandidateDoc(
                document_id=did,
                source_file_name=fname or did,
                confidence=0.4,
                reason="discovery_auto (not in KG selection)",
                source="discovery_auto",
            )
        )
        state.doc_id_to_name[did] = fname or did
        if job_result_id:
            state.doc_job_map[did] = job_result_id


async def select_revision_documents(
    db: AsyncSession,
    *,
    state: AgentState,
    trace: TraceRecorder,
    trace_enabled: bool,
    user_id: str,
    namespace: str,
    query: str,
    exclude_document_ids: list[str],
    bootstrap_llm_fn: LLMFn,
    revision_hint: str,
) -> str | None:
    try:
        kg_result = await tools.kg_document_select(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            llm_fn=bootstrap_llm_fn,
            exclude_document_ids=list(set(exclude_document_ids)),
            revision_hint=revision_hint,
            budget_snapshot=state.ledger.snapshot() if state.ledger else None,
        )
    except BudgetExceeded:
        logger.info("  agentic: bootstrap budget exhausted during revision doc selection")
        if trace_enabled:
            trace.record_budget_stop("bootstrap_exhausted")
        return "bootstrap_budget"
    state.step_count += 1
    _append_selected_docs(state, kg_result)
    return None


async def _select_documents(
    db: AsyncSession,
    *,
    state: AgentState,
    trace: TraceRecorder,
    trace_enabled: bool,
    user_id: str,
    namespace: str,
    query: str,
    exclude_document_ids: list[str],
    bootstrap_llm_fn: LLMFn,
) -> None:
    try:
        kg_result = await tools.kg_document_select(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            llm_fn=bootstrap_llm_fn,
            exclude_document_ids=list(state.ever_explored_doc_ids | set(exclude_document_ids)),
            budget_snapshot=state.ledger.snapshot() if state.ledger else None,
        )
    except BudgetExceeded:
        logger.info("  agentic: bootstrap budget exhausted during document selection")
        if trace_enabled:
            trace.record_budget_stop("bootstrap_exhausted")
        kg_result = ToolResult(
            status="no_confident_doc",
            payload={"reason": "bootstrap budget exhausted"},
        )
    state.step_count += 1

    if trace_enabled:
        trace.record_step(
            "kg_document_select",
            kg_result,
            decision_reason="phase_1_doc_selection",
        )

    _append_selected_docs(state, kg_result)
    if not state.selected_docs and state.discovery_top_doc_ids:
        await _append_discovery_hints(db, state=state)

    logger.info(
        f"  agentic step {state.step_count}: kg_document_select "
        f"status={kg_result.status} docs={len(state.selected_docs)} "
        f"latency={kg_result.latency_ms}ms"
    )


async def _append_discovery_hints(db: AsyncSession, *, state: AgentState) -> None:
    hint_ids = [
        doc_id
        for doc_id in state.discovery_top_doc_ids
        if doc_id not in state.ever_explored_doc_ids
    ]
    if not hint_ids:
        return
    doc_stmt = (
        select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
        .where(Document.document_id.in_(hint_ids))
    )
    doc_result = await db.execute(doc_stmt)
    for doc_id, source_file_name, job_result_id in doc_result.all():
        state.selected_docs.append(
            CandidateDoc(
                document_id=doc_id,
                source_file_name=source_file_name or doc_id,
                confidence=0.5,
                reason="discovery_hint (KG returned 0)",
                source="discovery_hint",
            )
        )
        state.doc_id_to_name[doc_id] = source_file_name or doc_id
        if job_result_id:
            state.doc_job_map[doc_id] = job_result_id


def _append_selected_docs(state: AgentState, kg_result: ToolResult) -> None:
    if kg_result.status != "selected_docs":
        return
    for doc_data in kg_result.payload.get("candidate_docs", []):
        state.selected_docs.append(
            CandidateDoc(
                document_id=doc_data.get("document_id", ""),
                source_file_name=doc_data.get("source_file_name", ""),
                confidence=doc_data.get("confidence", 0.0),
                reason=doc_data.get("reason", ""),
                source=doc_data.get("source", ""),
            )
        )
    state.doc_id_to_name.update(kg_result.payload.get("doc_id_to_name", {}))
    state.doc_job_map.update(kg_result.payload.get("doc_job_map", {}))
