"""Graft contained pending TOC trees into the primary hierarchy by physical start page."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    coverage_by_path,
    deepest_covering_path,
    resolve_hierarchy_page_ranges,
)

_LOG_PREFIX = "[profile.toc_graft]"


@dataclass(frozen=True)
class ContainedGraftResult:
    nodes: list[TitleNode]
    match_overrides: dict[tuple[str, ...], TitleMatch]
    events: list[dict[str, Any]]


def graft_contained_toc(
    *,
    primary_nodes: list[TitleNode],
    primary_overrides: dict[tuple[str, ...], TitleMatch],
    contained_nodes: list[TitleNode],
    contained_overrides: dict[tuple[str, ...], TitleMatch],
    page_count: int,
    body_pages: list[int],
) -> ContainedGraftResult:
    """Merge one contained TOC forest into the current primary tree."""
    overrides = dict(primary_overrides)
    events: list[dict[str, Any]] = []
    ranges = resolve_hierarchy_page_ranges(
        primary_nodes,
        page_count=page_count,
        body_pages=body_pages,
        match_overrides=overrides,
    )
    coverage = coverage_by_path(ranges)
    nodes = _graft_children(
        parent=None,
        parent_path=(),
        primary_children=list(primary_nodes),
        contained_children=list(contained_nodes),
        contained_prefix=(),
        primary_overrides=overrides,
        contained_overrides=contained_overrides,
        coverage=coverage,
        events=events,
    )
    return ContainedGraftResult(nodes=nodes, match_overrides=overrides, events=events)


def _graft_children(
    *,
    parent: TitleNode | None,
    parent_path: tuple[str, ...],
    primary_children: list[TitleNode],
    contained_children: list[TitleNode],
    contained_prefix: tuple[str, ...],
    primary_overrides: dict[tuple[str, ...], TitleMatch],
    contained_overrides: dict[tuple[str, ...], TitleMatch],
    coverage: dict[tuple[str, ...], tuple[int, int]],
    events: list[dict[str, Any]],
) -> list[TitleNode]:
    working = list(primary_children)
    for child in contained_children:
        working = _graft_one_child(
            parent=parent,
            parent_path=parent_path,
            primary_children=working,
            child=child,
            contained_prefix=contained_prefix,
            primary_overrides=primary_overrides,
            contained_overrides=contained_overrides,
            coverage=coverage,
            events=events,
        )
    return working


def _graft_one_child(
    *,
    parent: TitleNode | None,
    parent_path: tuple[str, ...],
    primary_children: list[TitleNode],
    child: TitleNode,
    contained_prefix: tuple[str, ...],
    primary_overrides: dict[tuple[str, ...], TitleMatch],
    contained_overrides: dict[tuple[str, ...], TitleMatch],
    coverage: dict[tuple[str, ...], tuple[int, int]],
    events: list[dict[str, Any]],
) -> list[TitleNode]:
    contained_path = (*contained_prefix, child.title)
    contained_match = contained_overrides.get(contained_path)
    if contained_match is None:
        return _graft_children(
            parent=parent,
            parent_path=parent_path,
            primary_children=primary_children,
            contained_children=list(child.children),
            contained_prefix=contained_path,
            primary_overrides=primary_overrides,
            contained_overrides=contained_overrides,
            coverage=coverage,
            events=events,
        )

    start = contained_match.page
    hit_indexes = _sibling_hits(
        siblings=primary_children,
        parent_path=parent_path,
        start=start,
        primary_overrides=primary_overrides,
    )
    if hit_indexes:
        if len(hit_indexes) > 1:
            logger.info(
                "{} collision parent_path={} start={} sibling_titles={}",
                _LOG_PREFIX,
                parent_path,
                start,
                [primary_children[index].title for index in hit_indexes],
            )
        index = hit_indexes[0]
        matched = primary_children[index]
        matched_path = (*parent_path, matched.title)
        events.append(
            {
                "action": "dedup",
                "contained_path": contained_path,
                "primary_path": matched_path,
                "start": start,
                "title_equal": matched.title == child.title,
            }
        )
        # Do not remap descendant overrides here — only successfully
        # attached/deduped children may write into primary_overrides.
        new_matched = replace(
            matched,
            children=_graft_children(
                parent=matched,
                parent_path=matched_path,
                primary_children=list(matched.children),
                contained_children=list(child.children),
                contained_prefix=contained_path,
                primary_overrides=primary_overrides,
                contained_overrides=contained_overrides,
                coverage=coverage,
                events=events,
            ),
        )
        updated = list(primary_children)
        updated[index] = new_matched
        return updated

    if parent is not None:
        span = coverage.get(parent_path)
        if span is not None and span[0] <= start <= span[1]:
            return _attach_new_child(
                parent=parent,
                parent_path=parent_path,
                primary_children=primary_children,
                child=child,
                contained_path=contained_path,
                start=start,
                primary_overrides=primary_overrides,
                contained_overrides=contained_overrides,
                events=events,
            )
        reason = (
            "outside_parent_coverage"
            if span is not None
            else "missing_parent_coverage"
        )
        _record_skip(
            events=events,
            contained_path=contained_path,
            start=start,
            reason=reason,
            parent_path=parent_path,
            parent_span=span,
        )
        return primary_children

    covering = deepest_covering_path(coverage, start=start, end=start)
    if covering is None:
        _record_skip(
            events=events,
            contained_path=contained_path,
            start=start,
            reason="no_covering_path",
        )
        return primary_children
    covering_node = _node_at_path(primary_children, covering)
    if covering_node is None:
        _record_skip(
            events=events,
            contained_path=contained_path,
            start=start,
            reason="covering_node_missing",
            parent_path=covering,
        )
        return primary_children
    updated_parent = replace(
        covering_node,
        children=_graft_one_child(
            parent=covering_node,
            parent_path=covering,
            primary_children=list(covering_node.children),
            child=child,
            contained_prefix=contained_prefix,
            primary_overrides=primary_overrides,
            contained_overrides=contained_overrides,
            coverage=coverage,
            events=events,
        ),
    )
    return _replace_node_at_path(primary_children, covering, updated_parent)


def _attach_new_child(
    *,
    parent: TitleNode,
    parent_path: tuple[str, ...],
    primary_children: list[TitleNode],
    child: TitleNode,
    contained_path: tuple[str, ...],
    start: int,
    primary_overrides: dict[tuple[str, ...], TitleMatch],
    contained_overrides: dict[tuple[str, ...], TitleMatch],
    events: list[dict[str, Any]],
) -> list[TitleNode]:
    grafted = _rebase_levels(child, (parent.level + 1) - child.level)
    new_path = (*parent_path, grafted.title)
    events.append(
        {
            "action": "attach",
            "contained_path": contained_path,
            "primary_path": new_path,
            "start": start,
        }
    )
    _remap_overrides(
        contained_overrides=contained_overrides,
        primary_overrides=primary_overrides,
        old_prefix=contained_path,
        new_prefix=new_path,
        drop_root=False,
    )
    return _insert_by_start(
        siblings=primary_children,
        new_node=grafted,
        start=start,
        parent_path=parent_path,
        primary_overrides=primary_overrides,
    )


def _sibling_hits(
    *,
    siblings: list[TitleNode],
    parent_path: tuple[str, ...],
    start: int,
    primary_overrides: dict[tuple[str, ...], TitleMatch],
) -> list[int]:
    hits: list[int] = []
    for index, sibling in enumerate(siblings):
        match = primary_overrides.get((*parent_path, sibling.title))
        if match is not None and match.page == start:
            hits.append(index)
    return hits


def _record_skip(
    *,
    events: list[dict[str, Any]],
    contained_path: tuple[str, ...],
    start: int,
    reason: str,
    parent_path: tuple[str, ...] | None = None,
    parent_span: tuple[int, int] | None = None,
) -> None:
    event: dict[str, Any] = {
        "action": "skip",
        "contained_path": contained_path,
        "start": start,
        "reason": reason,
    }
    if parent_path is not None:
        event["parent_path"] = parent_path
    if parent_span is not None:
        event["parent_span"] = list(parent_span)
    events.append(event)


def _remap_overrides(
    *,
    contained_overrides: dict[tuple[str, ...], TitleMatch],
    primary_overrides: dict[tuple[str, ...], TitleMatch],
    old_prefix: tuple[str, ...],
    new_prefix: tuple[str, ...],
    drop_root: bool,
) -> None:
    prefix_len = len(old_prefix)
    for path, match in contained_overrides.items():
        if path[:prefix_len] != old_prefix:
            continue
        if drop_root and path == old_prefix:
            continue
        new_path = new_prefix + path[prefix_len:]
        if new_path not in primary_overrides:
            primary_overrides[new_path] = match


def _rebase_levels(node: TitleNode, delta: int) -> TitleNode:
    return replace(
        node,
        level=node.level + delta,
        children=[_rebase_levels(child, delta) for child in node.children],
    )


def _insert_by_start(
    *,
    siblings: list[TitleNode],
    new_node: TitleNode,
    start: int,
    parent_path: tuple[str, ...],
    primary_overrides: dict[tuple[str, ...], TitleMatch],
) -> list[TitleNode]:
    insert_at = len(siblings)
    for index, sibling in enumerate(siblings):
        match = primary_overrides.get((*parent_path, sibling.title))
        if match is not None and match.page > start:
            insert_at = index
            break
    updated = list(siblings)
    updated.insert(insert_at, new_node)
    return updated


def _node_at_path(nodes: list[TitleNode], path: tuple[str, ...]) -> TitleNode | None:
    current: list[TitleNode] = nodes
    node: TitleNode | None = None
    for title in path:
        node = next((item for item in current if item.title == title), None)
        if node is None:
            return None
        current = list(node.children)
    return node


def _replace_node_at_path(
    nodes: list[TitleNode],
    path: tuple[str, ...],
    new_node: TitleNode,
) -> list[TitleNode]:
    title, *rest = path
    updated: list[TitleNode] = []
    for node in nodes:
        if node.title != title:
            updated.append(node)
            continue
        if not rest:
            updated.append(new_node)
            continue
        updated.append(
            replace(
                node,
                children=_replace_node_at_path(node.children, tuple(rest), new_node),
            )
        )
    return updated
