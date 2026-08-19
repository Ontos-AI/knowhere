"""Production calibration entry: Agent Phase-1 offset discovery.

Returns a full ``CalibrationResult`` (all regimes). Callers run multi-regime
Phase-2 via ``finalize_calibration_result`` / ``anchor_hierarchy``.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.services.document_agent.agents.calibration.loop import run_calibration_phase1
from app.services.document_agent.agents.calibration.types import CalibrationResult
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import TitleNode


def calibrate_offset(
    *,
    nodes: list[TitleNode],
    toc_hierarchies: list[dict[str, Any]] | None,
    ctx: ToolContext | None,
    page_texts: dict[int, str],
    page_count: int,
) -> CalibrationResult:
    """Discover printed→physical offsets via the calibration SubAgent (Phase 1).

    Returns the full Phase-1 ``CalibrationResult`` including every regime the
    agent submitted. Phase-2 (per-regime bulk / bisect / null-page merge) is
    owned by ``finalize_calibration_result`` / ``anchor_hierarchy``.
    """
    del nodes  # Phase-1 works from toc_hierarchies entries; nodes used in Phase-2.
    if ctx is None:
        return CalibrationResult(status="failed", notes="ctx missing")
    hierarchies = list(toc_hierarchies or [])
    if not hierarchies:
        return CalibrationResult(status="failed", notes="toc_hierarchies empty")

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
        return CalibrationResult(status="failed", notes=str(exc))

    logger.info(
        "[calibration] Phase-1 status={} failure_kind={} regime_offsets={}",
        phase1.status,
        phase1.failure_kind,
        [regime.offset for regime in phase1.regimes],
    )
    return phase1
