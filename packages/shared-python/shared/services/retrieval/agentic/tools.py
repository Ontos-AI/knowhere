"""Agentic retrieval tools — thin wrappers around existing retrieval components.

Each tool:
  1. Calls existing functions from channels.py, agent_navigate.py, app_service.py
  2. Returns a unified ToolResult
  3. Never raises — errors are captured in ToolResult.error
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document
from shared.services.retrieval.agentic.types import DocTreeNode, ToolResult
from shared.services.retrieval.agent_navigate import (
    _build_knowledge_map_overview,
    _expand_by_edges,
    _format_items_for_llm,
    _grep_discover_document_ids,
    _load_child_sections,
    _parse_json_array,
    _parse_scope_nav_response,
    _SCOPE_NAV_PROMPT,
    _DISCOVERY_SELECT_PROMPT,
    _FILE_SELECT_PROMPT,
)
from shared.services.retrieval.app_service import (
    _CHANNEL_WEIGHT_CONTENT,
    _CHANNEL_WEIGHT_PATH,
    _CHANNEL_WEIGHT_TERM,
    _INTERNAL_RECALL_K_MULTIPLIER,
    _merge_same_section_rows,
    _normalize_row_scores,
    _resolve_allowed_chunk_types,
    hydrate_connected_target_rows,
    merge_channels_rrf,
)
from shared.services.retrieval.channels import content_channel, path_channel, term_channel
from shared.services.retrieval.llm_adapter import LLMFn


# ---------------------------------------------------------------------------
# Tool: bottom_discovery
# ---------------------------------------------------------------------------

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
    filter_mode: str = 'delete',
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    internal_recall_k: int | None = None,
    **_kwargs: Any,
) -> ToolResult:
    """Run 3-channel BM25 discovery + RRF fusion."""
    t0 = time.monotonic()
    try:
        allowed_chunk_types = _resolve_allowed_chunk_types(data_type)
        effective_recall_k = internal_recall_k if internal_recall_k is not None else top_k * _INTERNAL_RECALL_K_MULTIPLIER
        active_channels = set(channels) if channels else {'path', 'content', 'term'}

        path_rows: list[dict[str, Any]] = []
        content_rows: list[dict[str, Any]] = []
        term_rows: list[dict[str, Any]] = []

        if 'path' in active_channels:
            path_rows = await path_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )

        if 'content' in active_channels:
            content_rows = await content_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )

        if 'term' in active_channels:
            term_rows = await term_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )

        # RRF fusion
        default_weights = {
            'path': _CHANNEL_WEIGHT_PATH,
            'content': _CHANNEL_WEIGHT_CONTENT,
            'term': _CHANNEL_WEIGHT_TERM,
        }
        effective_weights = {**default_weights, **(channel_weights or {})}

        channel_lists: list[list[dict[str, Any]]] = []
        weight_list: list[float] = []
        if path_rows:
            channel_lists.append(path_rows)
            weight_list.append(effective_weights.get('path', _CHANNEL_WEIGHT_PATH))
        if content_rows:
            channel_lists.append(content_rows)
            weight_list.append(effective_weights.get('content', _CHANNEL_WEIGHT_CONTENT))
        if term_rows:
            channel_lists.append(term_rows)
            weight_list.append(effective_weights.get('term', _CHANNEL_WEIGHT_TERM))

        fused_rows = merge_channels_rrf(channel_lists, weight_list, top_k) if channel_lists else []
        fused_rows = _merge_same_section_rows(fused_rows)

        if fused_rows:
            _normalize_row_scores(fused_rows, source_field='score', target_field='discovery_score', default=0.5)

        # Extract top document IDs as hints for KG selection
        doc_id_counts: dict[str, int] = {}
        for row in fused_rows:
            did = row.get('document_id', '')
            if did:
                doc_id_counts[did] = doc_id_counts.get(did, 0) + 1
        top_doc_ids = sorted(doc_id_counts, key=lambda d: doc_id_counts[d], reverse=True)[:5]

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f'  agentic.bottom_discovery: {len(fused_rows)} fused rows, '
            f'top_doc_ids={top_doc_ids}, {latency}ms'
        )
        return ToolResult(
            status='discovery_done',
            payload={
                'fused_rows': fused_rows,
                'top_doc_ids': top_doc_ids,
                'channel_counts': {
                    'path': len(path_rows),
                    'content': len(content_rows),
                    'term': len(term_rows),
                },
            },
            latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f'  agentic.bottom_discovery failed: {e}')
        return ToolResult(status='error', error=str(e), latency_ms=latency)


# ---------------------------------------------------------------------------
# Tool: kg_document_select
# ---------------------------------------------------------------------------

async def kg_document_select(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    llm_fn: LLMFn | None,
    exclude_document_ids: list[str],
    **_kwargs: Any,
) -> ToolResult:
    """Select candidate documents from document-level KG."""
    t0 = time.monotonic()
    try:
        overview_text, doc_id_to_name = await _build_knowledge_map_overview(
            db, user_id=user_id, namespace=namespace,
        )
        if overview_text == '(empty)':
            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                status='no_confident_doc',
                payload={'reason': 'no active documents in namespace'},
                latency_ms=latency,
            )

        if llm_fn is None:
            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                status='no_confident_doc',
                payload={'reason': 'LLM not available'},
                latency_ms=latency,
            )

        file_prompt = _FILE_SELECT_PROMPT.format(
            overview=overview_text, query=query,
        )
        file_response = await llm_fn(file_prompt)
        selected_ids = _parse_json_array(file_response)

        exclude_set = set(exclude_document_ids)
        valid_ids = [did for did in selected_ids if did in doc_id_to_name and did not in exclude_set]

        if not valid_ids:
            latency = int((time.monotonic() - t0) * 1000)
            logger.info(f'  agentic.kg_document_select: LLM returned no valid docs, {latency}ms')
            return ToolResult(
                status='no_confident_doc',
                payload={'reason': 'LLM returned no valid document IDs', 'raw_ids': selected_ids},
                latency_ms=latency,
            )

        # Load job_result_ids for selected documents
        doc_job_map: dict[str, str] = {}
        doc_stmt = (
            select(Document.document_id, Document.current_job_result_id)
            .where(Document.document_id.in_(valid_ids))
        )
        doc_result = await db.execute(doc_stmt)
        for did, jrid in doc_result.all():
            if jrid:
                doc_job_map[did] = jrid

        candidate_docs = []
        for did in valid_ids:
            candidate_docs.append({
                'document_id': did,
                'source_file_name': doc_id_to_name.get(did, ''),
                'confidence': 1.0,
                'reason': 'LLM selected from KG overview',
                'source': 'kg_llm_select',
            })

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(f'  agentic.kg_document_select: {len(candidate_docs)} docs selected, {latency}ms')
        return ToolResult(
            status='selected_docs',
            payload={
                'candidate_docs': candidate_docs,
                'doc_id_to_name': doc_id_to_name,
                'doc_job_map': doc_job_map,
            },
            latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f'  agentic.kg_document_select failed: {e}')
        return ToolResult(status='error', error=str(e), latency_ms=latency)


# ---------------------------------------------------------------------------
# Tool: grep_document_discover
# ---------------------------------------------------------------------------

async def grep_document_discover(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    exclude_document_ids: list[str],
    **_kwargs: Any,
) -> ToolResult:
    """Discover documents via term search (GREP)."""
    t0 = time.monotonic()
    try:
        grep_doc_ids = await _grep_discover_document_ids(
            db, user_id=user_id, namespace=namespace, query=query,
            exclude_document_ids=exclude_document_ids,
        )

        if not grep_doc_ids:
            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                status='no_docs_found',
                payload={'reason': 'GREP found no matching documents'},
                latency_ms=latency,
            )

        doc_stmt = (
            select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
            .where(Document.document_id.in_(grep_doc_ids))
        )
        doc_result = await db.execute(doc_stmt)
        doc_id_to_name: dict[str, str] = {}
        doc_job_map: dict[str, str] = {}
        for did, fname, jrid in doc_result.all():
            doc_id_to_name[did] = fname or did
            if jrid:
                doc_job_map[did] = jrid

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(f'  agentic.grep_document_discover: {len(grep_doc_ids)} docs found, {latency}ms')
        return ToolResult(
            status='discovered_docs',
            payload={
                'document_ids': grep_doc_ids,
                'doc_id_to_name': doc_id_to_name,
                'doc_job_map': doc_job_map,
            },
            latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f'  agentic.grep_document_discover failed: {e}')
        return ToolResult(status='error', error=str(e), latency_ms=latency)


# ---------------------------------------------------------------------------
# Tool: graph_expand_docs
# ---------------------------------------------------------------------------

async def graph_expand_docs(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    document_ids: list[str],
    **_kwargs: Any,
) -> ToolResult:
    """Expand document set via KG edge traversal."""
    t0 = time.monotonic()
    try:
        expanded_ids = await _expand_by_edges(
            db, document_ids=document_ids, user_id=user_id, namespace=namespace,
        )
        new_ids = [did for did in expanded_ids if did not in document_ids]

        if not new_ids:
            latency = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                status='no_expansion',
                payload={'reason': 'no new neighbors found via edges'},
                latency_ms=latency,
            )

        doc_stmt = (
            select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
            .where(Document.document_id.in_(new_ids))
        )
        doc_result = await db.execute(doc_stmt)
        doc_id_to_name: dict[str, str] = {}
        doc_job_map: dict[str, str] = {}
        for did, fname, jrid in doc_result.all():
            doc_id_to_name[did] = fname or did
            if jrid:
                doc_job_map[did] = jrid

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(f'  agentic.graph_expand_docs: {len(new_ids)} new docs from edges, {latency}ms')
        return ToolResult(
            status='expanded_docs',
            payload={
                'document_ids': new_ids,
                'doc_id_to_name': doc_id_to_name,
                'doc_job_map': doc_job_map,
            },
            latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f'  agentic.graph_expand_docs failed: {e}')
        return ToolResult(status='error', error=str(e), latency_ms=latency)


# ---------------------------------------------------------------------------
# Tool: scope_navigate_step (single-step navigation)
# ---------------------------------------------------------------------------

_LLM_MODE_TO_HYDRATE: dict[str, str] = {
    'all': 'chunks',
    'image': 'image_only',
    'table': 'table_only',
}

async def scope_navigate_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    query: str,
    llm_fn: LLMFn,
    user_id: str,
    namespace: str,
    doc_name: str = '',
    scope_path: str | None = None,
    exclude_paths: set[str] | None = None,
    revision_hint: str | None = None,
) -> tuple[DocTreeNode, list[dict]]:
    """Single navigation step — one LLM call, no recursion.

    Returns:
      - node: DocTreeNode with outline_items (current scope local items only)
              and leaf_content (hydrated leaf selections)
      - pending: list of {path, confidence, mode} for non-leaf selections
                 (orchestrator queues these for further drill-down)
    """
    from shared.services.retrieval.app_service import _hydrate_paths_to_rows

    empty = DocTreeNode.empty(scope_path)

    try:
        # 1. Load continuous context tree
        items = await _load_child_sections(
            db, document_id, job_result_id, scope_path,
            exclude_paths=exclude_paths,
        )
        if not items:
            return empty, []

        # 2. Build selectable index (only current-scope items with summary)
        selectable = {item['path']: item for item in items if item.get('show_summary', True)}

        # 3. Format full tree and call LLM
        text, overflowed = _format_items_for_llm(items)
        scope_header = (
            f'Current scope: navigating into "{scope_path}"'
            if scope_path else
            'Current scope: root (document top level)'
        )
        prompt = _SCOPE_NAV_PROMPT.format(
            doc_name=doc_name or document_id,
            doc_id=document_id,
            scope_header=scope_header,
            items_overview=text,
            query=query,
        )
        if revision_hint:
            prompt += (
                f'\n\nIMPORTANT: Previous round feedback: '
                f'"{revision_hint}". Select specific sections this time.'
            )
        response = await llm_fn(prompt)
        selections = _parse_scope_nav_response(response)

        logger.info(
            f'  scope_navigate_step scope={scope_path or "root"}: '
            f'selections={len(selections)}, selectable={len(selectable)}, '
            f'overflowed={overflowed}'
        )

        # 4. Build node with LOCAL items only (no ancestors/siblings)
        node = DocTreeNode(scope_path=scope_path)
        local_items = [item for item in items if item.get('show_summary', True)]
        node.outline_items = local_items

        # 5. Dispatch selections (guard: never re-select scope_path itself)
        valid_selections = [
            s for s in selections
            if s['path'] in selectable and s['path'] != scope_path
        ]

        pending: list[dict] = []
        for sel in valid_selections:
            path = sel['path']
            conf = sel.get('confidence', 0.7)
            item = selectable[path]
            node.confidence[path] = conf

            if item.get('is_leaf'):
                # Leaf → hydrate chunks
                hydrate_mode = _LLM_MODE_TO_HYDRATE.get(
                    str(sel.get('mode', 'all')).strip().lower(), 'chunks'
                )
                chunks = await _hydrate_paths_to_rows(
                    db,
                    path_selections=[
                        {'path': path, 'confidence': conf, 'hydrate_mode': hydrate_mode}
                    ],
                    user_id=user_id,
                    namespace=namespace,
                    document_id=document_id,
                )
                # Also hydrate connected targets (image/table chunks referenced via connect_to)
                if chunks:
                    connected = await hydrate_connected_target_rows(
                        db=db,
                        rows=chunks,
                        exclude_document_ids=[],
                        exclude_sections=[],
                    )
                    if connected:
                        chunks = chunks + connected
                    node.leaf_content[path] = chunks
            else:
                # Non-leaf → return as pending for orchestrator to queue
                pending.append(sel)

        return node, pending

    except Exception as e:
        logger.error(f'  scope_navigate_step failed for doc={document_id}: {e}')
        return empty, []


# ---------------------------------------------------------------------------
# Tool: discovery_select_step (post-navigation discovery selection)
# ---------------------------------------------------------------------------

_MAX_DISCOVERY_PER_DOC = 3


async def discovery_select_step(
    db: AsyncSession,
    *,
    document_id: str,
    query: str,
    llm_fn: LLMFn,
    user_id: str,
    namespace: str,
    doc_name: str = '',
    discovery_hints: list[dict[str, Any]],
) -> DocTreeNode:
    """Post-navigation discovery selection step.

    After BFS navigation exhausts for a document, present discovery-found
    section paths (from bottom_discovery BM25) to the LLM for selection.
    Selected paths are hydrated as leaf content.

    For B-class documents (discovery-only, not KG-selected), this is the
    only navigation step — no prior BFS.
    """
    from shared.services.retrieval.app_service import _hydrate_paths_to_rows

    node = DocTreeNode(scope_path=None)
    if not discovery_hints:
        return node

    # Limit hints per document
    hints = discovery_hints[:_MAX_DISCOVERY_PER_DOC]

    t0 = time.monotonic()
    try:
        # 1. Format hints for LLM
        hint_lines: list[str] = []
        hint_by_path: dict[str, dict] = {}
        for h in hints:
            sp = h.get('section_path', '')
            if not sp:
                continue
            title = sp.rsplit(' / ', 1)[-1] if ' / ' in sp else sp
            summary = h.get('summary', '') or ''
            hint_lines.append(f'▸ path="{sp}"  {title}  [Leaf]')
            if summary:
                clipped = summary[:300]
                hint_lines.append(f'    {clipped}')
            hint_by_path[sp] = h

        if not hint_lines:
            return node

        items_text = '\n'.join(hint_lines)
        prompt = _DISCOVERY_SELECT_PROMPT.format(
            doc_name=doc_name or document_id,
            items=items_text,
            query=query,
        )
        response = await llm_fn(prompt)
        selections = _parse_scope_nav_response(response)

        logger.info(
            f'  discovery_select_step doc="{doc_name}": '
            f'hints={len(hints)} selections={len(selections)}'
        )

        # 2. Hydrate selected paths
        valid_selections = [s for s in selections if s['path'] in hint_by_path]
        for sel in valid_selections:
            path = sel['path']
            conf = sel.get('confidence', 0.7)
            hydrate_mode = _LLM_MODE_TO_HYDRATE.get(
                str(sel.get('mode', 'all')).strip().lower(), 'chunks'
            )
            node.confidence[path] = conf

            chunks = await _hydrate_paths_to_rows(
                db,
                path_selections=[
                    {'path': path, 'confidence': conf, 'hydrate_mode': hydrate_mode}
                ],
                user_id=user_id,
                namespace=namespace,
                document_id=document_id,
            )
            if chunks:
                connected = await hydrate_connected_target_rows(
                    db=db,
                    rows=chunks,
                    exclude_document_ids=[],
                    exclude_sections=[],
                )
                if connected:
                    chunks = chunks + connected
                node.leaf_content[path] = chunks

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f'  discovery_select_step done: hydrated={len(node.leaf_content)} '
            f'latency={latency}ms'
        )
        return node

    except Exception as e:
        logger.error(f'  discovery_select_step failed for doc={document_id}: {e}')
        return node
