"""Retrieval Agent orchestrator — evidence-only navigation loop.

Flow:
  Phase 1: Document selection (bottom_discovery + kg_document_select)
  Phase 2: Per-document navigation (iterative BFS via navigate_step)
  Phase 3: Render evidence text for downstream agents

The orchestrator drives navigation via an iterative BFS queue per document,
calling navigate_step at each level. Each navigate_step is a single LLM call
that decides action (NAVIGATE/STOP), asset tools (FIND_IMAGES/FIND_TABLES),
and section selections. STOP terminates the drill-down for that scope.

KNOWHERE does not generate final answers. Downstream agents decide whether the
returned evidence is sufficient for their task and may call retrieval again.
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.core.budget import BudgetLedger
from shared.services.retrieval.agentic.discovery.phase import (
    run_initial_discovery,
)
from shared.services.retrieval.agentic.navigation.document import DocumentNavigationRunner
from shared.services.retrieval.agentic.evidence.builder import (
    trim_evidence_to_budget as _trim_evidence_to_budget,
)
from shared.services.retrieval.agentic.core.runtime import (
    AgentLlmBudget,
    build_config_from_env as _build_config_from_env,
    load_budget_inventory as _load_budget_inventory,
)
from shared.services.retrieval.agentic.core.trace import TraceRecorder
from shared.services.retrieval.agentic.core.types import (
    AgentRunConfig,
    AgentState,
    AgenticResult,
)
from shared.services.retrieval.llm_adapter import LLMFn
from shared.services.retrieval.settings import DEFAULT_TOP_K


class RetrievalAgent:
    """Agentic retrieval orchestrator — navigate and return evidence.

    Usage::

        agent = RetrievalAgent()
        result = await agent.run(
            db, user_id=..., namespace=..., query=..., llm_fn=..., ...
        )
        # result.evidence_text — hierarchical context for downstream agents
        # result.answer_text — deprecated, always empty
        # result.referenced_chunks — chunk IDs for hit stats / frontend

    The agent requires a valid ``llm_fn`` for LLM-driven navigation.
    If ``llm_fn`` is None, the run returns discovery-only results.
    """

    async def run(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        llm_fn: LLMFn | None = None,
        exclude_document_ids: list[str] | None = None,
        exclude_sections: list[dict[str, str]] | None = None,
        data_type: int = 1,
        signal_paths: list[str] | None = None,
        filter_mode: str = 'delete',
        channels: list[str] | None = None,
        channel_weights: dict[str, float] | None = None,
        internal_recall_k: int | None = None,
        config: AgentRunConfig | None = None,
        ledger: BudgetLedger | None = None,
        parent_run_id: str | None = None,
        workflow_step_id: str | None = None,
    ) -> AgenticResult:
        """Run the agentic retrieval pipeline.

        Returns an ``AgenticResult`` containing rendered evidence text
        and referenced chunk IDs. Never raises — errors are captured in
        trace and the best available evidence is returned.
        """
        config = config or _build_config_from_env()
        exclude_document_ids = exclude_document_ids or []
        exclude_sections = exclude_sections or []

        state = AgentState()
        state.ledger = ledger or BudgetLedger(
            total=config.token_budget_total,
            planning_ratio=config.planning_ratio,
            bootstrap=config.bootstrap_budget,
            per_doc_min_share=config.per_doc_min_share,
        )
        total_chunks, total_docs, chunks_count_by_doc = await _load_budget_inventory(
            db,
            user_id=user_id,
            namespace=namespace,
            exclude_document_ids=exclude_document_ids,
        )
        state.kg_total_chunks = total_chunks
        state.kg_total_docs = total_docs
        state.ledger.total_chunks = total_chunks
        state.ledger.total_docs = total_docs
        trace = TraceRecorder(
            db, user_id=user_id, namespace=namespace, query=query,
            config=config, top_k=top_k, data_type=data_type,
            filters={
                'exclude_document_ids': exclude_document_ids,
                'exclude_sections': exclude_sections,
                'signal_paths': signal_paths,
                'internal_recall_k': internal_recall_k,
            },
            parent_run_id=parent_run_id,
            workflow_step_id=workflow_step_id,
        )

        trace_enabled = os.environ.get('RETRIEVAL_AGENTIC_TRACE_ENABLED', 'true') == 'true'
        if trace_enabled:
            await trace.create_run()

        logger.info(
            f'agentic retrieval START: query="{query[:60]}..." '
            f'top_k={top_k} latency_budget={config.latency_budget_ms}ms '
            f'token_budget={config.token_budget_total}'
        )

        if llm_fn is None:
            logger.warning('agentic: no llm_fn provided — running discovery-only mode')

        bootstrap_llm_fn: LLMFn | None = None
        llm_budget = AgentLlmBudget(state)
        if llm_fn is not None:
            bootstrap_llm_fn = llm_budget.for_pool(llm_fn, pool='bootstrap')

        discovery_rows = await run_initial_discovery(
            db,
            state=state,
            trace=trace,
            trace_enabled=trace_enabled,
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
            bootstrap_llm_fn=bootstrap_llm_fn,
        )

        # If no LLM or no docs selected, return discovery rows directly
        if not state.selected_docs:
            logger.info('agentic: no documents selected — returning discovery results')
            discovery_refs = [
                {
                    'chunk_id': r.get('chunk_id', ''),
                    'document_id': r.get('document_id', ''),
                    'chunk_type': r.get('chunk_type', ''),
                    'section_path': (
                        r.get('source_file_name', '')
                        if r.get('section_path') == 'Root'
                        else r.get('section_path', '')
                    ),
                    'file_path': r.get('file_path', ''),
                }
                for r in discovery_rows[:top_k]
                if r.get('chunk_id')
            ]
            if trace_enabled:
                await trace.complete(
                    discovery_rows,
                    'agentic_discovery_only',
                    budget_snapshot=state.ledger.snapshot() if state.ledger else None,
                )
            return AgenticResult(
                evidence_text='',
                answer_text='',
                referenced_chunks=discovery_refs,
                router_used='agentic_discovery_only',
            )

        discovery_by_doc: dict[str, list[dict[str, Any]]] = {}
        for row in discovery_rows:
            doc_id = row.get('document_id', '')
            # Root chunks are navigable via the section tree; exclude them
            # from discovery hints where the bare "Root" label gives the LLM
            # no actionable information.
            section_path = str(row.get('section_path', '') or '').strip()
            if not doc_id or section_path == 'Root':
                continue
            discovery_by_doc.setdefault(doc_id, []).append(row)


        if state.ledger is not None:
            await state.ledger.allocate_doc_caps({
                doc.document_id: chunks_count_by_doc.get(doc.document_id, 1)
                for doc in state.selected_docs
            })

        # Phase 2 + 3: navigate once, then render evidence for downstream agents.
        evidence_text = ''
        stop_reason = 'evidence_only'
        failure_reason = ''
        decision_trace: list[dict[str, Any]] = []

        # Record KG document selection as the first decision trace entry
        if state.selected_docs:
            decision_trace.append({
                'phase': 'kg_select',
                'action': 'select',
                'reason': f'{len(state.selected_docs)} document(s) selected for navigation',
                'documents': [
                    {
                        'document': doc.source_file_name,
                        'document_id': doc.document_id,
                        'confidence': doc.confidence,
                        'reason': doc.reason,
                        'source': doc.source,
                    }
                    for doc in state.selected_docs
                ],
            })

        if state.elapsed_ms >= config.latency_budget_ms:
            stop_reason = 'latency_budget'
        else:
            navigation_runner = DocumentNavigationRunner(
                db=db,
                state=state,
                trace=trace,
                trace_enabled=trace_enabled,
                user_id=user_id,
                namespace=namespace,
                query=query,
                config=config,
                discovery_by_doc=discovery_by_doc,
                llm_fn=llm_fn,
                llm_budget=llm_budget,
            )
            await navigation_runner.navigate_selected_documents()
            decision_trace.extend(navigation_runner.decision_steps)
            context_remaining = state.ledger.remaining('context') if state.ledger else config.token_budget_total
            evidence_text = await _trim_evidence_to_budget(
                db,
                doc_trees=state.doc_trees,
                doc_id_to_name=state.doc_id_to_name,
                context_remaining=context_remaining,
                user_id=user_id,
                namespace=namespace,
                ledger=state.ledger,
            )

        # ══════════════════════════════════════════════════════════════════
        # Final Assembly
        # ══════════════════════════════════════════════════════════════════
        router_used = (
            'agentic_llm' if any(t.has_content() for t in state.doc_trees.values())
            else 'agentic_discovery_only'
        )

        # Collect referenced chunk IDs from all doc trees
        all_refs: list[dict[str, str]] = []
        seen_ref_ids: set[str] = set()
        for doc_id, doc_tree in state.doc_trees.items():
            doc_name = state.doc_id_to_name.get(doc_id, doc_id)
            for ref in doc_tree.collect_referenced_ids(document_name=doc_name):
                cid = ref.get('chunk_id', '')
                if cid and cid not in seen_ref_ids:
                    seen_ref_ids.add(cid)
                    all_refs.append(ref)




        result = AgenticResult(
            evidence_text=evidence_text,
            answer_text='',
            referenced_chunks=all_refs,
            router_used=router_used,
            budget_snapshot=state.ledger.snapshot() if state.ledger else None,
            stop_reason=stop_reason,
            failure_reason=failure_reason,
            decision_trace=decision_trace,
        )

        logger.info(
            f'agentic retrieval DONE: {len(all_refs)} referenced chunks, '
            f'evidence_text={len(evidence_text)} chars, '
            f'router={router_used}, steps={state.step_count}, '
            f'stop_reason={stop_reason}, '
            f'{state.elapsed_ms}ms'
        )

        if trace_enabled:
            await trace.complete(
                all_refs,
                router_used,
                budget_snapshot=state.ledger.snapshot() if state.ledger else None,
            )

        return result
