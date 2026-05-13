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
import json
from typing import Any, cast

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, RetrievalHitStat

from shared.services.retrieval.agentic.budget import BudgetExceeded, BudgetLedger, BudgetPoolName
from shared.services.retrieval.agentic.trace import TraceRecorder
from shared.services.retrieval.agentic.types import (
    AgentRunConfig,
    AgentState,
    AgenticResult,
    CandidateDoc,
    DocTreeNode,
    ToolResult,
)
from shared.services.retrieval.app_service import (
    generate_retrieval_asset_url,
    _is_client_result_artifact_ref,
)
from shared.services.retrieval.llm_adapter import LLMFn
from shared.services.retrieval.llm_adapter import current_llm_usage
from shared.services.retrieval.hit_stats_service import compute_importance_score
from shared.utils.token_estimate import estimate_tokens



def _with_context_prompt_projection(
    snapshot: dict[str, object],
    *,
    prompt_tokens: int,
) -> dict[str, object]:
    """Return a display snapshot that includes the upcoming answer prompt."""
    projected: dict[str, object] = dict(snapshot)
    context_raw = projected.get('context') or {}
    if not isinstance(context_raw, dict):
        return projected
    context = dict(context_raw)
    used = int(context.get('used', 0) or 0)
    reserved = int(context.get('reserved', 0) or 0)
    capacity = int(context.get('capacity', 0) or 0)
    projected_used = min(capacity, used + max(int(prompt_tokens), 0))
    projected_remaining = max(capacity - projected_used - reserved, 0)
    context.update({
        'used_projected_before_answer': projected_used,
        'answer_prompt_estimate': max(int(prompt_tokens), 0),
        'remaining': projected_remaining,
        'used_pct': 100 if capacity <= 0 else min(
            100,
            int(round((projected_used + reserved) * 100 / capacity)),
        ),
    })
    if projected_remaining <= 0:
        context['status'] = 'EXHAUSTED'
    elif context['used_pct'] >= 80:
        context['status'] = 'CRITICAL'
    elif context['used_pct'] >= 50:
        context['status'] = 'TIGHT'
    else:
        context['status'] = 'HEALTHY'
    projected['context'] = context
    return projected





def _collect_media_chunks(node: DocTreeNode) -> list[dict[str, Any]]:
    """Recursively collect image/table chunks from a doc tree's leaf_content."""
    media: list[dict[str, Any]] = []
    for chunks in node.leaf_content.values():
        for chunk in chunks:
            ct = (chunk.get('chunk_type') or chunk.get('type') or '').strip().lower()
            if ct in ('image', 'table'):
                media.append(chunk)
    for child in node.children.values():
        media.extend(_collect_media_chunks(child))
    return media


def _collect_media_chunks_all(doc_trees: dict[str, DocTreeNode]) -> list[dict[str, Any]]:
    """Collect media chunks from all doc trees."""
    media: list[dict[str, Any]] = []
    for tree in doc_trees.values():
        media.extend(_collect_media_chunks(tree))
    return media


async def _build_asset_url_map(
    media_chunks: list[dict[str, Any]],
) -> dict[str, str]:
    """Generate presigned asset URLs for media chunks.

    Uses the same ``generate_retrieval_asset_url`` as ``_to_public_response``
    in ``app_service.py`` — no separate logic.
    """
    url_map: dict[str, str] = {}
    for chunk in media_chunks:
        chunk_id = str(chunk.get('chunk_id') or '').strip()
        file_path = chunk.get('file_path') or ''
        job_id = chunk.get('job_id') or ''
        if not chunk_id or not file_path or not job_id:
            continue
        if not _is_client_result_artifact_ref(file_path):
            continue
        try:
            url = await generate_retrieval_asset_url(
                job_id=str(job_id),
                artifact_ref=str(file_path),
            )
            if url:
                url_map[chunk_id] = url
        except Exception as e:
            logger.warning(f'Failed to generate asset URL for {chunk_id} (ignored): {e}')
    return url_map


def _collect_all_leaf_paths(node: DocTreeNode) -> set[str]:
    """Recursively collect all leaf_content keys across the entire tree."""
    paths = set(node.leaf_content.keys())
    for child in node.children.values():
        paths.update(_collect_all_leaf_paths(child))
    return paths


def _collect_visible_paths(node: DocTreeNode) -> set[str]:
    """Collect all outline_items paths across the entire tree.

    These are sections that are "visible" in the rendered tree (shown to the
    LLM during navigation) even if no chunks have been hydrated into them yet.
    Used as fallback targets for asset reconciliation.
    """
    paths = {item['path'] for item in node.outline_items if item.get('path')}
    for child in node.children.values():
        paths.update(_collect_visible_paths(child))
    return paths


def _find_closest_ancestor(path: str, target_paths: set[str]) -> str | None:
    """Walk up a section path to find the closest ancestor in target_paths.

    Example: path="kb/file/Ch1/S1.1/S1.1.1", target_paths={"kb/file/Ch1/S1.1"}
    → returns "kb/file/Ch1/S1.1"

    Uses the ' / ' separator convention from the section path format.
    """
    parts = path.split(' / ')
    # Walk from most specific to least specific (skip the full path itself)
    for i in range(len(parts) - 1, 0, -1):
        ancestor = ' / '.join(parts[:i])
        if ancestor in target_paths:
            return ancestor
    return None


def _reconcile_deferred_assets(
    tree: DocTreeNode,
    pending_assets: list[dict],
) -> None:
    """Place collected assets into the tree based on final navigated paths.

    Called ONCE after the entire BFS + discovery merge completes for a
    document.  Asset placement uses a two-tier strategy:

    1. **Exact match**: If the asset's ``owner_section_path`` matches a
       leaf_content key, place directly (existing behavior).
    2. **Closest visible ancestor**: If exact match fails, walk up the
       owner_section_path hierarchy to find the nearest ancestor that
       appears in either leaf_content or outline_items.  This handles
       the case where the LLM stopped navigation early (e.g. at root)
       but still requested images/tables — assets at L3 get attributed
       to the visible L2 section on their path.
    """
    final_paths = _collect_all_leaf_paths(tree)
    visible_paths = _collect_visible_paths(tree)
    all_target_paths = final_paths | visible_paths

    if not all_target_paths:
        return

    # Collect existing chunk_ids to avoid duplicates
    existing_ids = {
        str(row.get('chunk_id') or '')
        for row in tree.flatten_chunk_rows()
        if row.get('chunk_id')
    }

    placed = 0
    ancestor_placed = 0
    for asset in pending_assets:
        chunk_id = str(asset.get('chunk_id') or '')
        if chunk_id and chunk_id in existing_ids:
            continue  # already in tree via hydrate_connected_target_rows

        owner_path = (
            asset.get('owner_section_path')
            or asset.get('section_path')
        )
        if not owner_path:
            continue

        # Tier 1: exact match in leaf_content or visible outline
        target_path = owner_path if owner_path in all_target_paths else None

        # Tier 2: closest visible ancestor fallback
        if target_path is None:
            target_path = _find_closest_ancestor(owner_path, all_target_paths)
            if target_path:
                ancestor_placed += 1

        if target_path is None:
            continue  # no visible ancestor → discard

        # Place into root; reparent_leaf_content will move to correct child
        tree.add_leaf_chunks(target_path, [asset])
        if chunk_id:
            existing_ids.add(chunk_id)
        placed += 1

    if placed:
        tree.reparent_leaf_content()
        logger.info(
            f'  deferred asset reconcile: {placed}/{len(pending_assets)} '
            f'assets placed into {len(final_paths)} leaf + {len(visible_paths)} visible paths '
            f'(ancestor_fallback={ancestor_placed})'
        )


def _build_config_from_env() -> AgentRunConfig:
    """Read agent config from environment, with sensible defaults."""
    return AgentRunConfig(
        max_revisions=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_REVISIONS', '2')),
        max_nav_depth=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_NAV_DEPTH', '3')),
        latency_budget_ms=int(os.environ.get('RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS', '12000')),
        token_budget_total=int(os.environ.get('RETRIEVAL_AGENTIC_TOKEN_BUDGET_TOTAL', '40000')),
        planning_ratio=float(os.environ.get('RETRIEVAL_AGENTIC_PLANNING_RATIO', '0.5')),
        bootstrap_budget=int(os.environ.get('RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET', '2000')),
        per_doc_min_share=int(os.environ.get('RETRIEVAL_AGENTIC_PER_DOC_MIN_SHARE', '1500')),
        inventory_aware=os.environ.get('RETRIEVAL_AGENTIC_INVENTORY_AWARE', 'true') == 'true',
    )


def _stringify_llm_input(prompt: Any) -> str:
    if isinstance(prompt, str):
        return prompt
    try:
        return json.dumps(prompt, ensure_ascii=False, default=str)
    except Exception:
        return str(prompt)


async def _render_evidence(
    db: AsyncSession,
    doc_trees: dict[str, DocTreeNode],
    doc_id_to_name: dict[str, str],
) -> str:
    """Render unified evidence text from doc trees.

    Discovery paths are now handled by ``discovery_select_step`` in Phase 2
    and merged into doc_trees — no separate fallback needed.
    """
    from shared.services.retrieval.agent_navigate import render_unified_doc_tree

    # Build asset URL map for all media chunks (images/tables)
    # — same pattern as _to_public_response in app_service.py
    all_media_chunks: list[dict[str, Any]] = []
    for doc_tree in doc_trees.values():
        all_media_chunks.extend(_collect_media_chunks(doc_tree))
    asset_url_map = await _build_asset_url_map(all_media_chunks)

    # Render unified evidence from doc trees
    evidence_parts: list[str] = []
    for doc_id, doc_tree in doc_trees.items():
        if doc_tree.has_content():
            doc_name = doc_id_to_name.get(doc_id, doc_id)
            rendered = render_unified_doc_tree(doc_tree, doc_name, asset_lookup=asset_url_map)
            if rendered.strip():
                evidence_parts.append(rendered)

    return '\n\n'.join(evidence_parts) if evidence_parts else '(no evidence collected)'


async def _load_budget_inventory(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
) -> tuple[int, int, dict[str, int]]:
    stmt = (
        select(Document.document_id, func.count(DocumentChunk.id))
        .join(
            DocumentChunk,
            (DocumentChunk.document_id == Document.document_id)
            & (DocumentChunk.job_result_id == Document.current_job_result_id),
        )
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
        .group_by(Document.document_id)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))

    result = await db.execute(stmt)
    doc_chunks = {str(doc_id): int(count or 0) for doc_id, count in result.all()}
    return sum(doc_chunks.values()), len(doc_chunks), doc_chunks


def _iter_leaf_content(node: DocTreeNode):
    for path, chunks in node.leaf_content.items():
        yield path, chunks
    for child in node.children.values():
        yield from _iter_leaf_content(child)


def _collect_confidences(node: DocTreeNode) -> dict[str, float]:
    values = dict(node.confidence)
    for child in node.children.values():
        for path, score in _collect_confidences(child).items():
            values[path] = max(values.get(path, 0.0), score)
    return values


def _pop_leaf_path(node: DocTreeNode, path: str) -> bool:
    if path in node.leaf_content:
        node.leaf_content.pop(path)
        return True
    for child in node.children.values():
        if _pop_leaf_path(child, path):
            return True
    return False


def _estimate_chunks_tokens(chunks: list[dict[str, Any]]) -> int:
    text = '\n'.join(str(chunk.get('content') or '') for chunk in chunks)
    return estimate_tokens(text)


async def _fetch_importance_norm_scores(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    chunk_ids: list[str],
) -> dict[str, float]:
    if not chunk_ids:
        return {}
    stmt = (
        select(
            RetrievalHitStat.chunk_id,
            RetrievalHitStat.hit_count,
            RetrievalHitStat.last_hit_at,
            RetrievalHitStat.created_at,
        )
        .where(RetrievalHitStat.user_id == user_id)
        .where(RetrievalHitStat.namespace == namespace)
        .where(RetrievalHitStat.hit_kind == 'chunk')
        .where(RetrievalHitStat.chunk_id.in_(chunk_ids))
    )
    result = await db.execute(stmt)
    scores: dict[str, float] = {}
    for chunk_id, hit_count, last_hit_at, created_at in result.all():
        if chunk_id and last_hit_at and created_at:
            scores[str(chunk_id)] = compute_importance_score(hit_count, last_hit_at, created_at)
    return scores


async def _trim_evidence_to_budget(
    db: AsyncSession,
    *,
    doc_trees: dict[str, DocTreeNode],
    doc_id_to_name: dict[str, str],
    context_remaining: int,
    user_id: str,
    namespace: str,
    ledger: BudgetLedger | None,
    safety_margin: float = 0.9,
) -> str:
    full_text = await _render_evidence(db, doc_trees, doc_id_to_name)
    target = int(max(context_remaining, 0) * safety_margin)
    if estimate_tokens(full_text) <= target:
        return full_text

    candidates: list[tuple[str, str, tuple[float, float, float], int]] = []
    for doc_id, tree in doc_trees.items():
        confidence = _collect_confidences(tree)
        for path, chunks in _iter_leaf_content(tree):
            chunk_ids = [
                str(chunk.get('chunk_id'))
                for chunk in chunks
                if chunk.get('chunk_id')
            ]
            importance = 0.0
            importance_scores = await _fetch_importance_norm_scores(
                db,
                user_id=user_id,
                namespace=namespace,
                chunk_ids=chunk_ids,
            )
            if importance_scores:
                importance = max(importance_scores.values())
            discovery_score = (
                float(chunks[0].get('discovery_score', 0.0) or 0.0)
                if chunks else 0.0
            )
            score = (float(confidence.get(path, 0.0) or 0.0), discovery_score, importance)
            candidates.append((doc_id, path, score, _estimate_chunks_tokens(chunks)))

    current_estimate = estimate_tokens(full_text)
    removed: list[dict[str, Any]] = []
    for doc_id, path, _score, token_estimate in sorted(
        candidates,
        key=lambda item: (item[2], -item[3]),
    ):
        if current_estimate <= target:
            break
        if _pop_leaf_path(doc_trees[doc_id], path):
            confidence_score, discovery_score, importance_score = _score
            removed.append({
                'document_id': doc_id,
                'document_name': doc_id_to_name.get(doc_id, doc_id),
                'path': path,
                'confidence_score': round(confidence_score, 4),
                'discovery_score': round(discovery_score, 4),
                'importance_score': round(importance_score, 4),
                'token_estimate': token_estimate,
            })
            current_estimate = max(current_estimate - token_estimate, 0)

    if ledger is not None:
        ledger.trimmed_paths.extend(removed)
    logger.info(
        f'  agentic.trim_evidence: removed={len(removed)} '
        f'est_tokens={current_estimate} target={target}'
    )
    return await _render_evidence(db, doc_trees, doc_id_to_name)


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

    async def _call_llm_with_budget(
        self,
        state: AgentState,
        llm_fn: LLMFn,
        prompt: Any,
        *,
        pool: BudgetPoolName,
        doc_id: str | None = None,
        priority: str = 'normal',
    ) -> str:
        ledger = state.ledger
        if ledger is None:
            return await llm_fn(prompt)

        prompt_text = _stringify_llm_input(prompt)
        est = estimate_tokens(prompt_text)
        reserved = await ledger.try_reserve(
            pool,
            est,
            doc_id=doc_id,
            priority='low' if priority == 'low' else 'normal',
        )
        if not reserved:
            raise BudgetExceeded(f'{pool} budget exhausted')

        try:
            response = await llm_fn(prompt)
        except Exception:
            await ledger.refund(pool, est=est, doc_id=doc_id)
            raise

        usage = current_llm_usage.get() or {}
        actual = int(usage.get('prompt_tokens') or est)
        await ledger.commit(pool, actual=actual, est=est, doc_id=doc_id)
        return response

    def _budgeted_doc_llm_fn(
        self,
        state: AgentState,
        llm_fn: LLMFn,
        *,
        doc_id: str,
        depth: int,
    ) -> LLMFn:
        async def _call(prompt):
            return await self._call_llm_with_budget(
                state,
                llm_fn,
                prompt,
                pool='planning',
                doc_id=doc_id,
                priority='low' if depth >= 2 else 'normal',
            )

        return _call

    def _budgeted_discovery_llm_fn(
        self,
        state: AgentState,
        llm_fn: LLMFn,
        *,
        doc_id: str,
        low_priority: bool,
    ) -> LLMFn:
        async def _call(prompt):
            return await self._call_llm_with_budget(
                state,
                llm_fn,
                prompt,
                pool='planning',
                doc_id=doc_id,
                priority='low' if low_priority else 'normal',
            )

        return _call

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
        from shared.services.retrieval.agentic import tools
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

        planning_llm_fn: LLMFn | None = None
        bootstrap_llm_fn: LLMFn | None = None
        context_llm_fn: LLMFn | None = None
        if llm_fn is not None:
            base_llm_fn = llm_fn

            async def _planning_llm_call(prompt):
                return await self._call_llm_with_budget(
                    state, base_llm_fn, prompt, pool='planning'
                )

            async def _bootstrap_llm_call(prompt):
                return await self._call_llm_with_budget(
                    state, base_llm_fn, prompt, pool='bootstrap'
                )

            async def _context_llm_call(prompt):
                return await self._call_llm_with_budget(
                    state, base_llm_fn, prompt, pool='context'
                )

            planning_llm_fn = _planning_llm_call
            bootstrap_llm_fn = _bootstrap_llm_call
            context_llm_fn = _context_llm_call

        # Shared kwargs for bottom_discovery
        discovery_kwargs: dict[str, Any] = {
            'user_id': user_id,
            'namespace': namespace,
            'query': query,
            'top_k': top_k,
            'exclude_document_ids': exclude_document_ids,
            'exclude_sections': exclude_sections,
            'data_type': data_type,
            'signal_paths': signal_paths,
            'filter_mode': filter_mode,
            'channels': channels,
            'channel_weights': channel_weights,
        }

        # ══════════════════════════════════════════════════════════════════
        # Phase 1: Discovery + Document Selection
        # ══════════════════════════════════════════════════════════════════
        logger.info('  agentic: Phase 1 — discovery + document selection')

        # 1a. Bottom discovery (always runs)
        discovery_result = await tools.bottom_discovery(db, **discovery_kwargs)
        state.step_count += 1
        discovery_rows = discovery_result.payload.get('fused_rows', []) if discovery_result.status != 'error' else []
        state.discovery_top_doc_ids = discovery_result.payload.get('top_doc_ids', []) if discovery_result.status != 'error' else []

        if trace_enabled:
            trace.record_step(
                'bottom_discovery', discovery_result,
                decision_reason='phase_1_mandatory',
            )

        logger.info(
            f'  agentic step {state.step_count}: bottom_discovery '
            f'status={discovery_result.status} latency={discovery_result.latency_ms}ms'
        )

        # 1b. KG document selection (requires LLM)
        if bootstrap_llm_fn is not None:
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
                logger.info('  agentic: bootstrap budget exhausted during document selection')
                if trace_enabled:
                    trace.record_budget_stop('bootstrap_exhausted')
                kg_result = ToolResult(
                    status='no_confident_doc',
                    payload={'reason': 'bootstrap budget exhausted'},
                )
            state.step_count += 1

            if trace_enabled:
                trace.record_step(
                    'kg_document_select', kg_result,
                    decision_reason='phase_1_doc_selection',
                )

            if kg_result.status == 'selected_docs':
                for doc_data in kg_result.payload.get('candidate_docs', []):
                    state.selected_docs.append(CandidateDoc(
                        document_id=doc_data.get('document_id', ''),
                        source_file_name=doc_data.get('source_file_name', ''),
                        confidence=doc_data.get('confidence', 0.0),
                        reason=doc_data.get('reason', ''),
                        source=doc_data.get('source', ''),
                    ))
                state.doc_id_to_name.update(kg_result.payload.get('doc_id_to_name', {}))
                state.doc_job_map.update(kg_result.payload.get('doc_job_map', {}))

            # If KG returned nothing, use discovery hints
            if not state.selected_docs and state.discovery_top_doc_ids:
                hint_ids = [d for d in state.discovery_top_doc_ids if d not in state.ever_explored_doc_ids]
                if hint_ids:
                    doc_stmt = (
                        select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
                        .where(Document.document_id.in_(hint_ids))
                    )
                    doc_result = await db.execute(doc_stmt)
                    for did, fname, jrid in doc_result.all():
                        state.selected_docs.append(CandidateDoc(
                            document_id=did,
                            source_file_name=fname or did,
                            confidence=0.5,
                            reason='discovery_hint (KG returned 0)',
                            source='discovery_hint',
                        ))
                        state.doc_id_to_name[did] = fname or did
                        if jrid:
                            state.doc_job_map[did] = jrid

            logger.info(
                f'  agentic step {state.step_count}: kg_document_select '
                f'status={kg_result.status} docs={len(state.selected_docs)} '
                f'latency={kg_result.latency_ms}ms'
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

        # ══════════════════════════════════════════════════════════════════
        # Discovery → Navigation integration
        # Group discovery_rows by document for post-BFS discovery selection
        # ══════════════════════════════════════════════════════════════════
        discovery_by_doc: dict[str, list[dict[str, Any]]] = {}
        for row in discovery_rows:
            doc_id = row.get('document_id', '')
            if doc_id:
                discovery_by_doc.setdefault(doc_id, []).append(row)

        # Auto-register B-class docs (discovery-only, not selected by KG)
        selected_doc_ids = {d.document_id for d in state.selected_docs}
        for doc_id in discovery_by_doc:
            if doc_id not in selected_doc_ids and doc_id not in state.ever_explored_doc_ids:
                doc_stmt = (
                    select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
                    .where(Document.document_id == doc_id)
                )
                doc_result = await db.execute(doc_stmt)
                row_data = doc_result.first()
                if row_data:
                    did, fname, jrid = row_data
                    state.selected_docs.append(CandidateDoc(
                        document_id=did,
                        source_file_name=fname or did,
                        confidence=0.4,
                        reason='discovery_auto (not in KG selection)',
                        source='discovery_auto',
                    ))
                    state.doc_id_to_name[did] = fname or did
                    if jrid:
                        state.doc_job_map[did] = jrid

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

            # ── Phase 2: Per-Document Navigation ────────────────────────
            logger.info(
                f'  agentic: Phase 2 (round {round_idx}) — '
                f'navigating {len(state.selected_docs)} documents'
            )

            for doc in state.selected_docs:
                if state.elapsed_ms >= config.latency_budget_ms:
                    logger.info('  agentic: latency budget hit during Phase 2, stopping')
                    break

                job_result_id = state.doc_job_map.get(doc.document_id, '')
                if not job_result_id:
                    logger.info(f'  agentic: skipping doc {doc.document_id} — no job_result_id')
                    state.ever_explored_doc_ids.add(doc.document_id)
                    continue

                doc_name = doc.source_file_name or state.doc_id_to_name.get(doc.document_id, '')

                # B-class docs (discovery_auto) skip BFS, go to discovery_select
                is_b_class = doc.source == 'discovery_auto'

                if not is_b_class:
                    # Build exclude_paths for this doc from seen_section_keys
                    # Starts with revision-carried paths, then accumulates
                    # leaf paths hydrated during THIS BFS round to prevent
                    # re-selection in deeper drill-downs.
                    doc_exclude: set[str] = {
                        key.split('::', 1)[1]
                        for key in state.seen_section_keys
                        if key.startswith(f'{doc.document_id}::')
                    } if state.seen_section_keys else set()

                    # BFS queue: (scope_path(s), parent_node, depth)
                    # scope can be: None (root), str, or list[str] (multi-scope)
                    root = DocTreeNode(scope_path=None)
                    pending: list[tuple[str | list[str] | None, DocTreeNode, int]] = [(None, root, 0)]
                    doc_pending_assets: list[dict] = []  # deferred asset reconcile

                    while pending:
                        if state.elapsed_ms >= config.latency_budget_ms:
                            break

                        scope, parent_node, depth = pending.pop(0)
                        if depth >= config.max_nav_depth:
                            continue

                        if planning_llm_fn is None:
                            break
                        if state.ledger and state.ledger.status('planning') in ('CRITICAL', 'EXHAUSTED'):
                            logger.info('  agentic: planning budget critical, ending BFS for current doc')
                            break

                        doc_llm_fn = self._budgeted_doc_llm_fn(
                            state,
                            cast(LLMFn, llm_fn),
                            doc_id=doc.document_id,
                            depth=depth,
                        )

                        # ★ Unified navigate step (supports multi-scope batching)
                        try:
                            action, asset_tools, step_node, drill_paths = await tools.navigate_step(
                                db,
                                document_id=doc.document_id,
                                job_result_id=job_result_id,
                                query=query,
                                llm_fn=doc_llm_fn,
                                user_id=user_id,
                                namespace=namespace,
                                doc_name=doc_name,
                                scope_path=scope,
                                exclude_paths=doc_exclude,
                                revision_hint=revision_hint if depth == 0 else None,
                                budget_snapshot=state.ledger.snapshot() if state.ledger else None,
                            )
                        except BudgetExceeded:
                            logger.info('  agentic: planning budget exhausted during navigation')
                            if trace_enabled:
                                trace.record_budget_stop('planning_exhausted')
                            break
                        state.step_count += 1

                        # ★ Asset collection (deferred reconcile) — runs if LLM selected tools.
                        # If this navigation call selected sections, bind asset tools to
                        # those selections; otherwise keep the current scope (STOP/root).
                        selected_asset_scopes = list(step_node.confidence.keys())
                        asset_scope = selected_asset_scopes or scope
                        for asset_tool in asset_tools:
                            if asset_tool not in ('FIND_IMAGES', 'FIND_TABLES'):
                                continue
                            asset_type = 'image' if asset_tool == 'FIND_IMAGES' else 'table'
                            asset_chunks = await tools.asset_filter_step(
                                db,
                                document_id=doc.document_id,
                                job_result_id=job_result_id,
                                scope_path=asset_scope,
                                asset_type=asset_type,
                            )
                            if asset_chunks:
                                doc_pending_assets.extend(asset_chunks)

                            scope_display = (
                                asset_scope if isinstance(asset_scope, list)
                                else (asset_scope or 'root')
                            )
                            if trace_enabled:
                                trace.record_step(
                                    'asset_filter_step', ToolResult(
                                        status='filtered' if asset_chunks else 'empty',
                                        payload={
                                            'document_id': doc.document_id,
                                            'scope': scope_display,
                                            'navigation_scope': scope if isinstance(scope, str) else (scope or 'root'),
                                            'asset_type': asset_type,
                                            'chunks_found': len(asset_chunks) if asset_chunks else 0,
                                        },
                                    ),
                                    decision_reason=f'asset_r{round_idx}_d{depth}_{doc.source_file_name}',
                                )

                            logger.info(
                                f'  agentic step {state.step_count}: asset_filter_step '
                                f'doc="{doc.source_file_name}" scope={scope_display} '
                                f'type={asset_type} chunks={len(asset_chunks) if asset_chunks else 0}'
                            )

                        # Merge step result into parent node
                        parent_node.outline_items = step_node.outline_items
                        for leaf_path, chunks in step_node.leaf_content.items():
                            parent_node.add_leaf_chunks(leaf_path, chunks)
                        parent_node.confidence = step_node.confidence

                        # Accumulate hydrated leaf paths into doc_exclude
                        drill_path_set = {sel['path'] for sel in drill_paths}
                        for leaf_path in step_node.leaf_content:
                            if leaf_path not in drill_path_set:
                                doc_exclude.add(leaf_path)

                        # Queue non-leaf selections as a SINGLE batched item
                        # (all drill paths expand simultaneously in the next call)
                        if drill_paths:
                            for sel in drill_paths:
                                child = DocTreeNode(scope_path=sel['path'])
                                parent_node.children[sel['path']] = child
                            batch_scope = [sel['path'] for sel in drill_paths]
                            pending.append((batch_scope, parent_node, depth + 1))

                        # Re-parent leaf paths that belong to a child's subtree
                        parent_node.reparent_leaf_content()

                        if trace_enabled:
                            trace.record_step(
                                'navigate_step', ToolResult(
                                    status=f'{action.lower()}' + (' (content)' if step_node.has_content() else ''),
                                    payload={
                                        'document_id': doc.document_id,
                                         'scope': scope if isinstance(scope, str) else (scope or 'root'),
                                        'depth': depth,
                                        'action': action,
                                        'asset_tools': asset_tools,
                                        'outline_count': len(step_node.outline_items),
                                        'leaf_count': len(step_node.leaf_content),
                                        'pending_drills': len(drill_paths),
                                    },
                                ),
                                decision_reason=f'nav_r{round_idx}_d{depth}_{doc.source_file_name}',
                            )

                        scope_log = scope if isinstance(scope, str) else (', '.join(scope) if scope else 'root')
                        logger.info(
                            f'  agentic step {state.step_count}: navigate_step '
                            f'doc="{doc.source_file_name}" scope={scope_log} '
                            f'depth={depth} action={action} tools={asset_tools} '
                            f'outline={len(step_node.outline_items)} '
                            f'leaves={len(step_node.leaf_content)} '
                            f'drills={len(drill_paths)}'
                        )
                        if state.ledger is not None:
                            state.ledger.mark_explored(
                                chunks=sum(len(chunks) for chunks in step_node.leaf_content.values()),
                            )
                else:
                    # B-class: no BFS, create empty root
                    root = DocTreeNode(scope_path=None)

                # ── Post-BFS: Discovery selection step ─────────────────────
                doc_hints = discovery_by_doc.get(doc.document_id, [])
                if doc_hints and planning_llm_fn is not None and state.elapsed_ms < config.latency_budget_ms:
                    discovery_exclude_paths = {
                        key.split('::', 1)[1]
                        for key in root.collect_all_paths(doc.document_id)
                    }
                    doc_discovery_llm_fn = self._budgeted_discovery_llm_fn(
                        state,
                        cast(LLMFn, llm_fn),
                        doc_id=doc.document_id,
                        low_priority=root.has_content(),
                    )
                    try:
                        discovery_node = await tools.discovery_select_step(
                            db,
                            document_id=doc.document_id,
                            query=query,
                            llm_fn=doc_discovery_llm_fn,
                            user_id=user_id,
                            namespace=namespace,
                            doc_name=doc_name,
                            discovery_hints=doc_hints,
                            exclude_paths=discovery_exclude_paths,
                            revision_hint=revision_hint,
                            budget_snapshot=state.ledger.snapshot() if state.ledger else None,
                        )
                    except BudgetExceeded:
                        logger.info('  agentic: planning budget exhausted during discovery selection')
                        if trace_enabled:
                            trace.record_budget_stop('planning_exhausted')
                        discovery_node = DocTreeNode(scope_path=None)
                    state.step_count += 1

                    if trace_enabled:
                        trace.record_step(
                            'discovery_select_step', ToolResult(
                                status='selected' if discovery_node.has_content() else 'empty',
                                payload={
                                    'document_id': doc.document_id,
                                    'hints_count': len(doc_hints),
                                    'hydrated_count': len(discovery_node.leaf_content),
                                },
                            ),
                            decision_reason=f'discovery_r{round_idx}_{doc.source_file_name}',
                        )

                    # Merge discovery results into BFS tree
                    root.merge(discovery_node)
                    if state.ledger is not None:
                        state.ledger.mark_explored(
                            chunks=sum(len(chunks) for chunks in discovery_node.leaf_content.values()),
                        )

                # ── Deferred asset reconcile ──────────────────────────────
                # Assets were collected across all BFS depths but NOT placed
                # into the tree yet.  Now that the final navigated paths are
                # known (BFS + discovery), filter and place only those assets
                # whose owner path matches a navigated leaf.
                if not is_b_class and doc_pending_assets:
                    # Inject doc file name as a visible root-level path, but
                    # ONLY when BFS stopped at root (no children = STOP action).
                    if doc_name and not root.children and not any(
                        item.get('path') == doc_name for item in root.outline_items
                    ):
                        root.outline_items.insert(0, {'path': doc_name, 'level': 0})
                    _reconcile_deferred_assets(root, doc_pending_assets)
                    if trace_enabled:
                        trace.record_step(
                            'deferred_asset_reconcile', ToolResult(
                                status='reconciled',
                                payload={
                                    'document_id': doc.document_id,
                                    'pending_count': len(doc_pending_assets),
                                    'placed_count': sum(
                                        1 for a in doc_pending_assets
                                        if str(a.get('chunk_id') or '') in {
                                            str(r.get('chunk_id') or '')
                                            for r in root.flatten_chunk_rows()
                                        }
                                    ),
                                },
                            ),
                            decision_reason=f'deferred_reconcile_r{round_idx}_{doc.source_file_name}',
                        )

                # Merge or store doc tree
                if doc.document_id in state.doc_trees:
                    state.doc_trees[doc.document_id].merge(root)
                else:
                    state.doc_trees[doc.document_id] = root
                state.ever_explored_doc_ids.add(doc.document_id)
                if state.ledger is not None:
                    state.ledger.mark_explored(docs=1)

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
                return await self._call_llm_with_budget(
                    state, cast(LLMFn, _vlm_fn), prompt, pool='context'
                )

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
                logger.info('  agentic: bootstrap budget exhausted during revision doc selection')
                if trace_enabled:
                    trace.record_budget_stop('bootstrap_exhausted')
                stop_reason = 'bootstrap_budget'
                break
            state.step_count += 1

            if kg_result.status == 'selected_docs':
                for doc_data in kg_result.payload.get('candidate_docs', []):
                    state.selected_docs.append(CandidateDoc(
                        document_id=doc_data.get('document_id', ''),
                        source_file_name=doc_data.get('source_file_name', ''),
                        confidence=doc_data.get('confidence', 0.0),
                        reason=doc_data.get('reason', ''),
                        source=doc_data.get('source', ''),
                    ))
                state.doc_id_to_name.update(kg_result.payload.get('doc_id_to_name', {}))
                state.doc_job_map.update(kg_result.payload.get('doc_job_map', {}))

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
