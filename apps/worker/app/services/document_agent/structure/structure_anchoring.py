"""Compatibility exports for hierarchy anchoring.

Low-level anchoring primitives live in :mod:`anchoring_primitives`; the
calibration-owned orchestrator is resolved lazily to keep imports acyclic.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.services.document_agent.structure import anchoring_primitives as _anchoring
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    TitleMatch,
    TitleNode,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.page_locate_agent import (
    verify_section_page_choice,
)

__all__ = [
    "SkeletonAnchor",
    "TitleMatch",
    "TitleNode",
    "anchor_hierarchy",
    "anchor_hierarchy_from_offset",
    "bulk_offset_matches",
    "deserialize_skeleton_anchor",
    "deserialize_title_match",
    "locate_null_page_parent_overrides",
    "offset_guided_anchoring",
    "prune_out_of_scope_nodes",
    "prune_unanchored_toc_leaves",
    "serialize_skeleton_anchor",
    "serialize_title_match",
    "toc_range_end",
    "toc_range_start",
]

anchor_hierarchy_from_offset = _anchoring.anchor_hierarchy_from_offset
bulk_offset_matches = _anchoring.bulk_offset_matches
deserialize_skeleton_anchor = _anchoring.deserialize_skeleton_anchor
deserialize_title_match = _anchoring.deserialize_title_match
locate_null_page_parent_overrides = _anchoring.locate_null_page_parent_overrides
prune_out_of_scope_nodes = _anchoring.prune_out_of_scope_nodes
prune_unanchored_toc_leaves = _anchoring.prune_unanchored_toc_leaves
serialize_skeleton_anchor = _anchoring.serialize_skeleton_anchor
serialize_title_match = _anchoring.serialize_title_match
toc_range_end = _anchoring.toc_range_end
toc_range_start = _anchoring.toc_range_start


def offset_guided_anchoring(
    *,
    nodes: list[TitleNode],
    offset: int,
    ctx: ToolContext,
    page_count: int,
    calibration_overrides: dict[tuple[str, ...], TitleMatch],
) -> dict[tuple[str, ...], TitleMatch] | None:
    """Forward phase-2 anchoring while preserving the historical patch seam."""
    original = _anchoring.verify_section_page_choice
    _anchoring.verify_section_page_choice = verify_section_page_choice
    try:
        return _anchoring.offset_guided_anchoring(
            nodes=nodes,
            offset=offset,
            ctx=ctx,
            page_count=page_count,
            calibration_overrides=calibration_overrides,
        )
    finally:
        _anchoring.verify_section_page_choice = original


def anchor_hierarchy(
    *,
    nodes: list[TitleNode],
    toc_hierarchies: list[dict[str, Any]] | None,
    page_texts: dict[int, str],
    body_pages: list[int],
    page_count: int,
    ctx: ToolContext | None,
) -> tuple[list[TitleNode], SkeletonAnchor]:
    """Resolve the calibration-owned orchestration entry point on demand."""
    orchestrator = import_module(
        "app.services.document_agent.agents.calibration.orchestrator"
    )
    return orchestrator.anchor_hierarchy(
        nodes=nodes,
        toc_hierarchies=toc_hierarchies,
        page_texts=page_texts,
        body_pages=body_pages,
        page_count=page_count,
        ctx=ctx,
    )
