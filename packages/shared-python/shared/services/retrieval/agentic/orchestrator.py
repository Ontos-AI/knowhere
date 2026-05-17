"""Retrieval Agent orchestrator — navigate-then-answer loop.

Flow:
  Phase 1: Document selection (bottom_discovery + kg_document_select)
  Phase 2: Per-document navigation (iterative BFS via navigate_step)
  Phase 3: Render evidence → attempt_answer
    → DONE (has answer) → return answer + evidence
    → NOT_FOUND + reason → revision_hint → re-select docs + re-navigate
                           (exclude seen paths) → re-attempt
    → max_revisions → return best available

The orchestrator drives navigation via an iterative BFS queue per document,
calling navigate_step at each level. Each navigate_step is a single LLM call
that decides action (NAVIGATE/STOP), asset tools (FIND_IMAGES/FIND_TABLES),
and section selections. STOP terminates the drill-down for that scope.
After navigation completes, attempt_answer is called automatically.
"""
from __future__ import annotations

import os
from typing import Any, cast

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.budget import BudgetExceeded, BudgetLedger
from shared.services.retrieval.agentic.discovery_phase import (
    register_discovery_documents,
    run_initial_discovery,
    select_revision_documents,
)
from shared.services.retrieval.agentic.document_navigation import DocumentNavigationRunner
from shared.services.retrieval.agentic.evidence import (
    build_asset_url_map as _build_asset_url_map,
    collect_media_chunks_all as _collect_media_chunks_all,
    render_evidence as _render_evidence,
    trim_evidence_to_budget as _trim_evidence_to_budget,
    with_context_prompt_projection as _with_context_prompt_projection,
)
from shared.services.retrieval.agentic.runtime import (
    AgentLlmBudget,
    build_config_from_env as _build_config_from_env,
    load_budget_inventory as _load_budget_inventory,
)
from shared.services.retrieval.agentic.trace import TraceRecorder
from shared.services.retrieval.agentic.types import (
    AgentRunConfig,
    AgentState,
    AgenticResult,
    ToolResult,
)
from shared.services.retrieval.llm_adapter import LLMFn


class RetrievalAgent:
    """Agentic retrieval orchestrator — navigate-then-answer loop.

    Usage::

        agent = RetrievalAgent()
        result = await agent.run(
            db, user_id=..., namespace=..., query=..., llm_fn=..., ...
        )
        # result.evidence_text — hierarchical context
        # result.answer_text — LLM answer (empty if NOT_FOUND after max revisions)
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
        ledger: BudgetLedger | None = None,
        parent_run_id: str | None = None,
        workflow_step_id: str | None = None,
    ) -> AgenticResult:
        """Run the agentic retrieval pipeline.

        Returns an ``AgenticResult`` containing the rendered evidence
        text, LLM answer, and referenced chunk IDs.  Never raises —
        errors are captured in trace and the best available result
        is returned.
        """
        from shared.services.retrieval.agentic.policy import (
            attempt_answer,
            estimate_attempt_answer_prompt_tokens,
        )
        from shared.services.retrieval.llm_adapter import create_retrieval_vlm_fn

        vlm_fn = create_retrieval_vlm_fn()

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
        context_llm_fn: LLMFn | None = None
        llm_budget = AgentLlmBudget(state)
        if llm_fn is not None:
            bootstrap_llm_fn = llm_budget.for_pool(llm_fn, pool='bootstrap')
            context_llm_fn = llm_budget.for_pool(llm_fn, pool='context')

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
            if doc_id:
                discovery_by_doc.setdefault(doc_id, []).append(row)

        await register_discovery_documents(
            db,
            state=state,
            discovery_by_doc=discovery_by_doc,
        )

        if state.ledger is not None:
            await state.ledger.allocate_doc_caps({
                doc.document_id: chunks_count_by_doc.get(doc.document_id, 1)
                for doc in state.selected_docs
            })

        # ══════════════════════════════════════════════════════════════════
        # Phase 2 + 3 Loop: Navigate → Render → Attempt Answer → (Revise)
        # ══════════════════════════════════════════════════════════════════
        answer_text = ''
        evidence_text = ''
        revision_hint: str | None = None
        stop_reason = 'max_revisions'
        failure_reason = ''

        for round_idx in range(config.max_revisions + 1):
            if state.elapsed_ms >= config.latency_budget_ms:
                stop_reason = 'latency_budget'
                break

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
            await navigation_runner.navigate_selected_documents(revision_hint=revision_hint)

            # ── Phase 3: Render evidence + attempt_answer ────────────────
            budget_snapshot_before_answer = state.ledger.snapshot() if state.ledger else None
            context_remaining = (
                state.ledger.remaining('context') if state.ledger else config.token_budget_total
            )
            answer_prompt_overhead = estimate_attempt_answer_prompt_tokens(
                query=query,
                evidence_text='',
                state=state,
                config=config,
                budget_snapshot=budget_snapshot_before_answer,
            )
            evidence_text = await _trim_evidence_to_budget(
                db,
                doc_trees=state.doc_trees,
                doc_id_to_name=state.doc_id_to_name,
                context_remaining=max(context_remaining - answer_prompt_overhead, 0),
                user_id=user_id,
                namespace=namespace,
                ledger=state.ledger,
            )
            answer_prompt_tokens = estimate_attempt_answer_prompt_tokens(
                query=query,
                evidence_text=evidence_text,
                state=state,
                config=config,
                budget_snapshot=budget_snapshot_before_answer,
            )
            budget_snapshot_for_answer = (
                _with_context_prompt_projection(
                    state.ledger.snapshot(),
                    prompt_tokens=answer_prompt_tokens,
                )
                if state.ledger else None
            )
            if budget_snapshot_for_answer is not None:
                answer_prompt_tokens = estimate_attempt_answer_prompt_tokens(
                    query=query,
                    evidence_text=evidence_text,
                    state=state,
                    config=config,
                    budget_snapshot=budget_snapshot_for_answer,
                )
                budget_snapshot_for_answer = _with_context_prompt_projection(
                    state.ledger.snapshot(),
                    prompt_tokens=answer_prompt_tokens,
                )
                context_budget = (budget_snapshot_for_answer.get('context') or {})
                logger.info(
                    '  agentic: answer context projection '
                    f'prompt_tokens={answer_prompt_tokens} '
                    f'remaining={context_budget.get("remaining")}/'
                    f'{context_budget.get("capacity")} '
                    f'status={context_budget.get("status")}'
                )

            if context_llm_fn is None:
                stop_reason = 'no_llm'
                break

            # Collect image URLs from evidence for VLM switch
            evidence_image_urls: list[str] = []
            if vlm_fn:
                asset_url_map = await _build_asset_url_map(
                    _collect_media_chunks_all(state.doc_trees),
                )
                evidence_image_urls = [
                    url for url in asset_url_map.values() if url
                ]

            async def vlm_context_call(prompt, _vlm_fn=vlm_fn):
                return await llm_budget.call(cast(LLMFn, _vlm_fn), prompt, pool='context')

            # Auto-trigger attempt_answer (VLM if images present)
            try:
                status, answer_text, reason = await attempt_answer(
                    context_llm_fn,
                    query=query,
                    evidence_text=evidence_text,
                    state=state,
                    config=config,
                    vlm_fn=vlm_context_call if vlm_fn else None,
                    image_urls=evidence_image_urls or None,
                    budget_snapshot=budget_snapshot_for_answer,
                )
            except BudgetExceeded:
                logger.info('  agentic: context budget exhausted before attempt_answer')
                if trace_enabled:
                    trace.record_budget_stop('context_exhausted')
                status, answer_text, reason = 'NOT_FOUND', '', 'context budget exhausted'
                stop_reason = 'context_budget'
                break
            state.step_count += 1

            if trace_enabled:
                trace.record_step(
                    'attempt_answer', ToolResult(
                        status=status,
                        payload={
                            'answer_length': len(answer_text),
                            'reason': reason,
                        },
                    ),
                    decision_reason=f'phase_3_answer_r{round_idx}',
                )

            logger.info(
                f'  agentic: attempt_answer status={status} '
                f'round={round_idx}/{config.max_revisions} '
                f'answer_len={len(answer_text)} reason="{reason}"'
            )

            if status == 'DONE':
                stop_reason = 'answer_done'
                failure_reason = ''
                break

            # ── NOT_FOUND: prepare revision ──────────────────────────────
            failure_reason = reason
            if round_idx >= config.max_revisions:
                stop_reason = 'max_revisions'
                break

            state.revision_count += 1
            revision_hint = reason
            logger.info(f'  agentic: starting revision {state.revision_count}, hint="{reason}"')

            # Record all explored paths for masking (deepest-first removal)
            for doc_id, doc_tree in state.doc_trees.items():
                state.seen_section_keys.update(doc_tree.collect_all_paths(doc_id))

            # Clear doc selection for re-exploration (preserve doc_trees for merge)
            state.selected_docs.clear()

            # Re-run KG select with revision hint
            if bootstrap_llm_fn is None:
                stop_reason = 'no_llm'
                break
            revision_stop_reason = await select_revision_documents(
                db,
                state=state,
                trace=trace,
                trace_enabled=trace_enabled,
                user_id=user_id,
                namespace=namespace,
                query=query,
                exclude_document_ids=exclude_document_ids,
                bootstrap_llm_fn=bootstrap_llm_fn,
                revision_hint=revision_hint,
            )
            if revision_stop_reason is not None:
                stop_reason = revision_stop_reason
                break

            if not state.selected_docs:
                logger.info('  agentic: revision found no new docs — stopping')
                stop_reason = 'no_new_docs'
                break

            if state.ledger is not None:
                await state.ledger.allocate_doc_caps({
                    doc.document_id: chunks_count_by_doc.get(doc.document_id, 1)
                    for doc in state.selected_docs
                })

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

        # Re-render final evidence (may have been updated in last revision)
        if not evidence_text or evidence_text == '(no evidence collected)':
            evidence_text = await _render_evidence(
                db,
                state.doc_trees, state.doc_id_to_name,
            )

        result = AgenticResult(
            evidence_text=evidence_text,
            answer_text=answer_text,
            referenced_chunks=all_refs,
            router_used=router_used,
            budget_snapshot=state.ledger.snapshot() if state.ledger else None,
            stop_reason=stop_reason,
            failure_reason=failure_reason,
        )

        logger.info(
            f'agentic retrieval DONE: {len(all_refs)} referenced chunks, '
            f'evidence_text={len(evidence_text)} chars, '
            f'answer_text={len(answer_text)} chars, '
            f'router={router_used}, steps={state.step_count}, '
            f'stop_reason={stop_reason}, revisions={state.revision_count}, '
            f'{state.elapsed_ms}ms'
        )

        if trace_enabled:
            await trace.complete(
                all_refs,
                router_used,
                budget_snapshot=state.ledger.snapshot() if state.ledger else None,
            )

        return result
