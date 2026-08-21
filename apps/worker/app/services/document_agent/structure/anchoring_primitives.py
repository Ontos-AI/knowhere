"""Shared hierarchy anchoring: Phase-2 bulk/bisect/null-page + SkeletonAnchor.
Phase-1 offset discovery lives in ``document_agent.calibration``.
``anchor_hierarchy`` composes Phase-1 + Phase-2 for production callers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    iter_leaf_title_nodes,
)
from app.services.document_agent.structure.null_page_react import (
    locate_null_page_node_overrides,
)
from app.services.document_agent.structure.section_page_verify import (
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
    keep_null_page_nodes: bool = False,
) -> tuple[list[TitleNode], int]:
    """Remove TOC nodes that have no physical ``match_overrides`` entry.

    Implements Phase-2 ``suffix = no TOC``: after bulk/bisect/recalibrate, any
    leaf that was not successfully anchored is dropped from the coarse tree
    instead of sticky ``inherited_unlocated`` ranges. Childless parents are
    removed unless they themselves have an override.

    When ``keep_null_page_nodes`` is True (pre null-page ReAct), nodes with
    ``printed_page is None`` are retained so they can be probed. Call again with
    the default after locate to drop still-unanchored null-page nodes.
    """
    removed = 0

    def _keep(path: tuple[str, ...], node: TitleNode) -> bool:
        if path in match_overrides:
            return True
        return keep_null_page_nodes and node.printed_page is None

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
            if _keep(path, node):
                return replace(node, children=[])
            removed += 1
            return None
        if _keep(path, node):
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
            "(keep_null_page_nodes={} → no TOC)",
            removed,
            keep_null_page_nodes,
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


# Null-page locate lives in ``null_page_react.locate_null_page_node_overrides``.
# Imported above for ``anchor_hierarchy_from_offset``.


# ── Offset-guided bulk anchoring with recursive recalibrate (Phase-2) ───────

_MAX_RECALIBRATE_DEPTH = 5


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
        source="inspect_vlm",
        matched_line="",
        candidates=[expected_page],
        evidence={"tail_verify_probe": True},
    )
    result = verify_section_page_choice(
        ctx=ctx,
        title=node.title,
        candidate_matches=[candidate],
        candidate_page_cap=1,
    )
    confirmed = result.get("selected_page") == expected_page
    logger.info(
        "[structure_anchoring] tail verify: title={!r} expected_page={} confirmed={}",
        node.title,
        expected_page,
        confirmed,
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
        source="inspect_vlm",
        matched_line="",
        candidates=[expected_page],
        evidence={"bisect_probe": True},
    )
    result = verify_section_page_choice(
        ctx=ctx,
        title=title,
        candidate_matches=[candidate],
        candidate_page_cap=1,
    )
    return result.get("selected_page") == expected_page



def _bisect_offset_breakpoint(
    *,
    leaves: list[tuple[tuple[str, ...], TitleNode]],
    offset: int,
    ctx: ToolContext,
    page_count: int,
) -> int:
    """Return the last leaf index where ``offset`` holds, or ``-1`` if none do.

    Does not assume the first leaf is valid: every candidate index is confirmed
    (or rejected) before it can become the breakpoint. O(log n) VLM calls.
    """
    if not leaves:
        return -1
    lo, hi = 0, len(leaves) - 1
    last_valid = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        _, node = leaves[mid]
        if node.printed_page is None:
            hi = mid - 1
            continue
        expected = node.printed_page + offset
        if _vlm_confirm_single_page(
            ctx=ctx, title=node.title, expected_page=expected, page_count=page_count
        ):
            last_valid = mid
            lo = mid + 1
        else:
            hi = mid - 1
    logger.info(
        "[structure_anchoring] bisect breakpoint: last_valid_index={} / total={}",
        last_valid,
        len(leaves),
    )
    return last_valid


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
            source="bulk_offset",
            matched_line="",
            candidates=[page],
            evidence={
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
    """Re-find offset for the first remaining leaf after a breakpoint.

    Same mechanic as Phase-1: forward ``inspect.pages`` scan until the title
    START is found. Monotonicity says the physical page is strictly after the
    failed ``printed + old_offset`` slot, so the scan cursor starts there.
    """
    entry_printed_page = entry_node.printed_page
    if entry_printed_page is None:
        return None

    # Lazy import: scan → inspect_pages → tools must not load at module import.
    from app.services.document_agent.calibration.scan import scan_title_forward

    start_page = entry_printed_page + old_offset + 1
    if start_page > page_count:
        return None

    scan = scan_title_forward(
        ctx=ctx,
        title=entry_node.title,
        start_page=start_page,
        page_count=page_count,
    )
    if not scan.found or scan.found_page is None:
        logger.info(
            "[structure_anchoring] recalibrate miss: title={!r} start={} scanned={}",
            entry_node.title,
            start_page,
            scan.scanned_pages,
        )
        return None

    new_offset = int(scan.found_page) - entry_printed_page
    if new_offset <= old_offset:
        logger.info(
            "[structure_anchoring] recalibrate rejected non-monotonic offset: "
            "title={!r} old={} new={} found_page={}",
            entry_node.title,
            old_offset,
            new_offset,
            scan.found_page,
        )
        return None

    logger.info(
        "[structure_anchoring] recalibrate: title={!r} new_offset={} "
        "(delta=+{}, found_page={})",
        entry_node.title,
        new_offset,
        new_offset - old_offset,
        scan.found_page,
    )
    return new_offset


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
      3. If fail → binary search for last valid index (``-1`` if none)
      4. Bulk apply only verified prefix (empty when breakpoint is ``-1``)
      5. Recalibrate remaining[0] via Phase-1 forward scan from printed+old+1
      6. Recurse on remaining segment with new offset
      7. If recalibrate fails → keep prefix only (caller prunes the rest)

    Returns match_overrides for anchored leaves (including Phase-1 seeds), or
    None when nothing was anchored.
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

    bp = _bisect_offset_breakpoint(
        leaves=leaves, offset=offset, ctx=ctx, page_count=page_count
    )
    # bp == -1 → no leaf confirmed under this offset; do not invent a prefix.
    confirmed_leaves = leaves[: bp + 1] if bp >= 0 else []
    if confirmed_leaves:
        matches.update(bulk_offset_matches(confirmed_leaves, offset))

    remaining = leaves[bp + 1 :] if bp >= 0 else list(leaves)
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
    locate_method: str = "offset_only"
    source: str = ""


def serialize_title_match(match: TitleMatch) -> dict[str, Any]:
    return {
        "page": match.page,
        "source": match.source,
        "matched_line": match.matched_line,
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
        "locate_method": anchor.locate_method,
        "source": anchor.source,
    }


def deserialize_title_match(data: dict[str, Any]) -> TitleMatch:
    return TitleMatch(
        page=int(data["page"]),
        source=str(data.get("source") or "bulk_offset"),  # type: ignore[arg-type]
        matched_line=str(data.get("matched_line") or ""),
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
        locate_method=str(data.get("locate_method") or "offset_only"),
        source=str(data.get("source") or ""),
    )


def _iter_all_title_nodes(
    nodes: list[TitleNode],
    *,
    parent_titles: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], TitleNode]]:
    rows: list[tuple[tuple[str, ...], TitleNode]] = []
    for node in nodes:
        path = (*parent_titles, node.title)
        rows.append((path, node))
        if node.children:
            rows.extend(_iter_all_title_nodes(node.children, parent_titles=path))
    return rows


def _filter_overrides_to_tree(
    nodes: list[TitleNode],
    match_overrides: dict[tuple[str, ...], TitleMatch],
) -> dict[tuple[str, ...], TitleMatch]:
    if not nodes:
        return {}
    surviving = {path for path, _node in _iter_all_title_nodes(nodes)}
    return {
        path: match
        for path, match in match_overrides.items()
        if path in surviving
    }


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
    """Production prune → bulk → null-page ReAct → final prune.

    Phase-2 entry after ``calibrate_offset`` (Phase-1).
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
        locate_method = "offset_guided_bulk"
        bulk_count = len(offset_matches)
    else:
        match_overrides = seed_overrides
        locate_method = "offset_only"
        bulk_count = 0

    working, unanchored_removed = prune_unanchored_toc_leaves(
        working,
        match_overrides=match_overrides,
        keep_null_page_nodes=True,
    )
    pruned_count += unanchored_removed
    match_overrides = _filter_overrides_to_tree(working, match_overrides)

    match_overrides, null_page_report = locate_null_page_node_overrides(
        nodes=working,
        match_overrides=match_overrides,
        page_texts=page_texts,
        body_pages=body_pages,
        ctx=ctx,
    )

    working, failed_null_removed = prune_unanchored_toc_leaves(
        working,
        match_overrides=match_overrides,
        keep_null_page_nodes=False,
    )
    pruned_count += failed_null_removed
    match_overrides = _filter_overrides_to_tree(working, match_overrides)

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
        locate_method=locate_method,
    )
