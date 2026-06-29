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
    PageLocateResidualAgent,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    TitleNode,
    extract_toc_nodes,
    resolve_hierarchy_page_ranges,
)
from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_heading_text,
)
from loguru import logger

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
    if hierarchy_nodes:
        nodes = hierarchy_nodes
    else:
        toc_hierarchies, toc_selection = _select_global_toc_hierarchies(
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
    offset_hint = _estimate_page_offset(nodes=nodes, anatomy=anatomy)
    locate_result = PageLocateResidualAgent(
        ctx=ctx,
        page_texts=page_texts,
        body_pages=body_pages,
        page_count=page_count,
        page_offset_hint=offset_hint,
    ).prepare(nodes)
    ranges = resolve_hierarchy_page_ranges(
        locate_result.nodes,
        page_count=page_count,
        page_texts=page_texts,
        body_pages=body_pages,
        page_offset_hint=offset_hint,
        match_overrides=locate_result.match_overrides,
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
            locate_summary=locate_result.summary,
            toc_selection=toc_selection,
        )
        for item in ranges
    ]
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
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Keep the front/global TOC cluster and skip later embedded TOCs.

    Profile-time TOC extraction can find local TOCs inside a long document
    (for example, an embedded standard with its own English outline). Page
    memory C4 currently emits a document-level skeleton, so later page-based
    TOC regions must not be concatenated as root siblings.
    """
    hierarchies = list(_toc_hierarchies(anatomy) or [])
    if len(hierarchies) <= 1:
        return (hierarchies or None), {}

    page_based = [
        hierarchy
        for hierarchy in hierarchies
        if hierarchy.get("toc_range_unit") == "page" and _toc_range_start(hierarchy) is not None
    ]
    if not page_based or len(page_based) != len(hierarchies):
        return hierarchies, {}

    sorted_items = sorted(enumerate(hierarchies), key=lambda item: _toc_range_start(item[1]) or 0)
    selected_indices: set[int] = set()
    skipped: list[dict[str, Any]] = []
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
        skipped.append(
            {
                "index": original_index,
                "toc_range": [start, end],
                "scan_range": hierarchy.get("scan_range"),
                "reason": "embedded_toc_region_outside_front_cluster",
            }
        )

    selected = [
        hierarchy
        for index, hierarchy in enumerate(hierarchies)
        if index in selected_indices
    ]
    if skipped:
        logger.warning(
            "[page_memory.skeleton] skipped embedded toc regions filename={} skipped={}",
            filename,
            skipped,
        )
    summary = {
        "strategy": "front_page_toc_cluster",
        "input_count": len(hierarchies),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "skipped": skipped,
    }
    return (selected or None), summary


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


def _estimate_page_offset(*, nodes: list[TitleNode], anatomy: Any | None) -> int | None:
    printed_by_title: dict[str, int] = {}
    for node in _walk_nodes(nodes):
        if node.printed_page is None:
            continue
        printed_by_title[_title_key(node.title)] = node.printed_page

    offsets: list[int] = []
    h1_result = getattr(anatomy, "h1_result", None)
    for candidate in getattr(h1_result, "h1_candidates", []) or []:
        printed_page = printed_by_title.get(_title_key(candidate.title))
        if printed_page is not None:
            offsets.append(int(candidate.page) - printed_page)
    if not offsets:
        return None
    offsets.sort()
    return offsets[len(offsets) // 2]


def _walk_nodes(nodes: list[TitleNode]) -> list[TitleNode]:
    walked: list[TitleNode] = []
    for node in nodes:
        walked.append(node)
        walked.extend(_walk_nodes(node.children))
    return walked


def _title_key(title: str) -> str:
    return normalize_heading_text(clean_toc_title(title) or title).casefold()


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
