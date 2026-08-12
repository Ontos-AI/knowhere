"""Compatibility exports for hierarchy anchoring.

Low-level anchoring primitives live in :mod:`anchoring_primitives`; the
calibration-owned orchestrator is resolved lazily to keep imports acyclic.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.services.document_agent.structure.anchoring_primitives import (
    anchor_hierarchy_from_offset,
    bulk_offset_matches,
    deserialize_skeleton_anchor,
    deserialize_title_match,
    locate_null_page_parent_overrides,
    prune_out_of_scope_nodes,
    prune_unanchored_toc_leaves,
    serialize_skeleton_anchor,
    serialize_title_match,
    toc_range_end,
    toc_range_start,
)
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    TitleMatch,
    TitleNode,
    ToolContext,
)
from app.services.document_agent.structure import anchoring_primitives as _anchoring
from app.services.document_agent.structure.page_locate_agent import (
    verify_section_page_choice,
)


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
