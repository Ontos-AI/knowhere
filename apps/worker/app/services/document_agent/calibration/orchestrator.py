"""Calibration orchestration across Phase-1 and structure Phase-2."""

from __future__ import annotations

from typing import Any

from app.services.document_agent.calibration.procedure import (
    finalize_calibration_result,
    flat_toc_entries,
)
from app.services.document_agent.calibration import service
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import TitleNode
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    anchor_hierarchy_from_offset,
)


def anchor_hierarchy(
    *,
    nodes: list[TitleNode],
    toc_hierarchies: list[dict[str, Any]] | None,
    page_texts: dict[int, str],
    body_pages: list[int],
    page_count: int,
    ctx: ToolContext | None,
) -> tuple[list[TitleNode], SkeletonAnchor]:
    """Run calibration Phase-1 and the production Phase-2 completion."""
    phase1 = service.calibrate_offset(
        nodes=nodes,
        toc_hierarchies=toc_hierarchies,
        ctx=ctx,
        page_texts=page_texts,
        page_count=page_count,
    )
    if phase1.status == "failed" and not phase1.regimes:
        return anchor_hierarchy_from_offset(
            nodes=nodes,
            offset_hint=None,
            calibration_overrides={},
            page_texts=page_texts,
            body_pages=body_pages,
            page_count=page_count,
            ctx=ctx,
        )
    working, anchor, _finalized = finalize_calibration_result(
        result=phase1,
        entries=flat_toc_entries(toc_hierarchies),
        toc_hierarchies=list(toc_hierarchies or []),
        ctx=ctx,
        page_count=page_count,
        page_texts=page_texts,
        body_pages=body_pages,
        nodes=nodes,
    )
    return working, anchor
