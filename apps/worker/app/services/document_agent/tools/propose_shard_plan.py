"""Deterministic long-PDF shard planning from anchored TOC hierarchy ranges."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from app.services.document_agent.manifest import (
    Shard,
    ShardPlan,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import has_doc_stats, has_toc_result, register_tool
from app.services.document_agent.structure.anchoring_primitives import (
    deserialize_skeleton_anchor,
    deserialize_title_node,
    toc_range_start,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    TitleMatch,
    TitleNode,
    resolve_hierarchy_page_ranges,
)
from app.services.document_agent.structure.toc_anchoring import (
    body_pages_excluding_toc,
    pending_toc_body_scope,
    select_global_toc_hierarchies,
)
from app.services.document_agent.validators import single_shard_plan, validate_shard_plan


@dataclass(frozen=True)
class _AnchoredTocEntry:
    heading: str
    level: int
    physical_page: int
    path_titles: tuple[str, ...]


def _walk_anchored_entries(
    nodes: list[TitleNode],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    *,
    prefix: tuple[str, ...] = (),
) -> list[_AnchoredTocEntry]:
    """DFS title tree; keep only nodes already pinned to a physical page."""
    entries: list[_AnchoredTocEntry] = []
    for node in nodes:
        path = (*prefix, node.title)
        match = match_overrides.get(path)
        if match is not None:
            entries.append(
                _AnchoredTocEntry(
                    heading=node.title,
                    level=int(node.level),
                    physical_page=int(match.page),
                    path_titles=path,
                )
            )
        if node.children:
            entries.extend(
                _walk_anchored_entries(
                    list(node.children),
                    match_overrides,
                    prefix=path,
                )
            )
    return entries


def _collect_calibrated_toc_entries(ctx: ToolContext) -> list[_AnchoredTocEntry]:
    """Primary (post-graft) + parallel pending entries with calibrated pages."""
    entries: list[_AnchoredTocEntry] = []
    anchor_raw = ctx.blackboard.skeleton_anchor
    nodes_raw = ctx.blackboard.skeleton_nodes
    if isinstance(anchor_raw, dict) and isinstance(nodes_raw, list):
        nodes = [
            deserialize_title_node(node)
            for node in nodes_raw
            if isinstance(node, dict)
        ]
        if nodes:
            anchor = deserialize_skeleton_anchor(anchor_raw)
            entries.extend(
                _walk_anchored_entries(nodes, anchor.match_overrides)
            )

    for record in ctx.blackboard.pending_skeleton_anchors or []:
        if record.get("relationship") != "parallel":
            continue
        pending_anchor_raw = record.get("skeleton_anchor")
        pending_nodes_raw = record.get("nodes") or []
        if not isinstance(pending_anchor_raw, dict) or not isinstance(
            pending_nodes_raw, list
        ):
            continue
        pending_nodes = [
            deserialize_title_node(node)
            for node in pending_nodes_raw
            if isinstance(node, dict)
        ]
        if not pending_nodes:
            continue
        pending_anchor = deserialize_skeleton_anchor(pending_anchor_raw)
        entries.extend(
            _walk_anchored_entries(pending_nodes, pending_anchor.match_overrides)
        )
    return entries


def _toc_hierarchies_for_shard(
    entries: list[_AnchoredTocEntry],
    *,
    shard_page_start: int,
    shard_page_end: int,
) -> list[dict[str, Any]] | None:
    """Slice calibrated TOC entries to a shard and prepend open ancestors."""
    if not entries:
        return None

    shard_entries: list[_AnchoredTocEntry] = []
    first_idx: int | None = None
    for idx, entry in enumerate(entries):
        if shard_page_start <= entry.physical_page <= shard_page_end:
            if first_idx is None:
                first_idx = idx
            shard_entries.append(entry)
    if not shard_entries or first_idx is None:
        return None

    # Reopen the first entry's real TOC ancestors so the shard slice keeps its
    # place in the tree. Ancestors precede the shard in this pre-order walk.
    preceding_by_path = {entry.path_titles: entry for entry in entries[:first_idx]}
    first_path = shard_entries[0].path_titles
    ancestors = [
        {"heading": ancestor.heading, "level": ancestor.level}
        for ancestor in (
            preceding_by_path.get(first_path[:depth])
            for depth in range(1, len(first_path))
        )
        if ancestor is not None
    ]

    toc_with_level = ancestors + [
        {"heading": entry.heading, "level": entry.level}
        for entry in shard_entries
    ]
    return [
        {
            "toc_range": [shard_page_start, shard_page_end],
            "toc_range_unit": "page",
            "source": "calibrated_shard_split",
            "toc_with_level": toc_with_level,
        }
    ]


def _attach_shard_toc_hierarchies(ctx: ToolContext, shards: list[Shard]) -> None:
    """Attach calibrated per-shard TOC slices onto the plan (in place)."""
    entries = _collect_calibrated_toc_entries(ctx)
    for shard in shards:
        shard.toc_hierarchies = _toc_hierarchies_for_shard(
            entries,
            shard_page_start=shard.page_start,
            shard_page_end=shard.page_end,
        )


def _thresholds(ctx: ToolContext) -> tuple[int, int]:
    threshold = int(
        ctx.settings.get("shard_threshold")
        or os.environ.get("PARSE_AGENT_SHARD_THRESHOLD", "200")
    )
    max_pages = int(
        ctx.settings.get("max_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MAX_PAGES_PER_SHARD", "200")
    )
    return threshold, max_pages


def _cuts_to_shards(cuts: list[tuple[int, str, str, float]], page_count: int) -> list[Shard]:
    shards: list[Shard] = []
    previous = 0
    for cut_page, anchor_type, evidence, confidence in cuts:
        if cut_page <= previous:
            continue
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=cut_page,
                page_offset=previous,
                anchor_type=anchor_type,  # type: ignore[arg-type]
                anchor_evidence=evidence,
                confidence=confidence,
            )
        )
        previous = cut_page
    if previous < page_count:
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=page_count,
                page_offset=previous,
                anchor_type="forced_max_size",
                anchor_evidence="final shard",
                confidence=1.0,
            )
        )
    return shards


@dataclass(frozen=True)
class _PackUnit:
    start: int
    end: int
    path: tuple[str, ...]


def _page_span(start: int, end: int) -> int:
    return end - start + 1


def _resolve_hierarchy_forests(
    ctx: ToolContext,
) -> list[list[ResolvedHierarchyRange]]:
    hierarchies = list(ctx.blackboard.toc_hierarchies or [])
    anchor_raw = ctx.blackboard.skeleton_anchor
    nodes_raw = ctx.blackboard.skeleton_nodes
    if (
        not hierarchies
        or not isinstance(anchor_raw, dict)
        or not isinstance(nodes_raw, list)
    ):
        return []

    filename = os.path.basename(ctx.pdf_path)
    _primary, pending, _summary = select_global_toc_hierarchies(
        hierarchies=hierarchies,
        filename=filename,
    )
    nodes = [
        deserialize_title_node(node)
        for node in nodes_raw
        if isinstance(node, dict)
    ]
    if not nodes:
        return []

    page_count = ctx.blackboard.page_count
    page_texts = dict(ctx.blackboard.page_full_text_cache or {})
    toc_result = ctx.blackboard.toc_result
    body_pages = body_pages_excluding_toc(
        getattr(toc_result, "toc_pages", None) if toc_result else None,
        page_count,
    )
    records_by_range: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in ctx.blackboard.pending_skeleton_anchors or []:
        toc = record.get("toc")
        if isinstance(toc, dict):
            records_by_range[tuple(toc.get("toc_range") or [])] = record

    parallel_pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pending_toc in pending:
        record = records_by_range.get(tuple(pending_toc.get("toc_range") or []))
        if record is None:
            continue
        relationship = record.get("relationship")
        if relationship in {"unresolvable", "contained"}:
            continue
        if relationship != "parallel":
            raise ValueError(
                "pending TOC relationship missing after PROFILE classify"
            )
        parallel_pending.append((pending_toc, record))

    parallel_tocs = [toc for toc, _record in parallel_pending]
    pending_starts: list[int] = []
    for toc in parallel_tocs:
        start = toc_range_start(toc)
        if start is not None:
            pending_starts.append(start)
    primary_page_count = page_count
    primary_body_pages = body_pages
    if pending_starts:
        primary_page_count = min(pending_starts) - 1
        primary_body_pages = [
            page for page in body_pages if page <= primary_page_count
        ]

    forests: list[list[ResolvedHierarchyRange]] = []
    anchor = deserialize_skeleton_anchor(anchor_raw)
    primary_ranges = resolve_hierarchy_page_ranges(
        nodes,
        page_count=primary_page_count,
        page_texts=page_texts,
        body_pages=primary_body_pages,
        match_overrides=anchor.match_overrides,
    )
    if primary_ranges:
        forests.append(primary_ranges)

    for index, (_pending_toc, record) in enumerate(parallel_pending):
        resolve_nodes = [
            deserialize_title_node(node)
            for node in (record.get("nodes") or [])
            if isinstance(node, dict)
        ]
        pending_anchor_raw = record.get("skeleton_anchor")
        if not isinstance(pending_anchor_raw, dict) or not resolve_nodes:
            raise ValueError(
                "pending TOC skeleton_anchor/nodes missing after PROFILE classify"
            )
        pending_anchor = deserialize_skeleton_anchor(pending_anchor_raw)
        toc_scope_end, toc_body_pages = pending_toc_body_scope(
            pending_tocs=parallel_tocs,
            index=index,
            page_count=page_count,
            body_pages=body_pages,
        )
        pending_ranges = resolve_hierarchy_page_ranges(
            resolve_nodes,
            page_count=toc_scope_end,
            page_texts=page_texts,
            body_pages=toc_body_pages,
            match_overrides=pending_anchor.match_overrides,
        )
        if pending_ranges:
            forests.append(pending_ranges)
    return forests


def _greedy_pack_siblings(units: list[_PackUnit], max_pages: int) -> list[_PackUnit]:
    if not units:
        return []
    parent = units[0].path[:-1]
    packed: list[_PackUnit] = []
    current_start, current_end = units[0].start, units[0].end
    for unit in units[1:]:
        merged_start = min(current_start, unit.start)
        merged_end = max(current_end, unit.end)
        if _page_span(merged_start, merged_end) <= max_pages:
            current_start, current_end = merged_start, merged_end
        else:
            packed.append(_PackUnit(current_start, current_end, parent))
            current_start, current_end = unit.start, unit.end
    packed.append(_PackUnit(current_start, current_end, parent))
    return packed


def _pack_forest(
    ranges: list[ResolvedHierarchyRange],
    max_pages: int,
) -> list[_PackUnit]:
    units = [
        _PackUnit(item.start_page, item.end_page, tuple(item.path_titles))
        for item in ranges
        if item.start_page <= item.end_page
    ]
    while units and any(unit.path for unit in units):
        max_depth = max(len(unit.path) for unit in units)
        next_units: list[_PackUnit] = []
        index = 0
        while index < len(units):
            unit = units[index]
            if len(unit.path) < max_depth:
                next_units.append(unit)
                index += 1
                continue
            parent = unit.path[:-1]
            group = [unit]
            index += 1
            while (
                index < len(units)
                and len(units[index].path) == max_depth
                and units[index].path[:-1] == parent
            ):
                group.append(units[index])
                index += 1
            next_units.extend(_greedy_pack_siblings(group, max_pages))
        units = next_units
    return units


def _pack_forests(
    forests: list[list[ResolvedHierarchyRange]],
    max_pages: int,
) -> list[_PackUnit]:
    packed_units: list[_PackUnit] = []
    for index, forest in enumerate(forests):
        packed = _pack_forest(forest, max_pages)
        if index == 0 and packed and packed[0].start > 1:
            packed = _greedy_pack_siblings(
                [_PackUnit(1, packed[0].start - 1, ()), *packed],
                max_pages,
            )
        packed_units.extend(packed)
    return packed_units


def _exclusive_pieces(
    units: list[_PackUnit],
) -> list[tuple[int, int]]:
    ordered = sorted(units, key=lambda unit: (unit.start, unit.end))
    pieces: list[tuple[int, int]] = []
    for index, unit in enumerate(ordered):
        start = unit.start
        end = unit.end
        if index + 1 < len(ordered):
            next_start = ordered[index + 1].start
            if next_start <= end:
                end = next_start - 1
        if end >= start:
            pieces.append((start, end))
    return pieces


def _pack_range_by_blanks(
    *,
    previous: int,
    end: int,
    max_pages: int,
    blank_pages: list[int],
) -> list[tuple[int, str, str, float]]:
    cuts: list[tuple[int, str, str, float]] = []
    while end - previous > max_pages:
        target = previous + max_pages
        eligible = [
            page for page in blank_pages
            if previous + (max_pages - 20) < page <= target
        ]
        if eligible:
            chosen = max(eligible)
            cuts.append((chosen, "blank_separator", f"blank-like page at {chosen}", 0.5))
            previous = chosen
        else:
            cut_page = previous + max_pages
            cuts.append((cut_page, "forced_max_size", "no separator in range", 0.2))
            previous = cut_page
    return cuts


def _hierarchy_plan(
    *,
    units: list[_PackUnit],
    page_count: int,
    max_pages: int,
    blank_pages: list[int],
) -> list[tuple[int, str, str, float]]:
    cuts: list[tuple[int, str, str, float]] = []
    previous = 0
    for start, end in _exclusive_pieces(units):
        if end <= previous:
            continue
        if end - previous > max_pages:
            range_cuts = _pack_range_by_blanks(
                previous=previous,
                end=end,
                max_pages=max_pages,
                blank_pages=blank_pages,
            )
            cuts.extend(range_cuts)
            if range_cuts:
                previous = range_cuts[-1][0]
            if previous < end and end < page_count:
                cuts.append((end, "toc_leaf_boundary", f"toc leaf at page {end + 1}", 0.85))
                previous = end
            continue
        if end < page_count:
            cuts.append((end, "toc_leaf_boundary", f"toc leaf at page {end + 1}", 0.85))
            previous = end
    return cuts


def _get_blank_pages(ctx: ToolContext) -> list[int]:
    features = ctx.blackboard.page_features or []
    return sorted(feature.page for feature in features if feature.is_blank_like)


@register_tool(
    name="propose.shard_plan",
    description=(
        "Split a long PDF by packing anchored TOC hierarchy ranges, "
        "then blank pages, then max page size."
    ),
    preconditions=(has_doc_stats, has_toc_result),
)
def propose_shard_plan(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    page_count = ctx.blackboard.page_count
    threshold, max_pages = _thresholds(ctx)
    if page_count <= threshold:
        plan = single_shard_plan(page_count)
        _attach_shard_toc_hierarchies(ctx, plan.shards)
        ctx.blackboard.shard_plan = plan
        return ToolResult(
            status="ok",
            payload={"enabled": False, "shard_count": len(plan.shards)},
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    forests = _resolve_hierarchy_forests(ctx)
    blank_pages = _get_blank_pages(ctx)
    packed_units = _pack_forests(forests, max_pages) if forests else []
    if packed_units:
        cuts = _hierarchy_plan(
            units=packed_units,
            page_count=page_count,
            max_pages=max_pages,
            blank_pages=blank_pages,
        )
        reason = "too_large"
        rationale = "Deterministic plan from anchored TOC hierarchy ranges."
    else:
        cuts = _pack_range_by_blanks(
            previous=0,
            end=page_count,
            max_pages=max_pages,
            blank_pages=blank_pages,
        )
        reason = "too_large"
        rationale = "Deterministic plan from blank-like page boundaries (no TOC)."

    shards = _cuts_to_shards(cuts, page_count)
    _attach_shard_toc_hierarchies(ctx, shards)
    enabled = len(shards) > 1
    if not enabled:
        reason = "not_needed"
    plan = ShardPlan(
        enabled=enabled,
        reason=reason,  # type: ignore[arg-type]
        shards=shards,
        validation=validate_shard_plan(
            ShardPlan(enabled=enabled, reason=reason, shards=shards),  # type: ignore[arg-type]
            page_count=page_count,
            max_pages=max_pages,
        ),
    )
    ctx.blackboard.shard_plan = plan
    return ToolResult(
        status="ok",
        payload={
            "enabled": plan.enabled,
            "reason": plan.reason,
            "shard_count": len(plan.shards),
            "valid": plan.validation.valid,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        input_summary={
            "page_count": page_count,
            "forest_count": len(forests),
        },
        output_summary={
            "enabled": plan.enabled,
            "reason": plan.reason,
            "rationale": rationale,
            "shards": [shard.to_dict() for shard in plan.shards],
        },
    )
