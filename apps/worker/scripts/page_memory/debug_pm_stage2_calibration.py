#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 2: Calibration over Stage-1 TOC ``doc_profile.json``.

Reads Stage-1 output (TOC hierarchies + optional ``link.physical_page``),
runs calibration (Agent Phase-1 + Phase-2 completion), and writes
``skeleton_anchor`` / ``toc_page_offset`` back onto ``doc_profile.json``.

Requires Stage 0 → Stage 1 first:
  uv run python scripts/page_memory/debug_pm_stage0_bootstrap.py --file ...
  uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ...

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage2_calibration.py --file /path/to/doc.pdf
  uv run python scripts/page_memory/debug_pm_stage2_calibration.py --file /path/to/doc.pdf --no-links
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    base_argparser,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_paths,
    stop_with_trace,
    update_pipeline_state,
    write_debug_json,
)


def main() -> int:
    parser = base_argparser("Stage 2: Calibration SubAgent")
    parser.add_argument(
        "--no-links",
        action="store_true",
        help="Strip link.physical_page from TOC entries before the agent runs",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=16,
        help="Max ReAct rounds per TOC region",
    )
    args = parser.parse_args()

    from app.services.document_agent.agents.calibration import (
        run_calibration_for_all_regions,
    )
    from app.services.document_agent.pdf_text import read_page_texts
    from app.services.document_agent.persist import DOC_PROFILE_FILENAME

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    require_file(
        anatomy_cache,
        hint=(
            "Run Stage 0 then Stage 1 first:\n"
            "  uv run python scripts/page_memory/debug_pm_stage0_bootstrap.py --file ...\n"
            "  uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ..."
        ),
    )

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = int(anatomy.page_count or 0)
    hierarchies = list(getattr(anatomy, "toc_hierarchies", None) or [])

    logger.info("█" * 70)
    logger.info(f"  STAGE 2: CALIBRATION SUBAGENT — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info(f"  no_links={bool(args.no_links)} regions={len(hierarchies)}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    # Production null-page locate needs page texts (same as C4).
    page_texts = read_page_texts(pdf_path, list(range(1, page_count + 1)), timeout=300)
    body_pages = sorted(page_texts.keys())
    logger.info(
        "   read {} pages, {} non-empty",
        len(page_texts),
        sum(1 for text in page_texts.values() if str(text).strip()),
    )

    vlm_model = args.vlm_model or os.environ.get("IMAGE_MODEL") or ""
    planner_model = (
        args.model
        or os.environ.get("HIERARCHY_LLM_MODEL")
        or os.environ.get("NORMOL_MODEL")
        or vlm_model
    )

    doc_agent_dir = out_dir / "_doc_agent"
    doc_agent_dir.mkdir(parents=True, exist_ok=True)

    calibration = run_calibration_for_all_regions(
        pdf_path=pdf_path,
        page_count=page_count,
        toc_hierarchies=hierarchies,
        output_dir=str(doc_agent_dir),
        vlm_model=vlm_model,
        planner_model=planner_model,
        no_links=bool(args.no_links),
        max_rounds=max(1, int(args.max_rounds)),
        page_texts=page_texts,
        body_pages=body_pages,
    )

    # Production-compatible core fields.
    skeleton_anchor = {
        "offset": calibration.get("offset"),
        "offset_status": calibration.get("offset_status"),
        "match_overrides": calibration.get("match_overrides") or {},
        "null_page_report": calibration.get("null_page_report") or [],
        "bulk_count": calibration.get("bulk_count") or 0,
        "pruned_count": calibration.get("pruned_count") or 0,
        "locate_agent": calibration.get("locate_agent") or "offset_only",
    }

    profile_path = out_dir / DOC_PROFILE_FILENAME
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["skeleton_anchor"] = skeleton_anchor
    payload["calibration"] = calibration
    if calibration.get("offset") is not None:
        payload["toc_page_offset"] = calibration.get("offset")
    write_debug_json(profile_path, payload)

    state_path = pipeline_state_path(out_dir)
    update_pipeline_state(
        state_path,
        stage=2,
        document={
            "source_file_name": filename,
            "page_count": page_count,
            "anatomy_path": str(anatomy_cache),
        },
        payload={"skeleton_anchor": skeleton_anchor, "calibration": calibration},
    )

    record_stage(
        trace_stages,
        "calibration",
        page_info={"page_count": page_count},
        variables={
            "status": calibration.get("status"),
            "failure_kind": calibration.get("failure_kind"),
            "offset": calibration.get("offset"),
            "offset_status": calibration.get("offset_status"),
            "bulk_count": calibration.get("bulk_count"),
            "locate_agent": calibration.get("locate_agent"),
            "regime_count": len(calibration.get("regimes") or []),
            "tool_calls": calibration.get("tool_calls"),
            "no_links": bool(args.no_links),
        },
    )
    token_cost_tracker.snapshot_stage("calibration")

    elapsed = time.time() - t_start
    logger.info(
        "✅ Stage 2 done status={} offset={} bulk={} locate={} in {:.1f}s → {}",
        calibration.get("status"),
        calibration.get("offset"),
        calibration.get("bulk_count"),
        calibration.get("locate_agent"),
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
