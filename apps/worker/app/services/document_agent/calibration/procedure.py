"""Phase-2 completion aligned with production anchoring.

After Phase-1 returns candidate regime offsets, this module:
1. Builds TitleNodes the same way production does
2. Runs Phase-2 **per regime** (prune → bulk/bisect → recalibrate)
3. Merges physical-page ``match_overrides`` across regimes
4. Locates null-page leaves, then null-page parents, then final prune

Returns production ``SkeletonAnchor`` plus regime diagnostics for debug payloads.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from loguru import logger

from app.services.document_agent.calibration.types import (
    CalibrationRegime,
    CalibrationResult,
    CalibrationSegment,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    classify_page_number_kind,
    extract_toc_nodes,
    iter_leaf_title_nodes,
    normalize_page_kind,
    parse_printed_page,
)
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    apply_null_page_locates_and_prune,
    backfill_parent_offset_matches,
    prune_unanchored_toc_leaves,
    serialize_skeleton_anchor,
)
from app.services.document_agent.structure import anchoring_primitives as _anchoring

# Re-export under prior names so existing imports keep working.
normalize_kind = normalize_page_kind


def offset_guided_anchoring(
    *,
    nodes: list[TitleNode],
    offset: int,
    ctx: ToolContext,
    page_count: int,
    calibration_overrides: dict[tuple[str, ...], TitleMatch],
) -> dict[tuple[str, ...], TitleMatch] | None:
    """Forward to production Phase-2 anchoring."""
    return _anchoring.offset_guided_anchoring(
        nodes=nodes,
        offset=offset,
        ctx=ctx,
        page_count=page_count,
        calibration_overrides=calibration_overrides,
    )

def pick_primary_offset(result: CalibrationResult) -> int | None:
    """Prefer decimal-regime candidate offset; else first regime with an offset."""
    for regime in result.regimes:
        if normalize_kind(regime.kind) == "decimal" and regime.offset is not None:
            return int(regime.offset)
    for regime in result.regimes:
        if regime.offset is not None:
            return int(regime.offset)
    return None


def seed_overrides_from_samples(
    *,
    result: CalibrationResult,
    nodes: list[Any],
) -> dict[tuple[str, ...], TitleMatch]:
    """Map Phase-1 confirmed samples onto leaf paths when titles match."""
    title_to_path: dict[str, tuple[str, ...]] = {}
    for path, node in iter_leaf_title_nodes(nodes):
        title_to_path[node.title] = path

    overrides: dict[tuple[str, ...], TitleMatch] = {}
    for regime in result.regimes:
        for sample in regime.samples:
            if sample.physical is None or not sample.title:
                continue
            path = title_to_path.get(sample.title.strip())
            if path is None:
                needle = sample.title.strip().lower()
                for title, candidate in title_to_path.items():
                    if title.lower() == needle:
                        path = candidate
                        break
            if path is None:
                continue
            overrides[path] = TitleMatch(
                page=int(sample.physical),
                source="inspect_vlm",
                matched_line="",
                candidates=[int(sample.physical)],
                evidence={
                    "calibration": True,
                    "method": "phase1_forward_scan",
                    "regime_kind": regime.kind,
                },
            )
    return overrides


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
            rows.extend(
                _iter_all_title_nodes(node.children, parent_titles=path)
            )
    return rows


def flat_toc_entries(toc_hierarchies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for hierarchy in toc_hierarchies or []:
        raw = hierarchy.get("toc_with_level") if isinstance(hierarchy, dict) else None
        if isinstance(raw, list):
            entries.extend(e for e in raw if isinstance(e, dict))
    return entries


def _entry_titles_for_regime(
    *,
    regime: CalibrationRegime,
    entries: list[dict[str, Any]],
) -> set[str] | None:
    """Titles belonging to this regime; None means fall back to page_kind match."""
    from app.services.document_parser.structure.body_boundary import (
        normalize_heading_label,
    )

    kind = normalize_kind(regime.kind)
    indices = list(regime.entry_indices or [])
    if not indices and entries:
        indices = [
            idx
            for idx, entry in enumerate(entries)
            if classify_page_number_kind(entry.get("page_number")) == kind
        ]
    if not indices:
        return None
    titles: set[str] = set()
    for idx in indices:
        if idx < 0 or idx >= len(entries):
            continue
        heading = entries[idx].get("heading")
        title = normalize_heading_label(str(heading or ""))
        if title:
            titles.add(title)
    return titles or None


def _leaf_in_regime(
    node: TitleNode,
    *,
    kind: str,
    entry_titles: set[str] | None,
) -> bool:
    if entry_titles is not None:
        return node.title in entry_titles
    leaf_kind = normalize_kind(node.page_kind or classify_page_number_kind(node.printed_label))
    return leaf_kind == kind


def project_nodes_for_regime(
    nodes: list[TitleNode],
    *,
    kind: str,
    entry_titles: set[str] | None,
) -> list[TitleNode]:
    """Copy tree: only this regime's leaves keep a parsed ``printed_page``."""

    def walk(node: TitleNode) -> TitleNode:
        children = [walk(child) for child in node.children]
        if node.children:
            return replace(node, children=children)
        if not _leaf_in_regime(node, kind=kind, entry_titles=entry_titles):
            return replace(node, printed_page=None, children=[])
        printed = node.printed_page
        if printed is None and node.printed_label is not None:
            printed = parse_printed_page(node.printed_label, kind=kind)
        return replace(
            node,
            printed_page=printed,
            page_kind=kind,
            children=[],
        )

    return [walk(node) for node in nodes]


def prune_regime_out_of_scope(
    nodes: list[TitleNode],
    *,
    kind: str,
    entry_titles: set[str] | None,
    offset: int,
    page_count: int,
) -> tuple[list[TitleNode], int]:
    """Drop only this regime's leaves whose printed+offset falls outside the PDF."""
    removed = 0

    def prune(node: TitleNode) -> TitleNode | None:
        nonlocal removed
        if not node.children:
            if _leaf_in_regime(node, kind=kind, entry_titles=entry_titles):
                printed = node.printed_page
                if printed is None and node.printed_label is not None:
                    printed = parse_printed_page(node.printed_label, kind=kind)
                if printed is not None:
                    expected = printed + offset
                    if expected < 1 or expected > page_count:
                        removed += 1
                        return None
                return replace(node, printed_page=printed)
            return node
        children: list[TitleNode] = []
        for child in node.children:
            kept = prune(child)
            if kept is not None:
                children.append(kept)
        if not children:
            removed += 1
            return None
        return replace(node, children=children)

    out: list[TitleNode] = []
    for node in nodes:
        kept = prune(node)
        if kept is not None:
            out.append(kept)
    return out, removed


def anchor_hierarchy_from_regimes(
    *,
    nodes: list[TitleNode],
    result: CalibrationResult,
    entries: list[dict[str, Any]] | None,
    page_texts: dict[int, str],
    body_pages: list[int],
    page_count: int,
    ctx: ToolContext | None,
) -> tuple[list[TitleNode], SkeletonAnchor]:
    """Phase-2: per-regime offset bulk/bisect, then merge physical overrides."""
    flat_entries = list(entries or [])
    seed = seed_overrides_from_samples(result=result, nodes=nodes)
    merged: dict[tuple[str, ...], TitleMatch] = dict(seed)
    working = nodes
    total_pruned = 0
    regime_bulk = 0

    usable_regimes = [
        regime
        for regime in result.regimes
        if regime.offset is not None
    ]

    for regime in usable_regimes:
        kind = normalize_kind(regime.kind)
        offset = int(regime.offset)  # type: ignore[arg-type]
        entry_titles = _entry_titles_for_regime(regime=regime, entries=flat_entries)
        working, pruned = prune_regime_out_of_scope(
            working,
            kind=kind,
            entry_titles=entry_titles,
            offset=offset,
            page_count=page_count,
        )
        total_pruned += pruned
        if not working:
            continue

        projected = project_nodes_for_regime(
            working, kind=kind, entry_titles=entry_titles
        )
        regime_paths = {
            path
            for path, node in iter_leaf_title_nodes(projected)
            if node.printed_page is not None
        }
        regime_seed = {
            path: match
            for path, match in seed.items()
            if path in regime_paths
        }

        if ctx is None:
            # Offline: still apply deterministic printed+offset for this regime.
            from app.services.document_agent.structure.anchoring_primitives import (
                bulk_offset_matches,
            )

            leaves = [
                (path, node)
                for path, node in iter_leaf_title_nodes(projected)
                if node.printed_page is not None
            ]
            if leaves:
                matches = bulk_offset_matches(leaves, offset)
                matches.update(regime_seed)
                merged.update(matches)
                regime_bulk += len(matches)
            continue

        matches = offset_guided_anchoring(
            nodes=projected,
            offset=offset,
            ctx=ctx,
            page_count=page_count,
            calibration_overrides=regime_seed,
        )
        if matches:
            merged.update(matches)
            regime_bulk += len(
                {
                    path
                    for path in matches
                    if path in regime_paths or path in regime_seed
                }
            )
            logger.info(
                "[calibration.phase2] regime={} offset={} anchored={}",
                kind,
                offset,
                len(matches),
            )
        elif regime_seed:
            merged.update(regime_seed)
            logger.info(
                "[calibration.phase2] regime={} offset={} seed_only={}",
                kind,
                offset,
                len(regime_seed),
            )

    # Capture parent identity before prune so empty shells retain ``kind=parent``.
    structural_parent_paths = {
        path
        for path, node in _iter_all_title_nodes(working)
        if node.children
    }
    # Failed printed-page leaves → drop; keep null-page nodes for ReAct.
    working, unanchored_removed = prune_unanchored_toc_leaves(
        working,
        match_overrides=merged,
        keep_null_page_nodes=True,
    )
    total_pruned += unanchored_removed
    if working:
        surviving_paths = {
            path
            for path, _node in _iter_all_title_nodes(working)
        }
        merged = {
            path: match
            for path, match in merged.items()
            if path in surviving_paths
        }

    parent_matches = backfill_parent_offset_matches(
        nodes=working,
        matches=merged,
        page_count=page_count,
    )
    if parent_matches:
        merged.update(parent_matches)
        logger.info(
            "[calibration.phase2] parent backfill: {} printed-page TOC parents "
            "anchored from descendant offset",
            len(parent_matches),
        )

    # Freeze bulk before unified null-page ReAct (same as offset path:
    # offset_guided → len(overrides then); else 0). Never recount after ReAct.
    bulk_count = len(merged) if regime_bulk > 0 else 0

    working, match_overrides, null_page_report, failed_null_removed = (
        apply_null_page_locates_and_prune(
            nodes=working,
            match_overrides=merged,
            body_pages=body_pages,
            ctx=ctx,
            structural_parent_paths=structural_parent_paths,
        )
    )
    total_pruned += failed_null_removed

    primary = pick_primary_offset(result)
    if primary is None and usable_regimes:
        primary = int(usable_regimes[0].offset)  # type: ignore[arg-type]

    if primary is None:
        offset_status = "failed" if ctx is not None else "skipped"
    else:
        offset_status = "ok"

    locate_method = (
        "offset_guided_bulk"
        if match_overrides and (regime_bulk > 0 or seed)
        else "offset_only"
    )

    return working, SkeletonAnchor(
        offset=primary,
        offset_status=offset_status,
        match_overrides=match_overrides,
        null_page_report=null_page_report,
        bulk_count=bulk_count,
        pruned_count=total_pruned,
        locate_method=locate_method,
    )


def _annotate_regimes_from_anchor(
    *,
    result: CalibrationResult,
    anchor: SkeletonAnchor,
    entries: list[dict[str, Any]],
    nodes: list[TitleNode],
) -> list[CalibrationRegime]:
    """Attach production segment view onto agent regimes for diagnostics."""
    path_by_title = {
        node.title: path for path, node in iter_leaf_title_nodes(nodes)
    }
    out: list[CalibrationRegime] = []
    for regime in result.regimes:
        kind = normalize_kind(regime.kind)
        entry_titles = _entry_titles_for_regime(regime=regime, entries=entries)
        indices = list(regime.entry_indices or [])
        if not indices:
            indices = [
                idx
                for idx, entry in enumerate(entries)
                if isinstance(entry, dict)
                and classify_page_number_kind(entry.get("page_number")) == kind
            ]

        ok_indices: list[int] = []
        no_toc: list[int] = []
        for idx in indices:
            if idx < 0 or idx >= len(entries):
                continue
            heading = str(entries[idx].get("heading") or "")
            from app.services.document_parser.structure.body_boundary import (
                normalize_heading_label,
            )

            title = normalize_heading_label(heading)
            path = path_by_title.get(title)
            if path is not None and path in (anchor.match_overrides or {}):
                ok_indices.append(idx)
            else:
                no_toc.append(idx)

        # Fallback: kind-matched leaves present in overrides.
        if not ok_indices and entry_titles is None:
            for path, node in iter_leaf_title_nodes(nodes):
                if _leaf_in_regime(node, kind=kind, entry_titles=None) and path in (
                    anchor.match_overrides or {}
                ):
                    # No stable entry index — treat as complete via offset status.
                    ok_indices = indices
                    no_toc = []
                    break

        segments: list[CalibrationSegment] = []
        if ok_indices and regime.offset is not None:
            segments = [
                CalibrationSegment(
                    offset=int(regime.offset),
                    leaf_start=0,
                    leaf_end=max(0, len(ok_indices) - 1),
                    entry_indices=ok_indices,
                    status="ok",
                )
            ]

        out.append(
            CalibrationRegime(
                kind=kind,
                offset=regime.offset,
                offset_status="ok" if segments else "failed",
                entry_indices=indices,
                samples=list(regime.samples),
                segments=segments,
                no_toc_entry_indices=no_toc,
                notes=(
                    f"production_bulk={anchor.bulk_count}; "
                    f"locate_method={anchor.locate_method}; "
                    f"regime_anchored={len(ok_indices)}"
                ),
            )
        )
    return out


def finalize_calibration_result(
    *,
    result: CalibrationResult,
    entries: list[dict[str, Any]],
    toc_hierarchies: list[dict[str, Any]],
    ctx: ToolContext | None,
    page_count: int,
    page_texts: dict[int, str] | None = None,
    body_pages: list[int] | None = None,
    nodes: list[TitleNode] | None = None,
) -> tuple[list[TitleNode], SkeletonAnchor, CalibrationResult]:
    """Run production multi-regime Phase-2 from agent candidate offsets."""
    texts = dict(page_texts or {})
    bodies = list(body_pages or sorted(texts.keys()) or list(range(1, page_count + 1)))
    if nodes is None:
        from app.services.document_agent.structure.hierarchy_locator import (
            collapse_intermediate_single_child_chains,
        )

        nodes = collapse_intermediate_single_child_chains(
            extract_toc_nodes(toc_hierarchies)
        )

    working, anchor = anchor_hierarchy_from_regimes(
        nodes=nodes,
        result=result,
        entries=entries or flat_toc_entries(toc_hierarchies),
        page_texts=texts,
        body_pages=bodies,
        page_count=page_count,
        ctx=ctx,
    )
    logger.info(
        "[calibration.completion] offset={} status={} bulk={} pruned={} nodes={} regimes={}",
        anchor.offset,
        anchor.offset_status,
        anchor.bulk_count,
        anchor.pruned_count,
        len(working),
        len(result.regimes),
    )

    regimes = _annotate_regimes_from_anchor(
        result=result, anchor=anchor, entries=entries, nodes=working
    )
    complete = sum(len(r.segments) for r in regimes)
    notes_parts = [result.notes] if result.notes else []
    notes_parts.append(
        f"phase2 production locate_method={anchor.locate_method} "
        f"bulk={anchor.bulk_count} complete_regime_segments={complete}"
    )
    finalized = CalibrationResult(
        status="ok" if anchor.offset_status == "ok" and anchor.bulk_count > 0 else "failed",
        regimes=regimes,
        offset=anchor.offset,
        offset_status=anchor.offset_status,
        tool_calls=result.tool_calls,
        notes="; ".join(p for p in notes_parts if p),
        failure_kind=result.failure_kind,
        region_index=result.region_index,
        scans=list(result.scans),
    )
    return working, anchor, finalized


def build_calibration_payload(
    *,
    anchor: SkeletonAnchor,
    result: CalibrationResult,
    region_payloads: list[dict[str, Any]] | None = None,
    tool_calls: int | None = None,
) -> dict[str, Any]:
    """Production SkeletonAnchor fields + experiment diagnostics."""
    payload = serialize_skeleton_anchor(anchor)
    payload.update(
        {
            "status": result.status,
            "regimes": [
                regime.to_dict() if hasattr(regime, "to_dict") else regime
                for regime in (result.to_dict().get("regimes") or [])
            ],
            "regions": list(region_payloads or []),
            "tool_calls": int(tool_calls if tool_calls is not None else result.tool_calls),
            "notes": result.notes,
            "failure_kind": result.failure_kind,
        }
    )
    return payload
