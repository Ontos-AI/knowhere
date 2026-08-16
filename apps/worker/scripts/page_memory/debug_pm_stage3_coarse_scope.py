#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 3: Coarse scope generation + per-scope directory creation.

Generates coarse hierarchy scopes from skeletons and creates per-scope
directories with ``skeletons.json`` (meta + coarse nodes) plus empty
``page_tags.json`` / ``assets.json`` placeholders for later stages.

Requires Stage 2 output: _doc_agent/pipeline_state.json, doc_profile.json

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage3_coarse_scope.py --file /path/to/doc.pdf
  uv run python scripts/page_memory/debug_pm_stage3_coarse_scope.py --fat-only
  uv run python scripts/page_memory/debug_pm_stage3_coarse_scope.py --page-range 225-302
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from _debug_pm_shared import *  # noqa: F401,F403
import time

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    base_argparser,
    build_debug_coarse_scopes,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    load_pipeline_skeletons,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_paths,
    scope_id_for_pages,
    stop_with_trace,
    update_pipeline_state,
    write_debug_json,
    _serialize_skeletons,
    _serialize_scope_skeletons,
)


def main() -> int:
    parser = base_argparser("Stage 3: Coarse scope generation")
    parser.add_argument(
        "--fat-only", action="store_true",
        help="Auto-select the largest coarse scope only",
    )
    parser.add_argument(
        "--page-range", default=None,
        help="Only process page range, e.g. '225-302'",
    )
    parser.add_argument(
        "--all-scopes", action="store_true",
        help="Process all coarse scopes (default behavior)",
    )
    args = parser.parse_args()

    from app.services.page_memory.skeleton_extractor import SectionSkeleton

    pdf_path, filename, out_dir = resolve_paths(args)
    doc_agent_dir = out_dir / "_doc_agent"
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    state_path = pipeline_state_path(out_dir)
    legacy_locate_cache = doc_agent_dir / "locate_cache.json"

    if not state_path.exists() and not legacy_locate_cache.exists():
        require_file(
            state_path,
            hint="Run Stage 2 first: uv run python scripts/page_memory/debug_pm_stage2_calibration.py --file ...",
        )
    require_file(
        anatomy_cache,
        hint="Run Stage 1 first: uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ...",
    )

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = anatomy.page_count
    skeletons = load_pipeline_skeletons(
        state_path,
        legacy_locate_cache=legacy_locate_cache,
    )
    if not state_path.exists():
        update_pipeline_state(
            state_path,
            stage=2,
            document={
                "source_file_name": filename,
                "page_count": page_count,
                "anatomy_path": str(anatomy_cache),
            },
            payload={
                "calibration": {},
                "null_page_parent_locate": {},
                "skeletons": _serialize_skeletons(skeletons),
                "migrated_from": str(legacy_locate_cache),
            },
        )

    logger.info("█" * 70)
    logger.info(f"  STAGE 3: COARSE SCOPE GENERATION — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()
    from toc_page_policy import TocPagePolicy

    toc_policy = TocPagePolicy.from_anatomy(anatomy)

    # ── Build coarse scopes ──
    coarse_scopes = build_debug_coarse_scopes(
        skeletons=skeletons,
        filename=filename,
        page_count=page_count,
        anatomy=anatomy,
    )

    if not coarse_scopes:
        root_skel = SectionSkeleton(
            section_path=f"{filename}/Root",
            level=1,
            start_page=1,
            end_page=page_count,
            title="Root",
            parent_path=filename,
            evidence={"source": "fallback_root", "confidence": 0.0},
        )
        coarse_scopes = [
            {
                "scope_id": scope_id_for_pages(1, page_count),
                "skeletons": [root_skel],
                "start_page": 1,
                "end_page": page_count,
                "strategy": "fallback_root",
                "processing_pages": toc_policy.filter_processing_pages(
                    list(range(1, page_count + 1))
                ),
                "excluded_toc_pages": sorted(toc_policy.pure_toc_pages),
            }
        ]
        logger.info("   no skeleton hierarchy → fallback Root scope p1-{}", page_count)

    # ── Scope selection ──
    if args.fat_only:
        selected_scopes = [
            max(coarse_scopes, key=lambda s: int(s["end_page"]) - int(s["start_page"]))
        ]
        logger.info(
            "🎯 --fat-only: 1/{} scopes selected  {}  p{}-{}",
            len(coarse_scopes),
            selected_scopes[0]["scope_id"],
            selected_scopes[0]["start_page"],
            selected_scopes[0]["end_page"],
        )
    elif args.page_range:
        parts = args.page_range.split("-")
        pr_start = int(parts[0])
        pr_end = int(parts[1]) if len(parts) > 1 else pr_start
        requested_pages = list(range(pr_start, pr_end + 1))
        pr_skeletons = [
            s for s in skeletons
            if s.start_page <= pr_end and s.end_page >= pr_start
        ]
        selected_scopes = [
            {
                "scope_id": scope_id_for_pages(pr_start, pr_end),
                "skeletons": pr_skeletons,
                "start_page": pr_start,
                "end_page": pr_end,
                "strategy": "manual_page_range",
                "processing_pages": toc_policy.filter_processing_pages(
                    requested_pages
                ),
                "excluded_toc_pages": sorted(
                    set(requested_pages) & toc_policy.pure_toc_pages
                ),
            }
        ]
        logger.info(f"   --page-range: p{pr_start}-{pr_end} ({len(pr_skeletons)} skeletons)")
    else:
        selected_scopes = coarse_scopes
        logger.info(
            "   default: all {} scopes selected", len(selected_scopes),
        )

    record_stage(
        trace_stages,
        "C4.coarse_scopes",
        variables={
            "total_coarse_scopes": len(coarse_scopes),
            "selected_scopes": len(selected_scopes),
            "mode": (
                "fat_only" if args.fat_only
                else "page_range" if args.page_range
                else "all_scopes"
            ),
            "scopes": [
                {
                    "scope_id": s["scope_id"],
                    "start_page": s["start_page"],
                    "end_page": s["end_page"],
                    "strategy": s.get("strategy", ""),
                    "skeleton_count": len(s["skeletons"]),
                    "processing_pages": list(s.get("processing_pages") or []),
                    "excluded_toc_pages": list(s.get("excluded_toc_pages") or []),
                }
                for s in selected_scopes
            ],
        },
    )

    # ── Create per-scope directories ──
    scopes_dir = out_dir / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    for s in selected_scopes:
        scope_dir = scopes_dir / s["scope_id"]
        scope_dir.mkdir(parents=True, exist_ok=True)
        write_debug_json(
            scope_dir / "skeletons.json",
            {
                **_serialize_scope_skeletons(
                    scope_id=str(s["scope_id"]),
                    start_page=int(s["start_page"]),
                    end_page=int(s["end_page"]),
                    strategy=str(s.get("strategy") or ""),
                    skeletons=s["skeletons"],
                ),
                "processing_pages": list(s.get("processing_pages") or []),
                "excluded_toc_pages": list(s.get("excluded_toc_pages") or []),
            },
        )
        # Placeholders for later stages (explicit empty slots for viewing).
        write_debug_json(scope_dir / "page_tags.json", [])
        write_debug_json(scope_dir / "assets.json", [])

    scope_rows = [
        {
            "scope_id": str(scope["scope_id"]),
            "start_page": int(scope["start_page"]),
            "end_page": int(scope["end_page"]),
            "strategy": str(scope.get("strategy") or ""),
            "skeleton_count": len(scope["skeletons"]),
            "processing_pages": list(scope.get("processing_pages") or []),
            "excluded_toc_pages": list(scope.get("excluded_toc_pages") or []),
            "artifact_path": str(
                scopes_dir / str(scope["scope_id"]) / "skeletons.json"
            ),
        }
        for scope in selected_scopes
    ]
    update_pipeline_state(
        state_path,
        stage=3,
        payload={
            "selection_mode": (
                "fat_only"
                if args.fat_only
                else "page_range"
                if args.page_range
                else "all_scopes"
            ),
            "total_scope_count": len(coarse_scopes),
            "selected_scope_count": len(selected_scopes),
            "scopes": scope_rows,
        },
    )
    (out_dir / "coarse_scopes.json").unlink(missing_ok=True)

    elapsed = time.time() - t_start
    logger.info(f"✅ Stage 3 done in {elapsed:.1f}s")
    logger.info(f"   {len(selected_scopes)} scope dirs created → {scopes_dir}/")

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="scope",
        page_count=page_count,
        pipeline_stage=3,
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
    )


if __name__ == "__main__":
    raise SystemExit(main())
