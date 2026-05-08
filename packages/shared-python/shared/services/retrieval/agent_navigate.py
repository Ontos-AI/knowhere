"""Shared helpers for agentic KG document routing and scope navigation."""
from __future__ import annotations

import json
import re
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection, GraphNode, GraphEdge
from shared.services.retrieval.lexical_text import normalize_section_path, split_section_path
from shared.utils.text_utils import tokenize_for_retrieval

_MAX_OVERVIEW_FILES = 50

_FILE_SELECT_PROMPT = """\
You are a document routing assistant.

Below is a knowledge base overview showing all available documents,
their navigation summaries, chunk counts, and media counts.

=== Knowledge Base Overview ===
{overview}
=== End Overview ===

User query: {query}

Based on the query, select all documents that may contain relevant information.
Only skip documents that are clearly irrelevant to the query.
Return ONLY a JSON array of document IDs, e.g.: ["doc_abc123", "doc_def456"]
Do not include any explanation.
"""

_SCOPE_NAV_PROMPT = """\
You are a document navigation assistant.

Document: "{doc_name}" (id: {doc_id})
Current scope: {scope_label}

Below are candidate section paths at this scope level (up to 2 depth levels).
Indented items are sub-items of the item above.
Select section paths directly. A selected section path represents the chunks
under that section subtree; do not ask to drill deeper.

=== Items ===
{items_overview}
=== End Items ===

User query: {query}

Select the most relevant section paths (at most {max_select}).
Prefer specific sub-items over broad parents when both are listed and the sub-item is sufficient.
Return ONLY a JSON array:
[{{"path": "section/path", "confidence": 0.9}}, ...]
Do not include any explanation.
"""


def _extract_json_array_payload(text: str) -> list[Any]:
    """Best-effort extraction of a JSON array payload from LLM response text."""
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _parse_json_array(text: str) -> list[str]:
    """Best-effort extraction of a JSON array of strings from LLM response text."""
    result = _extract_json_array_payload(text)
    return [str(x) for x in result]


def _normalize_confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().rstrip('%')
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed > 1.0:
        parsed = parsed / 100.0
    return max(0.0, min(parsed, 1.0))


def _default_confidence_for_rank(rank: int) -> float:
    return round(max(0.25, 0.85 - rank * 0.15), 4)


def _parse_chunk_path_selections(text: str) -> list[dict[str, Any]]:
    """Parse chunk path selections from LLM output.

    Accepts either a legacy JSON array of strings or a structured array of
    objects with `path` and optional `confidence`.
    """
    payload = _extract_json_array_payload(text)
    selections: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, str):
            path = item.strip()
            if path:
                selections.append({'path': path, 'confidence': None})
            continue
        if not isinstance(item, dict):
            continue
        path = str(item.get('path') or item.get('chunk_path') or '').strip()
        if not path:
            continue
        selections.append({
            'path': path,
            'confidence': _normalize_confidence(item.get('confidence')),
        })
    return selections


async def _build_knowledge_map_overview(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
) -> tuple[str, dict[str, str]]:
    """Build a file-level knowledge map overview for LLM file selection.

    Returns (overview_text, doc_id_to_name) where doc_id_to_name maps
    document_id -> source_file_name for validation after LLM response.
    """
    doc_stmt = (
        select(Document)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
        .where(Document.current_job_result_id.is_not(None))
        .order_by(Document.updated_at.desc())
        .limit(_MAX_OVERVIEW_FILES)
    )
    doc_result = await db.execute(doc_stmt)
    documents = list(doc_result.scalars())

    if not documents:
        return '(empty)', {}

    doc_ids = [d.document_id for d in documents]
    doc_id_to_name: dict[str, str] = {
        d.document_id: (d.source_file_name or d.document_id)
        for d in documents
    }

    chunk_stats_stmt = (
        select(
            DocumentChunk.document_id,
            func.count(DocumentChunk.id).label('chunk_count'),
            func.count(func.nullif(DocumentChunk.chunk_type, 'text')).label('media_count'),
        )
        .join(Document, (Document.document_id == DocumentChunk.document_id) & (Document.current_job_result_id == DocumentChunk.job_result_id))
        .where(DocumentChunk.document_id.in_(doc_ids))
        .group_by(DocumentChunk.document_id)
    )
    chunk_stats_result = await db.execute(chunk_stats_stmt)
    chunk_stats: dict[str, dict[str, int]] = {}
    for row in chunk_stats_result.all():
        chunk_stats[row[0]] = {'total': row[1], 'media': row[2]}

    graph_summary_stmt = (
        select(GraphNode.owner_document_id, GraphNode.properties)
        .where(GraphNode.owner_document_id.in_(doc_ids))
        .where(GraphNode.node_kind == 'document')
    )
    graph_summary_result = await db.execute(graph_summary_stmt)
    doc_top_summaries: dict[str, str] = {}
    for did, properties in graph_summary_result.all():
        if not isinstance(properties, dict):
            continue
        top_summary = str(properties.get('top_summary') or '').strip()
        if top_summary:
            doc_top_summaries[did] = top_summary

    lines: list[str] = []
    for doc in documents:
        did = doc.document_id
        name = doc_id_to_name[did]
        stats = chunk_stats.get(did, {'total': 0, 'media': 0})
        top_summary = doc_top_summaries.get(did, '')

        line = f'- [{did}] {name}  chunks={stats["total"]}'
        if stats['media'] > 0:
            line += f' media={stats["media"]}'
        if top_summary:
            line += f'\n  top_summary:\n{_indent_block(top_summary, 4)}'
        lines.append(line)

    return '\n'.join(lines), doc_id_to_name


def _indent_block(text: str, spaces: int) -> str:
    prefix = ' ' * spaces
    return '\n'.join(f'{prefix}{line}' for line in str(text or '').splitlines())


def _format_items_for_llm(
    items: list[dict],
    max_chars: int = 20000,
) -> tuple[str, bool]:
    """Unified formatting with overflow guard for scope navigation.

    Always shows ALL items (L1 + L2).  Overflow controls whether
    summaries are included — not which levels are shown.

    Normal:   path + title + chunk_count + assets_count + summary
    Overflow: path + title + chunk_count + assets_count  (no summary)

    Returns (text, overflowed).
    """
    from shared.utils.text_utils import truncate_content_preview

    if not items:
        return '(no items available)', False

    SUMMARY_HEAD_TOKENS = 80

    def _render_line(item: dict, include_summary: bool) -> str:
        level = item.get('level', 1)
        indent = '  ' if level == 2 else ''
        line = f'{indent}- path="{item["path"]}"  title="{item["title"]}"'
        chunk_count = item.get('chunk_count', 0)
        if chunk_count > 0:
            line += f'  chunks={chunk_count}'
        assets = item.get('assets_count', 0)
        if assets > 0:
            line += f'  assets={assets}'
        if include_summary:
            summary = item.get('summary') or item.get('title', '')
            if summary:
                clipped = truncate_content_preview(summary, head=SUMMARY_HEAD_TOKENS, tail=0)
                line += f'\n{indent}  summary: {clipped}'
        return line

    # Try full render (with summaries)
    full_lines = [_render_line(item, include_summary=True) for item in items]
    full_text = '\n'.join(full_lines)
    if len(full_text) <= max_chars:
        return full_text, False

    # Overflow: render without summaries
    slim_lines = [_render_line(item, include_summary=False) for item in items]
    slim_text = '\n'.join(slim_lines)
    return slim_text[:max_chars], True


# ------------------------------------------------------------------
# GREP document discovery (aligned with KB do_discover_files)
# ------------------------------------------------------------------

async def _grep_discover_document_ids(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    exclude_document_ids: Sequence[str] = (),
    limit: int = 10,
) -> list[str]:
    """GREP discovery: search term_search_text for query terms, return parent document_ids.

    Aligned with KB's do_discover_files(): if a chunk's term_search_text
    contains query terms, its parent document is included in the KG scope.
    """
    units = tokenize_for_retrieval(query, dedupe=True)
    logger.info(f'  GREP tokenized units (cap 8): {units[:8]}  (total={len(units)})')
    if not units:
        return []

    # Build OR conditions for ILIKE matching
    conditions = []
    params: dict[str, str] = {
        'user_id': user_id,
        'namespace': namespace,
    }
    for i, unit in enumerate(units[:8]):  # cap at 8 terms to avoid huge queries
        param_name = f'unit_{i}'
        params[param_name] = f'%{unit}%'
        conditions.append(DocumentChunk.term_search_text.ilike(f'%{unit}%'))

    if not conditions:
        return []

    stmt = (
        select(Document.document_id)
        .join(DocumentChunk, (DocumentChunk.document_id == Document.document_id)
              & (DocumentChunk.job_result_id == Document.current_job_result_id))
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
        .where(DocumentChunk.term_search_text.is_not(None))
        .where(or_(*conditions))
        .distinct()
        .limit(limit)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))

    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


# ------------------------------------------------------------------
# Edge expansion (aligned with KB KGIndex.neighbors)
# ------------------------------------------------------------------

async def _expand_by_edges(
    db: AsyncSession,
    *,
    document_ids: list[str],
    user_id: str,
    namespace: str,
    hops: int = 1,
) -> list[str]:
    """Expand document set by following GraphEdge relationships.

    Aligned with KB's KGIndex.neighbors(): traverse edges to include
    related documents. Only queries document-level nodes (no section nodes).
    No weight filtering — edges already passed threshold during publication.
    """
    if not document_ids:
        return document_ids

    current = set(document_ids)

    for hop_idx in range(hops):
        # Find document-level graph nodes for current document set
        doc_node_ids = [f"doc:{did}" for did in current]
        node_stmt = (
            select(GraphNode.node_id, GraphNode.owner_document_id)
            .where(GraphNode.user_id == user_id)
            .where(GraphNode.namespace == namespace)
            .where(GraphNode.node_kind == 'document')
            .where(GraphNode.node_id.in_(doc_node_ids))
        )
        node_result = await db.execute(node_stmt)
        node_rows = node_result.all()
        logger.info(f'  edge_expand hop={hop_idx}: doc_nodes_found={len(node_rows)} (of {len(doc_node_ids)} requested)')

        if not node_rows:
            break

        node_ids = {row[0] for row in node_rows}

        # Follow edges from/to these document nodes
        edge_stmt = (
            select(GraphEdge.source_node_id, GraphEdge.target_node_id)
            .where(GraphEdge.user_id == user_id)
            .where(GraphEdge.namespace == namespace)
            .where(or_(
                GraphEdge.source_node_id.in_(list(node_ids)),
                GraphEdge.target_node_id.in_(list(node_ids)),
            ))
        )
        edge_result = await db.execute(edge_stmt)
        edge_rows = edge_result.all()

        neighbor_node_ids: set[str] = set()
        for src, tgt in edge_rows:
            if src in node_ids:
                neighbor_node_ids.add(tgt)
            if tgt in node_ids:
                neighbor_node_ids.add(src)
        logger.info(f'  edge_expand hop={hop_idx}: edges_traversed={len(edge_rows)} neighbor_nodes={len(neighbor_node_ids)}')

        if not neighbor_node_ids:
            break

        # Resolve neighbor nodes to document_ids
        neighbor_doc_stmt = (
            select(GraphNode.owner_document_id)
            .where(GraphNode.node_id.in_(list(neighbor_node_ids)))
            .where(GraphNode.node_kind == 'document')
        )
        neighbor_doc_result = await db.execute(neighbor_doc_stmt)
        for (doc_id,) in neighbor_doc_result.all():
            current.add(doc_id)

    # Preserve original order, append new ones at end
    ordered = list(document_ids)
    for doc_id in current:
        if doc_id not in document_ids:
            ordered.append(doc_id)
    return ordered


# ---------------------------------------------------------------------------
# Unified scope navigation: load child sections (2-level)
# ---------------------------------------------------------------------------

async def _load_child_sections(
    db: AsyncSession,
    document_id: str,
    job_result_id: str,
    scope_path: str | None = None,
) -> list[dict]:
    """Load the next 2 available section depth bands under *scope_path*.

    Returns a flat list sorted by sort_order, each item:
        {path, title, summary, chunk_count, assets_count, level}

    - level=1: nearest available descendant depth under scope
    - level=2: second nearest available descendant depth under scope
    - chunk_count: text chunks under this section (excluding image/table)
    - assets_count: image + table chunks under this section
    """
    # ── Fetch all sections for this document revision ────────────────────
    stmt = (
        select(
            DocumentSection.section_id,
            DocumentSection.section_title,
            DocumentSection.section_path,
            DocumentSection.summary,
            DocumentSection.sort_order,
        )
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
        .order_by(DocumentSection.sort_order)
    )
    section_rows = (await db.execute(stmt)).all()
    if not section_rows:
        return []

    scope = normalize_section_path(scope_path) if scope_path else ''
    scope_parts = split_section_path(scope)
    scope_depth = len(scope_parts)

    # Build full section metadata index
    all_sections: dict[str, dict] = {}  # path → {title, summary, sort_order, section_id}
    for section_id, title, path, summary, sort_order in section_rows:
        if not path:
            continue
        path = normalize_section_path(path)
        parts = split_section_path(path)
        all_sections[path] = {
            'title': title or parts[-1] if parts else path,
            'summary': summary or '',
            'sort_order': int(sort_order or 0),
            'section_id': section_id,
            'parts': parts,
            'depth': len(parts),
        }

    # ── Identify the next two real depth bands ───────────────────────────
    #
    # The stored hierarchy is authoritative. Some ingested documents may only
    # expose deeper section rows at root; in that case those rows become this
    # round's relative L1/L2 instead of synthesizing missing ancestors.
    visible_sections: list[tuple[str, dict, int]] = []
    visible_depths: set[int] = set()
    for path, meta in all_sections.items():
        parts = meta['parts']
        if scope_parts and (
            parts[:scope_depth] != scope_parts or len(parts) <= scope_depth
        ):
            continue
        relative_depth = len(parts) - scope_depth
        if relative_depth < 1:
            continue
        visible_sections.append((path, meta, relative_depth))
        visible_depths.add(relative_depth)

    selected_depths = sorted(visible_depths)[:2]
    depth_to_level = {
        depth: idx + 1
        for idx, depth in enumerate(selected_depths)
    }

    items_by_path: dict[str, dict] = {}

    for path, meta, relative_depth in visible_sections:
        level = depth_to_level.get(relative_depth)
        if level is None:
            continue
        if path not in items_by_path:
            items_by_path[path] = {
                'path': path,
                'title': meta['title'],
                'summary': meta['summary'],
                'level': level,
                'sort_order': meta['sort_order'],
                'chunk_count': 0,
                'assets_count': 0,
                'section_id': meta['section_id'],
            }

    if not items_by_path:
        return []

    # ── Count chunks per section (text vs assets) ───────────────────────
    section_ids = [meta['section_id'] for meta in all_sections.values()]
    if section_ids:
        from sqlalchemy import case, literal_column
        chunk_stmt = (
            select(
                DocumentChunk.section_id,
                func.count(
                    case(
                        (DocumentChunk.chunk_type.notin_(['image', 'table']), literal_column('1')),
                    )
                ).label('text_count'),
                func.count(
                    case(
                        (DocumentChunk.chunk_type.in_(['image', 'table']), literal_column('1')),
                    )
                ).label('asset_count'),
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.section_id.in_(section_ids))
            .group_by(DocumentChunk.section_id)
        )
        chunk_rows = (await db.execute(chunk_stmt)).all()
        section_id_counts: dict[str, tuple[int, int]] = {
            sid: (int(tc), int(ac)) for sid, tc, ac in chunk_rows
        }
    else:
        section_id_counts = {}

    # Build section_id → path mapping for aggregation
    sid_to_path = {meta['section_id']: path for path, meta in all_sections.items()}

    # Aggregate chunk counts upward: each item gets counts from itself + descendants
    for sid, (text_c, asset_c) in section_id_counts.items():
        chunk_path = sid_to_path.get(sid, '')
        if not chunk_path:
            continue
        # Add to every ancestor item that is in our items_by_path
        for item_path, item in items_by_path.items():
            if chunk_path == item_path or chunk_path.startswith(item_path + ' / '):
                item['chunk_count'] += text_c
                item['assets_count'] += asset_c

    # ── Sort: interleave L2 under their L1 parent ────────────────────────
    #
    # Previous sort `(level, sort_order, path)` grouped all L1 first, then
    # all L2.  The LLM prompt uses indentation to show L2 as sub-items, so
    # they should appear directly after their L1 parent for readability.
    #
    # Sort key: (parent_sort_order, is_child, own_sort_order)
    #   L1 items:  (own_sort_order, 0, 0)       → primary position
    #   L2 items:  (parent_sort_order, 1, own)   → right after their parent

    def _interleave_key(item: dict) -> tuple:
        if item['level'] == 2:
            parts = split_section_path(item['path'])
            if len(parts) >= 2:
                parent_path = ' / '.join(parts[:-1])
                parent_item = items_by_path.get(parent_path)
                if parent_item is not None:
                    return (parent_item['sort_order'], 1, item['sort_order'])
            # Orphan L2 (no matching L1 parent in this view): sort by own order
            return (item['sort_order'], 1, item['sort_order'])
        return (item['sort_order'], 0, 0)

    sorted_items = sorted(items_by_path.values(), key=_interleave_key)
    # Clean up internal fields
    for item in sorted_items:
        item.pop('sort_order', None)
        item.pop('section_id', None)
    return sorted_items


