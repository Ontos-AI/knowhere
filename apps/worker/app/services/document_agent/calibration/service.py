"""Production calibration entry: Phase-1 offset discovery.

Returns a full ``CalibrationResult`` (all regimes). Callers run multi-regime
Phase-2 via ``finalize_calibration_result`` / ``anchor_hierarchy``.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.services.document_agent.calibration.phase1 import run_calibration_phase1
from app.services.document_agent.calibration.types import (
    FAILURE_TOC_EMPTY,
    CalibrationResult,
)
from app.services.document_agent.manifest import ToolContext


def calibrate_offset(
    *,
    toc_hierarchies: list[dict[str, Any]] | None,
    ctx: ToolContext | None,
    page_texts: dict[int, str],
    page_count: int,
) -> CalibrationResult:
    """Discover printed→physical offsets by deterministic forward scan (Phase 1).

    Returns the full Phase-1 ``CalibrationResult`` including every regime that
    was confirmed. Phase-2 (per-regime bulk / bisect / null-page merge) is owned
    by ``finalize_calibration_result`` / ``anchor_hierarchy``.
    """
    if ctx is None:
        return CalibrationResult(status="failed", notes="ctx missing")
    hierarchies = list(toc_hierarchies or [])
    if not hierarchies:
        return CalibrationResult(
            status="failed",
            notes="toc_hierarchies empty",
            failure_kind=FAILURE_TOC_EMPTY,
        )

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
