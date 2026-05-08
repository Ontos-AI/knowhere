"""Retrieval Agent orchestrator — the core agent loop.

Runs the state/action/observation cycle using the LLM-driven LLMPolicy
and agentic tools.  The fixed terminal step (hydrate + rank) always
executes, even if all tools fail — in that case it uses whatever
discovery rows were collected.

This module never raises exceptions to the caller.  Errors are captured
in the trace and the best available result is returned.
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.trace import TraceRecorder
from shared.services.retrieval.agentic.types import (
    ActionType,
    AgentRunConfig,
    AgentState,
    ToolResult,
)
from shared.services.retrieval.app_service import (
    _hydrate_paths_to_rows,
    _load_chunk_importance_scores,
    _normalize_row_scores,
    _rank_candidates_by_path,
)
from shared.services.retrieval.llm_adapter import LLMFn


def _build_config_from_env() -> AgentRunConfig:
    """Read agent config from environment, with sensible defaults."""
    return AgentRunConfig(
        max_steps=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_STEPS', '10')),
        max_docs=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_DOCS', '0')),
        max_path_expansions=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_PATH_EXPANSIONS', '2')),
        max_doc_retries=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_DOC_RETRIES', '2')),
        latency_budget_ms=int(os.environ.get('RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS', '12000')),
        min_evidence_paths=int(os.environ.get('RETRIEVAL_AGENTIC_MIN_EVIDENCE_PATHS', '1')),
    )


class RetrievalAgent:
    """Agentic retrieval orchestrator using LLMPolicy.

    Usage::

        agent = RetrievalAgent()
        ranked_rows, router = await agent.run(
            db, user_id=..., namespace=..., query=..., llm_fn=..., ...
        )

    The agent requires a valid ``llm_fn`` to run LLMPolicy.  If ``llm_fn``
    is None, the run terminates immediately after bottom_discovery only.
    """

    async def run(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        top_k: int = 10,
        llm_fn: LLMFn | None = None,
        exclude_document_ids: list[str] | None = None,
        exclude_sections: list[dict[str, str]] | None = None,
        data_type: int = 1,
        signal_paths: list[str] | None = None,
        filter_mode: str = 'delete',
        channels: list[str] | None = None,
        channel_weights: dict[str, float] | None = None,
        config: AgentRunConfig | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Run the agentic retrieval pipeline.

        Returns (ranked_rows, router_used).  Never raises — errors are
        captured in trace and the best available result is returned.
        """
        from shared.services.retrieval.agentic.policy import LLMPolicy

        config = config or _build_config_from_env()
        exclude_document_ids = exclude_document_ids or []
        exclude_sections = exclude_sections or []

        state = AgentState()
        trace = TraceRecorder(
            db, user_id=user_id, namespace=namespace, query=query,
            config=config, top_k=top_k, data_type=data_type,
            filters={
                'exclude_document_ids': exclude_document_ids,
                'exclude_sections': exclude_sections,
                'signal_paths': signal_paths,
            },
        )

        trace_enabled = os.environ.get('RETRIEVAL_AGENTIC_TRACE_ENABLED', 'true') == 'true'
        if trace_enabled:
            await trace.create_run()

        logger.info(
            f'agentic retrieval START: query="{query[:60]}..." '
            f'top_k={top_k} max_steps={config.max_steps} '
            f'budget={config.latency_budget_ms}ms'
        )

        if llm_fn is None:
            logger.warning('agentic: no llm_fn provided — running discovery-only mode')

        # Build LLMPolicy (needs llm_fn; gracefully handles None)
        policy = LLMPolicy(llm_fn, query=query) if llm_fn else None

        # Shared kwargs for tool calls
        tool_kwargs: dict[str, Any] = {
            'user_id': user_id,
            'namespace': namespace,
            'query': query,
            'top_k': top_k,
            'llm_fn': llm_fn,
            'exclude_document_ids': exclude_document_ids,
            'exclude_sections': exclude_sections,
            'data_type': data_type,
            'signal_paths': signal_paths,
            'filter_mode': filter_mode,
            'channels': channels,
            'channel_weights': channel_weights,
        }

        # ── Agent loop ──
        stop_reason = 'max_steps'
        while state.step_count < config.max_steps:
            if state.elapsed_ms >= config.latency_budget_ms:
                logger.info(
                    f'  agentic: latency budget hit ({state.elapsed_ms}ms >= {config.latency_budget_ms}ms), '
                    f'stopping at step {state.step_count}'
                )
                trace.record_budget_stop('latency')
                stop_reason = 'latency_budget'
                break

            if policy is None:
                # No LLM: run discovery once then stop
                if not state.discovery_done:
                    action_type = ActionType.BOTTOM_DISCOVERY
                    decision_reason = 'no_llm_fn: discovery only'
                else:
                    stop_reason = 'no_llm_fn'
                    break
            else:
                action_type, decision_reason = await policy.decide(state, config)

            if action_type is None or action_type == ActionType.DONE:
                stop_reason = 'llm_done' if action_type == ActionType.DONE else 'llm_stop'
                logger.info(
                    f'  agentic: policy returned {action_type} at step {state.step_count} '
                    f'(reason="{decision_reason}"), stopping'
                )
                # Record DONE as a trace step
                if trace_enabled and action_type == ActionType.DONE:
                    trace.record_step(
                        ActionType.DONE,
                        ToolResult(status='done', payload={'reason': decision_reason}),
                        decision_reason=decision_reason,
                    )
                break

            result = await self._execute_tool(db, action_type, state, config, **tool_kwargs)
            state.apply(action_type, result)

            if trace_enabled:
                trace.record_step(action_type, result, decision_reason=decision_reason)

            state.step_count += 1

            logger.info(
                f'  agentic step {state.step_count}: action={action_type.value} '
                f'status={result.status} latency={result.latency_ms}ms '
                f'docs={len(state.selected_docs)} paths={len(state.selected_paths)}'
            )

        # ── Fixed terminal step: hydrate + rank ──
        ranked_rows = await self._hydrate_and_rank(
            db, state, user_id=user_id, namespace=namespace, top_k=top_k,
        )

        router_used = (
            'agentic_llm' if state.selected_paths
            else 'agentic_discovery_only'
        )

        logger.info(
            f'agentic retrieval DONE: {len(ranked_rows)} results, '
            f'router={router_used}, steps={state.step_count}, '
            f'stop_reason={stop_reason}, {state.elapsed_ms}ms'
        )

        if trace_enabled:
            await trace.complete(ranked_rows, router_used)

        return ranked_rows, router_used

    async def _execute_tool(
        self,
        db: AsyncSession,
        action_type: ActionType,
        state: AgentState,
        config: AgentRunConfig,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute a single tool.  Never raises."""
        from shared.services.retrieval.agentic import tools

        try:
            if action_type == ActionType.BOTTOM_DISCOVERY:
                return await tools.bottom_discovery(db, **kwargs)

            elif action_type == ActionType.KG_DOCUMENT_SELECT:
                return await tools.kg_document_select(
                    db,
                    user_id=kwargs['user_id'],
                    namespace=kwargs['namespace'],
                    query=kwargs['query'],
                    llm_fn=kwargs.get('llm_fn'),
                    exclude_document_ids=list(state.excluded_doc_ids | set(kwargs.get('exclude_document_ids', []))),
                )

            elif action_type == ActionType.DOCUMENT_PATH_SELECT:
                # Find which document to process next
                doc = None
                if state.pending_doc_index < len(state.selected_docs):
                    doc = state.selected_docs[state.pending_doc_index]

                if doc is None:
                    return ToolResult(status='error', error='no document to process')

                job_result_id = state.doc_job_map.get(doc.document_id, '')
                if not job_result_id:
                    return ToolResult(
                        status='no_confident_match',
                        payload={'document_id': doc.document_id, 'reason': 'no job_result_id'},
                    )

                return await tools.document_path_select(
                    db,
                    user_id=kwargs['user_id'],
                    namespace=kwargs['namespace'],
                    query=kwargs['query'],
                    llm_fn=kwargs.get('llm_fn'),
                    document_id=doc.document_id,
                    job_result_id=job_result_id,
                    doc_name=doc.source_file_name or state.doc_id_to_name.get(doc.document_id, ''),
                )

            elif action_type == ActionType.GREP_DOCUMENT_DISCOVER:
                return await tools.grep_document_discover(
                    db,
                    user_id=kwargs['user_id'],
                    namespace=kwargs['namespace'],
                    query=kwargs['query'],
                    exclude_document_ids=list(state.excluded_doc_ids | set(kwargs.get('exclude_document_ids', []))),
                )

            elif action_type == ActionType.GRAPH_EXPAND_DOCS:
                doc_ids = [d.document_id for d in state.selected_docs]
                return await tools.graph_expand_docs(
                    db,
                    user_id=kwargs['user_id'],
                    namespace=kwargs['namespace'],
                    document_ids=doc_ids,
                )

            else:
                return ToolResult(status='error', error=f'unknown action: {action_type}')

        except Exception as e:
            logger.warning(f'  agentic tool {action_type.value} raised: {e}')
            return ToolResult(status='error', error=str(e))

    async def _hydrate_and_rank(
        self,
        db: AsyncSession,
        state: AgentState,
        *,
        user_id: str,
        namespace: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Fixed terminal step: hydrate selected paths + rank against discovery.

        Reuses _hydrate_paths_to_rows and _rank_candidates_by_path unchanged.

        TODO (Token Budget): Replace `ranked_rows[:top_k]` in
        `_rank_candidates_by_path` with token-accumulation truncation
        (tiktoken or character estimate) so the final result set respects
        a configurable LLM context window budget rather than a fixed count.
        """
        try:
            # Hydrate agent-selected paths
            navigated_paths: list[dict[str, Any]] = []
            if state.selected_paths:
                navigated_paths = await _hydrate_paths_to_rows(
                    db,
                    path_selections=state.selected_paths,
                    user_id=user_id,
                    namespace=namespace,
                )

            # Load importance scores for all candidate rows
            all_candidates = state.discovery_paths + navigated_paths
            if all_candidates:
                importance_map = await _load_chunk_importance_scores(
                    db, user_id=user_id, namespace=namespace,
                    rows=all_candidates,
                )
                for row in all_candidates:
                    chunk_id = row.get('chunk_id', '')
                    row['importance_raw_score'] = importance_map.get(chunk_id, 0.0)

                _normalize_row_scores(
                    all_candidates,
                    source_field='importance_raw_score',
                    target_field='importance_norm_score',
                    default=0.0,
                )

            # Rank: merge discovery + agent rows
            ranked = _rank_candidates_by_path(
                discovery_rows=state.discovery_paths,
                routed_rows=navigated_paths,
                top_k=top_k,
            )

            return ranked

        except Exception as e:
            logger.error(f'agentic hydrate_and_rank failed: {e}')
            # Last resort: return raw discovery rows
            return state.discovery_paths[:top_k]
