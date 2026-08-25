"""Same-forest TOC rehome: global paged-leaf monotonic repair before classify."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from app.services.document_agent.structure.anchoring_primitives import SkeletonAnchor
from app.services.document_agent.structure.hierarchy_locator import TitleMatch, TitleNode
from app.services.document_agent.structure.toc_graft import (
    insert_by_start,
    node_at_path,
    rebase_levels,
    replace_node_at_path,
)

_LOG_PREFIX = "[profile.toc_rehome]"


@dataclass(frozen=True)
class RehomePlan:
    source_path: tuple[str, ...]
    physical_page: int
    segment_index: int
    source_toc_index: int
    anchor_source_path: tuple[str, ...]
    action: str = "moved"


@dataclass(frozen=True)
class RehomeResult:
    nodes: list[TitleNode]
    match_overrides: dict[tuple[str, ...], TitleMatch]
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class _LeafRef:
    path: tuple[str, ...]
    page: int
    toc_index: int


def own_page(
    path: tuple[str, ...],
    match_overrides: dict[tuple[str, ...], TitleMatch],
) -> int | None:
    """Node's own calibrated physical page; never inferred from descendants."""
    match = match_overrides.get(path)
    if match is None or match.page is None:
        return None
    return int(match.page)


def rehome_forest(
    nodes: list[TitleNode],
    match_overrides: dict[tuple[str, ...], TitleMatch],
    *,
    toc_pages: list[int] | None = None,
) -> RehomeResult:
    """Repair TOC-order backjumps among paged leaves within one calibrated forest."""
    overrides = dict(match_overrides)
    toc_page_set = {int(page) for page in (toc_pages or [])}
    plans = _collect_backjump_plans(nodes, overrides, toc_pages=toc_page_set)
    if not plans:
        return RehomeResult(nodes=list(nodes), match_overrides=overrides, events=[])

    prune_plans = [plan for plan in plans if plan.action == "pruned"]
    move_plans = [plan for plan in plans if plan.action == "moved"]
    logger.info(
        "{} plans={} prunes={} moves={}",
        _LOG_PREFIX,
        len(plans),
        len(prune_plans),
        len(move_plans),
    )
    working = list(nodes)
    events: list[dict[str, Any]] = []
    if prune_plans:
        working, overrides, prune_events = _apply_prunes(
            working, overrides, prune_plans
        )
        events.extend(prune_events)
    if move_plans:
        working, overrides, move_events = _apply_plans(
            working, overrides, move_plans
        )
        events.extend(move_events)
    return RehomeResult(nodes=working, match_overrides=overrides, events=events)


def rehome_skeleton_forest(
    nodes: list[TitleNode],
    anchor: SkeletonAnchor,
    *,
    toc_pages: list[int] | None = None,
) -> tuple[list[TitleNode], SkeletonAnchor, list[dict[str, Any]]]:
    """Run ``rehome_forest`` and return an updated ``SkeletonAnchor``."""
    result = rehome_forest(
        nodes,
        anchor.match_overrides,
        toc_pages=toc_pages,
    )
    return (
        result.nodes,
        replace(anchor, match_overrides=result.match_overrides),
        result.events,
    )


def _collect_paged_leaves(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    parent_path: tuple[str, ...] = (),
    *,
    toc_index_start: int = 0,
) -> list[_LeafRef]:
    """TOC-order list of paged leaves (nodes with no children and own page)."""
    leaves: list[_LeafRef] = []
    toc_index = toc_index_start
    for node in nodes:
        path = (*parent_path, node.title)
        if node.children:
            child_leaves = _collect_paged_leaves(
                list(node.children),
                overrides,
                path,
                toc_index_start=toc_index,
            )
            leaves.extend(child_leaves)
            toc_index += len(child_leaves)
            continue
        page = own_page(path, overrides)
        if page is None:
            continue
        leaves.append(_LeafRef(path=path, page=page, toc_index=toc_index))
        toc_index += 1
    return leaves


def _split_monotonic_segments(leaves: list[_LeafRef]) -> list[list[_LeafRef]]:
    """Split TOC-order leaves at every physical-page backjump."""
    segments: list[list[_LeafRef]] = []
    current: list[_LeafRef] = []
    for leaf in leaves:
        if current and leaf.page < current[-1].page:
            segments.append(current)
            current = []
        current.append(leaf)
    if current:
        segments.append(current)
    return segments


def _nearest_leaf_in_segment(
    *,
    physical_page: int,
    segment: list[_LeafRef],
) -> _LeafRef | None:
    """Nearest leaf with ``page <= physical_page`` inside one fixed segment."""
    candidates = [
        leaf
        for leaf in segment
        if leaf.page <= physical_page
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda leaf: (leaf.page, leaf.toc_index))


def _first_segment_duplicate(
    leaf: _LeafRef,
    first_segment: list[_LeafRef],
) -> _LeafRef | None:
    """First-segment leaf with the same hierarchy path and physical page, if any."""
    for candidate in first_segment:
        if candidate.path == leaf.path and candidate.page == leaf.page:
            return candidate
    return None


def _collect_backjump_plans(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    *,
    toc_pages: set[int],
) -> list[RehomePlan]:
    """Plan every post-break segment against the first monotonic segment only."""
    segments = _split_monotonic_segments(_collect_paged_leaves(nodes, overrides))
    if len(segments) < 2:
        return []
    first_segment = segments[0]
    plans: list[RehomePlan] = []
    for segment_index in range(1, len(segments)):
        for leaf in segments[segment_index]:
            duplicate = _first_segment_duplicate(leaf, first_segment)
            if duplicate is not None:
                logger.info(
                    "{} prune same_path_page path={} page={} segment={}",
                    _LOG_PREFIX,
                    leaf.path,
                    leaf.page,
                    segment_index,
                )
                plans.append(
                    RehomePlan(
                        source_path=leaf.path,
                        physical_page=leaf.page,
                        segment_index=segment_index,
                        source_toc_index=leaf.toc_index,
                        anchor_source_path=duplicate.path,
                        action="pruned",
                    )
                )
                continue
            if leaf.page in toc_pages:
                logger.info(
                    "{} skip toc_page path={} page={} segment={}",
                    _LOG_PREFIX,
                    leaf.path,
                    leaf.page,
                    segment_index,
                )
                continue
            anchor = _nearest_leaf_in_segment(
                physical_page=leaf.page,
                segment=first_segment,
            )
            if anchor is None:
                continue
            plans.append(
                RehomePlan(
                    source_path=leaf.path,
                    physical_page=leaf.page,
                    segment_index=segment_index,
                    source_toc_index=leaf.toc_index,
                    anchor_source_path=anchor.path,
                    action="moved",
                )
            )
    return plans


def _apply_prunes(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    plans: list[RehomePlan],
) -> tuple[list[TitleNode], dict[tuple[str, ...], TitleMatch], list[dict[str, Any]]]:
    """Drop post-first-segment duplicates (same path + page as a first-segment leaf)."""
    working = list(nodes)
    events: list[dict[str, Any]] = []
    for plan in plans:
        leaves = _collect_paged_leaves(working, overrides)
        matches = [
            leaf
            for leaf in leaves
            if leaf.path == plan.source_path and leaf.page == plan.physical_page
        ]
        if len(matches) < 2:
            raise ValueError(
                f"{_LOG_PREFIX} prune miss path={plan.source_path!r} "
                f"page={plan.physical_page} remaining={len(matches)}"
            )
        target = matches[-1]
        working, detached, detached_path = _detach_by_toc_index(
            working,
            overrides,
            target_toc_index=target.toc_index,
        )
        if detached is None or detached_path is None:
            raise ValueError(
                f"{_LOG_PREFIX} prune detach miss toc_index={target.toc_index}"
            )
        remaining = [
            leaf
            for leaf in _collect_paged_leaves(working, overrides)
            if leaf.path == detached_path
        ]
        if not remaining:
            overrides.pop(detached_path, None)
        working, overrides = _drop_empty_ancestors(
            working,
            overrides,
            detached_path[:-1],
        )
        events.append(
            {
                "action": "pruned",
                "source_path": list(detached_path),
                "physical_page": plan.physical_page,
                "segment_index": plan.segment_index,
                "anchor_path": list(plan.anchor_source_path),
            }
        )
        logger.info(
            "{} pruned {} page={} segment={}",
            _LOG_PREFIX,
            detached_path,
            plan.physical_page,
            plan.segment_index,
        )
    return working, overrides, events


def _apply_plans(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    plans: list[RehomePlan],
) -> tuple[list[TitleNode], dict[tuple[str, ...], TitleMatch], list[dict[str, Any]]]:
    working = list(nodes)
    events: list[dict[str, Any]] = []
    current_paths = {
        leaf.path: leaf.path for leaf in _collect_paged_leaves(working, overrides)
    }
    for plan in plans:
        source_path = current_paths[plan.source_path]
        anchor_path = current_paths[plan.anchor_source_path]
        dest_parent = anchor_path[:-1]
        if dest_parent and node_at_path(working, dest_parent) is None:
            raise ValueError(
                f"{_LOG_PREFIX} dest parent missing path={dest_parent!r}"
            )

        working, detached = _detach_node(working, source_path)
        if detached is None:
            raise ValueError(
                f"{_LOG_PREFIX} detach miss source_path={source_path!r}"
            )
        target_level = len(dest_parent) + 1
        detached = rebase_levels(detached, target_level - detached.level)
        new_path = (*dest_parent, detached.title)
        working = _insert_under_parent(
            nodes=working,
            parent_path=dest_parent,
            new_node=detached,
            start=plan.physical_page,
            overrides=overrides,
        )
        if new_path != source_path:
            _move_override_prefix(overrides, source_path, new_path)
            working, overrides = _drop_empty_ancestors(
                working,
                overrides,
                source_path[:-1],
            )
        current_paths[plan.source_path] = new_path
        _mark_rehome_override(
            overrides,
            new_path,
            segment_index=plan.segment_index,
        )
        events.append(
            {
                "action": "moved",
                "source_path": list(source_path),
                "dest_parent_path": list(dest_parent),
                "anchor_path": list(anchor_path),
                "new_path": list(new_path),
                "physical_page": plan.physical_page,
                "segment_index": plan.segment_index,
            }
        )
        logger.info(
            "{} moved {} -> {} after {} page={} segment={}",
            _LOG_PREFIX,
            source_path,
            new_path,
            anchor_path,
            plan.physical_page,
            plan.segment_index,
        )
    return working, overrides, events


def _detach_by_toc_index(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    *,
    target_toc_index: int,
    parent_path: tuple[str, ...] = (),
    toc_index_start: int = 0,
) -> tuple[list[TitleNode], TitleNode | None, tuple[str, ...] | None]:
    """Detach the paged leaf whose TOC-order index matches ``target_toc_index``."""
    toc_index = toc_index_start
    result: list[TitleNode] = []
    detached: TitleNode | None = None
    detached_path: tuple[str, ...] | None = None
    for node in nodes:
        if detached is not None:
            result.append(node)
            continue
        path = (*parent_path, node.title)
        if node.children:
            new_children, child_detached, child_path = _detach_by_toc_index(
                list(node.children),
                overrides,
                target_toc_index=target_toc_index,
                parent_path=path,
                toc_index_start=toc_index,
            )
            child_leaves = _collect_paged_leaves(
                list(node.children),
                overrides,
                path,
                toc_index_start=toc_index,
            )
            toc_index += len(child_leaves)
            if child_detached is not None:
                detached = child_detached
                detached_path = child_path
                result.append(replace(node, children=new_children))
            else:
                result.append(node)
            continue
        page = own_page(path, overrides)
        if page is None:
            result.append(node)
            continue
        if toc_index == target_toc_index:
            detached = node
            detached_path = path
            toc_index += 1
            continue
        result.append(node)
        toc_index += 1
    return result, detached, detached_path


def _mark_rehome_override(
    overrides: dict[tuple[str, ...], TitleMatch],
    path: tuple[str, ...],
    *,
    segment_index: int,
) -> None:
    match = overrides.get(path)
    if match is None:
        raise ValueError(f"{_LOG_PREFIX} moved override missing path={path!r}")
    overrides[path] = replace(
        match,
        evidence={
            **dict(match.evidence or {}),
            "toc_rehome": {"segment_index": segment_index},
        },
    )


def _detach_node(
    nodes: list[TitleNode],
    path: tuple[str, ...],
) -> tuple[list[TitleNode], TitleNode | None]:
    if not path:
        return nodes, None
    title, *rest = path
    if not rest:
        detached: TitleNode | None = None
        kept: list[TitleNode] = []
        for node in nodes:
            if detached is None and node.title == title:
                detached = node
                continue
            kept.append(node)
        return kept, detached

    updated: list[TitleNode] = []
    detached_node: TitleNode | None = None
    for node in nodes:
        if node.title != title:
            updated.append(node)
            continue
        new_children, detached_node = _detach_node(list(node.children), tuple(rest))
        updated.append(replace(node, children=new_children))
    return updated, detached_node


def _drop_empty_ancestors(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    ancestor_path: tuple[str, ...],
) -> tuple[list[TitleNode], dict[tuple[str, ...], TitleMatch]]:
    """Delete parents that became empty after children moved out."""
    if not ancestor_path:
        return nodes, overrides
    working = nodes
    for depth in range(len(ancestor_path), 0, -1):
        path = ancestor_path[:depth]
        node = node_at_path(working, path)
        if node is None:
            continue
        if node.children:
            break
        working, removed = _detach_node(working, path)
        if removed is not None:
            overrides.pop(path, None)
            logger.info("{} drop empty shell path={}", _LOG_PREFIX, path)
    return working, overrides


def _move_override_prefix(
    overrides: dict[tuple[str, ...], TitleMatch],
    old_prefix: tuple[str, ...],
    new_prefix: tuple[str, ...],
) -> None:
    if old_prefix == new_prefix:
        return
    prefix_len = len(old_prefix)
    moved: dict[tuple[str, ...], TitleMatch] = {}
    for path in list(overrides.keys()):
        if path[:prefix_len] != old_prefix:
            continue
        match = overrides.pop(path)
        moved[new_prefix + path[prefix_len:]] = match
    overrides.update(moved)


def _insert_under_parent(
    *,
    nodes: list[TitleNode],
    parent_path: tuple[str, ...],
    new_node: TitleNode,
    start: int,
    overrides: dict[tuple[str, ...], TitleMatch],
) -> list[TitleNode]:
    if not parent_path:
        return insert_by_start(
            siblings=nodes,
            new_node=new_node,
            start=start,
            parent_path=(),
            primary_overrides=overrides,
        )
    parent = node_at_path(nodes, parent_path)
    if parent is None:
        raise ValueError(
            f"{_LOG_PREFIX} insert parent missing path={parent_path!r}"
        )
    new_children = insert_by_start(
        siblings=list(parent.children),
        new_node=new_node,
        start=start,
        parent_path=parent_path,
        primary_overrides=overrides,
    )
    return replace_node_at_path(
        nodes,
        parent_path,
        replace(parent, children=new_children),
    )
