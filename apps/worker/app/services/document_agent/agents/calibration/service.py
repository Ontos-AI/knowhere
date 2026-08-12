"""Production calibration entry: Agent Phase-1 offset discovery.

Same return shape as the former ``calibrate_offset_via_vlm`` so callers can
swap without changing prune / bulk / null-page.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.services.document_agent.agents.calibration.loop import run_calibration_phase1
from app.services.document_agent.agents.calibration.procedure import (
    pick_primary_offset,
    _seed_overrides_from_samples,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import TitleMatch, TitleNode


def calibrate_offset(
    *,
    nodes: list[TitleNode],
    toc_hierarchies: list[dict[str, Any]] | None,
    ctx: ToolContext | None,
    page_texts: dict[int, str],
    page_count: int,
) -> tuple[int | None, dict[tuple[str, ...], TitleMatch]]:
    """Discover printed→physical offset via the calibration SubAgent (Phase 1).

    Returns ``(offset, seed_overrides)``. Phase 2 (tail / bisect / null-page)
    stays in ``anchor_hierarchy_from_offset`` using the caller's node tree.
    """
    if ctx is None:
        return None, {}
    hierarchies = list(toc_hierarchies or [])
    if not hierarchies:
        return None, {}

    if page_count and not ctx.blackboard.page_count:
        ctx.blackboard.page_count = int(page_count)
    if page_texts and not ctx.blackboard.page_full_text_cache:
        ctx.blackboard.page_full_text_cache = dict(page_texts)

    try:
        phase1 = run_calibration_phase1(
            ctx=ctx,
            toc_hierarchies=hierarchies,
            region_index=0,
            page_count=int(page_count or ctx.blackboard.page_count or 0),
        )
    except Exception as exc:
        logger.warning("[calibration] Phase-1 failed: {}", exc)
        return None, {}

    offset = pick_primary_offset(phase1)
    if offset is None:
        logger.info("[calibration] Phase-1 produced no primary offset")
        return None, {}

    seed = _seed_overrides_from_samples(result=phase1, nodes=nodes)
    logger.info(
        "[calibration] Phase-1 offset={} seed_overrides={}",
        offset,
        len(seed),
    )
    return offset, seed
