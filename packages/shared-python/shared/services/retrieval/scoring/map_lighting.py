"""Score-based folding for an oversized ``corpus.outline``/``corpus.node_filter`` map.

Adapted from the archived MAP-NAV pooling/hiding algorithm
(``deprecated/mapnav/nav/nav_map_scores.py``'s ``_pool_unit_scores_to_tree``
and ``nav_projection.py``'s ``_apply_budget_hide``) onto the live,
``document_sections``-based tree ``outline``/``node_filter`` already build —
not onto the retired ``ToolSpace``/``LazyKnowhereProvider`` abstraction those
files used. The deprecated files are unmodified; this is a fresh module using
the same idea against live data.

Only invoked when a rendered map exceeds
``agent_tools.registry.MAP_TOOL_CHAR_BUDGET``. Scoring needs the end user's
original query (``ToolContext.query``) — see that field's docstring for why a
caller without one gets a "narrow the scope" error instead of a silently
unscored fold.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import (
    DocumentMapUnit,
    DocumentMapUnitIndex,
    DocumentMapUnitToken,
)
from shared.services.retrieval.scoring.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    score_persisted_corpus_many,
    tokenize_query_for_ranker,
)
from shared.services.retrieval.scoring.persisted_score_load import (
    build_channel_bm25_stats,
    combine_average_idf,
)

_SCORE_CHANNELS = ("path", "content")


async def load_leaf_unit_scores(
    db: AsyncSession,
    *,
    revision_by_document: dict[str, str],
    section_ids: list[str],
    query: str,
) -> dict[str, float] | None:
    """BM25 path+content score, pooled onto ``section_id``, for one ``query``.

    Scoped to exactly the ``section_ids`` the caller already rendered (an
    outline/node_filter result), not a document/signal-path predicate — the
    map tools already know their own scope, so this skips rebuilding it in
    SQL. Mirrors ``search.map_unit_discovery``'s scoring formula (same stats
    builder, same channel-sum via ``score_persisted_corpus_many``) but reads
    the persisted map-unit index directly through the ORM instead of the
    discovery module's hand-rolled ``text()`` CTEs, since lighting's scope is
    already a concrete id list, not a filter to compile.

    Returns ``None`` when the map cannot be scored at all: the query has no
    rankable token, no rendered section has a map unit on its current
    revision, or some     revision lacks a compatible map-unit index; callers fail the call
    instead of folding unscored. A section that simply has no unit of its
    own is absent from the mapping and scores ``0.0``.
    """
    query_tokens = tokenize_query_for_ranker(query)
    if not query_tokens or not section_ids or not revision_by_document:
        return None
    token_hashes = [sha256(token.encode("utf-8")).hexdigest() for token in query_tokens]

    unit_rows = (
        (
            await db.execute(
                select(DocumentMapUnit).where(
                    DocumentMapUnit.section_id.in_(section_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    unit_rows = [
        row
        for row in unit_rows
        if revision_by_document.get(row.document_id) == row.job_result_id
    ]
    if not unit_rows:
        return None

    map_unit_ids = [row.id for row in unit_rows]
    token_rows = await db.execute(
        select(
            DocumentMapUnitToken.map_unit_id,
            DocumentMapUnitToken.channel,
            DocumentMapUnitToken.token,
            DocumentMapUnitToken.frequency,
        ).where(
            DocumentMapUnitToken.map_unit_id.in_(map_unit_ids),
            DocumentMapUnitToken.channel.in_(_SCORE_CHANNELS),
            DocumentMapUnitToken.token_hash.in_(token_hashes),
        )
    )
    frequencies: dict[tuple[str, str], dict[str, int]] = {}
    for map_unit_id, channel, token, frequency in token_rows.all():
        frequencies.setdefault((str(map_unit_id), str(channel)), {})[str(token)] = int(
            frequency
        )

    revision_pairs = sorted(revision_by_document.items())
    index_rows = (
        (
            await db.execute(
                select(DocumentMapUnitIndex).where(
                    tuple_(
                        DocumentMapUnitIndex.document_id,
                        DocumentMapUnitIndex.job_result_id,
                    ).in_(revision_pairs)
                )
            )
        )
        .scalars()
        .all()
    )
    compatible_indexes = [
        row for row in index_rows if row.format_version == MAP_UNIT_INDEX_FORMAT_VERSION
    ]
    if len(compatible_indexes) != len(revision_pairs):
        return None
    average_idf_path = combine_average_idf(
        [(row.average_idf_path, row.unit_count) for row in compatible_indexes]
    )
    average_idf_content = combine_average_idf(
        [(row.average_idf_content, row.unit_count) for row in compatible_indexes]
    )
    path_document_count = sum(row.path_document_count or 0 for row in compatible_indexes)
    path_total_length = sum(row.path_total_length or 0 for row in compatible_indexes)
    content_document_count = sum(
        row.content_document_count or 0 for row in compatible_indexes
    )
    content_total_length = sum(row.content_total_length or 0 for row in compatible_indexes)

    unit_dicts = [
        {
            "map_unit_id": row.id,
            "path_length": row.path_token_count,
            "content_length": row.content_token_count,
        }
        for row in unit_rows
    ]
    path_stats = build_channel_bm25_stats(
        unit_rows=unit_dicts,
        map_unit_id_field="map_unit_id",
        length_field="path_length",
        channel="path",
        query_tokens=query_tokens,
        frequencies=frequencies,
        average_idf=average_idf_path,
        document_count_override=path_document_count or None,
        total_length_override=path_total_length or None,
    )
    content_stats = build_channel_bm25_stats(
        unit_rows=unit_dicts,
        map_unit_id_field="map_unit_id",
        length_field="content_length",
        channel="content",
        query_tokens=query_tokens,
        frequencies=frequencies,
        average_idf=average_idf_content,
        document_count_override=content_document_count or None,
        total_length_override=content_total_length or None,
    )
    score_units = [
        PersistedScoreUnit(
            unit_id=row.id,
            path_length=row.path_token_count,
            content_length=row.content_token_count,
            path_frequencies=frequencies.get((row.id, "path"), {}),
            content_frequencies=frequencies.get((row.id, "content"), {}),
        )
        for row in unit_rows
    ]
    corpus = PersistedScoreCorpus(
        units=score_units, path_stats=path_stats, content_stats=content_stats
    )
    scored = score_persisted_corpus_many(corpus, [query])
    unit_scores = scored.get(query, {})

    section_scores: dict[str, float] = {}
    for row in unit_rows:
        score = float(unit_scores.get(row.id, 0.0) or 0.0)
        if score > section_scores.get(row.section_id, 0.0):
            section_scores[row.section_id] = score
    return section_scores


def pool_scores_to_tree(
    *,
    children_by_parent: dict[str, list[str]],
    roots: list[str],
    leaf_scores: dict[str, float],
) -> dict[str, float]:
    """Post-order max-pool: an internal node's score is the best of its own
    leaf score (if it owns a unit) and every descendant's score."""
    pooled: dict[str, float] = {}

    def visit(section_id: str) -> float:
        if section_id in pooled:
            return pooled[section_id]
        children = children_by_parent.get(section_id) or []
        best = leaf_scores.get(section_id, 0.0)
        for child in children:
            best = max(best, visit(child))
        pooled[section_id] = best
        return best

    for root in roots:
        visit(root)
    return pooled


def best_score_ids(pooled: dict[str, float]) -> set[str]:
    """Every section whose pooled score equals the best one — the best chain
    (max-pooling gives each ancestor at least its best descendant's score)."""
    best = max(pooled.values(), default=0.0)
    return {section_id for section_id, score in pooled.items() if score == best}


_TOP_LEVEL_KEY = "<root>"


@dataclass(frozen=True)
class MapNode:
    """One row of a rendered outline/node_filter map.

    ``parent_section_id`` is the parent only when that parent is itself a
    row of the same map; ``depth`` is the row's indent level.
    """

    section_id: str
    parent_section_id: str | None
    render: str
    depth: int = 0
    score: float = 0.0


@dataclass(frozen=True)
class FoldResult:
    lines: list[str]
    visible_ids: set[str]
    hidden_count: int
    overflow: bool


def hidden_placeholder_line(count: int, *, depth: int | None) -> str:
    """The row left in place of a folded subtree (``depth=None``: top level)."""
    if depth is None:
        return f"(hidden {count} nodes at the top level — re-scope to expand)"
    return "  " * (depth + 1) + (
        f"(hidden {count} nodes under this section — "
        "re-scope to its section_path to expand)"
    )


def render_map_lines(
    nodes: list[MapNode],
    *,
    hidden_ids: set[str] | frozenset[str] = frozenset(),
    hidden_counts: dict[str, int] | None = None,
) -> list[str]:
    counts = hidden_counts or {}
    lines: list[str] = []
    for node in nodes:
        if node.section_id in hidden_ids:
            continue
        lines.append(node.render)
        count = counts.get(node.section_id)
        if count:
            lines.append(hidden_placeholder_line(count, depth=node.depth))
    top_level = counts.get(_TOP_LEVEL_KEY)
    if top_level:
        lines.append(hidden_placeholder_line(top_level, depth=None))
    return lines


def _joined_chars(lines: list[str]) -> int:
    return sum(len(line) for line in lines) + max(len(lines) - 1, 0)


def fold_map_nodes(
    nodes: list[MapNode],
    *,
    char_budget: int,
    keep_ids: set[str] | frozenset[str] = frozenset(),
) -> FoldResult:
    """Hide lowest-score subtrees until the joined map lines fit ``char_budget``.

    Every id in ``keep_ids`` and all of its ancestors are never hidden;
    a subtree is a removal candidate only when no kept row lies in it.
    Candidates go lowest score first; ties follow the archived MAP-NAV
    order (``_apply_budget_hide``): most descendants first, then deepest
    first, then ``section_id``. The budget counts the placeholder rows left
    behind, not just the visible rows. ``overflow`` is set when the rows
    that must stay (plus their placeholders) still exceed ``char_budget``;
    the caller fails the call rather than hiding a kept row or cutting text.
    """
    by_id = {node.section_id: node for node in nodes}
    children_by_parent: dict[str, list[str]] = {}
    for node in nodes:
        if node.parent_section_id in by_id:
            children_by_parent.setdefault(str(node.parent_section_id), []).append(
                node.section_id
            )

    keep: set[str] = set()
    for kept_id in keep_ids:
        current = by_id.get(kept_id)
        while current is not None and current.section_id not in keep:
            keep.add(current.section_id)
            current = by_id.get(current.parent_section_id or "")

    subtree_cache: dict[str, list[str]] = {}

    def subtree_ids(section_id: str) -> list[str]:
        cached = subtree_cache.get(section_id)
        if cached is not None:
            return cached
        ids = [section_id]
        for child_id in children_by_parent.get(section_id, []):
            ids.extend(subtree_ids(child_id))
        subtree_cache[section_id] = ids
        return ids

    row_chars = {node.section_id: len(node.render) + 1 for node in nodes}
    visible_chars = sum(row_chars.values())
    hidden_ids: set[str] = set()
    hidden_counts: dict[str, int] = {}

    def placeholder_chars() -> int:
        total = 0
        for key, count in hidden_counts.items():
            depth = None if key == _TOP_LEVEL_KEY else by_id[key].depth
            total += len(hidden_placeholder_line(count, depth=depth)) + 1
        return total

    candidates = sorted(
        (node for node in nodes if node.section_id not in keep),
        key=lambda node: (
            node.score,
            -(len(subtree_ids(node.section_id)) - 1),
            -node.depth,
            node.section_id,
        ),
    )
    for candidate in candidates:
        if visible_chars + placeholder_chars() - 1 <= char_budget:
            break
        if candidate.section_id in hidden_ids:
            continue
        ids = subtree_ids(candidate.section_id)
        newly_hidden = [sid for sid in ids if sid not in hidden_ids]
        folded = len(newly_hidden) + sum(
            hidden_counts.pop(sid) for sid in ids if sid in hidden_counts
        )
        hidden_ids.update(newly_hidden)
        visible_chars -= sum(row_chars[sid] for sid in newly_hidden)
        key = (
            str(candidate.parent_section_id)
            if candidate.parent_section_id in by_id
            else _TOP_LEVEL_KEY
        )
        hidden_counts[key] = hidden_counts.get(key, 0) + folded

    lines = render_map_lines(nodes, hidden_ids=hidden_ids, hidden_counts=hidden_counts)
    return FoldResult(
        lines=lines,
        visible_ids={node.section_id for node in nodes if node.section_id not in hidden_ids},
        hidden_count=sum(hidden_counts.values()),
        overflow=_joined_chars(lines) > char_budget,
    )
