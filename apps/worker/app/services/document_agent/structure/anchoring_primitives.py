"""Shared hierarchy anchoring: Phase-2 bulk/bisect/null-page + SkeletonAnchor.
Phase-1 offset discovery lives in ``document_agent.agents.calibration``.
``anchor_hierarchy`` composes Phase-1 + Phase-2 for production callers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    first_leaf_start_under,
    iter_leaf_title_nodes,
    last_leaf_start_under,
    locate_title_compact_strict,
)
from app.services.document_agent.structure.page_locate_agent import (
    verify_section_page_choice,
)
from loguru import logger


def prune_out_of_scope_nodes(
    nodes: list[TitleNode],
    *,
    offset: int,
    page_count: int,
) -> tuple[list[TitleNode], int]:
    """Remove leaf nodes whose printed_page + offset exceeds page_count.

    Bottom-up: prune out-of-scope leaves, then remove intermediate nodes
    that become childless after pruning. Returns (pruned_tree, removed_count).
    """
    removed = 0

    def _prune(node: TitleNode) -> TitleNode | None:
        nonlocal removed
        if not node.children:
            if node.printed_page is not None:
                expected = node.printed_page + offset
                if expected > page_count or expected < 1:
                    removed += 1
                    return None
            return node
        pruned_children = []
        for child in node.children:
            result = _prune(child)
            if result is not None:
                pruned_children.append(result)
        if not pruned_children:
            removed += 1
            return None
        return replace(node, children=pruned_children)

    pruned = []
    for node in nodes:
        result = _prune(node)
        if result is not None:
            pruned.append(result)

    if removed:
        logger.info(
            "[structure_anchoring] pruned {} out-of-scope TOC nodes "
            "(printed_page + offset={} exceeds page_count={})",
            removed,
            offset,
            page_count,
        )

    return pruned, removed


def prune_unanchored_toc_leaves(
    nodes: list[TitleNode],
    *,
    match_overrides: dict[tuple[str, ...], TitleMatch],
) -> tuple[list[TitleNode], int]:
    """Remove TOC leaves that have no physical ``match_overrides`` entry.

    Implements Phase-2 ``suffix = no TOC``: after bulk/bisect/recalibrate, any
    leaf that was not successfully anchored is dropped from the coarse tree
    instead of sticky ``inherited_unlocated`` ranges. Childless parents are
    removed unless they themselves have an override.
    """
    removed = 0

    def _prune(
        node: TitleNode, parent_titles: tuple[str, ...]
    ) -> TitleNode | None:
        nonlocal removed
        path = (*parent_titles, node.title)
        if node.children:
            children: list[TitleNode] = []
            for child in node.children:
                kept = _prune(child, path)
                if kept is not None:
                    children.append(kept)
            if children:
                return replace(node, children=children)
            if path in match_overrides:
                return replace(node, children=[])
            removed += 1
            return None
        if path in match_overrides:
            return node
        removed += 1
        return None

    out: list[TitleNode] = []
    for node in nodes:
        kept = _prune(node, ())
        if kept is not None:
            out.append(kept)

    if removed:
        logger.info(
            "[structure_anchoring] pruned {} unanchored TOC nodes "
            "(suffix / no match_overrides → no TOC)",
            removed,
        )
    return out, removed


def toc_range_start(hierarchy: dict[str, Any]) -> int | None:
    toc_range = hierarchy.get("toc_range")
    if not isinstance(toc_range, (list, tuple)) or not toc_range:
        return None
    try:
        return int(toc_range[0])
    except (TypeError, ValueError):
        return None


def toc_range_end(hierarchy: dict[str, Any]) -> int | None:
    toc_range = hierarchy.get("toc_range")
    if not isinstance(toc_range, (list, tuple)) or not toc_range:
        return None
    try:
        return int(toc_range[-1])
    except (TypeError, ValueError):
        return None


# ── Null-page parent locate (compact-strict + RTL visual) ───────────────────

_NULL_PARENT_VISUAL_CONFIDENCE = 0.6


def locate_null_page_parent_overrides(
    *,
    nodes: list[TitleNode],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    page_texts: dict[int, str],
    body_pages: list[int],
    ctx: ToolContext | None,
) -> tuple[dict[tuple[str, ...], TitleMatch], list[dict[str, Any]]]:
    """Locate TOC parents with ``printed_page=None`` into ``match_overrides``.

    Window for parent P: ``[last leaf start under previous same-level sibling,
    first leaf start under P]``. Text path is compact→strict unique page; on
    miss/ambiguity, scan right→left with ``verify_section_page_choice``.

    Returns ``(overrides, report)`` where *report* lists every null-page parent
    attempt (for debug / LLM-call accounting).
    """
    if not nodes or not body_pages:
        return dict(match_overrides), []

    out = dict(match_overrides)
    body_set = set(body_pages)
    parent_scope_start = body_pages[0]
    report: list[dict[str, Any]] = []

    def walk(
        sibling_nodes: list[TitleNode],
        parent_titles: tuple[str, ...],
        scope_start: int,
    ) -> None:
        for index, node in enumerate(sibling_nodes):
            path_titles = (*parent_titles, node.title)
            if (
                node.children
                and node.printed_page is None
                and path_titles not in out
            ):
                if index > 0:
                    left = last_leaf_start_under(
                        sibling_nodes[index - 1], parent_titles, out
                    )
                    if left is None:
                        left = scope_start
                else:
                    left = scope_start
                right = first_leaf_start_under(node, parent_titles, out)
                entry: dict[str, Any] = {
                    "path_titles": list(path_titles),
                    "title": node.title,
                    "printed_page": None,
                    "window": None,
                    "result": "skipped_no_right",
                    "page": None,
                    "accept": None,
                    "visual_verify_calls": 0,
                }
                if right is None or right < left:
                    report.append(entry)
                    logger.info(
                        "[structure_anchoring] null-page parent skipped: "
                        "title={!r} reason=no_located_first_child left={}",
                        node.title,
                        left,
                    )
                else:
                    entry["window"] = [left, right]
                    scope_pages = [
                        page for page in body_pages if left <= page <= right
                    ]
                    match = locate_title_compact_strict(
                        node.title,
                        scope_pages=scope_pages,
                        page_texts=page_texts,
                    )
                    visual_calls = 0
                    if match is None and ctx is not None:
                        match, visual_calls = _visual_rtl_locate_parent(
                            title=node.title,
                            left=left,
                            right=right,
                            body_set=body_set,
                            ctx=ctx,
                        )
                    entry["visual_verify_calls"] = visual_calls
                    if match is not None and match.page in body_set:
                        out[path_titles] = match
                        entry["result"] = str(match.evidence.get("accept") or match.source)
                        entry["page"] = match.page
                        entry["accept"] = match.evidence.get("accept")
                        logger.info(
                            "[structure_anchoring] null-page parent located: "
                            "title={!r} page={} window={} accept={} visual_calls={}",
                            node.title,
                            match.page,
                            [left, right],
                            match.evidence.get("accept"),
                            visual_calls,
                        )
                    else:
                        entry["result"] = "unresolved"
                        logger.info(
                            "[structure_anchoring] null-page parent unresolved: "
                            "title={!r} window={} visual_calls={}",
                            node.title,
                            [left, right],
                            visual_calls,
                        )
                    report.append(entry)
            if node.children:
                child_scope_start = (
                    out[path_titles].page if path_titles in out else scope_start
                )
                walk(node.children, path_titles, child_scope_start)

    walk(nodes, (), parent_scope_start)
    logger.info(
        "[structure_anchoring] null-page parent locate summary: "
        "attempted={} located={} unresolved={} visual_verify_calls={}",
        len(report),
        sum(1 for row in report if row.get("page") is not None),
        sum(1 for row in report if row.get("result") == "unresolved"),
        sum(int(row.get("visual_verify_calls") or 0) for row in report),
    )
    return out, report


def _visual_rtl_locate_parent(
    *,
    title: str,
    left: int,
    right: int,
    body_set: set[int],
    ctx: ToolContext,
) -> tuple[TitleMatch | None, int]:
    """Confirm parent title from right boundary toward left via VLM verify."""
    visual_calls = 0
    for page in range(right, left - 1, -1):
        if page not in body_set:
            continue
        candidate = TitleMatch(
            page=page,
            confidence=0.4,
            source="agent_heuristic",
            matched_line="",
            score=0.4,
            candidates=[page],
            evidence={"null_page_parent_probe": True},
        )
        visual_calls += 1
        result = verify_section_page_choice(
            ctx=ctx,
            title=title,
            candidate_matches=[candidate],
            candidate_page_cap=1,
        )
        selected = result.get("selected_page")
        confidence = float(result.get("confidence") or 0.0)
        if selected != page or confidence < _NULL_PARENT_VISUAL_CONFIDENCE:
            continue
        if result.get("source") == "agent_vlm":
            return (
                TitleMatch(
                    page=page,
                    confidence=confidence,
                    source="agent_vlm",
                    matched_line="",
                    score=confidence,
                    candidates=[page],
                    evidence={
                        "accept": "visual_rtl",
                        "reason": result.get("reason", ""),
                        "visual_verify_calls": visual_calls,
                    },
                ),
                visual_calls,
            )
        return (
            TitleMatch(
                page=page,
                confidence=confidence,
                source="agent_heuristic",
                matched_line="",
                score=confidence,
                candidates=[page],
                evidence={
                    "accept": "visual_rtl",
                    "reason": result.get("reason", ""),
                    "visual_verify_calls": visual_calls,
                },
            ),
            visual_calls,
        )
    return None, visual_calls


# ── Offset-guided bulk anchoring with recursive recalibrate (Phase-2) ───────

_TAIL_VERIFY_CONFIDENCE_THRESHOLD = 0.6
_MAX_RECALIBRATE_DEPTH = 5
_MAX_RECALIBRATE_DELTA = 5


def _verify_offset_tail(
    *,
    leaves: list[tuple[tuple[str, ...], TitleNode]],
    offset: int,
    ctx: ToolContext,
    page_count: int,
) -> bool:
    """VLM-verify that the offset holds for the last leaf entry (Theorem 1).

    If head offset == tail offset, monotonicity guarantees all intermediate
    entries share the same offset.

    Prefers a tail leaf whose expected page is strictly less than page_count
    (boundary pages are unreliable for VLM verification).
    """
    tail_leaves = [
        (path, node) for path, node in reversed(leaves) if node.printed_page is not None
    ]
    if not tail_leaves:
        return True

    # Prefer non-boundary: printed_page + offset < page_count
    selected = None
    for path, node in tail_leaves:
        pp = node.printed_page
        if pp is None:
            continue
        expected = pp + offset
        if 1 <= expected < page_count:
            selected = (path, node)
            break
    if selected is None:
        # All leaves are at the boundary; fall back to the last one
        selected = tail_leaves[0]

    path, node = selected
    printed_page = node.printed_page
    if printed_page is None:
        return True
    expected_page = printed_page + offset
    if expected_page < 1 or expected_page > page_count:
        return False

    candidate = TitleMatch(
        page=expected_page,
        confidence=0.4,
        source="agent_heuristic",
        matched_line="",
        score=0.4,
        candidates=[expected_page],
        evidence={"tail_verify_probe": True},
    )
    result = verify_section_page_choice(
        ctx=ctx,
        title=node.title,
        candidate_matches=[candidate],
        candidate_page_cap=1,
    )
    confirmed = (
        result.get("selected_page") == expected_page
        and result.get("confidence", 0) >= _TAIL_VERIFY_CONFIDENCE_THRESHOLD
    )
    logger.info(
        "[structure_anchoring] tail verify: title={!r} expected_page={} confirmed={} confidence={}",
        node.title,
        expected_page,
        confirmed,
        result.get("confidence", 0),
    )
    return confirmed


def _vlm_confirm_single_page(
    *,
    ctx: ToolContext,
    title: str,
    expected_page: int,
    page_count: int,
) -> bool:
    """Single-page VLM confirmation for binary search steps."""
    if expected_page < 1 or expected_page > page_count:
        return False
    candidate = TitleMatch(
        page=expected_page,
        confidence=0.4,
        source="agent_heuristic",
        matched_line="",
        score=0.4,
        candidates=[expected_page],
        evidence={"bisect_probe": True},
    )
    result = verify_section_page_choice(
        ctx=ctx,
        title=title,
        candidate_matches=[candidate],
        candidate_page_cap=1,
    )
    return (
        result.get("selected_page") == expected_page
        and result.get("confidence", 0) >= _TAIL_VERIFY_CONFIDENCE_THRESHOLD
    )


def _bisect_offset_breakpoint(
    *,
    leaves: list[tuple[tuple[str, ...], TitleNode]],
    offset: int,
    ctx: ToolContext,
    page_count: int,
) -> int:
    """Binary search for the last leaf index where offset is valid. O(log n) VLM calls."""
    lo, hi = 0, len(leaves) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        _, node = leaves[mid]
        if node.printed_page is None:
            hi = mid - 1
            continue
        expected = node.printed_page + offset
        if _vlm_confirm_single_page(
            ctx=ctx, title=node.title, expected_page=expected, page_count=page_count
        ):
            lo = mid
        else:
            hi = mid - 1
    logger.info(
        "[structure_anchoring] bisect breakpoint: last_valid_index={} / total={}",
        lo,
        len(leaves),
    )
    return lo


def bulk_offset_matches(
    leaves: list[tuple[tuple[str, ...], TitleNode]],
    offset: int,
) -> dict[tuple[str, ...], TitleMatch]:
    """Generate TitleMatch overrides for all leaves using offset. No VLM calls."""
    matches: dict[tuple[str, ...], TitleMatch] = {}
    for path_titles, node in leaves:
        if node.printed_page is None:
            continue
        page = node.printed_page + offset
        matches[path_titles] = TitleMatch(
            page=page,
            confidence=0.88,
            source="agent_vlm",
            matched_line="",
            score=0.88,
            candidates=[page],
            evidence={
                "bulk_offset": True,
                "offset": offset,
                "printed_page": node.printed_page,
            },
        )
    return matches


def _iter_printed_page_parents(
    nodes: list[TitleNode],
    *,
    parent_titles: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], TitleNode]]:
    """DFS non-leaf nodes that print their own page in the TOC."""
    parents: list[tuple[tuple[str, ...], TitleNode]] = []
    for node in nodes:
        path_titles = (*parent_titles, node.title)
        if not node.children:
            continue
        if node.printed_page is not None:
            parents.append((path_titles, node))
        parents.extend(
            _iter_printed_page_parents(node.children, parent_titles=path_titles)
        )
    return parents


def _descendant_regime_offset(
    node: TitleNode,
    path_titles: tuple[str, ...],
    matches: dict[tuple[str, ...], TitleMatch],
) -> int | None:
    """Offset of the parent's first anchored descendant leaf, i.e. its regime."""
    for leaf_path, _leaf in iter_leaf_title_nodes(
        node.children, parent_titles=path_titles
    ):
        match = matches.get(leaf_path)
        if match is None:
            continue
        offset = match.evidence.get("offset")
        if offset is not None:
            return int(offset)
    return None


def backfill_parent_offset_matches(
    *,
    nodes: list[TitleNode],
    matches: dict[tuple[str, ...], TitleMatch],
    page_count: int,
) -> dict[tuple[str, ...], TitleMatch]:
    """Anchor TOC parents that print a page, reusing their descendant's offset.

    Bulk anchoring consumes leaves only, so a TOC whose section headings carry
    printed pages leaves every parent without a physical page. The parent shares
    the calibration regime of its first anchored descendant, so ``printed_page +
    that regime's offset`` is the parent's physical page.
    """
    by_offset: dict[int, list[tuple[tuple[str, ...], TitleNode]]] = {}
    for path_titles, node in _iter_printed_page_parents(nodes):
        if path_titles in matches:
            continue
        offset = _descendant_regime_offset(node, path_titles, matches)
        if offset is None:
            continue
        by_offset.setdefault(offset, []).append((path_titles, node))

    out: dict[tuple[str, ...], TitleMatch] = {}
    for offset, group in by_offset.items():
        for path_titles, match in bulk_offset_matches(group, offset).items():
            if 1 <= match.page <= page_count:
                out[path_titles] = replace(
                    match,
                    evidence={**match.evidence, "parent_backfill": True},
                )
    return out


def _recalibrate_after_breakpoint(
    *,
    entry_node: TitleNode,
    old_offset: int,
    ctx: ToolContext,
    page_count: int,
) -> int | None:
    """Probe offsets old_offset+1, +2, ... to find new offset after breakpoint.

    Monotonicity guarantees new offset > old offset, so search space is tiny.
    """
    entry_printed_page = entry_node.printed_page
    if entry_printed_page is None:
        return None
    for delta in range(1, _MAX_RECALIBRATE_DELTA + 1):
        new_offset = old_offset + delta
        if _vlm_confirm_single_page(
            ctx=ctx,
            title=entry_node.title,
            expected_page=entry_printed_page + new_offset,
            page_count=page_count,
        ):
            logger.info(
                "[structure_anchoring] recalibrate: title={!r} new_offset={} (delta=+{})",
                entry_node.title,
                new_offset,
                delta,
            )
            return new_offset
    return None


def offset_guided_anchoring(
    *,
    nodes: list[TitleNode],
    offset: int,
    ctx: ToolContext,
    page_count: int,
    calibration_overrides: dict[tuple[str, ...], TitleMatch],
) -> dict[tuple[str, ...], TitleMatch] | None:
    """Offset-guided bulk anchoring with recursive recalibrate on breakpoints.

    Strategy:
      1. Tail verify last leaf with current offset
      2. If pass → bulk apply all leaves (Theorem 1)
      3. If fail → binary search for breakpoint
      4. Bulk apply leaves before breakpoint
      5. Recalibrate: probe remaining[0] with offset+1, +2, ... (monotonicity)
      6. Recurse on remaining segment with new offset
      7. If recalibrate fails → return partial (caller falls back for remainder)

    Returns match_overrides for all anchored leaves, or None for full fallback.
    """
    leaves = [
        (path, node)
        for path, node in iter_leaf_title_nodes(nodes)
        if node.printed_page is not None
    ]
    if not leaves:
        return dict(calibration_overrides) or None

    all_matches: dict[tuple[str, ...], TitleMatch] = {}
    all_matches.update(calibration_overrides)

    # Single-leaf regimes (roman front-matter, F-1 appendix, …) still get a
    # deterministic printed→physical override; Phase-1 already calibrated them.
    if len(leaves) == 1:
        all_matches.update(bulk_offset_matches(leaves, offset))
    else:
        _anchor_segment_recursive(
            leaves=leaves,
            offset=offset,
            ctx=ctx,
            page_count=page_count,
            matches=all_matches,
            depth=0,
        )

    if not all_matches:
        return None

    logger.info(
        "[structure_anchoring] offset bulk anchoring: {} / {} leaves anchored",
        len(all_matches),
        len(leaves),
    )
    return all_matches


def _anchor_segment_recursive(
    *,
    leaves: list[tuple[tuple[str, ...], TitleNode]],
    offset: int,
    ctx: ToolContext,
    page_count: int,
    matches: dict[tuple[str, ...], TitleMatch],
    depth: int,
) -> None:
    """Recursively anchor a segment of leaves, handling multiple breakpoints."""
    if not leaves or depth >= _MAX_RECALIBRATE_DEPTH:
        return

    if _verify_offset_tail(leaves=leaves, offset=offset, ctx=ctx, page_count=page_count):
        bulk = bulk_offset_matches(leaves, offset)
        matches.update(bulk)
        return

    bp = _bisect_offset_breakpoint(leaves=leaves, offset=offset, ctx=ctx, page_count=page_count)
    confirmed_leaves = leaves[: bp + 1]
    if confirmed_leaves:
        bulk = bulk_offset_matches(confirmed_leaves, offset)
        matches.update(bulk)

    remaining = leaves[bp + 1:]
    if not remaining:
        return

    _, first_remaining_node = remaining[0]
    new_offset = _recalibrate_after_breakpoint(
        entry_node=first_remaining_node,
        old_offset=offset,
        ctx=ctx,
        page_count=page_count,
    )
    if new_offset is None:
        return

    _anchor_segment_recursive(
        leaves=remaining,
        offset=new_offset,
        ctx=ctx,
        page_count=page_count,
        matches=matches,
        depth=depth + 1,
    )


@dataclass
class SkeletonAnchor:
    offset: int | None
    offset_status: str
    match_overrides: dict[tuple[str, ...], TitleMatch]
    null_page_report: list[dict[str, Any]]
    bulk_count: int
    pruned_count: int = 0
    locate_agent: str = "offset_only"


def serialize_title_match(match: TitleMatch) -> dict[str, Any]:
    return {
        "page": match.page,
        "confidence": match.confidence,
        "source": match.source,
        "matched_line": match.matched_line,
        "score": match.score,
        "candidates": list(match.candidates),
        "evidence": dict(match.evidence or {}),
    }


def serialize_skeleton_anchor(anchor: SkeletonAnchor) -> dict[str, Any]:
    """JSON-friendly SkeletonAnchor (path tuples joined by ' / ')."""
    overrides: dict[str, Any] = {}
    for path, match in (anchor.match_overrides or {}).items():
        key = " / ".join(str(part) for part in path)
        overrides[key] = serialize_title_match(match)
    return {
        "offset": anchor.offset,
        "offset_status": anchor.offset_status,
        "match_overrides": overrides,
        "null_page_report": list(anchor.null_page_report or []),
        "bulk_count": int(anchor.bulk_count or 0),
        "pruned_count": int(anchor.pruned_count or 0),
        "locate_agent": anchor.locate_agent,
    }


def deserialize_title_match(data: dict[str, Any]) -> TitleMatch:
    return TitleMatch(
        page=int(data["page"]),
        confidence=float(data.get("confidence") or 0.0),
        source=data.get("source") or "agent_vlm",  # type: ignore[arg-type]
        matched_line=str(data.get("matched_line") or ""),
        score=float(data.get("score") or 0.0),
        candidates=[int(p) for p in (data.get("candidates") or [])],
        evidence=dict(data.get("evidence") or {}),
    )


def serialize_title_node(node: TitleNode) -> dict[str, Any]:
    return {
        "title": node.title,
        "level": node.level,
        "printed_page": node.printed_page,
        "printed_label": node.printed_label,
        "page_kind": node.page_kind,
        "physical_page_hint": node.physical_page_hint,
        "children": [serialize_title_node(child) for child in node.children],
    }


def deserialize_title_node(data: dict[str, Any]) -> TitleNode:
    children_raw = data.get("children") or []
    children = [
        deserialize_title_node(child)
        for child in children_raw
        if isinstance(child, dict)
    ]
    printed_page = data.get("printed_page")
    physical_page_hint = data.get("physical_page_hint")
    return TitleNode(
        title=str(data.get("title") or ""),
        level=int(data.get("level") or 1),
        printed_page=None if printed_page is None else int(printed_page),
        printed_label=data.get("printed_label")
        if isinstance(data.get("printed_label"), str)
        else None,
        page_kind=data.get("page_kind")
        if isinstance(data.get("page_kind"), str)
        else None,
        physical_page_hint=(
            None if physical_page_hint is None else int(physical_page_hint)
        ),
        children=children,
    )


def deserialize_skeleton_anchor(data: dict[str, Any]) -> SkeletonAnchor:
    raw_overrides = data.get("match_overrides") or {}
    overrides: dict[tuple[str, ...], TitleMatch] = {}
    if isinstance(raw_overrides, dict):
        for key, value in raw_overrides.items():
            if not isinstance(value, dict):
                continue
            if isinstance(key, str):
                path = tuple(part.strip() for part in key.split(" / ") if part.strip())
            elif isinstance(key, (list, tuple)):
                path = tuple(str(part) for part in key)
            else:
                continue
            if path:
                overrides[path] = deserialize_title_match(value)
    return SkeletonAnchor(
        offset=data.get("offset") if data.get("offset") is None else int(data["offset"]),
        offset_status=str(data.get("offset_status") or "failed"),
        match_overrides=overrides,
        null_page_report=list(data.get("null_page_report") or []),
        bulk_count=int(data.get("bulk_count") or 0),
        pruned_count=int(data.get("pruned_count") or 0),
        locate_agent=str(data.get("locate_agent") or "offset_only"),
    )


def anchor_hierarchy_from_offset(
    *,
    nodes: list[TitleNode],
    offset_hint: int | None,
    calibration_overrides: dict[tuple[str, ...], TitleMatch] | None = None,
    page_texts: dict[int, str],
    body_pages: list[int],
    page_count: int,
    ctx: ToolContext | None,
) -> tuple[list[TitleNode], SkeletonAnchor]:
    """Production prune → bulk → null-page given a precomputed offset.

    Phase-2 entry after Agent ``calibrate_offset`` (Phase-1).
    """
    seed_overrides = dict(calibration_overrides or {})
    pruned_count = 0
    working = nodes
    if offset_hint is not None:
        working, pruned_count = prune_out_of_scope_nodes(
            working, offset=offset_hint, page_count=page_count
        )

    offset_matches: dict[tuple[str, ...], TitleMatch] | None = None
    if offset_hint is not None and ctx is not None and working:
        offset_matches = offset_guided_anchoring(
            nodes=working,
            offset=offset_hint,
            ctx=ctx,
            page_count=page_count,
            calibration_overrides=seed_overrides,
        )

    if offset_matches is not None:
        match_overrides = offset_matches
        locate_agent = "offset_guided_bulk"
        bulk_count = len(offset_matches)
    else:
        match_overrides = seed_overrides
        locate_agent = "offset_only"
        bulk_count = 0

    working, unanchored_removed = prune_unanchored_toc_leaves(
        working, match_overrides=match_overrides
    )
    pruned_count += unanchored_removed

    match_overrides, null_page_report = locate_null_page_parent_overrides(
        nodes=working,
        match_overrides=match_overrides,
        page_texts=page_texts,
        body_pages=body_pages,
        ctx=ctx,
    )

    if offset_hint is None:
        offset_status = "failed" if ctx is not None else "skipped"
    else:
        offset_status = "ok"

    return working, SkeletonAnchor(
        offset=offset_hint,
        offset_status=offset_status,
        match_overrides=match_overrides,
        null_page_report=null_page_report,
        bulk_count=bulk_count,
        pruned_count=pruned_count,
        locate_agent=locate_agent,
    )
