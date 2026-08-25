"""Same-forest TOC rehome: layer-wise own-page monotonic repair before classify."""

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
    dest_parent_path: tuple[str, ...]
    physical_page: int


@dataclass(frozen=True)
class RehomeResult:
    nodes: list[TitleNode]
    match_overrides: dict[tuple[str, ...], TitleMatch]
    events: list[dict[str, Any]]


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
) -> RehomeResult:
    """Repair non-monotonic own-page order within one calibrated TOC forest."""
    overrides = dict(match_overrides)
    plans = _collect_plans(siblings=list(nodes), parent_path=(), overrides=overrides)
    if not plans:
        return RehomeResult(nodes=list(nodes), match_overrides=overrides, events=[])

    events: list[dict[str, Any]] = [
        {
            "action": "plan",
            "source_path": list(plan.source_path),
            "dest_parent_path": list(plan.dest_parent_path),
            "physical_page": plan.physical_page,
        }
        for plan in plans
    ]
    logger.info("{} plans={}", _LOG_PREFIX, len(plans))
    new_nodes, overrides = _apply_plans(list(nodes), overrides, plans)
    return RehomeResult(nodes=new_nodes, match_overrides=overrides, events=events)


def rehome_skeleton_forest(
    nodes: list[TitleNode],
    anchor: SkeletonAnchor,
) -> tuple[list[TitleNode], SkeletonAnchor, list[dict[str, Any]]]:
    """Run ``rehome_forest`` and return an updated ``SkeletonAnchor``."""
    result = rehome_forest(nodes, anchor.match_overrides)
    return (
        result.nodes,
        replace(anchor, match_overrides=result.match_overrides),
        result.events,
    )


def _collect_plans(
    *,
    siblings: list[TitleNode],
    parent_path: tuple[str, ...],
    overrides: dict[tuple[str, ...], TitleMatch],
) -> list[RehomePlan]:
    plans: list[RehomePlan] = []
    effective = _expand_effective(
        siblings=siblings,
        parent_path=parent_path,
        overrides=overrides,
    )
    cursor: int | None = None
    for _node, source_path, page in effective:
        if cursor is not None and page < cursor:
            plans.append(
                RehomePlan(
                    source_path=source_path,
                    dest_parent_path=parent_path,
                    physical_page=page,
                )
            )
            continue
        if cursor is None or page > cursor:
            cursor = page

    # Drill into every own-paged effective node (includes transparent promotions).
    # Unpaged shells are not parents here — their children already appear in effective.
    for node, source_path, _page in effective:
        if node.children:
            plans.extend(
                _collect_plans(
                    siblings=list(node.children),
                    parent_path=source_path,
                    overrides=overrides,
                )
            )
    return plans


def _expand_effective(
    *,
    siblings: list[TitleNode],
    parent_path: tuple[str, ...],
    overrides: dict[tuple[str, ...], TitleMatch],
) -> list[tuple[TitleNode, tuple[str, ...], int]]:
    """Build this level's ordered sequence of own-paged nodes (unpaged non-leaves transparent)."""
    effective: list[tuple[TitleNode, tuple[str, ...], int]] = []
    for sibling in siblings:
        path = (*parent_path, sibling.title)
        page = own_page(path, overrides)
        if page is not None:
            effective.append((sibling, path, page))
            continue
        if sibling.children:
            effective.extend(
                _expand_effective(
                    siblings=list(sibling.children),
                    parent_path=path,
                    overrides=overrides,
                )
            )
    return effective


def _apply_plans(
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], TitleMatch],
    plans: list[RehomePlan],
) -> tuple[list[TitleNode], dict[tuple[str, ...], TitleMatch]]:
    ordered = sorted(
        plans,
        key=lambda plan: (-len(plan.source_path), plan.physical_page, plan.source_path),
    )
    working = list(nodes)
    for plan in ordered:
        working, detached = _detach_node(working, plan.source_path)
        if detached is None:
            raise ValueError(
                f"{_LOG_PREFIX} detach miss source_path={plan.source_path!r}"
            )
        target_level = len(plan.dest_parent_path) + 1
        detached = rebase_levels(detached, target_level - detached.level)
        new_path = (*plan.dest_parent_path, detached.title)
        working = _insert_under_parent(
            nodes=working,
            parent_path=plan.dest_parent_path,
            new_node=detached,
            start=plan.physical_page,
            overrides=overrides,
        )
        _move_override_prefix(overrides, plan.source_path, new_path)
        working, overrides = _drop_empty_ancestors(
            working,
            overrides,
            plan.source_path[:-1],
        )
        logger.info(
            "{} moved {} -> {} page={}",
            _LOG_PREFIX,
            plan.source_path,
            new_path,
            plan.physical_page,
        )
    return working, overrides


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
    # Drop from deepest ancestor upward.
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
