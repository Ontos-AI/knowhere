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
from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.types import DocTreeNode, ToolResult
from shared.services.retrieval.agent_navigate import (
    _build_knowledge_map_overview,
    _format_items_for_llm,
    _load_child_sections,
    _parse_json_array,
    _parse_action_response,
    _ACTION_PROMPT,
    _DISCOVERY_SELECT_PROMPT,
    _FILE_SELECT_PROMPT,
    _format_budget_block,
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
from shared.services.retrieval.lexical_text import normalize_section_path
from shared.services.retrieval.llm_adapter import LLMFn


# ---------------------------------------------------------------------------
# Helper: resolve connected asset → owner text chunk section_path
# ---------------------------------------------------------------------------

def _build_connected_owner_map(text_chunks: list[dict[str, Any]]) -> dict[str, str]:
    """Build target_chunk_id → owner text chunk section_path mapping.

    When text chunks reference images/tables via connect_to metadata,
    the referenced assets live in Root section. This map lets us attribute
    those assets back to the text chunk's section for correct tree placement.
    """
    owner_map: dict[str, str] = {}
    for chunk in text_chunks:
        if (chunk.get('chunk_type') or 'text') != 'text':
            continue
        section_path = chunk.get('section_path') or ''
        if not section_path:
            continue
        metadata = chunk.get('chunk_metadata') or {}
        if not isinstance(metadata, dict):
            continue
        for conn in metadata.get('connect_to') or []:
            if not isinstance(conn, dict):
                continue
            target_id = str(conn.get('target') or '').strip()
            if target_id and target_id not in owner_map:
                owner_map[target_id] = section_path
    return owner_map


async def _resolve_root_asset_owners(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    chunks: list[dict[str, Any]],
) -> dict[str, str]:
    """Resolve owner section_path for Root-stranded image/table chunks.

    When Root is hydrated directly (e.g. via discovery selection), the
    batch contains standalone image/table chunks whose section_path is
    'Root'.  ``_build_connected_owner_map`` cannot help because the
    referencing text chunks live in other sections outside the batch.

    This function queries the *entire document* for text chunks with
    connect_to metadata, using the same logic as
    ``_build_connected_owner_map``, to resolve the true owner.

    Returns target_chunk_id → owner_section_path for Root assets only.
    Returns empty dict when there are no Root assets (zero DB overhead).
    """
    from shared.models.database.document import DocumentChunk, DocumentSection

    root_asset_ids = [
        str(c.get('chunk_id') or '')
        for c in chunks
        if not c.get('owner_section_path')  # skip if already resolved by batch-level owner map
        and (c.get('section_path') or '') == 'Root'
        and (c.get('chunk_type') or '').lower() in ('image', 'table')
        and c.get('chunk_id')
    ]
    if not root_asset_ids:
        return {}

    root_asset_set = set(root_asset_ids)

    # Query all text chunks in this document for connect_to metadata
    text_stmt = (
        select(
            DocumentChunk.chunk_metadata,
            DocumentSection.section_path,
        )
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.chunk_type == 'text')
    )
    result = await db.execute(text_stmt)

    owner_map: dict[str, str] = {}
    for metadata, section_path in result.all():
        if not isinstance(metadata, dict) or not section_path:
            continue
        for conn in metadata.get('connect_to') or []:
            if not isinstance(conn, dict):
                continue
            target_id = str(conn.get('target') or '').strip()
            if target_id in root_asset_set and target_id not in owner_map:
                owner_map[target_id] = section_path

    if owner_map:
        logger.info(
            f'  _resolve_root_asset_owners: resolved {len(owner_map)}/{len(root_asset_ids)} '
            f'Root assets to their owner sections'
        )
    return owner_map


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
    revision_hint: str | None = None,
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

        revision_context = ''
        if revision_hint:
            revision_context = (
                f'\nIMPORTANT: This is a REVISION round. '
                f'The previous search attempt failed because:\n'
                f'"{revision_hint}"\n'
                f'Adjust your document selection accordingly. '
                f'If no document can address this, return an EMPTY array [].\n'
            )

        file_prompt = _FILE_SELECT_PROMPT.format(
            overview=overview_text, query=query,
            revision_context=revision_context,
            budget_block=_format_budget_block(_kwargs.get('budget_snapshot')),
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
    except BudgetExceeded:
        raise
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f'  agentic.kg_document_select failed: {e}')
        return ToolResult(status='error', error=str(e), latency_ms=latency)



# ---------------------------------------------------------------------------
# Tool: asset_filter_step (programmatic asset extraction)
# ---------------------------------------------------------------------------

async def asset_filter_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None,
    asset_type: str,  # 'image' | 'table'
) -> list[dict[str, Any]]:
    """Extract assets from all descendants under scope_path.

    Terminal action — no LLM involved.
    Algorithm: load all text chunks under scope → parse connect_to metadata →
    batch-load target image/table chunks → return directly.

    Also collects standalone asset chunks (image/table) that exist directly
    under the scope but are not referenced via connect_to.

    scope_path can be:
      - None: root scope (entire document)
      - str: single scope path
      - list[str]: multiple scope paths (queried simultaneously)
    """
    from shared.models.database.document import DocumentChunk, DocumentSection

    t0 = time.monotonic()
    try:
        # 1. Find all section_ids under scope_path(s)
        # Normalize scope to list for uniform handling
        scope_list = (
            scope_path if isinstance(scope_path, list)
            else [scope_path] if scope_path
            else []
        )

        section_stmt = (
            select(DocumentSection.section_id, DocumentSection.section_path)
            .where(DocumentSection.document_id == document_id)
            .where(DocumentSection.job_result_id == job_result_id)
        )
        if scope_list:
            from sqlalchemy import or_
            scope_filters = []
            for sp in scope_list:
                scope_filters.append(DocumentSection.section_path == sp)
                scope_filters.append(DocumentSection.section_path.like(f'{sp} / %'))
            section_stmt = section_stmt.where(or_(*scope_filters))
        section_result = await db.execute(section_stmt)
        section_rows = section_result.all()
        section_ids = {row[0] for row in section_rows}

        if not section_ids:
            logger.info(f'  asset_filter_step: no sections found under scope={scope_path}')
            return []

        # 2. Load target asset chunks directly (standalone assets in the scope)
        asset_stmt = (
            select(
                DocumentChunk.chunk_id,
                DocumentChunk.chunk_type,
                DocumentChunk.content,
                DocumentChunk.file_path,
                DocumentChunk.section_id,
                DocumentChunk.source_chunk_path,
                DocumentChunk.chunk_metadata,
                DocumentChunk.sort_order,
                DocumentChunk.job_result_id,
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.section_id.in_(list(section_ids)))
            .where(DocumentChunk.chunk_type == asset_type)
            .order_by(DocumentChunk.sort_order)
        )
        asset_result = await db.execute(asset_stmt)
        asset_rows = asset_result.all()

        section_path_by_id = {section_id: section_path for section_id, section_path in section_rows}

        # 3. Resolve media → owner text section via connect_to tracing
        text_stmt = (
            select(
                DocumentChunk.section_id,
                DocumentChunk.chunk_type,
                DocumentChunk.chunk_metadata,
                DocumentChunk.source_chunk_path,
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.section_id.in_(list(section_ids)))
            .where(DocumentChunk.chunk_type == 'text')
        )
        text_result = await db.execute(text_stmt)
        text_row_dicts = [
            {
                'chunk_type': chunk_type,
                'chunk_metadata': metadata or {},
                'section_id': sid,
                'section_path': section_path_by_id.get(sid, ''),
                'source_chunk_path': scp,
            }
            for sid, chunk_type, metadata, scp in text_result.all()
        ]
        owner_by_target_id = _build_connected_owner_map(text_row_dicts)

        # Replace synthetic "Root" owner with the document's source_file_name.
        # Root is a hybrid node whose real path is the file name (e.g.
        # "32_安全大模型技术与市场研究报告_1.docx"); the DB stores the
        # synthetic label "Root" which cannot match any outline node.
        if any(v == 'Root' for v in owner_by_target_id.values()):
            doc_stmt = select(Document.source_file_name).where(
                Document.document_id == document_id
            )
            doc_file_name = (await db.execute(doc_stmt)).scalar() or ''
            if doc_file_name:
                for tid in list(owner_by_target_id):
                    if owner_by_target_id[tid] == 'Root':
                        owner_by_target_id[tid] = doc_file_name

        # Collect connected target IDs for batch-loading
        connected_target_ids: set[str] = set(owner_by_target_id.keys())

        # Load connected targets that match asset_type
        if connected_target_ids:
            connected_stmt = (
                select(
                    DocumentChunk.chunk_id,
                    DocumentChunk.chunk_type,
                    DocumentChunk.content,
                    DocumentChunk.file_path,
                    DocumentChunk.section_id,
                    DocumentChunk.source_chunk_path,
                    DocumentChunk.chunk_metadata,
                    DocumentChunk.sort_order,
                    DocumentChunk.job_result_id,
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentChunk.chunk_id.in_(list(connected_target_ids)))
                .where(DocumentChunk.chunk_type == asset_type)
                .order_by(DocumentChunk.sort_order)
            )
            connected_result = await db.execute(connected_stmt)
            connected_rows = connected_result.all()
        else:
            connected_rows = []

        # 4. Merge and deduplicate
        seen_ids: set[str] = set()
        chunks: list[dict[str, Any]] = []

        # Helper to look up job_id from job_result
        from shared.models.database.job_result import JobResult
        job_stmt = (
            select(JobResult.job_id)
            .where(JobResult.id == job_result_id)
        )
        job_result_row = await db.execute(job_stmt)
        job_id = job_result_row.scalar() or ''

        for row in list(asset_rows) + list(connected_rows):
            chunk_id = row[0]
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)

            # Owner resolution: prefer connect_to-based owner
            owner_section_path = owner_by_target_id.get(chunk_id)

            # Fallback: media's own section_id path, but guard against
            # Root / top-level aggregation sections
            if not owner_section_path:
                own_section_path = section_path_by_id.get(row[4])
                if own_section_path and own_section_path == 'Root':
                    # Reject only the synthetic Root aggregation label;
                    # legitimate L1 sections (e.g. "前言") are valid owners.
                    logger.warning(
                        f'  asset_filter_step: rejecting root-level owner fallback '
                        f'chunk_id={chunk_id} section_path={own_section_path}'
                    )
                    own_section_path = None
                owner_section_path = own_section_path

            if not owner_section_path:
                logger.warning(
                    f'  asset_filter_step unresolved owner: chunk_id={chunk_id} '
                    f'file_path={row[3]} scope={scope_path or "root"}'
                )
                continue
            chunks.append({
                'document_id': document_id,
                'chunk_id': chunk_id,
                'chunk_type': row[1],
                'content': row[2],
                'file_path': row[3],
                'section_id': row[4],
                'section_path': owner_section_path,
                'owner_section_path': owner_section_path,
                'source_chunk_path': row[5],
                'chunk_metadata': row[6] or {},
                'sort_order': row[7],
                'job_result_id': job_result_id,
                'job_id': job_id,
            })

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f'  asset_filter_step scope={scope_path or "root"} '
            f'type={asset_type}: {len(chunks)} chunks found, {latency}ms'
        )
        return chunks

    except Exception as e:
        logger.error(f'  asset_filter_step failed: {e}')
        return []

# ---------------------------------------------------------------------------
# Tool: navigate_step (unified action — merges tool_select + scope_navigate)
# ---------------------------------------------------------------------------

async def navigate_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    query: str,
    llm_fn: LLMFn,
    user_id: str,
    namespace: str,
    doc_name: str = '',
    scope_path: str | list[str] | None = None,
    exclude_paths: set[str] | None = None,
    revision_hint: str | None = None,
    budget_snapshot: dict | None = None,
) -> tuple[str, list[str], DocTreeNode, list[dict]]:
    """Unified navigation step — one LLM call for action + tools + selections.

    scope_path can be:
      - None: root scope
      - str: single scope to drill into
      - list[str]: multiple scopes to expand simultaneously

    Returns:
      - action: 'STOP' | 'NAVIGATE'
      - asset_tools: list of asset tools to run (FIND_IMAGES, FIND_TABLES)
      - node: DocTreeNode with outline_items and leaf_content
      - pending: list of {path, confidence} for non-leaf drill-downs (empty when STOP)
    """
    from shared.services.retrieval.app_service import _hydrate_paths_to_rows

    # Normalize scope for internal use
    scope_paths: list[str] = (
        scope_path if isinstance(scope_path, list)
        else [scope_path] if scope_path
        else []
    )
    # Set of scope path strings (for filtering selections)
    scope_path_set = set(scope_paths)

    empty = DocTreeNode.empty(scope_paths[0] if scope_paths else None)

    try:
        # 1. Load continuous context tree (supports multi-scope)
        items = await _load_child_sections(
            db, document_id, job_result_id, scope_path,
            exclude_paths=exclude_paths,
        )
        if not items:
            return 'STOP', [], empty, []

        # 2. Build selectable index
        selectable = {item['path']: item for item in items if item.get('selectable', False)}

        # 3. Count ALL image/table chunks under the scope subtree(s)
        from shared.models.database.document import DocumentChunk, DocumentSection
        from sqlalchemy import func as sa_func

        scope_section_stmt = (
            select(DocumentSection.section_id)
            .where(DocumentSection.document_id == document_id)
            .where(DocumentSection.job_result_id == job_result_id)
        )
        if scope_paths:
            from sqlalchemy import or_
            scope_filters = []
            for sp in scope_paths:
                scope_filters.append(DocumentSection.section_path == sp)
                scope_filters.append(DocumentSection.section_path.like(f'{sp} / %'))
            scope_section_stmt = scope_section_stmt.where(or_(*scope_filters))
        scope_section_ids = await db.execute(scope_section_stmt)
        all_section_ids = [r[0] for r in scope_section_ids.all()]

        total_images = 0
        total_tables = 0
        if all_section_ids:
            count_stmt = (
                select(
                    DocumentChunk.chunk_type,
                    sa_func.count(DocumentChunk.id),
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentChunk.section_id.in_(all_section_ids))
                .where(DocumentChunk.chunk_type.in_(['image', 'table']))
                .group_by(DocumentChunk.chunk_type)
            )
            count_result = await db.execute(count_stmt)
            for chunk_type, cnt in count_result.all():
                if chunk_type == 'image':
                    total_images = cnt
                elif chunk_type == 'table':
                    total_tables = cnt

        tools_block = ''
        if total_images > 0 or total_tables > 0:
            tools_lines = ['\nOptional asset tools (usable with NAVIGATE or STOP):\n']
            if total_images > 0:
                tools_lines.append(
                    f'  FIND_IMAGES — Extract image/chart assets under the current scope ({total_images} available).\n'
                )
            if total_tables > 0:
                tools_lines.append(
                    f'  FIND_TABLES — Extract table/data assets under the current scope ({total_tables} available).\n'
                )
            tools_lines.append(
                '  Note: with NAVIGATE selections, asset tools are limited to the selected sections; '
                'with STOP or no selections, they use the current scope.\n'
            )
            tools_block = ''.join(tools_lines)

        # 4. Format tree and build prompt
        text, overflowed = _format_items_for_llm(items)
        if not scope_paths:
            scope_header = 'Current scope: root (document top level)'
        elif len(scope_paths) == 1:
            scope_header = f'Current scope: navigating into "{scope_paths[0]}"'
        else:
            scope_header = f'Current scope: navigating into {len(scope_paths)} sections'
        prompt = _ACTION_PROMPT.format(
            doc_name=doc_name or document_id,
            doc_id=document_id,
            scope_header=scope_header,
            budget_block=_format_budget_block(budget_snapshot),
            items_overview=text,
            query=query,
            tools_block=tools_block,
        )
        if revision_hint:
            prompt += (
                f'\n\nIMPORTANT: Previous round feedback: '
                f'"{revision_hint}". Adjust your selections accordingly.'
            )

        # 5. Single LLM call
        response = await llm_fn(prompt)
        parsed = _parse_action_response(response)
        action = parsed['action']
        asset_tools = parsed['tools']
        selections = parsed['selections']

        scope_label = ', '.join(scope_paths) if scope_paths else 'root'
        logger.info(
            f'  navigate_step scope={scope_label}: '
            f'action={action} tools={asset_tools} '
            f'selections={len(selections)} selectable={len(selectable)} '
            f'overflowed={overflowed}'
        )

        # 6. Build node with LOCAL items only
        node = DocTreeNode(scope_path=scope_paths[0] if scope_paths else None)
        local_items = [item for item in items if item.get('show_summary', True)]
        node.outline_items = local_items

        # 7. Dispatch selections (only present when action == NAVIGATE)
        valid_selections = [
            s for s in selections
            if s['path'] in selectable and s['path'] not in scope_path_set
        ]

        pending: list[dict] = []
        path_selections = []
        for sel in valid_selections:
            path = sel['path']
            conf = sel.get('confidence', 0.7)
            item = selectable[path]
            node.confidence[path] = conf

            if item.get('is_leaf'):
                path_selections.append({'path': path, 'confidence': conf, 'hydrate_mode': 'chunks'})
            else:
                # Non-leaf → will be batched into a single next call
                pending.append({'path': path, 'confidence': conf})
                path_selections.append({'path': path, 'confidence': conf, 'hydrate_mode': 'self_only'})

        if path_selections:
            chunks = await _hydrate_paths_to_rows(
                db,
                path_selections=path_selections,
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
                    _owner_map = _build_connected_owner_map(chunks)
                    for c in connected:
                        if not c.get('owner_section_path'):
                            c['owner_section_path'] = _owner_map.get(str(c.get('chunk_id') or ''))
                    chunks = chunks + connected

                _root_map = await _resolve_root_asset_owners(
                    db,
                    document_id=document_id,
                    job_result_id=job_result_id,
                    chunks=chunks,
                )
                if _root_map:
                    for c in chunks:
                        if c.get('owner_section_path'):
                            continue
                        cid = str(c.get('chunk_id') or '')
                        if cid in _root_map:
                            c['owner_section_path'] = _root_map[cid]

                for chunk in chunks:
                    real_path = chunk.get('owner_section_path') or chunk.get('section_path') or chunk.get('source_chunk_path')
                    if real_path:
                        node.add_leaf_chunks(str(real_path), [chunk])

        return action, asset_tools, node, pending

    except BudgetExceeded:
        raise
    except Exception as e:
        logger.error(f'  navigate_step failed for doc={document_id}: {e}')
        return 'STOP', [], empty, []


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
    exclude_paths: set[str] | None = None,
    revision_hint: str | None = None,
    budget_snapshot: dict | None = None,
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
        # 1. Format hints for LLM (deduplicate by section_path)
        exclude_set = {
            normalize_section_path(path)
            for path in (exclude_paths or set())
            if path
        }
        hint_lines: list[str] = []
        hint_by_path: dict[str, dict] = {}
        for h in hints:
            sp = normalize_section_path(h.get('section_path', ''))
            if not sp or sp == 'Root':
                continue
            if sp in exclude_set:
                continue
            if sp in hint_by_path:
                continue  # skip duplicate section_path
            summary = h.get('summary', '') or ''
            hint_lines.append(f'▸ path="{sp}"')
            if summary:
                clipped = summary[:300]
                hint_lines.append(f'    {clipped}')
            hint_by_path[sp] = h

        if not hint_lines:
            return node

        items_text = '\n'.join(hint_lines)

        revision_context = ''
        if revision_hint:
            revision_context = (
                f'\nIMPORTANT: This is a REVISION round. '
                f'The previous search attempt failed because:\n'
                f'"{revision_hint}"\n'
                f'Adjust your selection accordingly. '
                f'If no candidate is relevant, return an EMPTY list [].\n'
            )

        prompt = _DISCOVERY_SELECT_PROMPT.format(
            doc_name=doc_name or document_id,
            budget_block=_format_budget_block(budget_snapshot),
            items=items_text,
            query=query,
            revision_context=revision_context,
        )
        response = await llm_fn(prompt)
        # Parse {"selections": [...]} response — reuse action parser's extraction
        parsed = _parse_action_response(response)
        selections = parsed.get('selections', [])

        logger.info(
            f'  discovery_select_step doc="{doc_name}": '
            f'hints={len(hints)} selections={len(selections)}'
        )

        # 2. Hydrate selected paths
        valid_selections = [s for s in selections if s['path'] in hint_by_path]
        path_selections = []
        for sel in valid_selections:
            path = sel['path']
            conf = sel.get('confidence', 0.7)
            node.confidence[path] = conf
            path_selections.append({'path': path, 'confidence': conf})

        if path_selections:
            chunks = await _hydrate_paths_to_rows(
                db,
                path_selections=path_selections,
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
                    _owner_map = _build_connected_owner_map(chunks)
                    for c in connected:
                        if not c.get('owner_section_path'):
                            c['owner_section_path'] = _owner_map.get(str(c.get('chunk_id') or ''))
                    chunks = chunks + connected

                # Resolve Root-stranded assets to their true owner sections
                _disc_job_result_id = next(
                    (str(c['job_result_id']) for c in chunks if c.get('job_result_id')),
                    None,
                )
                _root_map = await _resolve_root_asset_owners(
                    db,
                    document_id=document_id,
                    job_result_id=_disc_job_result_id,
                    chunks=chunks,
                ) if _disc_job_result_id else {}
                if _root_map:
                    for c in chunks:
                        if c.get('owner_section_path'):
                            continue  # already resolved by batch-level owner map
                        cid = str(c.get('chunk_id') or '')
                        if cid in _root_map:
                            c['owner_section_path'] = _root_map[cid]

                for chunk in chunks:
                    # Distribute chunk to its real path or fallback to the selection path
                    real_path = chunk.get('owner_section_path') or chunk.get('section_path') or chunk.get('source_chunk_path')
                    if real_path:
                        node.add_leaf_chunks(str(real_path), [chunk])

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f'  discovery_select_step done: hydrated={len(node.leaf_content)} '
            f'latency={latency}ms'
        )
        return node

    except BudgetExceeded:
        raise
    except Exception as e:
        logger.error(f'  discovery_select_step failed for doc={document_id}: {e}')
        return node
