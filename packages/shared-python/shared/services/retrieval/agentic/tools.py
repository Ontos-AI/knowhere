"""Agentic retrieval tool adapters.

Concrete tool implementations live in focused Modules. This file is the stable
adapter seam used by the workflow orchestrator and contract tests.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.core.types import NavigateStepResult, ToolResult
from shared.services.retrieval.agentic.discovery import tools as discovery_tools
from shared.services.retrieval.agentic.navigation import assets as asset_tools
from shared.services.retrieval.agentic.navigation import tools as navigation_tools
from shared.services.retrieval.llm_adapter import LLMFn


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
    **kwargs: Any,
) -> ToolResult:
    return await discovery_tools.bottom_discovery(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        data_type=data_type,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        channels=channels,
        channel_weights=channel_weights,
        internal_recall_k=internal_recall_k,
        **kwargs,
    )


async def kg_document_select(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    llm_fn: LLMFn | None,
    exclude_document_ids: list[str],
    discovery_signals: dict[str, list[str]] | None = None,
    **kwargs: Any,
) -> ToolResult:
    return await discovery_tools.kg_document_select(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        llm_fn=llm_fn,
        exclude_document_ids=exclude_document_ids,
        discovery_signals=discovery_signals,
        **kwargs,
    )


async def navigate_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    query: str,
    llm_fn: LLMFn,
    user_id: str,
    namespace: str,
    doc_name: str = "",
    scope_path: str | None = None,
    exclude_paths: set[str] | None = None,
    budget_snapshot: dict | None = None,
    nav_trace: list[dict[str, Any]] | None = None,
    collected_paths: list[dict[str, Any]] | None = None,
    expanded_scopes: set[str] | None = None,
    rejected_paths: set[str] | None = None,
    rejected_collect_paths: set[str] | None = None,
    disabled_asset_types: set[str] | None = None,
    discovery_hints: list[dict[str, Any]] | None = None,
    section_rows: list | None = None,
    query_intent: str = "UNKNOWN",
    search_context: str = "",
    prior_tool_result: dict[str, Any] | None = None,
) -> NavigateStepResult:
    return await navigation_tools.navigate_step(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        query=query,
        llm_fn=llm_fn,
        user_id=user_id,
        namespace=namespace,
        doc_name=doc_name,
        scope_path=scope_path,
        exclude_paths=exclude_paths,
        budget_snapshot=budget_snapshot,
        nav_trace=nav_trace,
        collected_paths=collected_paths,
        expanded_scopes=expanded_scopes,
        rejected_paths=rejected_paths,
        rejected_collect_paths=rejected_collect_paths,
        disabled_asset_types=disabled_asset_types,
        discovery_hints=discovery_hints,
        section_rows=section_rows,
        query_intent=query_intent,
        search_context=search_context,
        prior_tool_result=prior_tool_result,
    )


async def search_assets_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None,
    asset_type: str,
    query: str,
    llm_fn: LLMFn,
    vlm_fn: LLMFn | None = None,
) -> dict[str, Any]:
    return await asset_tools.search_assets_step(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        scope_path=scope_path,
        asset_type=asset_type,
        query=query,
        llm_fn=llm_fn,
        vlm_fn=vlm_fn,
    )
