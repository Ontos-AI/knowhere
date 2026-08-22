#!/usr/bin/env python3
# ruff: noqa: E402
"""Debug: dump production null-page locate report via Stage-2 anchoring.

Uses the live leaf ReAct + parent window locator path (no patches).

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_null_page_react.py \\
    --file "/path/to/doc.pdf"
"""

from __future__ import annotations

import sys
import time
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from loguru import logger

from _debug_pm_shared import (
    _build_debug_coordinator,
    base_argparser,
    load_anatomy_cache,
    load_stage0_into_coordinator,
    page_text_cache_path,
    require_file,
    resolve_anatomy_cache_path,
    resolve_paths,
    stage0_state_path,
    write_debug_json,
)

from app.services.document_agent.structure.null_page_react import (
    REACT_PLANNER_GREP_BUDGET,
    react_budget,
)


def main() -> int:
    parser = base_argparser(
        "Debug: production null-page locate via run_toc_anchoring (Stage 2)"
    )
    args = parser.parse_args()

    from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
    from app.services.document_agent.validators import single_shard_plan
    from shared.core.config import settings

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    require_file(stage0_state_path(out_dir), hint="Run Stage 0 first")
    require_file(page_text_cache_path(out_dir), hint="Re-run Stage 0")
    require_file(anatomy_cache, hint="Run Stage 1 first")

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = int(anatomy.page_count or 0)
    hierarchies = list(getattr(anatomy, "toc_hierarchies", None) or [])

    logger.info("█" * 70)
    logger.info("  Production null-page locate dump — {}", filename)
    logger.info("  OUTPUT: {}", out_dir)
    logger.info("█" * 70)

    t0 = time.time()
    previous_image_model = settings.IMAGE_MODEL
    try:
        coordinator = _build_debug_coordinator(
            pdf_path=pdf_path,
            job_id=filename,
            out_dir=out_dir,
            model=None if args.no_vlm else args.model,
            settings_extra={"skip_toc_anchoring": False},
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

        anchor = bb.skeleton_anchor or {}
        report = list(anchor.get("null_page_report") or [])
        overrides = dict(anchor.get("match_overrides") or {})
        react_hits = []
        for path, match in overrides.items():
            source = (
                match.get("source")
                if isinstance(match, dict)
                else getattr(match, "source", None)
            )
            if source != "react_normalized_grep_vlm":
                continue
            titles = path if isinstance(path, (list, tuple)) else (path,)
            page = (
                match.get("page")
                if isinstance(match, dict)
                else getattr(match, "page", None)
            )
            react_hits.append({"path": list(titles), "page": page})

        payload = {
            "policy": {
                "prune_pre": "keep_null_page_nodes=True",
                "leaf_probe": "null_page_react.locate_null_page_node_overrides",
                "parent_probe": "anchoring_primitives.locate_null_page_parent_overrides",
                "prune_post": "keep_null_page_nodes=False (drop unresolved)",
                "react_budget": react_budget(),
                "react_planner_grep_budget": REACT_PLANNER_GREP_BUDGET,
                "seed_full_title_grep": "free",
                "strip_auto_regrep": "free",
                "max_planner_turns": REACT_PLANNER_GREP_BUDGET + 2,
            },
            "offset": anchor.get("offset"),
            "pruned_count": anchor.get("pruned_count"),
            "bulk_count": anchor.get("bulk_count"),
            "override_count": len(overrides),
            "null_page_report": report,
            "react_override_hits": react_hits,
            "elapsed_s": round(time.time() - t0, 2),
        }

        out_path = out_dir / "_doc_agent" / "null_page_react_report.json"
        write_debug_json(out_path, payload)
        logger.info("wrote {}", out_path)
        logger.info(
            "null_page rows={} react_override_hits={}",
            len(report),
            len(react_hits),
        )
        for row in report:
            logger.info(
                "  [{}] {} search_scope={} result={} page={} loops={} failed_sibling={}",
                row.get("kind"),
                row.get("path_titles"),
                row.get("search_scope"),
                row.get("result"),
                row.get("page"),
                len(row.get("react_attempts") or []),
                row.get("failed_sibling"),
            )
        for hit in react_hits:
            logger.info("  OVERRIDE {} -> p{}", hit["path"], hit["page"])
    finally:
        if args.model:
            settings.IMAGE_MODEL = previous_image_model

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
