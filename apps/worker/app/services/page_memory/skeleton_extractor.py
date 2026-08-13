"""Build page-memory section skeletons from profile-time anatomy.

Section boundaries come from TOC anchoring persisted on anatomy
(``skeleton_anchor``). This module only resolves page ranges.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.document_agent.manifest import (
    PageAnatomyMap,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    extract_toc_nodes,
    resolve_hierarchy_page_ranges,
)
from app.services.document_agent.structure.anchoring_primitives import (
    deserialize_skeleton_anchor,
    deserialize_title_node,
    toc_range_start,
)
from app.services.document_agent.structure.toc_anchoring import (
    body_pages_excluding_toc,
    pending_toc_body_scope,
    select_global_toc_hierarchies,
)
from loguru import logger
from shared.services.chunks.path_segments import (
    append_document_path,
    join_document_path,
)




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
) -> list[SectionSkeleton]:
    """Convert PageAnatomyMap hierarchy evidence into section skeletons.

    Section page ranges are anchored purely from the TOC hierarchy (every
    level, every document). Calibration runs at PROFILE; this stage only
    resolves persisted ``skeleton_anchor``.
    """
    page_count = _page_count(anatomy)
    root_path = f"{filename}/Root"
    if page_count <= 0:
        return [_root_skeleton(root_path=root_path, filename=filename, page_count=0)]

    toc_hierarchies, pending_tocs, toc_selection = select_global_toc_hierarchies(
        hierarchies=list(getattr(anatomy, "toc_hierarchies", None) or []),
        filename=filename,
    )
    toc_nodes = extract_toc_nodes(toc_hierarchies)
    if not toc_nodes:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="no_toc",
            )
        ]

    skeleton_anchor_raw = getattr(anatomy, "skeleton_anchor", None)
    skeleton_nodes_raw = getattr(anatomy, "skeleton_nodes", None)
    if not isinstance(skeleton_anchor_raw, dict) or not isinstance(
        skeleton_nodes_raw, list
    ):
        raise ValueError("anatomy.skeleton_anchor/skeleton_nodes missing after TOC extract")

    skeleton_anchor = deserialize_skeleton_anchor(skeleton_anchor_raw)
    resolve_nodes = [
        deserialize_title_node(node)
        for node in skeleton_nodes_raw
        if isinstance(node, dict)
    ]
    if skeleton_anchor.pruned_count and not resolve_nodes:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="all_toc_nodes_out_of_scope",
            )
        ]

    toc_result = getattr(anatomy, "toc_result", None)
    body_pages = body_pages_excluding_toc(
        getattr(toc_result, "toc_pages", None),
        page_count,
    )
    primary_page_count = page_count
    primary_body_pages = body_pages
    if pending_tocs:
        pending_starts: list[int] = []
        for pending_toc in pending_tocs:
            start = toc_range_start(pending_toc)
            if start is not None:
                pending_starts.append(start)
        if pending_starts:
            primary_page_count = min(pending_starts) - 1
            primary_body_pages = [page for page in body_pages if page <= primary_page_count]

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

    pending_records = list(getattr(anatomy, "pending_skeleton_anchors", None) or [])
    if pending_tocs and pending_records:
        secondary_skeletons = _resolve_pending_tocs(
            pending_tocs=pending_tocs,
            pending_records=pending_records,
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


def _resolve_pending_tocs(
    *,
    pending_tocs: list[dict[str, Any]],
    pending_records: list[dict[str, Any]],
    page_texts: dict[int, str],
    page_count: int,
    filename: str,
    body_pages: list[int],
) -> list[SectionSkeleton]:
    """Graft pending TOCs from PROFILE-persisted skeleton anchors."""
    if not pending_tocs or not pending_records:
        return []

    records_by_range: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in pending_records:
        toc = record.get("toc")
        if not isinstance(toc, dict):
            continue
        key = tuple(toc.get("toc_range") or [])
        records_by_range[key] = record

    all_secondary_skeletons: list[SectionSkeleton] = []

    for i, pending_toc in enumerate(pending_tocs):
        toc_range = pending_toc.get("toc_range")
        record = records_by_range.get(tuple(toc_range or []))
        if record is None:
            continue
        relationship = record.get("relationship")
        if relationship == "unresolvable":
            logger.info(
                "[page_memory.skeleton] pending TOC toc_range={}: unresolvable, skipping",
                toc_range,
            )
            continue
        if relationship not in {"parallel", "contained"}:
            raise ValueError(
                "pending TOC relationship missing after PROFILE classify"
            )
        resolve_nodes_raw = record.get("nodes") or []
        resolve_nodes = [
            deserialize_title_node(node)
            for node in resolve_nodes_raw
            if isinstance(node, dict)
        ]
        skeleton_anchor_raw = record.get("skeleton_anchor")
        if not isinstance(skeleton_anchor_raw, dict) or not resolve_nodes:
            raise ValueError(
                "pending TOC skeleton_anchor/nodes missing after PROFILE classify"
            )
        skeleton_anchor = deserialize_skeleton_anchor(skeleton_anchor_raw)
        offset = skeleton_anchor.offset
        if offset is None:
            raise ValueError("pending TOC offset missing after PROFILE classify")

        toc_scope_end, toc_body_pages = pending_toc_body_scope(
            pending_tocs=pending_tocs,
            index=i,
            page_count=page_count,
            body_pages=body_pages,
        )
        match_overrides = skeleton_anchor.match_overrides
        null_page_report = skeleton_anchor.null_page_report
        locate_summary: dict[str, Any] = {
            "agent": skeleton_anchor.locate_agent,
            "offset": skeleton_anchor.offset,
            "bulk_count": skeleton_anchor.bulk_count,
            "pruned_out_of_scope": skeleton_anchor.pruned_count,
            "toc_relationship": relationship,
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
