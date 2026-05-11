"""Retrieval Agent orchestrator — navigate-then-answer loop.

Flow:
  Phase 1: Document selection (bottom_discovery + kg_document_select)
  Phase 2: Per-document navigation (iterative BFS scope_navigate_step)
  Phase 3: Render evidence → attempt_answer
    → DONE (has answer) → return answer + evidence
    → NOT_FOUND + reason → revision_hint → re-select docs + re-navigate
                           (exclude seen paths) → re-attempt
    → max_revisions → return best available

The orchestrator drives navigation via an iterative BFS queue per document,
calling scope_navigate_step at each level. Navigation auto-terminates when
the LLM returns empty selections. After navigation completes, attempt_answer
is called automatically — no separate verdict step needed.
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document

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


def _build_config_from_env() -> AgentRunConfig:
    """Read agent config from environment, with sensible defaults."""
    return AgentRunConfig(
        max_revisions=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_REVISIONS', '2')),
        max_nav_depth=int(os.environ.get('RETRIEVAL_AGENTIC_MAX_NAV_DEPTH', '3')),
        latency_budget_ms=int(os.environ.get('RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS', '12000')),
    )


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
    ) -> AgenticResult:
        """Run the agentic retrieval pipeline.

        Returns an ``AgenticResult`` containing the rendered evidence
        text, LLM answer, and referenced chunk IDs.  Never raises —
        errors are captured in trace and the best available result
        is returned.
        """
        from shared.services.retrieval.agentic import tools
        from shared.services.retrieval.agentic.policy import attempt_answer

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
            f'top_k={top_k} budget={config.latency_budget_ms}ms'
        )

        if llm_fn is None:
            logger.warning('agentic: no llm_fn provided — running discovery-only mode')

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
        if llm_fn is not None:
            kg_result = await tools.kg_document_select(
                db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                llm_fn=llm_fn,
                exclude_document_ids=list(state.ever_explored_doc_ids | set(exclude_document_ids)),
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
                    'section_path': r.get('section_path', ''),
                    'file_path': r.get('file_path', ''),
                }
                for r in discovery_rows[:top_k]
                if r.get('chunk_id')
            ]
            if trace_enabled:
                await trace.complete(discovery_rows, 'agentic_discovery_only')
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

        # ══════════════════════════════════════════════════════════════════
        # Phase 2 + 3 Loop: Navigate → Render → Attempt Answer → (Revise)
        # ══════════════════════════════════════════════════════════════════
        answer_text = ''
        revision_hint: str | None = None
        stop_reason = 'max_revisions'

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
                    doc_exclude = {
                        key.split('::', 1)[1]
                        for key in state.seen_section_keys
                        if key.startswith(f'{doc.document_id}::')
                    } if state.seen_section_keys else None

                    # BFS queue: (scope_path, parent_node, depth)
                    root = DocTreeNode(scope_path=None)
                    pending: list[tuple[str | None, DocTreeNode, int]] = [(None, root, 0)]

                    while pending:
                        if state.elapsed_ms >= config.latency_budget_ms:
                            break

                        scope, parent_node, depth = pending.pop(0)
                        if depth >= config.max_nav_depth:
                            continue

                        if llm_fn is None:
                            break

                        step_node, drill_paths = await tools.scope_navigate_step(
                            db,
                            document_id=doc.document_id,
                            job_result_id=job_result_id,
                            query=query,
                            llm_fn=llm_fn,
                            user_id=user_id,
                            namespace=namespace,
                            doc_name=doc_name,
                            scope_path=scope,
                            exclude_paths=doc_exclude,
                            revision_hint=revision_hint if depth == 0 else None,
                        )
                        state.step_count += 1

                        # Merge step result into parent node
                        parent_node.outline_items = step_node.outline_items
                        parent_node.leaf_content = step_node.leaf_content
                        parent_node.confidence = step_node.confidence

                        # Queue non-leaf selections for further drill-down
                        for sel in drill_paths:
                            child = DocTreeNode(scope_path=sel['path'])
                            parent_node.children[sel['path']] = child
                            pending.append((sel['path'], child, depth + 1))

                        if trace_enabled:
                            trace.record_step(
                                'scope_navigate_step', ToolResult(
                                    status='navigated' if step_node.has_content() else 'empty',
                                    payload={
                                        'document_id': doc.document_id,
                                        'scope': scope or 'root',
                                        'depth': depth,
                                        'outline_count': len(step_node.outline_items),
                                        'leaf_count': len(step_node.leaf_content),
                                        'pending_drills': len(drill_paths),
                                    },
                                ),
                                decision_reason=f'nav_r{round_idx}_d{depth}_{doc.source_file_name}',
                            )

                        logger.info(
                            f'  agentic step {state.step_count}: scope_navigate_step '
                            f'doc="{doc.source_file_name}" scope={scope or "root"} '
                            f'depth={depth} outline={len(step_node.outline_items)} '
                            f'leaves={len(step_node.leaf_content)} '
                            f'drills={len(drill_paths)}'
                        )
                else:
                    # B-class: no BFS, create empty root
                    root = DocTreeNode(scope_path=None)

                # ── Post-BFS: Discovery selection step ─────────────────────
                doc_hints = discovery_by_doc.get(doc.document_id, [])
                if doc_hints and llm_fn is not None and state.elapsed_ms < config.latency_budget_ms:
                    discovery_node = await tools.discovery_select_step(
                        db,
                        document_id=doc.document_id,
                        query=query,
                        llm_fn=llm_fn,
                        user_id=user_id,
                        namespace=namespace,
                        doc_name=doc_name,
                        discovery_hints=doc_hints,
                    )
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

                # Merge or store doc tree
                if doc.document_id in state.doc_trees:
                    state.doc_trees[doc.document_id].merge(root)
                else:
                    state.doc_trees[doc.document_id] = root
                state.ever_explored_doc_ids.add(doc.document_id)

            # ── Phase 3: Render evidence + attempt_answer ────────────────
            evidence_text = await _render_evidence(
                db,
                state.doc_trees, state.doc_id_to_name,
            )

            if llm_fn is None:
                stop_reason = 'no_llm'
                break

            # Auto-trigger attempt_answer
            status, answer_text, reason = await attempt_answer(
                llm_fn,
                query=query,
                evidence_text=evidence_text,
                state=state,
                config=config,
            )
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
                break

            # ── NOT_FOUND: prepare revision ──────────────────────────────
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

            # Re-run KG select (allow re-exploring docs for different sections via path masking)
            kg_result = await tools.kg_document_select(
                db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                llm_fn=llm_fn,
                exclude_document_ids=list(set(exclude_document_ids)),
            )
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
        for doc_tree in state.doc_trees.values():
            for ref in doc_tree.collect_referenced_ids():
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
            await trace.complete(all_refs, router_used)

        return result
