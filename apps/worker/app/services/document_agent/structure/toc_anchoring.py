"""Profile-time TOC anchoring: run existing calibration after TOC extract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    anchor_hierarchy_from_offset,
    deserialize_skeleton_anchor,
    deserialize_title_node,
    serialize_skeleton_anchor,
    serialize_title_node,
    toc_range_end,
    toc_range_start,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    TitleMatch,
    TitleNode,
    collapse_intermediate_single_child_chains,
    coverage_by_path,
    deepest_covering_path,
    extract_toc_nodes,
    iter_leaf_title_nodes,
    normalize_heading_text,
    resolve_hierarchy_page_ranges,
)

_FRONT_TOC_REGION_GAP_PAGES = 5
_LOG_PREFIX = "[profile.toc_anchoring]"


def run_toc_anchoring(ctx: ToolContext) -> None:
    """Anchor extracted TOC hierarchies onto the profile blackboard."""
    from app.services.document_agent.calibration.orchestrator import (
        anchor_hierarchy,
    )

    page_count = int(ctx.blackboard.page_count or 0)
    hierarchies = list(ctx.blackboard.toc_hierarchies or [])
    if page_count <= 0 or not hierarchies:
        return

    page_texts = dict(ctx.blackboard.page_full_text_cache)
    if not page_texts:
        raise ValueError(
            "page_full_text_cache missing; run text scan before TOC anchoring"
        )
    filename = Path(ctx.pdf_path).name
    primary, pending, _summary = select_global_toc_hierarchies(
        hierarchies=hierarchies,
        filename=filename,
    )
    nodes = extract_toc_nodes(primary)
    if not nodes:
        return

    nodes = collapse_intermediate_single_child_chains(nodes)
    toc_result = ctx.blackboard.toc_result
    body_pages = body_pages_excluding_toc(
        getattr(toc_result, "toc_pages", None),
        page_count,
    )

    resolve_nodes, skeleton_anchor = anchor_hierarchy(
        nodes=nodes,
        toc_hierarchies=primary,
        page_texts=page_texts,
        body_pages=body_pages,
        page_count=page_count,
        ctx=ctx,
    )
    pending_records: list[dict[str, Any]] = []
    if pending:
        primary_ranges = resolve_hierarchy_page_ranges(
            resolve_nodes,
            page_count=page_count,
            page_texts=page_texts,
            body_pages=body_pages,
            match_overrides=skeleton_anchor.match_overrides,
        )
        pending_records = _anchor_pending_tocs(
            pending_tocs=pending,
            ctx=ctx,
            page_texts=page_texts,
            page_count=page_count,
            body_pages=body_pages,
            primary_ranges=primary_ranges,
        )
        resolve_nodes, skeleton_anchor = _graft_contained_pending(
            resolve_nodes=resolve_nodes,
            skeleton_anchor=skeleton_anchor,
            pending_records=pending_records,
            page_count=page_count,
            page_texts=page_texts,
            body_pages=body_pages,
        )
    ctx.blackboard.skeleton_anchor = serialize_skeleton_anchor(skeleton_anchor)
    ctx.blackboard.skeleton_nodes = [
        serialize_title_node(node) for node in resolve_nodes
    ]
    ctx.blackboard.pending_skeleton_anchors = pending_records


def run_outline_anchoring(ctx: ToolContext) -> None:
    """Anchor adopted PDF outline with physical-page overrides (zero calibrate VLM)."""
    page_count = int(ctx.blackboard.page_count or 0)
    hierarchies = list(ctx.blackboard.toc_hierarchies or [])
    if page_count <= 0 or not hierarchies:
        return

    page_texts = dict(ctx.blackboard.page_full_text_cache)
    if not page_texts:
        raise ValueError(
            "page_full_text_cache missing; run text scan before TOC anchoring"
        )

    primary = hierarchies
    nodes = extract_toc_nodes(primary)
    if not nodes:
        return
    nodes = collapse_intermediate_single_child_chains(nodes)

    toc_result = ctx.blackboard.toc_result
    body_pages = body_pages_excluding_toc(
        getattr(toc_result, "toc_pages", None),
        page_count,
    )
    overrides = outline_physical_overrides(primary)

    resolve_nodes, skeleton_anchor = anchor_hierarchy_from_offset(
        nodes=nodes,
        offset_hint=0,
        calibration_overrides=overrides,
        page_texts=page_texts,
        body_pages=body_pages,
        page_count=page_count,
        ctx=ctx,
    )
    skeleton_anchor = replace(skeleton_anchor, source="pdf_outline")
    ctx.blackboard.skeleton_anchor = serialize_skeleton_anchor(skeleton_anchor)
    ctx.blackboard.skeleton_nodes = [
        serialize_title_node(node) for node in resolve_nodes
    ]
    ctx.blackboard.pending_skeleton_anchors = []
    logger.info(
        "{} outline anchoring: overrides={} pruned={} nodes={}",
        _LOG_PREFIX,
        len(skeleton_anchor.match_overrides),
        skeleton_anchor.pruned_count,
        len(resolve_nodes),
    )


def outline_physical_overrides(
    hierarchies: list[dict[str, Any]],
) -> dict[tuple[str, ...], TitleMatch]:
    """Build path→physical TitleMatch from outline rows carrying ``physical_page``."""
    overrides: dict[tuple[str, ...], TitleMatch] = {}
    for hierarchy in hierarchies:
        entries = hierarchy.get("toc_with_level") or []
        if not isinstance(entries, list):
            continue
        stack: list[tuple[int, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = normalize_heading_text(str(entry.get("heading") or ""))
            if not title or len(title) < 2:
                continue
            try:
                level = int(entry.get("level") or 1)
            except (TypeError, ValueError):
                level = 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = tuple(item[1] for item in stack)
            raw_page = entry.get("physical_page")
            if raw_page is None:
                continue
            try:
                page = int(raw_page)
            except (TypeError, ValueError):
                continue
            if page < 1:
                continue
            overrides[path] = TitleMatch(
                page=page,
                confidence=1.0,
                source="pdf_outline",
                matched_line="",
                score=1.0,
                candidates=[page],
            )
    return overrides


def select_global_toc_hierarchies(
    *,
    hierarchies: list[dict[str, Any]],
    filename: str,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], dict[str, Any]]:
    """Split TOC hierarchies into primary (front cluster) and pending."""
    if len(hierarchies) <= 1:
        return (hierarchies or None), [], {}

    page_based = [
        hierarchy
        for hierarchy in hierarchies
        if hierarchy.get("toc_range_unit") == "page"
        and toc_range_start(hierarchy) is not None
    ]
    if not page_based or len(page_based) != len(hierarchies):
        return hierarchies, [], {}

    sorted_items = sorted(
        enumerate(hierarchies),
        key=lambda item: toc_range_start(item[1]) or 0,
    )
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
            "{} toc split: primary={} pending={} filename={}",
            _LOG_PREFIX,
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


def body_pages_excluding_toc(toc_pages: Any, page_count: int) -> list[int]:
    excluded = {int(page) for page in (toc_pages or [])}
    return [page for page in range(1, page_count + 1) if page not in excluded]


def pending_toc_body_scope(
    *,
    pending_tocs: list[dict[str, Any]],
    index: int,
    page_count: int,
    body_pages: list[int],
) -> tuple[int, list[int]]:
    pending_toc = pending_tocs[index]
    toc_end = toc_range_end(pending_toc)
    toc_scope_start = (toc_end + 1) if toc_end is not None else None
    next_starts: list[int] = []
    for j in range(index + 1, len(pending_tocs)):
        start = toc_range_start(pending_tocs[j])
        if start is not None:
            next_starts.append(start)
    toc_scope_end = (min(next_starts) - 1) if next_starts else page_count
    toc_body_pages = [
        page
        for page in body_pages
        if page <= toc_scope_end
        and (toc_scope_start is None or page >= toc_scope_start)
    ]
    return toc_scope_end, toc_body_pages


def classify_toc_relationship(
    *,
    offset: int,
    nodes: list[TitleNode],
    primary_ranges: list[ResolvedHierarchyRange],
    page_count: int,
) -> str:
    """Classify a pending TOC as parallel or contained vs primary ranges.

    contained: the pending TOC's page span fits inside the span of some primary
               node at any depth, so it refines structure the primary tree
               already covers. Graft hosts it under that node.
    parallel:  no primary node covers the span, so it describes content the
               primary tree does not structure at all.

    Uses the same ancestor coverage as ``toc_graft`` so classification and
    grafting agree on geometry.
    """
    leaves = [
        node
        for _, node in iter_leaf_title_nodes(nodes)
        if node.printed_page is not None
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

    host = deepest_covering_path(
        coverage_by_path(primary_ranges),
        start=first_physical,
        end=last_physical,
    )
    return "contained" if host is not None else "parallel"


def _anchor_pending_tocs(
    *,
    pending_tocs: list[dict[str, Any]],
    ctx: ToolContext,
    page_texts: dict[int, str],
    page_count: int,
    body_pages: list[int],
    primary_ranges: list[ResolvedHierarchyRange],
) -> list[dict[str, Any]]:
    from app.services.document_agent.calibration.procedure import (
        finalize_calibration_result,
        pick_primary_offset,
    )
    from app.services.document_agent.calibration.service import (
        calibrate_offset,
    )

    records: list[dict[str, Any]] = []
    for i, pending_toc in enumerate(pending_tocs):
        nodes = extract_toc_nodes([pending_toc])
        if not nodes:
            continue
        nodes = collapse_intermediate_single_child_chains(nodes)
        toc_scope_end, toc_body_pages = pending_toc_body_scope(
            pending_tocs=pending_tocs,
            index=i,
            page_count=page_count,
            body_pages=body_pages,
        )
        phase1 = calibrate_offset(
            nodes=nodes,
            toc_hierarchies=[pending_toc],
            ctx=ctx,
            page_texts=page_texts,
            page_count=toc_scope_end,
        )
        offset = pick_primary_offset(phase1)
        if offset is None:
            logger.info(
                "{} pending TOC toc_range={}: calibration failed, skipping",
                _LOG_PREFIX,
                pending_toc.get("toc_range"),
            )
            continue
        relationship = classify_toc_relationship(
            offset=offset,
            nodes=nodes,
            primary_ranges=primary_ranges,
            page_count=page_count,
        )
        if relationship == "unresolvable":
            logger.info(
                "{} pending TOC toc_range={}: unresolvable, skipping",
                _LOG_PREFIX,
                pending_toc.get("toc_range"),
            )
            records.append(
                {
                    "toc": pending_toc,
                    "relationship": relationship,
                }
            )
            continue
        resolve_nodes, skeleton_anchor, _finalized = finalize_calibration_result(
            result=phase1,
            entries=list(pending_toc.get("toc_with_level") or []),
            toc_hierarchies=[pending_toc],
            ctx=ctx,
            page_count=toc_scope_end,
            page_texts=page_texts,
            body_pages=toc_body_pages,
            nodes=nodes,
        )
        records.append(
            {
                "toc": pending_toc,
                "relationship": relationship,
                "nodes": [serialize_title_node(node) for node in resolve_nodes],
                "skeleton_anchor": serialize_skeleton_anchor(skeleton_anchor),
            }
        )
    return records


def _graft_contained_pending(
    *,
    resolve_nodes: list[TitleNode],
    skeleton_anchor: SkeletonAnchor,
    pending_records: list[dict[str, Any]],
    page_count: int,
    page_texts: dict[int, str],
    body_pages: list[int],
) -> tuple[list[TitleNode], SkeletonAnchor]:
    from app.services.document_agent.structure.toc_graft import graft_contained_toc

    nodes = resolve_nodes
    overrides = dict(skeleton_anchor.match_overrides)
    for record in pending_records:
        if record.get("relationship") != "contained":
            continue
        nodes_raw = record.get("nodes") or []
        anchor_raw = record.get("skeleton_anchor")
        if not isinstance(anchor_raw, dict) or not nodes_raw:
            continue
        contained_nodes = [
            deserialize_title_node(node) for node in nodes_raw if isinstance(node, dict)
        ]
        if not contained_nodes:
            continue
        contained_anchor = deserialize_skeleton_anchor(anchor_raw)
        grafted = graft_contained_toc(
            primary_nodes=nodes,
            primary_overrides=overrides,
            contained_nodes=contained_nodes,
            contained_overrides=contained_anchor.match_overrides,
            page_count=page_count,
            page_texts=page_texts,
            body_pages=body_pages,
        )
        nodes = grafted.nodes
        overrides = grafted.match_overrides
        record["grafted"] = True
        record["graft"] = grafted.events
    return nodes, replace(skeleton_anchor, match_overrides=overrides)
