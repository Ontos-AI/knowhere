"""Build page-memory section skeletons from profile-time anatomy.

Step 1 of the page-memory native hierarchy plan:
- Full TOC-depth grep anchoring + on-demand VLM confirmation
- Section boundaries come purely from TOC anchoring
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.document_agent.manifest import (
    PageAnatomyMap,
    ToolContext,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    TitleNode,
    extract_toc_nodes,
    iter_leaf_title_nodes,
    resolve_hierarchy_page_ranges,
)
from app.services.document_agent.structure.structure_anchoring import (
    anchor_hierarchy,
    calibrate_offset,
    locate_null_page_parent_overrides,
    offset_guided_anchoring,
    toc_range_end,
    toc_range_start,
)
from loguru import logger
from shared.services.chunks.path_segments import (
    append_document_path,
    join_document_path,
)

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
        if not toc_nodes:
            # TODO: explore lightweight hierarchy inference for no-TOC documents
            # (e.g. heading font-size clustering, visual layout analysis).
            # For now, no TOC → flat page tagging + asset extraction only.
            return [
                _root_skeleton(
                    root_path=root_path,
                    filename=filename,
                    page_count=page_count,
                    reason="no_toc",
                )
            ]
        nodes = toc_nodes

    # Collapse degenerate single-child intermediate chains before locate.
    # Rule: only merge a parent with its only child when that child is NOT a
    # leaf (i.e. the child still has children of its own). This preserves the
    # original leaf title so offset-guided anchoring can find it in the PDF.
    nodes = _collapse_intermediate_single_child_chains(nodes)

    body_pages = _body_pages(anatomy=anatomy, page_count=page_count)

    # When pending TOCs exist, limit primary scope so the last sibling's
    # end_page doesn't extend into the pending TOC region.
    primary_page_count = page_count
    primary_body_pages = body_pages
    if pending_tocs:
        pending_starts: list[int] = []
        for t in pending_tocs:
            start = toc_range_start(t)
            if start is not None:
                pending_starts.append(start)
        if pending_starts:
            primary_page_count = min(pending_starts) - 1
            primary_body_pages = [p for p in body_pages if p <= primary_page_count]

    resolve_nodes, skeleton_anchor = anchor_hierarchy(
        nodes=nodes,
        toc_hierarchies=toc_hierarchies if not hierarchy_nodes else None,
        page_texts=page_texts,
        body_pages=primary_body_pages,
        page_count=page_count,
        ctx=ctx,
    )
    if skeleton_anchor.pruned_count and not resolve_nodes:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="all_toc_nodes_out_of_scope",
            )
        ]

    match_overrides = skeleton_anchor.match_overrides
    null_page_report = skeleton_anchor.null_page_report
    if skeleton_anchor.locate_agent == "offset_guided_bulk":
        locate_summary: dict[str, Any] = {
            "agent": "offset_guided_bulk",
            "offset": skeleton_anchor.offset,
            "bulk_count": skeleton_anchor.bulk_count,
            "pruned_out_of_scope": skeleton_anchor.pruned_count,
        }
    else:
        locate_summary = {
            "agent": "offset_only",
            "offset": skeleton_anchor.offset,
            "reason": "offset_guided_anchoring_skipped_or_empty",
            "pruned_out_of_scope": skeleton_anchor.pruned_count,
        }
    locate_summary["null_page_parent_locate"] = {
        "attempted": len(null_page_report),
        "located": sum(1 for row in null_page_report if row.get("page") is not None),
        "unresolved": sum(
            1 for row in null_page_report if row.get("result") == "unresolved"
        ),
        "visual_verify_calls": sum(
            int(row.get("visual_verify_calls") or 0) for row in null_page_report
        ),
        "entries": null_page_report,
    }

    ranges = resolve_hierarchy_page_ranges(
        resolve_nodes,
        page_count=primary_page_count,
        page_texts=page_texts,
        body_pages=primary_body_pages,
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
    # Keep original TOC titles (incl. numbering) in section_path / HIERARCHY.
    path_titles = [str(title).strip() for title in item.path_titles if str(title).strip()]
    section_path = join_document_path([filename, *path_titles])
    parent_path = (
        join_document_path([filename, *path_titles[:-1]])
        if len(path_titles) > 1
        else filename
    )
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
# offset-guided anchoring focus on meaningful leaf titles.
#
# Critical invariant: a node whose only child is a LEAF (no grandchildren) is
# NOT merged, so the leaf's original title survives unchanged into
# offset-guided anchoring. Only pure-intermediate chains are compressed.


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
        if hierarchy.get("toc_range_unit") == "page" and toc_range_start(hierarchy) is not None
    ]
    if not page_based or len(page_based) != len(hierarchies):
        return hierarchies, [], {}

    sorted_items = sorted(enumerate(hierarchies), key=lambda item: toc_range_start(item[1]) or 0)
    selected_indices: set[int] = set()
    pending_indices: list[int] = []
    cluster_end: int | None = None

    for original_index, hierarchy in sorted_items:
        start = toc_range_start(hierarchy)
        end = toc_range_end(hierarchy)
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




def _body_pages(*, anatomy: Any | None, page_count: int) -> list[int]:
    excluded: set[int] = set()
    toc_result = getattr(anatomy, "toc_result", None)
    excluded.update(int(page) for page in getattr(toc_result, "toc_pages", []) or [])
    return [page for page in range(1, page_count + 1) if page not in excluded]




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
        toc_end = toc_range_end(pending_toc)
        toc_scope_start = (toc_end + 1) if toc_end is not None else None
        next_starts: list[int] = []
        for j in range(i + 1, len(pending_tocs)):
            start = toc_range_start(pending_tocs[j])
            if start is not None:
                next_starts.append(start)
        toc_scope_end = (min(next_starts) - 1) if next_starts else page_count
        toc_body_pages = [
            p for p in body_pages
            if p <= toc_scope_end and (toc_scope_start is None or p >= toc_scope_start)
        ]

        offset, cal_overrides = calibrate_offset(
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

        offset_matches = offset_guided_anchoring(
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
            match_overrides = cal_overrides
            locate_summary = {
                "agent": "offset_only",
                "offset": offset,
                "toc_relationship": relationship,
                "reason": "offset_guided_anchoring_skipped_or_empty",
            }

        match_overrides, null_page_report = locate_null_page_parent_overrides(
            nodes=nodes,
            match_overrides=match_overrides,
            page_texts=page_texts,
            body_pages=toc_body_pages,
            ctx=ctx,
        )
        locate_summary["null_page_parent_locate"] = {
            "attempted": len(null_page_report),
            "located": sum(1 for row in null_page_report if row.get("page") is not None),
            "unresolved": sum(
                1 for row in null_page_report if row.get("result") == "unresolved"
            ),
            "visual_verify_calls": sum(
                int(row.get("visual_verify_calls") or 0) for row in null_page_report
            ),
            "entries": null_page_report,
        }

        ranges = resolve_hierarchy_page_ranges(
            nodes,
            page_count=toc_scope_end,
            page_texts=page_texts,
            body_pages=toc_body_pages,
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

    first_printed = leaves[0].printed_page
    last_printed = leaves[-1].printed_page
    if first_printed is None or last_printed is None:
        return "unresolvable"
    first_physical = first_printed + offset
    last_physical = last_printed + offset

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
                new_path = append_document_path(node.section_path, gc.title)
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
