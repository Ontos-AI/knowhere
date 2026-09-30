"""Render an ``outline``/``node_filter`` map within ``MAP_TOOL_CHAR_BUDGET``.

A map that fits is returned whole. An oversized one shrinks in three steps,
each only if the previous did not fit:

1. Drop every row's ``summary``; the structure (rows, indentation, titles,
   paths) stays as is.
2. Score sections against the end user's original query
   (``ToolContext.query``) and fold the lowest-scoring subtrees into
   placeholder rows (``scoring.map_lighting``). The rows that must stay are
   the best-scored chain (outline) or the predicate matches (node_filter),
   each with its ancestors.
3. If the rows that must stay still overflow, or scoring is impossible (no
   query, no usable map-unit index), the call fails and tells the agent the
   scope is too large to map, to run ``corpus.grep``/``corpus.recall``/
   ``corpus.assets`` on that same scope instead.

The text is never cut mid-row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shared.services.retrieval.agent_tools.registry import MAP_TOOL_CHAR_BUDGET, ToolContext
from shared.services.retrieval.agent_tools.snippet import format_row
from shared.services.retrieval.scoring.map_lighting import (
    MapNode,
    best_score_ids,
    fold_map_nodes,
    load_leaf_unit_scores,
    pool_scores_to_tree,
    render_map_lines,
)

SUMMARIES_OMITTED_NOTE = " (summaries omitted: map over the size limit)"


@dataclass
class MapRender:
    text: str = ""
    visible_ids: set[str] = field(default_factory=set)
    error: str | None = None


def _nodes(
    order: list[tuple[str, str | None]],
    rows_by_id: dict[str, dict[str, Any]],
    scores: dict[str, float] | None = None,
) -> list[MapNode]:
    return [
        MapNode(
            section_id=section_id,
            parent_section_id=parent_id,
            render=format_row(rows_by_id[section_id]),
            depth=rows_by_id[section_id]["depth"],
            score=(scores or {}).get(section_id, 0.0),
        )
        for section_id, parent_id in order
    ]


def _too_large(tool: str, chars: int, reason: str) -> MapRender:
    return MapRender(
        error=(
            f"{tool}: the map is {chars} chars even without summaries, over the "
            f"{MAP_TOOL_CHAR_BUDGET}-char limit, and {reason}. This scope is too "
            "large to show as a map: run corpus.grep, corpus.recall or "
            "corpus.assets directly on this same scope instead."
        )
    )


async def render_map(
    ctx: ToolContext,
    *,
    tool: str,
    order: list[tuple[str, str | None]],
    rows_by_id: dict[str, dict[str, Any]],
    revision_by_document: dict[str, str],
    header: Callable[[int, int], str],
    keep_ids: set[str] | None = None,
) -> MapRender:
    """``order`` lists ``(section_id, in-map parent id)`` in display order.

    ``header(visible_count, hidden_count)`` renders the first line.
    ``keep_ids=None`` means "keep the best-scored chain" (outline); a set
    means "keep exactly these rows" (node_filter's matches). Ancestors of
    kept rows are always kept.

    When the map is oversized, every row's ``summary`` in ``rows_by_id`` is
    blanked in place, so the caller's payload rows match the rendered text.
    """
    nodes = _nodes(order, rows_by_id)
    text = header(len(nodes), 0) + "\n" + "\n".join(render_map_lines(nodes))
    if len(text) <= MAP_TOOL_CHAR_BUDGET:
        return MapRender(text=text, visible_ids={node.section_id for node in nodes})

    for row in rows_by_id.values():
        row["summary"] = ""
    nodes = _nodes(order, rows_by_id)
    text = header(len(nodes), 0) + SUMMARIES_OMITTED_NOTE + "\n" + "\n".join(
        render_map_lines(nodes)
    )
    if len(text) <= MAP_TOOL_CHAR_BUDGET:
        return MapRender(text=text, visible_ids={node.section_id for node in nodes})

    chars = len(text)
    if not ctx.query.strip():
        return _too_large(tool, chars, "no user query is available to rank sections")
    leaf_scores = await load_leaf_unit_scores(
        ctx.db,
        revision_by_document=revision_by_document,
        section_ids=[section_id for section_id, _ in order],
        query=ctx.query,
    )
    if leaf_scores is None:
        return _too_large(
            tool,
            chars,
            "these documents have no usable relevance index to decide which "
            "sections to fold",
        )

    children_by_parent: dict[str, list[str]] = {}
    for section_id, parent_id in order:
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(section_id)
    pooled = pool_scores_to_tree(
        children_by_parent=children_by_parent,
        roots=[section_id for section_id, parent_id in order if parent_id is None],
        leaf_scores=leaf_scores,
    )
    if keep_ids is None:
        keep_ids = best_score_ids(pooled)

    nodes = _nodes(order, rows_by_id, pooled)
    widest_header = header(len(nodes), len(nodes)) + SUMMARIES_OMITTED_NOTE
    folded = fold_map_nodes(
        nodes,
        char_budget=MAP_TOOL_CHAR_BUDGET - len(widest_header) - 1,
        keep_ids=keep_ids,
    )
    if folded.overflow:
        return _too_large(
            tool,
            chars,
            "the sections that must stay do not fit even after folding "
            "everything else",
        )
    return MapRender(
        text=header(len(folded.visible_ids), folded.hidden_count)
        + SUMMARIES_OMITTED_NOTE
        + "\n"
        + "\n".join(folded.lines),
        visible_ids=folded.visible_ids,
    )
