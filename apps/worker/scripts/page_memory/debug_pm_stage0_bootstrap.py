#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 0: bootstrap + coarse VLM + text scan + asset probe (production-aligned).

Matches ``ProfileCoordinator._run_coarse`` through ``stop_after_asset_probe``:
  bootstrap → coarse VLM → text scan → asset probe
  → persist stage0_state + page_full_text_cache

TOC Find / extract belong to Stage 1
(``debug_pm_stage1_hierarchy.py``). Calibration belongs to Stage 2.

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage0_bootstrap.py --file /path/to/doc.pdf
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import time

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    base_argparser,
    page_text_cache_path,
    record_stage,
    resolve_paths,
    run_stage0_bootstrap,
    stage0_state_path,
    stop_with_trace,
)


def main() -> int:
    parser = base_argparser(
        "Stage 0: bootstrap + coarse VLM + text scan + asset probe (no TOC)"
    )
    args = parser.parse_args()

    pdf_path, filename, out_dir = resolve_paths(args)

    logger.info("█" * 70)
    logger.info(
        f"  STAGE 0: BOOTSTRAP + COARSE VLM + TEXT SCAN + ASSET PROBE — {filename}"
    )
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    coordinator, profile, state_path = run_stage0_bootstrap(
        pdf_path,
        filename,
        out_dir,
        args.model,
    )
    page_count = int(coordinator.blackboard.page_count or 0)
    text_pages = len(coordinator.blackboard.page_full_text_cache or {})
    asset_pages = sum(
        1
        for feature in (coordinator.blackboard.page_features or [])
        if getattr(feature, "has_asset", False)
    )
    record_stage(
        trace_stages,
        "bootstrap_coarse_scan_assets",
        page_info={"page_count": page_count},
        variables={
            "source": "stop_after_asset_probe",
            "category": getattr(profile, "category", None),
            "routing_category": getattr(profile, "routing_category", None),
            "is_scanned": bool(getattr(profile, "is_scanned", False)),
            "text_pages": text_pages,
            "has_asset_pages": asset_pages,
            "assets_probed": bool(
                coordinator.blackboard.global_signals.get("assets_probed")
            ),
            "stage0_state": str(state_path),
            "page_full_text_cache": str(page_text_cache_path(out_dir)),
        },
    )
    token_cost_tracker.snapshot_stage("bootstrap_coarse_scan_assets")

    elapsed = time.time() - t_start
    logger.info(f"✅ Stage 0 done in {elapsed:.1f}s → {stage0_state_path(out_dir)}")

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="bootstrap",
        page_count=page_count,
        pipeline_stage=0,
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
    )


if __name__ == "__main__":
    raise SystemExit(main())
