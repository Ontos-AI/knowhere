"""Build page-memory section skeletons from profile-time anatomy.

Step 1 of the page-memory native hierarchy plan:
- Full TOC-depth grep anchoring + on-demand VLM confirmation
- Section boundaries come purely from TOC anchoring
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.document_agent.manifest import (
    H1Candidate,
    PageAnatomyMap,
    ToolContext,
)
from app.services.document_agent.structure.page_locate_agent import (
    PageLocateConfig,
    PageLocateResidualAgent,
    verify_section_page_choice,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    TitleMatch,
    TitleNode,
    extract_toc_nodes,
    iter_leaf_title_nodes,
    resolve_hierarchy_page_ranges,
)
from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_heading_text,
)
from loguru import logger
from shared.models.schemas.page_memory_config import PageMemoryConfig

_FRONT_TOC_REGION_GAP_PAGES = 5


@dataclass(frozen=True)
class SectionSkeleton:
    section_path: str
    level: int
    start_page: int
    end_page: int
    title: str
    parent_path: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_section_skeletons(
    *,
    anatomy: PageAnatomyMap | Any | None,
    filename: str,
    page_texts: dict[int, str],
    ctx: ToolContext | None = None,
    hierarchy_nodes: list[TitleNode] | None = None,
    page_memory_config: PageMemoryConfig | None = None,
) -> list[SectionSkeleton]:
    """Convert PageAnatomyMap hierarchy evidence into section skeletons.

    Section page ranges are anchored purely from the TOC hierarchy (every
    level, every document).
    """
    page_count = _page_count(anatomy)
    root_path = f"{filename}/Root"
    if page_count <= 0:
        return [_root_skeleton(root_path=root_path, filename=filename, page_count=0)]

    toc_selection: dict[str, Any] = {}
    pending_tocs: list[dict[str, Any]] = []
    toc_hierarchies: list[dict[str, Any]] | None = None
    if hierarchy_nodes:
        nodes = hierarchy_nodes
    else:
        toc_hierarchies, pending_tocs, toc_selection = _select_global_toc_hierarchies(
            anatomy=anatomy,
            filename=filename,
        )
        toc_nodes = extract_toc_nodes(toc_hierarchies)
        nodes = toc_nodes or _h1_nodes(anatomy)
    if not nodes:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="no_hierarchy",
            )
        ]

    # Collapse degenerate single-child intermediate chains before locate.
    # Rule: only merge a parent with its only child when that child is NOT a
    # leaf (i.e. the child still has children of its own). This preserves the
    # original leaf title so PageLocateResidualAgent can find it in the PDF.
    nodes = _collapse_intermediate_single_child_chains(nodes)

    body_pages = _body_pages(anatomy=anatomy, page_count=page_count)

    # When pending TOCs exist, limit primary scope so the last sibling's
    # end_page doesn't extend into the pending TOC region.
    primary_page_count = page_count
    primary_body_pages = body_pages
    if pending_tocs:
        pending_starts = [
            _toc_range_start(t) for t in pending_tocs if _toc_range_start(t) is not None
        ]
        if pending_starts:
            primary_page_count = min(pending_starts) - 1
            primary_body_pages = [p for p in body_pages if p <= primary_page_count]

    offset_hint, calibration_overrides = _calibrate_offset_via_vlm(
        nodes=nodes,
        toc_hierarchies=toc_hierarchies if not hierarchy_nodes else None,
        ctx=ctx,
        page_texts=page_texts,
        page_count=page_count,
    )

    # Phase A3: try offset-guided bulk anchoring before expensive residual agent.
    offset_matches: dict[tuple[str, ...], TitleMatch] | None = None
    if offset_hint is not None and ctx is not None:
        offset_matches = _offset_guided_anchoring(
            nodes=nodes,
            offset=offset_hint,
            ctx=ctx,
            page_count=page_count,
            calibration_overrides=calibration_overrides,
        )

    if offset_matches is not None:
        match_overrides = offset_matches
        locate_summary: dict[str, Any] = {
            "agent": "offset_guided_bulk",
            "offset": offset_hint,
            "bulk_count": len(offset_matches),
        }
        resolve_nodes = nodes
    else:
        # Fallback: full grep+VLM agent (kept for when calibration fails)
        locate_result = PageLocateResidualAgent(
            ctx=ctx,
            page_texts=page_texts,
            body_pages=primary_body_pages,
            page_count=page_count,
            page_offset_hint=offset_hint,
            config=(
                PageLocateConfig.from_page_memory_config(page_memory_config)
                if page_memory_config is not None
                else None
            ),
        ).prepare(nodes)
        match_overrides = {**calibration_overrides, **locate_result.match_overrides}
        locate_summary = locate_result.summary
        resolve_nodes = locate_result.nodes


    ranges = resolve_hierarchy_page_ranges(
        resolve_nodes,
        page_count=primary_page_count,
        page_texts=page_texts,
        body_pages=primary_body_pages,
        page_offset_hint=offset_hint,
        match_overrides=match_overrides,
    )
    if not ranges:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="unresolved_hierarchy",
            )
        ]

    skeletons = [
        _range_to_skeleton(
            item,
            filename=filename,
            page_count=page_count,
            locate_summary=locate_summary,
            toc_selection=toc_selection,
        )
        for item in ranges
    ]

    # Phase B: graft pending TOCs (appendix / parallel sections)
    if pending_tocs:
        secondary_skeletons = _resolve_pending_tocs(
            pending_tocs=pending_tocs,
            primary_ranges=ranges,
            ctx=ctx,
            page_texts=page_texts,
            page_count=page_count,
            filename=filename,
            body_pages=body_pages,
            page_memory_config=page_memory_config,
        )
        skeletons.extend(secondary_skeletons)

    _log_unlocated_title_warnings(filename=filename, skeletons=skeletons)
    return skeletons


def _range_to_skeleton(
    item: ResolvedHierarchyRange,
    *,
    filename: str,
    page_count: int,
    locate_summary: dict[str, Any],
    toc_selection: dict[str, Any],
) -> SectionSkeleton:
    start_page = _clamp_page(item.start_page, page_count)
    end_page = _clamp_page(item.end_page, page_count)
    path_titles = [clean_toc_title(title) or title for title in item.path_titles]
    section_path = "/".join([filename, *path_titles])
    parent_path = "/".join([filename, *path_titles[:-1]]) if len(path_titles) > 1 else filename
    evidence = {
        **item.evidence,
        "resolver": "hierarchy_locator",
        "path_titles": path_titles,
        "page_locate_summary": locate_summary,
    }
    if toc_selection:
        evidence["toc_selection"] = toc_selection
    return SectionSkeleton(
        section_path=section_path,
        level=item.level,
        start_page=start_page,
        end_page=end_page,
        title=item.title,
        parent_path=parent_path,
        evidence=evidence,
    )


# ── Single-child intermediate chain collapse ─────────────────────────────────
#
# Motivation: TOC hierarchies often contain "structural" intermediate nodes
# (category codes, volume identifiers) that add depth but carry no locatable
# text. Compressing them before locate keeps emit_depth small and lets the
# VLM/grep focus on meaningful leaf titles.
#
# Critical invariant: a node whose only child is a LEAF (no grandchildren) is
# NOT merged, so the leaf's original title survives unchanged into
# PageLocateResidualAgent. Only pure-intermediate chains are compressed.


def _collapse_intermediate_single_child_chains(
    nodes: list[TitleNode],
) -> list[TitleNode]:
    """Collapse single-child chains of intermediate (non-leaf) nodes.

    Leaf nodes (children=[]) are never absorbed into their parent title.
    """
    from dataclasses import replace as _replace

    def _collapse(node: TitleNode) -> TitleNode:
        # Recurse first (bottom-up), so grand-children are already collapsed.
        collapsed_children = [_collapse(c) for c in node.children]

        if len(collapsed_children) == 1:
            only_child = collapsed_children[0]
            # Only fold when the child is itself an intermediate node
            # (i.e. still has children). Leaf nodes are left intact.
            if only_child.children:
                merged_title = f"{node.title} {only_child.title}"
                merged_printed_page = only_child.printed_page or node.printed_page
                merged_physical_hint = (
                    only_child.physical_page_hint or node.physical_page_hint
                )
                # Promote grandchildren one level up (close the level gap).
                promoted = [
                    _replace(gc, level=max(1, gc.level - 1))
                    for gc in only_child.children
                ]
                return _replace(
                    node,
                    title=merged_title,
                    printed_page=merged_printed_page,
                    physical_page_hint=merged_physical_hint,
                    children=promoted,
                )

        return _replace(node, children=collapsed_children)

    return [_collapse(n) for n in nodes]


def _root_skeleton(
    *,
    root_path: str,
    filename: str,
    page_count: int,
    reason: str = "no_pages",
) -> SectionSkeleton:
    end_page = max(page_count, 1)
    return SectionSkeleton(
        section_path=root_path,
        level=1,
        start_page=1,
        end_page=end_page,
        title="Root",
        parent_path=filename,
        evidence={"source": "fallback_root", "reason": reason},
    )


def _page_count(anatomy: Any | None) -> int:
    return max(int(getattr(anatomy, "page_count", 0) or 0), 0)


def _toc_hierarchies(anatomy: Any | None) -> list[dict[str, Any]] | None:
    return getattr(anatomy, "toc_hierarchies", None) if anatomy is not None else None


def _select_global_toc_hierarchies(
    *,
    anatomy: Any | None,
    filename: str,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], dict[str, Any]]:
    """Split TOC hierarchies into primary (front cluster) and pending (for probe).

    Profile-time TOC extraction can find multiple TOCs in a long document.
    The front cluster is selected by physical page proximity. Remaining TOCs
    are returned as *pending* for downstream independent calibration rather
    than being unconditionally discarded.

    Returns (primary_hierarchies, pending_hierarchies, summary).
    """
    hierarchies = list(_toc_hierarchies(anatomy) or [])
    if len(hierarchies) <= 1:
        return (hierarchies or None), [], {}

    page_based = [
        hierarchy
        for hierarchy in hierarchies
        if hierarchy.get("toc_range_unit") == "page" and _toc_range_start(hierarchy) is not None
    ]
    if not page_based or len(page_based) != len(hierarchies):
        return hierarchies, [], {}

    sorted_items = sorted(enumerate(hierarchies), key=lambda item: _toc_range_start(item[1]) or 0)
    selected_indices: set[int] = set()
    pending_indices: list[int] = []
    cluster_end: int | None = None

    for original_index, hierarchy in sorted_items:
        start = _toc_range_start(hierarchy)
        end = _toc_range_end(hierarchy)
        if start is None or end is None:
            selected_indices.add(original_index)
            continue
        if cluster_end is None:
            selected_indices.add(original_index)
            cluster_end = end
            continue
        if start <= cluster_end + _FRONT_TOC_REGION_GAP_PAGES:
            selected_indices.add(original_index)
            cluster_end = max(cluster_end, end)
            continue
        pending_indices.append(original_index)

    selected = [
        hierarchy
        for index, hierarchy in enumerate(hierarchies)
        if index in selected_indices
    ]
    pending = [hierarchies[i] for i in pending_indices]

    if pending:
        logger.info(
            "[page_memory.skeleton] toc split: primary={} pending={} filename={}",
            len(selected),
            len(pending),
            filename,
        )
    summary = {
        "strategy": "front_cluster_with_pending",
        "input_count": len(hierarchies),
        "primary_count": len(selected),
        "pending_count": len(pending),
    }
    return (selected or None), pending, summary


def _toc_range_start(hierarchy: dict[str, Any]) -> int | None:
    toc_range = hierarchy.get("toc_range")
    if not isinstance(toc_range, (list, tuple)) or not toc_range:
        return None
    try:
        return int(toc_range[0])
    except (TypeError, ValueError):
        return None


def _toc_range_end(hierarchy: dict[str, Any]) -> int | None:
    toc_range = hierarchy.get("toc_range")
    if not isinstance(toc_range, (list, tuple)) or not toc_range:
        return None
    try:
        return int(toc_range[-1])
    except (TypeError, ValueError):
        return None


def _h1_nodes(anatomy: Any | None) -> list[TitleNode]:
    h1_result = getattr(anatomy, "h1_result", None)
    candidates: list[H1Candidate] = list(getattr(h1_result, "h1_candidates", []) or [])
    nodes: list[TitleNode] = []
    for candidate in sorted(candidates, key=lambda item: item.page):
        title = clean_toc_title(candidate.title) or normalize_heading_text(candidate.title)
        if not title:
            continue
        nodes.append(TitleNode(title=title, level=1, physical_page_hint=candidate.page))
    return nodes


def _body_pages(*, anatomy: Any | None, page_count: int) -> list[int]:
    excluded: set[int] = set()
    toc_result = getattr(anatomy, "toc_result", None)
    excluded.update(int(page) for page in getattr(toc_result, "toc_pages", []) or [])
    return [page for page in range(1, page_count + 1) if page not in excluded]


# ── VLM offset calibration (Phase A1) ───────────────────────────────────────

_CALIBRATION_WINDOW_PAGES = 10
_CALIBRATION_LEAF_PROBE_COUNT = 3


def _calibrate_offset_via_vlm(
    *,
    nodes: list[TitleNode],
    toc_hierarchies: list[dict[str, Any]] | None,
    ctx: ToolContext | None,
    page_texts: dict[int, str],
    page_count: int,
) -> tuple[int | None, dict[tuple[str, ...], TitleMatch]]:
    """Scan pages after the TOC to find the first leaf entry via VLM.

    Computes offset = confirmed_physical_page - printed_page.
    Returns (offset, match_overrides) where match_overrides contains the
    confirmed entry so downstream locate doesn't re-process it.
    """
    if ctx is None:
        return None, {}

    toc_physical_end = _toc_cluster_end_page(toc_hierarchies)
    if toc_physical_end is None:
        return None, {}

    scan_start = toc_physical_end + 1
    scan_end = min(scan_start + _CALIBRATION_WINDOW_PAGES - 1, page_count)
    if scan_start > page_count:
        return None, {}

    leaves = list(iter_leaf_title_nodes(nodes))
    probe_leaves = [
        (path_titles, node)
        for path_titles, node in leaves
        if node.printed_page is not None
    ][:_CALIBRATION_LEAF_PROBE_COUNT]

    if not probe_leaves:
        return None, {}

    scan_pages = list(range(scan_start, scan_end + 1))
    candidates = [
        TitleMatch(
            page=page,
            confidence=0.4,
            source="agent_heuristic",
            matched_line="",
            score=0.4,
            candidates=scan_pages,
            evidence={"calibration_probe": True},
        )
        for page in scan_pages
    ]

    for path_titles, node in probe_leaves:
        result = verify_section_page_choice(
            ctx=ctx,
            title=node.title,
            candidate_matches=candidates,
            candidate_page_cap=len(scan_pages),
        )
        selected = result.get("selected_page")
        if selected is not None and result.get("confidence", 0) >= 0.6:
            offset = selected - node.printed_page
            match = TitleMatch(
                page=selected,
                confidence=result.get("confidence", 0.75),
                source="agent_vlm",
                matched_line="",
                score=result.get("confidence", 0.75),
                candidates=[selected],
                evidence={
                    "calibration": True,
                    "printed_page": node.printed_page,
                    "reason": result.get("reason", ""),
                },
            )
            logger.info(
                "[page_memory.skeleton] calibration confirmed: title={!r} "
                "printed_page={} physical_page={} offset={}",
                node.title,
                node.printed_page,
                selected,
                offset,
            )
            return offset, {path_titles: match}

    logger.info("[page_memory.skeleton] calibration: no leaf confirmed in scan window")
    return None, {}


def _toc_cluster_end_page(toc_hierarchies: list[dict[str, Any]] | None) -> int | None:
    """Get the last physical page of the primary TOC cluster."""
    if not toc_hierarchies:
        return None
    end_pages: list[int] = []
    for hierarchy in toc_hierarchies:
        end = _toc_range_end(hierarchy)
        if end is not None:
            end_pages.append(end)
    return max(end_pages) if end_pages else None


# ── Offset-guided bulk anchoring with recursive recalibrate (Phase A3) ───────

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
    """
    tail_leaves = [
        (path, node) for path, node in reversed(leaves) if node.printed_page is not None
    ]
    if not tail_leaves:
        return True
    path, node = tail_leaves[0]
    expected_page = node.printed_page + offset
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
        "[page_memory.skeleton] tail verify: title={!r} expected_page={} confirmed={} confidence={}",
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
        "[page_memory.skeleton] bisect breakpoint: last_valid_index={} / total={}",
        lo,
        len(leaves),
    )
    return lo


def _bulk_offset_matches(
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
    for delta in range(1, _MAX_RECALIBRATE_DELTA + 1):
        new_offset = old_offset + delta
        if _vlm_confirm_single_page(
            ctx=ctx,
            title=entry_node.title,
            expected_page=entry_node.printed_page + new_offset,
            page_count=page_count,
        ):
            logger.info(
                "[page_memory.skeleton] recalibrate: title={!r} new_offset={} (delta=+{})",
                entry_node.title,
                new_offset,
                delta,
            )
            return new_offset
    return None


def _offset_guided_anchoring(
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
    if len(leaves) < 2:
        return None

    all_matches: dict[tuple[str, ...], TitleMatch] = {}
    all_matches.update(calibration_overrides)

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
        "[page_memory.skeleton] offset bulk anchoring: {} / {} leaves anchored",
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
        bulk = _bulk_offset_matches(leaves, offset)
        matches.update(bulk)
        return

    bp = _bisect_offset_breakpoint(leaves=leaves, offset=offset, ctx=ctx, page_count=page_count)
    confirmed_leaves = leaves[: bp + 1]
    if confirmed_leaves:
        bulk = _bulk_offset_matches(confirmed_leaves, offset)
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


# ── Multi-TOC grafting (Track B) ─────────────────────────────────────────────


def _resolve_pending_tocs(
    *,
    pending_tocs: list[dict[str, Any]],
    primary_ranges: list[ResolvedHierarchyRange],
    ctx: ToolContext | None,
    page_texts: dict[int, str],
    page_count: int,
    filename: str,
    body_pages: list[int],
    page_memory_config: PageMemoryConfig | None = None,
) -> list[SectionSkeleton]:
    """Independently calibrate and anchor each pending TOC, then graft results.

    Each pending TOC gets its own offset via VLM calibration + tail verify,
    then entries are bulk-anchored (or fallback to residual agent).
    Classification is PARALLEL (append at root level) or CONTAINED (skip).
    """
    if not pending_tocs or ctx is None:
        return []

    all_secondary_skeletons: list[SectionSkeleton] = []

    for i, pending_toc in enumerate(pending_tocs):
        toc_range = pending_toc.get("toc_range")
        nodes = extract_toc_nodes([pending_toc])
        if not nodes:
            continue
        nodes = _collapse_intermediate_single_child_chains(nodes)

        # Each TOC's content scope: [toc_range_end + 1, next_toc_start - 1]
        toc_end = _toc_range_end(pending_toc)
        toc_scope_start = (toc_end + 1) if toc_end is not None else None
        next_starts = [
            _toc_range_start(pending_tocs[j])
            for j in range(i + 1, len(pending_tocs))
            if _toc_range_start(pending_tocs[j]) is not None
        ]
        toc_scope_end = (min(next_starts) - 1) if next_starts else page_count
        toc_body_pages = [
            p for p in body_pages
            if p <= toc_scope_end and (toc_scope_start is None or p >= toc_scope_start)
        ]

        offset, cal_overrides = _calibrate_offset_via_vlm(
            nodes=nodes,
            toc_hierarchies=[pending_toc],
            ctx=ctx,
            page_texts=page_texts,
            page_count=toc_scope_end,
        )

        if offset is None:
            logger.info(
                "[page_memory.skeleton] pending TOC toc_range={}: calibration failed, skipping",
                toc_range,
            )
            continue

        relationship = _classify_toc_relationship(
            offset=offset,
            nodes=nodes,
            primary_ranges=primary_ranges,
            page_count=page_count,
        )
        if relationship == "unresolvable":
            logger.info(
                "[page_memory.skeleton] pending TOC toc_range={}: unresolvable, skipping",
                toc_range,
            )
            continue

        offset_matches = _offset_guided_anchoring(
            nodes=nodes,
            offset=offset,
            ctx=ctx,
            page_count=toc_scope_end,
            calibration_overrides=cal_overrides,
        )

        if offset_matches is not None:
            match_overrides = offset_matches
            locate_summary: dict[str, Any] = {
                "agent": "offset_guided_bulk",
                "offset": offset,
                "bulk_count": len(offset_matches),
                "toc_relationship": relationship,
            }
        else:
            locate_result = PageLocateResidualAgent(
                ctx=ctx,
                page_texts=page_texts,
                body_pages=toc_body_pages,
                page_count=toc_scope_end,
                page_offset_hint=offset,
                config=(
                    PageLocateConfig.from_page_memory_config(page_memory_config)
                    if page_memory_config is not None
                    else None
                ),
            ).prepare(nodes)
            match_overrides = {**cal_overrides, **locate_result.match_overrides}
            locate_summary = {**locate_result.summary, "toc_relationship": relationship}

        ranges = resolve_hierarchy_page_ranges(
            nodes,
            page_count=toc_scope_end,
            page_texts=page_texts,
            body_pages=toc_body_pages,
            page_offset_hint=offset,
            match_overrides=match_overrides,
        )

        toc_selection_info: dict[str, Any] = {
            "toc_range": toc_range,
            "offset": offset,
            "relationship": relationship,
        }
        for item in ranges:
            skeleton = _range_to_skeleton(
                item,
                filename=filename,
                page_count=toc_scope_end,
                locate_summary=locate_summary,
                toc_selection=toc_selection_info,
            )
            all_secondary_skeletons.append(skeleton)

        logger.info(
            "[page_memory.skeleton] pending TOC toc_range={}: "
            "relationship={} offset={} skeletons={}",
            toc_range,
            relationship,
            offset,
            len(ranges),
        )

    return all_secondary_skeletons


def _classify_toc_relationship(
    *,
    offset: int,
    nodes: list[TitleNode],
    primary_ranges: list[ResolvedHierarchyRange],
    page_count: int,
) -> str:
    """Classify a pending TOC as parallel or contained vs primary ranges.

    parallel: the pending TOC covers pages beyond the primary tree's *anchored*
              content (i.e. the last explicitly-located section start page).
    contained: the pending TOC's content falls strictly within a primary
              section's explicitly-anchored range.
    """
    leaves = [
        node for _, node in iter_leaf_title_nodes(nodes) if node.printed_page is not None
    ]
    if not leaves:
        return "unresolvable"

    first_physical = leaves[0].printed_page + offset
    last_physical = leaves[-1].printed_page + offset

    if first_physical < 1 or first_physical > page_count:
        return "unresolvable"

    if not primary_ranges:
        return "parallel"

    # Use the last *start_page* among primary ranges as the boundary of
    # explicitly-anchored content. The end_page of the last section is often
    # extended to page_count by default and doesn't reflect real content coverage.
    last_anchored_start = max(
        (r.start_page for r in primary_ranges if r.start_page is not None), default=0
    )

    if first_physical > last_anchored_start:
        return "parallel"

    min_level = min(r.level for r in primary_ranges)
    top_level_ranges = [r for r in primary_ranges if r.level == min_level]
    for r in top_level_ranges:
        if r.start_page and r.end_page:
            if r.start_page <= first_physical and last_physical <= r.end_page:
                return "contained"

    return "parallel"


def _clamp_page(page: int, page_count: int) -> int:
    return min(max(page, 1), max(page_count, 1))


def _log_unlocated_title_warnings(
    *,
    filename: str,
    skeletons: list[SectionSkeleton],
) -> None:
    for skeleton in skeletons:
        for warning in skeleton.evidence.get("warnings", []) or []:
            logger.warning(
                "[page_memory.skeleton] title unlocated filename={} title={!r} "
                "assigned_range={} parent_scope={} path_titles={}",
                filename,
                warning.get("title"),
                warning.get("assigned_range"),
                warning.get("parent_scope"),
                warning.get("path_titles"),
            )


def collapse_single_child_chains(
    skeletons: list[SectionSkeleton],
) -> list[SectionSkeleton]:
    """Collapse single-child chains in a flat skeleton list.

    Rebuilds the parent/child tree from ``parent_path`` references, then
    bottom-up merges any parent whose only child is itself a parent (has its
    own children).  Titles concatenate as ``"{parent.title} {child.title}"``,
    the parent keeps its own ``section_path`` and page range, and grandchildren
    are promoted one level and re-parented.

    Returns a sorted flat skeleton list.
    """
    from app.services.page_memory._utils import sort_skeletons

    if not skeletons:
        return []

    # Build child lookup: parent_path → list of children skeletons
    by_path: dict[str, SectionSkeleton] = {s.section_path: s for s in skeletons}
    children_of: dict[str, list[str]] = {}
    roots: list[str] = []

    for s in skeletons:
        pp = s.parent_path
        if pp is None or pp not in by_path:
            roots.append(s.section_path)
        else:
            children_of.setdefault(pp, []).append(s.section_path)

    # Bottom-up collapse via post-order traversal
    result: list[SectionSkeleton] = []

    def _collapse_node(path: str) -> None:
        node = by_path[path]
        child_paths = children_of.get(path, [])

        # Recurse into children first (bottom-up)
        for cp in list(child_paths):
            _collapse_node(cp)

        # Re-read children after recursive collapse may have mutated by_path
        child_paths = children_of.get(path, [])

        if len(child_paths) == 1:
            only_child_path = child_paths[0]
            only_child = by_path[only_child_path]
            grandchild_paths = children_of.get(only_child_path, [])

            # Merge: parent absorbs its only child
            collapsed_from = list(node.evidence.get("collapsed_from", []))
            collapsed_from.append(only_child_path)
            collapsed_from.extend(only_child.evidence.get("collapsed_from", []))

            merged_title = f"{node.title} {only_child.title}"
            merged_evidence = dict(node.evidence)
            merged_evidence["collapsed_from"] = collapsed_from

            merged = SectionSkeleton(
                section_path=node.section_path,
                level=node.level,
                start_page=node.start_page,
                end_page=node.end_page,
                title=merged_title,
                parent_path=node.parent_path,
                evidence=merged_evidence,
            )
            by_path[path] = merged

            # Promote grandchildren under the merged node
            new_children: list[str] = []
            for gc_path in grandchild_paths:
                gc = by_path[gc_path]
                new_path = f"{node.section_path}/{gc.title}"
                promoted = SectionSkeleton(
                    section_path=new_path,
                    level=gc.level - 1,
                    start_page=gc.start_page,
                    end_page=gc.end_page,
                    title=gc.title,
                    parent_path=node.section_path,
                    evidence=dict(gc.evidence),
                )
                by_path[new_path] = promoted
                new_children.append(new_path)
                # Transfer grandchild's children to the new path
                if gc_path in children_of:
                    children_of[new_path] = children_of.pop(gc_path)
                    # Update parent_path of great-grandchildren
                    for ggc_path in children_of.get(new_path, []):
                        ggc = by_path[ggc_path]
                        by_path[ggc_path] = SectionSkeleton(
                            section_path=ggc.section_path,
                            level=ggc.level,
                            start_page=ggc.start_page,
                            end_page=ggc.end_page,
                            title=ggc.title,
                            parent_path=new_path,
                            evidence=dict(ggc.evidence),
                        )
                # Remove old gc entry
                by_path.pop(gc_path, None)

            children_of[path] = new_children
            # Remove the absorbed child
            children_of.pop(only_child_path, None)
            by_path.pop(only_child_path, None)

    for root_path in roots:
        _collapse_node(root_path)

    # Flatten all remaining nodes
    def _collect(path: str) -> None:
        if path in by_path:
            result.append(by_path[path])
        for cp in children_of.get(path, []):
            _collect(cp)

    for root_path in roots:
        _collect(root_path)

    return sort_skeletons(result)
