#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 1: TOC Find → extract → link attach (no calibration).

Resumes Stage-0 blackboard (``stage0_state.json`` + ``page_full_text_cache.json``,
including asset-probe ``has_asset`` flags) and runs the production TOC segment:
  find.toc_anchor_pages → extract.toc_with_boundaries → attach links
  → persist doc_profile.json

Requires Stage 0 first:
  uv run python scripts/page_memory/debug_pm_stage0_bootstrap.py --file ...

Calibration belongs to Stage 2 (``debug_pm_stage2_calibration.py``).

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file /path/to/doc.pdf
  uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --reuse-anatomy
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import time

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    base_argparser,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    record_stage,
    remove_legacy_doc_agent_artifacts,
    require_file,
    resolve_paths,
    run_stage1_toc,
    stage0_state_path,
    stop_with_trace,
    toc_hierarchies_to_hierarchy_tree,
    write_toc_hierarchy_artifact,
)


def _count_hierarchy_keys(tree: dict) -> int:
    total = 0
    for children in (tree or {}).values():
        total += 1
        if isinstance(children, dict):
            total += _count_hierarchy_keys(children)
    return total


def _count_linked_entries(toc_hierarchies: list | None) -> tuple[int, int]:
    total = 0
    linked = 0
    for region in toc_hierarchies or []:
        if not isinstance(region, dict):
            continue
        entries = region.get("toc_with_level") or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            total += 1
            link = entry.get("link")
            if isinstance(link, dict) and link.get("physical_page") is not None:
                linked += 1
    return linked, total


def main() -> int:
    parser = base_argparser("Stage 1: TOC Find → extract → link (no calibration)")
    parser.add_argument(
        "--reuse-anatomy",
        action="store_true",
        help="Reuse cached Stage-1 doc_profile.json (skip Find/extract/link)",
    )
    args = parser.parse_args()

    pdf_path, filename, out_dir = resolve_paths(args)

    logger.info("█" * 70)
    logger.info(f"  STAGE 1: TOC FIND → EXTRACT → LINK — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    doc_agent_dir = out_dir / "_doc_agent"
    anatomy_cache = resolve_anatomy_cache_path(out_dir)

    if args.reuse_anatomy and anatomy_cache.exists():
        anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
        profile_source = "reuse_anatomy"
    else:
        require_file(
            stage0_state_path(out_dir),
            hint=(
                "Run Stage 0 first: uv run python "
                "scripts/page_memory/debug_pm_stage0_bootstrap.py --file ..."
            ),
        )
        anatomy = run_stage1_toc(pdf_path, filename, out_dir, args.model)
        profile_source = "stage0_resume_toc"

    page_count = anatomy.page_count
    linked, total = _count_linked_entries(list(anatomy.toc_hierarchies or []))
    record_stage(
        trace_stages,
        "toc",
        page_info={"page_count": page_count},
        variables={
            "source": profile_source,
            "toc_pages": anatomy.toc_result.toc_pages,
            "toc_entries": total,
            "toc_entries_with_link": linked,
            "skip_toc_anchoring": True,
            "skeleton_anchor": getattr(anatomy, "skeleton_anchor", None),
        },
    )
    token_cost_tracker.snapshot_stage("toc")
    remove_legacy_doc_agent_artifacts(doc_agent_dir)

    logger.info("=" * 70)
    logger.info("🧠 TOC hierarchy (Stage-1 debug dump)")
    logger.info("=" * 70)
    logger.info("   TOC entries with link: {}/{}", linked, total)

    hierarchy_tree = toc_hierarchies_to_hierarchy_tree(anatomy.toc_hierarchies)
    toc_path = write_toc_hierarchy_artifact(
        out_dir,
        hierarchy_tree=hierarchy_tree,
        stats={
            "source": "toc_hierarchies_raw",
            "region_count": len(list(anatomy.toc_hierarchies or [])),
            "hierarchy_key_count": _count_hierarchy_keys(hierarchy_tree),
            "toc_entries_with_link": linked,
            "toc_entries": total,
        },
    )
    logger.info(f"   toc_hierarchy → {toc_path}")

    record_stage(
        trace_stages,
        "C2.toc_hierarchy_dump",
        variables={
            "toc_hierarchy_path": str(toc_path),
            "region_count": len(list(anatomy.toc_hierarchies or [])),
            "hierarchy_key_count": _count_hierarchy_keys(hierarchy_tree),
        },
    )

    elapsed = time.time() - t_start
    logger.info(f"✅ Stage 1 done in {elapsed:.1f}s → {out_dir}")

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="toc",
        page_count=page_count,
        pipeline_stage=1,
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
    )


if __name__ == "__main__":
    raise SystemExit(main())
