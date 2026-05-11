"""Shared helpers for agentic KG document routing and scope navigation."""
from __future__ import annotations

import json
import re
from typing import Any, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from shared.services.retrieval.agentic.types import DocTreeNode

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
{scope_header}

Below is the document's section tree.
Sections tagged [SELECT] are within the current scope and may be selected.
Other sections are shown as structural context only (not selectable).
Nodes marked [Leaf] have no further sub-sections.

=== Section Tree ===
{items_overview}
=== End Section Tree ===

User query: {query}

Select sections to drill into for more detailed content.
- You may ONLY select sections marked with [SELECT]. Do NOT select any other sections.
- Select sections whose content is needed to answer the query.
- If the titles and summaries already visible are sufficient (e.g. the query asks for an outline or overview), return an EMPTY list [].

  Optional: set "mode" per selection to control what content is retrieved:
  - "all" (default): retrieve all content types (text, images, tables)
  - "image": retrieve only image assets from this section
  - "table": retrieve only table assets from this section

Return ONLY a JSON object:
{{"selections": [{{"path": "...", "confidence": <float>, "mode": "all"}}, ...]}}
Do not include any explanation.
"""


_DISCOVERY_SELECT_PROMPT = """\
You are a document navigation assistant.

Document: "{doc_name}"

After navigating the document's section tree, the following section paths
were additionally discovered via keyword and semantic search.
They may contain relevant evidence not found through hierarchical navigation.

=== Discovery Candidates ===
{items}
=== End Discovery Candidates ===

User query: {query}

Select section paths whose content is needed to answer the query.
If none are relevant, return an EMPTY list [].

Return ONLY a JSON object:
{{"selections": [{{"path": "...", "confidence": <float>, "mode": "all"}}, ...]}}
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
    """Format items with ▸ └ [Leaf] hierarchy for scope navigation.

    Supports arbitrary depth levels via absolute ``level`` field.
    Items with ``show_summary=False`` render title only (structural context).
    ``[LN]`` tags indicate the absolute document depth of each section.
    ``[Leaf]`` tags indicate bottom-level sections with no further children.
    Summaries are included when within budget, dropped on overflow.

    Returns (text, overflowed).
    """
    from shared.utils.text_utils import truncate_content_preview

    if not items:
        return '(no items available)', False

    SUMMARY_HEAD_TOKENS = 80

    def _render_item(item: dict, include_summary: bool) -> str:
        level = item.get('level', 1)
        show = item.get('show_summary', True)
        is_leaf = item.get('is_leaf', False)
        leaf_tag = ' [Leaf]' if is_leaf else ''
        title = item.get('title', '')
        path = item.get('path', '')
        summary = item.get('summary') or ''

        # Build chunk count tags (only for current-scope items)
        counts_str = ''
        if show:
            count_parts: list[str] = []
            chunk_count = item.get('chunk_count', 0)
            if chunk_count > 0:
                count_parts.append(f'text={chunk_count}')
            image_count = item.get('image_count', 0)
            if image_count > 0:
                count_parts.append(f'image={image_count}')
            table_count = item.get('table_count', 0)
            if table_count > 0:
                count_parts.append(f'table={table_count}')
            counts_str = f'  [{" ".join(count_parts)}]' if count_parts else ''

        indent = "    " * (level - 1)
        prefix = '▸' if level == 1 else '└'
        level_tag = f'[L{level}]'
        select_tag = '[SELECT] ' if show else ''

        lines: list[str] = []
        lines.append(f'{indent}{prefix} {select_tag}{level_tag} path="{path}" {title}{counts_str}{leaf_tag}')

        if include_summary and show and summary:
            sub_indent = "    " * level
            clipped = truncate_content_preview(summary, head=SUMMARY_HEAD_TOKENS, tail=0)
            lines.append(f'{sub_indent}{clipped}')

        return '\n'.join(lines)

    # Try full render (with summaries for show_summary=True items)
    full_lines = [_render_item(item, include_summary=True) for item in items]
    full_text = '\n'.join(full_lines)
    if len(full_text) <= max_chars:
        return full_text, False

    # Overflow: render without summaries
    slim_lines = [_render_item(item, include_summary=False) for item in items]
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
    exclude_paths: set[str] | None = None,
) -> list[dict]:
    """Load the Continuous Context Tree for *scope_path*.

    Returns a flat list sorted by document order, each item:
        {path, title, summary, chunk_count, image_count, table_count,
         level, show_summary, is_leaf}

    The tree contains three categories of nodes:
      1. Ancestors of scope_path + their siblings → show_summary=False (title only)
      2. Children of scope_path (2 depth bands) → show_summary=True (with summary)
      3. Everything else → pruned (not returned)

    When scope_path is None (root), all items are category 2.

    - level: absolute depth in the document (1-based)
    - show_summary: controls whether _format_items_for_llm renders summary
    - exclude_paths: paths already seen in prior revision rounds;
      any path matching (exact or subtree) is skipped from category 2
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
    all_sections: dict[str, dict] = {}  # path → {title, summary, sort_order, section_id, parts, depth}
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

    # ── Build the set of ancestor prefixes for pruning ────────────────────
    # e.g. scope = "A / B / K" → ancestor_prefixes = {"A", "A / B", "A / B / K"}
    ancestor_prefixes: set[str] = set()
    for i in range(1, scope_depth + 1):
        ancestor_prefixes.add(' / '.join(scope_parts[:i]))

    # ── Classify each section ────────────────────────────────────────────
    _excl = exclude_paths or set()
    items_by_path: dict[str, dict] = {}
    scope_child_depths: set[int] = set()

    for path, meta in all_sections.items():
        parts = meta['parts']
        depth = meta['depth']

        if scope_depth == 0:
            # Root scope: everything is a potential child
            if depth < 1:
                continue
            # Skip excluded paths
            if _excl and any(
                path == ep or path.startswith(ep + ' / ')
                for ep in _excl
            ):
                continue
            scope_child_depths.add(depth)
            items_by_path[path] = {
                'path': path,
                'title': meta['title'],
                'summary': meta['summary'],
                'level': depth,
                'sort_order': meta['sort_order'],
                'chunk_count': 0,
                'image_count': 0,
                'table_count': 0,
                'section_id': meta['section_id'],
                'show_summary': True,  # will be refined after depth band selection
            }
            continue

        # --- Non-root scope ---

        # Category 1: Ancestors and their siblings (structural context)
        # A node is an ancestor/sibling if its depth <= scope_depth AND
        # its parent prefix matches the scope's ancestry chain.
        if depth <= scope_depth:
            # Check: is this node in the ancestry chain or a sibling of one?
            if depth == 1:
                # All L1 nodes are either the ancestor or its siblings
                items_by_path[path] = {
                    'path': path,
                    'title': meta['title'],
                    'summary': meta['summary'],
                    'level': depth,
                    'sort_order': meta['sort_order'],
                    'chunk_count': 0,
                    'image_count': 0,
                    'table_count': 0,
                    'section_id': meta['section_id'],
                    'show_summary': False,
                }
            elif depth <= scope_depth:
                # For deeper ancestors/siblings: their parent must be in the
                # ancestor chain. e.g. "A / C" is a sibling of "A / B" only
                # if "A" is an ancestor of scope.
                parent_prefix = ' / '.join(parts[:-1])
                if parent_prefix in ancestor_prefixes:
                    items_by_path[path] = {
                        'path': path,
                        'title': meta['title'],
                        'summary': meta['summary'],
                        'level': depth,
                        'sort_order': meta['sort_order'],
                        'chunk_count': 0,
                        'image_count': 0,
                        'table_count': 0,
                        'section_id': meta['section_id'],
                        'show_summary': False,
                    }
            continue

        # Category 2: Descendants of scope_path (children to explore)
        if parts[:scope_depth] == scope_parts and depth > scope_depth:
            # Skip excluded paths
            if _excl and any(
                path == ep or path.startswith(ep + ' / ')
                for ep in _excl
            ):
                continue
            scope_child_depths.add(depth)
            items_by_path[path] = {
                'path': path,
                'title': meta['title'],
                'summary': meta['summary'],
                'level': depth,
                'sort_order': meta['sort_order'],
                'chunk_count': 0,
                'image_count': 0,
                'table_count': 0,
                'section_id': meta['section_id'],
                'show_summary': True,
            }
            continue

        # Category 3: Everything else → pruned (not added)

    if not items_by_path:
        return []

    # ── Limit children to 2 depth bands (relative to scope) ─────────────
    if scope_child_depths:
        if scope_depth == 0:
            allowed_depths = sorted(scope_child_depths)[:2]
        else:
            allowed_depths = sorted(scope_child_depths)[:2]
        allowed_set = set(allowed_depths)
        to_remove = []
        for path, item in items_by_path.items():
            if item['show_summary'] and item['level'] not in allowed_set:
                to_remove.append(path)
        for path in to_remove:
            del items_by_path[path]

    if not items_by_path:
        return []

    # ── Count chunks per section (text / image / table) ──────────────────
    # Only count for show_summary=True items (current scope children)
    scope_item_sids = {item['section_id'] for item in items_by_path.values() if item['show_summary']}
    # Also need all section_ids for upward aggregation
    all_section_ids = [meta['section_id'] for meta in all_sections.values()]
    if all_section_ids and scope_item_sids:
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
                        (DocumentChunk.chunk_type == 'image', literal_column('1')),
                    )
                ).label('image_count'),
                func.count(
                    case(
                        (DocumentChunk.chunk_type == 'table', literal_column('1')),
                    )
                ).label('table_count'),
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.section_id.in_(all_section_ids))
            .group_by(DocumentChunk.section_id)
        )
        chunk_rows = (await db.execute(chunk_stmt)).all()
        section_id_counts: dict[str, tuple[int, int, int]] = {
            sid: (int(tc), int(ic), int(tbc)) for sid, tc, ic, tbc in chunk_rows
        }
    else:
        section_id_counts = {}

    # Build section_id → path mapping for aggregation
    sid_to_path = {meta['section_id']: path for path, meta in all_sections.items()}

    # Aggregate chunk counts upward: each show_summary item gets counts from itself + descendants
    for sid, (text_c, img_c, tbl_c) in section_id_counts.items():
        chunk_path = sid_to_path.get(sid, '')
        if not chunk_path:
            continue
        for item_path, item in items_by_path.items():
            if not item['show_summary']:
                continue
            if chunk_path == item_path or chunk_path.startswith(item_path + ' / '):
                item['chunk_count'] += text_c
                item['image_count'] += img_c
                item['table_count'] += tbl_c

    # ── Sort by native document order ─────────────────────────────────────
    sorted_items = sorted(items_by_path.values(), key=lambda x: x['sort_order'])
    # Clean up internal fields
    for item in sorted_items:
        item.pop('sort_order', None)
        item.pop('section_id', None)

    # ── Detect leaf status ────────────────────────────────────────────────
    # A section is a leaf if no other section in the database for this
    # document has a path that descends from it.
    all_section_paths = set(all_sections.keys())
    for item in sorted_items:
        item_path = item['path']
        has_descendants = any(
            p != item_path and p.startswith(item_path + ' / ')
            for p in all_section_paths
        )
        item['is_leaf'] = not has_descendants

    return sorted_items


# ---------------------------------------------------------------------------
# LLM response parser (for scope_navigate)
# ---------------------------------------------------------------------------

def _parse_scope_nav_response(text: str) -> list[dict[str, Any]]:
    """Parse selections JSON from scope navigation LLM response.

    Returns list of {"path": str, "confidence": float, "mode": str}.
    """
    text = text.strip()
    # Try direct parse
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Extract JSON object from markdown wrapper
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            return []

    if not isinstance(data, dict):
        return []

    selections: list[dict[str, Any]] = []
    _VALID_MODES = {'all', 'image', 'table'}
    for item in (data.get('selections') or []):
        if not isinstance(item, dict):
            continue
        path = str(item.get('path') or '').strip()
        if not path:
            continue
        confidence = _normalize_confidence(item.get('confidence'))
        if confidence is None:
            confidence = 0.7
        mode = str(item.get('mode') or 'all').strip().lower()
        if mode not in _VALID_MODES:
            mode = 'all'
        selections.append({'path': path, 'confidence': confidence, 'mode': mode})

    return selections


# ---------------------------------------------------------------------------
# Unified document tree rendering (DocTreeNode → single coherent hierarchy)
# ---------------------------------------------------------------------------

def _render_leaf_chunks(
    parts: list[str],
    chunks: list[dict[str, Any]],
    indent: str,
    asset_lookup: dict[str, str] | None = None,
) -> None:
    """Render hydrated leaf chunks inline with table/image inlining and dedup.

    Uses ``connect_to`` metadata to resolve asset references — the same
    pattern as ``assemble_retrieval_results``:
      - **Tables**: inline HTML content at the ``ref`` placeholder
      - **Images**: inline the ``file_path`` (S3-compatible URL) at the
        placeholder for multimodal LLMs

    Connected target chunks (images/tables) are expected to already be
    present in ``chunks`` via ``hydrate_connected_target_rows``.
    """
    chunk_by_id: dict[str, dict] = {
        c.get('chunk_id', ''): c for c in chunks if c.get('chunk_id')
    }
    rendered_ids: set[str] = set()
    for chunk in chunks:
        cid = chunk.get('chunk_id', '')
        if cid and cid in rendered_ids:
            continue
        if cid:
            rendered_ids.add(cid)

        chunk_type = (chunk.get('chunk_type') or chunk.get('type') or 'text').strip().lower()

        # Skip standalone image/table chunks — they'll be inlined
        # via connect_to from their parent text chunk
        if chunk_type in ('image', 'table'):
            continue

        content = str(chunk.get('content', '')).strip()

        # Resolve connected assets via connect_to metadata
        for conn in (chunk.get('chunk_metadata') or {}).get('connect_to') or []:
            target = chunk_by_id.get(conn.get('target', ''))
            if not target:
                continue
            target_cid = target.get('chunk_id', '')
            target_type = (target.get('chunk_type') or target.get('type') or '').strip().lower()
            ref_str = conn.get('ref', '')
            if not ref_str or ref_str not in content:
                continue

            if target_cid:
                rendered_ids.add(target_cid)

            if target_type == 'table':
                table_html = str(target.get('content', '')).strip()
                content = content.replace(ref_str, f'\n[表格内容]\n{table_html}\n')
            elif target_type == 'image':
                file_path = target.get('file_path') or ''
                img_desc = str(target.get('content', '')).strip()
                # Strip self-reference from image description
                if ref_str in img_desc:
                    img_desc = img_desc.replace(ref_str, '').strip()
                # Use pre-generated asset URL if available, fall back to file_path
                asset_url = (asset_lookup or {}).get(target_cid, '') if target_cid else ''
                display_ref = asset_url or file_path
                if display_ref:
                    content = content.replace(ref_str, f'\n[图片: {display_ref}]\n{img_desc}\n')
                elif img_desc:
                    content = content.replace(ref_str, f'\n[图片描述]\n{img_desc}\n')

        for line in content.split('\n'):
            if line.strip():
                parts.append(f'{indent}┈ {line}')


def render_unified_doc_tree(
    node: DocTreeNode,
    doc_name: str,
    depth: int = 0,
    asset_lookup: dict[str, str] | None = None,
) -> str:
    """Render a DocTreeNode as a single coherent hierarchy.

    Summaries are navigation-only aids and NEVER appear in evidence.
    The rendered output contains:
      1. Structural titles for ALL sections (positioning context)
      2. Hydrated chunk content (┈ lines) ONLY for selected leaf paths

    Asset references (tables/images) are resolved via ``connect_to``
    metadata in hydrated chunks — no separate lookup needed.
    """

    parts: list[str] = []
    indent = '    ' * depth

    if depth == 0:
        parts.append(f'【文档】{doc_name}\n')

    # Track which paths have been rendered via outline_items
    rendered_paths: set[str] = set()

    # Collect children keys for path-hierarchy dedup:
    child_prefixes = set(node.children.keys())

    for item in node.outline_items:
        path = item.get('path', '')
        title = item.get('title', '')
        is_leaf = item.get('is_leaf', False)
        level = item.get('level', 1)
        leaf_tag = ' [Leaf]' if is_leaf else ''

        # Skip items that belong to a drilled-into child's subtree
        if any(path.startswith(cp + ' / ') for cp in child_prefixes):
            continue

        rendered_paths.add(path)

        # Section header (title only — summaries are navigation aids, not evidence)
        level_tag = f'[L{level}] ' if level else ''
        if level <= 1:
            parts.append(f'{indent}▸ {level_tag}{title}{leaf_tag}')
        else:
            parts.append(f'{indent}└ {level_tag}{title}{leaf_tag}')

        sub_indent = indent + '    '

        # Case 1: This section was drilled into → show child tree inline
        if path in node.children:
            child = node.children[path]
            child_text = render_unified_doc_tree(child, doc_name, depth + 1, asset_lookup=asset_lookup)
            if child_text.strip():
                parts.append(child_text)

        # Case 2: This is a hydrated leaf → show chunk content inline
        elif path in node.leaf_content:
            _render_leaf_chunks(parts, node.leaf_content[path], sub_indent, asset_lookup=asset_lookup)

        # Case 3: Unselected → title already rendered above, nothing more needed

    # Render orphan paths: leaf_content and children not covered by outline_items
    for path in node.leaf_content:
        if path not in rendered_paths:
            title = path.rsplit(' / ', 1)[-1] if ' / ' in path else path
            parts.append(f'{indent}▸ [Leaf] {title}')
            sub_indent = indent + '    '
            _render_leaf_chunks(parts, node.leaf_content[path], sub_indent, asset_lookup=asset_lookup)

    for path in node.children:
        if path not in rendered_paths:
            title = path.rsplit(' / ', 1)[-1] if ' / ' in path else path
            parts.append(f'{indent}▸ {title} [DrillDown]')
            child_text = render_unified_doc_tree(node.children[path], doc_name, depth + 1, asset_lookup=asset_lookup)
            if child_text.strip():
                parts.append(child_text)

    return '\n'.join(parts)

