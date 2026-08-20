#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 2: Production ``run_toc_anchoring`` over Stage-1 TOC.

Same PROFILE anchoring path as production PAGE/TEXT:

  select primary/pending → calibrate (Agent Phase-1 + Phase-2) →
  classify contained/parallel → graft contained → write skeleton_*

Also resolves coarse skeletons (C4 resolve-only) into pipeline state so
Stage 3 can resume without re-anchoring. No scope build or fine hierarchy.

Requires Stage 0 → Stage 1 first:
  uv run python scripts/page_memory/debug_pm_stage0_bootstrap.py --file ...
  uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ...

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage2_calibration.py --file /path/to/doc.pdf
"""

from __future__ import annotations

import sys
import time
from pathlib import Path as _Path
from typing import Any

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    _build_debug_coordinator,
    _serialize_skeletons,
    base_argparser,
    load_anatomy_cache,
    load_stage0_into_coordinator,
    page_text_cache_path,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_anatomy_cache_path,
    resolve_paths,
    stage0_state_path,
    stop_with_trace,
    update_pipeline_state,
    write_debug_json,
)


def _pending_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        toc = record.get("toc") if isinstance(record, dict) else None
        rows.append(
            {
                "toc_range": (toc or {}).get("toc_range") if isinstance(toc, dict) else None,
                "relationship": record.get("relationship"),
                "grafted": bool(record.get("grafted")),
                "graft_events": len(list(record.get("graft") or [])),
                "has_nodes": bool(record.get("nodes")),
            }
        )
    return rows


def main() -> int:
    parser = base_argparser(
        "Stage 2: Production run_toc_anchoring (calibrate + classify + graft)"
    )
    args = parser.parse_args()

    from app.services.document_agent.persist import DOC_PROFILE_FILENAME
    from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
    from app.services.document_agent.validators import single_shard_plan
    from app.services.page_memory.skeleton_extractor import extract_section_skeletons
    from shared.core.config import settings

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    require_file(
        stage0_state_path(out_dir),
        hint=(
            "Run Stage 0 first:\n"
            "  uv run python scripts/page_memory/debug_pm_stage0_bootstrap.py --file ..."
        ),
    )
    require_file(
        page_text_cache_path(out_dir),
        hint="Stage-0 page_full_text_cache.json missing; re-run Stage 0",
    )
    require_file(
        anatomy_cache,
        hint=(
            "Run Stage 1 first:\n"
            "  uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ..."
        ),
    )

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = int(anatomy.page_count or 0)
    hierarchies = list(getattr(anatomy, "toc_hierarchies", None) or [])

    logger.info("█" * 70)
    logger.info(f"  STAGE 2: run_toc_anchoring (production) — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info("  regions={}", len(hierarchies))
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    previous_image_model = settings.IMAGE_MODEL
    try:
        coordinator = _build_debug_coordinator(
            pdf_path=pdf_path,
            job_id=filename,
            out_dir=out_dir,
            model=args.model,
            settings_extra={
                # Stage-1 already extracted TOC; Stage-2 only anchors.
                "skip_toc_anchoring": False,
            },
        )
        load_stage0_into_coordinator(coordinator, out_dir)
        bb = coordinator.blackboard
        bb.toc_result = anatomy.toc_result
        bb.toc_hierarchies = hierarchies
        bb.shard_plan = anatomy.shard_plan or single_shard_plan(page_count)
        bb.skeleton_anchor = None
        bb.skeleton_nodes = None
        bb.pending_skeleton_anchors = []

        run_toc_anchoring(coordinator.ctx)

        # Persist the same PROFILE fields production anatomy carries.
        from app.services.document_agent.persist import build_anatomy_map

        bb.shard_plan = bb.shard_plan or single_shard_plan(page_count)
        anchored = build_anatomy_map(coordinator.ctx)
        profile_path = out_dir / DOC_PROFILE_FILENAME
        write_debug_json(profile_path, anchored.to_dict())
        try:
            (out_dir / "_doc_agent" / "anatomy_map.json").unlink()
        except FileNotFoundError:
            pass

        page_texts = dict(bb.page_full_text_cache or {})
        skeletons = extract_section_skeletons(
            anatomy=anchored,
            filename=filename,
            page_texts=page_texts,
        )
    finally:
        if args.model:
            settings.IMAGE_MODEL = previous_image_model

    pending_records = list(
        getattr(coordinator.blackboard, "pending_skeleton_anchors", None) or []
    )
    skeleton_anchor = getattr(coordinator.blackboard, "skeleton_anchor", None) or {}
    pending_summary = _pending_summary(pending_records)

    state_path = pipeline_state_path(out_dir)
    update_pipeline_state(
        state_path,
        stage=2,
        document={
            "source_file_name": filename,
            "page_count": page_count,
            "anatomy_path": str(profile_path),
        },
        payload={
            "skeleton_anchor": skeleton_anchor,
            "skeleton_nodes": list(
                getattr(coordinator.blackboard, "skeleton_nodes", None) or []
            ),
            "pending_skeleton_anchors": pending_records,
            "pending_summary": pending_summary,
            "skeletons": _serialize_skeletons(skeletons),
        },
    )

    record_stage(
        trace_stages,
        "toc_anchoring",
        page_info={"page_count": page_count},
        variables={
            "offset": skeleton_anchor.get("offset")
            if isinstance(skeleton_anchor, dict)
            else None,
            "offset_status": skeleton_anchor.get("offset_status")
            if isinstance(skeleton_anchor, dict)
            else None,
            "bulk_count": skeleton_anchor.get("bulk_count")
            if isinstance(skeleton_anchor, dict)
            else None,
            "locate_method": skeleton_anchor.get("locate_method")
            if isinstance(skeleton_anchor, dict)
            else None,
            "skeleton_count": len(skeletons),
            "pending": pending_summary,
        },
    )
    token_cost_tracker.snapshot_stage("toc_anchoring")

    elapsed = time.time() - t_start
    logger.info(
        "✅ Stage 2 done offset={} bulk={} skeletons={} pending={} in {:.1f}s → {}",
        skeleton_anchor.get("offset") if isinstance(skeleton_anchor, dict) else None,
        skeleton_anchor.get("bulk_count") if isinstance(skeleton_anchor, dict) else None,
        len(skeletons),
        pending_summary,
        elapsed,
        profile_path,
    )

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="calibration",
        page_count=page_count,
        pipeline_stage=2,
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
        extra_summary={"pending": pending_summary, "skeleton_count": len(skeletons)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
