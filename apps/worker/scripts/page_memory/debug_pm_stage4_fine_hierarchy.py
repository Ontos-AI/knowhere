#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 4: Document-level page tagging + per-scope fine hierarchy.

Renders and tags each processing page once (global concurrency), then fans
tag subsets into scopes for fine hierarchy refinement.

Requires Stage 3 output: scopes/<id>/skeletons.json
Uses Stage 2 skeletons in pipeline_state for ``next_title_by_path``.

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage4_fine_hierarchy.py \\
      --file /path/to/doc.pdf --scope-id p14-23 --out-suffix boundary_clip
  uv run python scripts/page_memory/debug_pm_stage4_fine_hierarchy.py --file ... --all-scopes
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from _debug_pm_shared import *  # noqa: F401,F403
import os
import time
from pathlib import Path
from typing import Any, cast

from loguru import logger

from _debug_pm_shared import (
    ScopeResult,
    TokenCostTracker,
    TraceStageAdapter,
    add_scope_selection_args,
    base_argparser,
    list_scope_dirs,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    load_pipeline_skeletons,
    load_scope_skeletons_artifact,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_debug_scope_ids,
    resolve_paths,
    sort_skeletons,
    stop_with_trace,
    update_pipeline_state,
    write_scope_artifacts,
    write_top_level_artifacts,
    page_scope_info,
    _derive_hierarchy_page_scope,
    _scope_manifest,
    _serialize_skeletons,
)


def _resolve_scope_processing_pages(
    *,
    scope_meta: dict[str, Any],
    skeletons: list[Any],
    page_count: int,
    toc_policy: Any,
) -> tuple[list[int], list[int]]:
    processing_pages = [
        int(page) for page in (scope_meta.get("processing_pages") or [])
    ]
    if not processing_pages:
        processing_pages = toc_policy.filter_processing_pages(
            _derive_hierarchy_page_scope(
                skeletons=skeletons,
                page_count=page_count,
            )
        )
    excluded_toc_pages = [
        int(page) for page in (scope_meta.get("excluded_toc_pages") or [])
    ]
    if not excluded_toc_pages:
        excluded_toc_pages = sorted(
            set(range(
                int(scope_meta.get("start_page") or 1),
                int(scope_meta.get("end_page") or page_count) + 1,
            ))
            & toc_policy.pure_toc_pages
        )
    return processing_pages, excluded_toc_pages


def _run_fine_hierarchy_for_scope(
    *,
    scope_id: str,
    scope_dir: Path,
    out_dir: Path,
    page_count: int,
    rendered_by_page: dict[int, Any],
    tags_by_page: dict[int, Any],
    next_title_by_path: dict[str, str | None],
    toc_policy: Any,
    page_memory_config: Any,
    token_cost_tracker: TokenCostTracker | None = None,
) -> ScopeResult:
    """Consume shared page tags and refine one coarse scope."""
    from app.services.page_memory.fine_hierarchy import (
        compute_fat_leaf_pages,
        refine_fat_leaf_skeletons,
    )
    from app.services.page_memory.memory_service import _resolve_hierarchy_model

    scope_stages: list[dict[str, Any]] = []
    if token_cost_tracker is not None:
        token_cost_tracker.register_child_thread()

    skel_path = scope_dir / "skeletons.json"
    require_file(skel_path, hint=f"Run Stage 3 first to create {skel_path}")
    scope_meta, active_skeletons = load_scope_skeletons_artifact(skel_path)
    strategy = str(scope_meta.get("strategy") or "coarse_scope")
    processing_pages, excluded_toc_pages = _resolve_scope_processing_pages(
        scope_meta=scope_meta,
        skeletons=active_skeletons,
        page_count=page_count,
        toc_policy=toc_policy,
    )

    scope_manifest = _scope_manifest(
        scope_id=scope_id,
        skeletons=active_skeletons,
        page_count=page_count,
        strategy=strategy,
        processing_pages=processing_pages,
        excluded_toc_pages=excluded_toc_pages,
    )

    logger.info(
        "🔬 [scope {}] {} skeletons  p{}-{} processing={}",
        scope_id,
        len(active_skeletons),
        scope_meta.get("start_page", "?"),
        scope_meta.get("end_page", "?"),
        processing_pages,
    )

    if not processing_pages:
        logger.info("   [scope {}] no processing pages after TOC exclusion", scope_id)
        return ScopeResult(
            scope_id=scope_id,
            skeletons=active_skeletons,
            tags=[],
            assets_by_page={},
            rendered=[],
            final_pages=[],
            scope_manifest=scope_manifest,
            trace_stages=scope_stages,
        )

    rendered = [
        rendered_by_page[page]
        for page in processing_pages
        if page in rendered_by_page
    ]
    tags = [
        tags_by_page[page]
        for page in processing_pages
        if page in tags_by_page
    ]

    fine_min = page_memory_config.fine_min_pages
    fat_leaf_pages = compute_fat_leaf_pages(
        active_skeletons,
        min_pages=fine_min,
        exclude_pages=toc_policy.pure_toc_pages,
    )
    if fat_leaf_pages:
        active_skeletons = refine_fat_leaf_skeletons(
            coarse_skeletons=active_skeletons,
            tag_results=tags,
            fat_leaf_pages=fat_leaf_pages,
            next_title_by_path=next_title_by_path,
            model_name=_resolve_hierarchy_model(page_memory_config),
            max_tokens=page_memory_config.hierarchy_max_tokens,
            max_depth=page_memory_config.max_heading_depth,
            trace_recorder=TraceStageAdapter(scope_stages),
        )
        logger.info(
            "   [scope {}] C4b: {} sections after fine hierarchy",
            scope_id, len(active_skeletons),
        )
    else:
        logger.info(
            "   [scope {}] no fat-leaf pages (min={}); skip fine hierarchy",
            scope_id, fine_min,
        )

    scope_manifest = _scope_manifest(
        scope_id=scope_id,
        skeletons=active_skeletons,
        page_count=page_count,
        strategy=f"{strategy}:refined",
        processing_pages=processing_pages,
        excluded_toc_pages=excluded_toc_pages,
    )
    record_stage(
        scope_stages, "C4b.fine_hierarchy",
        page_info={"fat_leaf": page_scope_info(sorted(fat_leaf_pages))},
        variables={
            "scope_id": scope_id,
            "scope": scope_manifest,
            "sections": _serialize_skeletons(active_skeletons),
        },
    )
    if token_cost_tracker is not None:
        token_cost_tracker.snapshot_stage(f"C4b.fine_hierarchy:{scope_id}")

    write_scope_artifacts(
        out_dir=out_dir,
        scope_id=scope_id,
        scope_manifest=scope_manifest,
        hierarchy=active_skeletons,
        tags=tags,
    )

    return ScopeResult(
        scope_id=scope_id,
        skeletons=active_skeletons,
        tags=tags,
        assets_by_page={},
        rendered=rendered,
        final_pages=processing_pages,
        scope_manifest=scope_manifest,
        trace_stages=scope_stages,
    )


def main() -> int:
    parser = base_argparser("Stage 4: Combined page tagging + fine hierarchy")
    add_scope_selection_args(parser)
    parser.add_argument(
        "--max-workers", type=int, default=5,
        help="Concurrent workers for scope fine hierarchy (default=5)",
    )
    args = parser.parse_args()

    from app.services.document_agent.pdf_text import read_page_texts
    from app.services.page_memory.fine_hierarchy import build_next_title_by_path
    from app.services.page_memory.memory_service import _render_and_tag_document_pages
    from toc_page_policy import TocPagePolicy
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    pdf_path, filename, out_dir = resolve_paths(args)
    doc_agent_dir = out_dir / "_doc_agent"
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    state_path = pipeline_state_path(out_dir)
    legacy_locate_cache = doc_agent_dir / "locate_cache.json"
    scopes_dir = out_dir / "scopes"

    require_file(
        anatomy_cache,
        hint="Run Stage 1 first: uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ...",
    )
    if not state_path.exists() and not legacy_locate_cache.exists():
        require_file(
            state_path,
            hint="Run Stage 2 first: uv run python scripts/page_memory/debug_pm_stage2_calibration.py --file ...",
        )

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = anatomy.page_count
    page_features = anatomy.page_features if anatomy else []
    page_labels = anatomy.page_labels if anatomy else []
    toc_policy = TocPagePolicy.from_anatomy(anatomy)
    page_memory_config = PageMemoryConfig.default()

    all_skeletons = load_pipeline_skeletons(
        state_path,
        legacy_locate_cache=legacy_locate_cache,
    )
    next_title_by_path = build_next_title_by_path(all_skeletons)
    logger.info(
        "   next_title_by_path: {} paths ({} with tail anchor)",
        len(next_title_by_path),
        sum(1 for title in next_title_by_path.values() if title),
    )

    scope_ids = resolve_debug_scope_ids(
        scopes_dir=scopes_dir,
        scope_id=args.scope_id,
        page_range=args.page_range,
        fat_only=args.fat_only,
        all_scopes=args.all_scopes,
        list_scopes=args.list_scopes,
        require_file="skeletons.json",
    )
    partial_run = len(scope_ids) < len(list_scope_dirs(scopes_dir))

    logger.info("█" * 70)
    logger.info(f"  STAGE 4: FINE HIERARCHY — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info(f"  SCOPES ({len(scope_ids)}): {scope_ids}")
    if partial_run:
        logger.info("  MODE: partial — will not overwrite top-level hierarchy.json")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    selected_processing_pages: set[int] = set()
    scope_payloads: list[tuple[str, Path, list[int]]] = []
    for sid in scope_ids:
        scope_dir = scopes_dir / sid
        scope_meta, skeletons = load_scope_skeletons_artifact(scope_dir / "skeletons.json")
        processing_pages, _excluded = _resolve_scope_processing_pages(
            scope_meta=scope_meta,
            skeletons=skeletons,
            page_count=page_count,
            toc_policy=toc_policy,
        )
        selected_processing_pages.update(processing_pages)
        scope_payloads.append((sid, scope_dir, processing_pages))

    processing_pages = sorted(selected_processing_pages)
    page_texts = read_page_texts(
        pdf_path,
        processing_pages or list(range(1, page_count + 1)),
        timeout=300,
    )
    logger.info(
        "   document page stage: {} processing pages (of {}), tag_concurrency={}",
        len(processing_pages),
        page_count,
        page_memory_config.tag_concurrency,
    )

    vlm_model = getattr(args, "vlm_model", None) or os.environ.get("IMAGE_MODEL")
    rendered_by_page, tags_by_page = _render_and_tag_document_pages(
        pdf_path=pdf_path,
        output_dir=str(out_dir),
        page_count=page_count,
        processing_pages=processing_pages,
        page_texts=page_texts,
        page_features=page_features,
        page_labels=page_labels,
        vlm_model=vlm_model,
        toc_policy=toc_policy,
        page_memory_config=page_memory_config,
        trace_recorder=TraceStageAdapter(trace_stages),
    )
    token_cost_tracker.snapshot_stage("C3.page_tagger")
    logger.info(
        "   tagged {} unique pages; refining {} scopes",
        len(tags_by_page),
        len(scope_ids),
    )

    def _run_selected_scope(scope_id: str, scope_dir: Path) -> ScopeResult:
        return _run_fine_hierarchy_for_scope(
            scope_id=scope_id,
            scope_dir=scope_dir,
            out_dir=out_dir,
            page_count=page_count,
            rendered_by_page=rendered_by_page,
            tags_by_page=tags_by_page,
            next_title_by_path=next_title_by_path,
            toc_policy=toc_policy,
            page_memory_config=page_memory_config,
            token_cost_tracker=token_cost_tracker,
        )

    if args.max_workers > 1 and len(scope_ids) > 1:
        import gevent
        from gevent.pool import Pool as GeventPool

        logger.info(
            "   scope fine-hierarchy concurrency: {} workers × {} scopes",
            args.max_workers, len(scope_ids),
        )
        gpool = GeventPool(size=min(args.max_workers, len(scope_ids)))
        greenlets = [
            gpool.spawn(
                _run_selected_scope,
                sid,
                scope_dir,
            )
            for sid, scope_dir, _pages in scope_payloads
        ]
        gevent.joinall(greenlets, raise_error=True)
        scope_results = [cast(ScopeResult, g.value) for g in greenlets]
    else:
        logger.info("   serial fine hierarchy: {} scope(s)", len(scope_ids))
        scope_results = [
            _run_selected_scope(sid, scope_dir)
            for sid, scope_dir, _pages in scope_payloads
        ]

    for sr in scope_results:
        trace_stages.extend(sr.trace_stages)

    merged_skeletons = sort_skeletons(
        [skel for sr in scope_results for skel in sr.skeletons]
    )
    merged_tags = [tags_by_page[page] for page in sorted(tags_by_page)]
    if not partial_run:
        write_top_level_artifacts(
            out_dir=out_dir,
            hierarchy=merged_skeletons,
            tags=merged_tags,
        )
    else:
        logger.info(
            "   skipped top-level hierarchy.json merge (partial {}/{} scopes)",
            len(scope_ids),
            len(list_scope_dirs(scopes_dir)),
        )

    elapsed = time.time() - t_start
    logger.info(f"✅ Stage 4 done in {elapsed:.1f}s")
    logger.info(
        f"   {len(scope_results)} scopes processed, "
        f"{len(merged_skeletons)} skeletons this run, "
        f"{len(merged_tags)} unique page tags"
    )
    for sid in scope_ids:
        logger.info(f"   → {scopes_dir / sid / 'fine_hierarchy.json'}")

    update_pipeline_state(
        state_path,
        stage=4,
        payload={
            "partial_run": partial_run,
            "processed_scope_ids": scope_ids,
            "processed_scope_count": len(scope_results),
            "skeleton_count": len(merged_skeletons),
            "tagged_page_count": len(merged_tags),
            "scope_artifacts": [
                str(scopes_dir / scope_id / "fine_hierarchy.json")
                for scope_id in scope_ids
            ],
        },
    )

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="fine_hierarchy",
        page_count=page_count,
        pipeline_stage=4,
        elapsed_s=elapsed,
        scope_id=scope_ids[0] if len(scope_ids) == 1 else None,
        token_cost_tracker=token_cost_tracker,
        extra_summary={
            "scope_count": len(scope_results),
            "skeleton_count": len(merged_skeletons),
            "tagged_page_count": len(merged_tags),
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
