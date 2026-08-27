#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 3: Coarse scopes + per-scope title detection / fine hierarchy / page tag.

Mirrors production ``_run_hierarchy_scope`` (without C5 assets; that is Stage 4):
fat-leaf title render → ``tag_page_titles`` → fine hierarchy → render → ``tag_pages``.

Requires Stage 2 output: _doc_agent/pipeline_state.json (with skeletons),
doc_profile.json (after production ``run_toc_anchoring``).

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage3_scope_fine_hierarchy.py --file /path/to/doc.pdf
  uv run python scripts/page_memory/debug_pm_stage3_scope_fine_hierarchy.py --fat-only
  uv run python scripts/page_memory/debug_pm_stage3_scope_fine_hierarchy.py --all-scopes
  uv run python scripts/page_memory/debug_pm_stage3_scope_fine_hierarchy.py --file ... --scope-id p14-23
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

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
    build_debug_coarse_scopes,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    load_pipeline_skeletons,
    load_scope_skeletons_artifact,
    list_scope_dirs,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_paths,
    scope_id_for_pages,
    sort_skeletons,
    stop_with_trace,
    update_pipeline_state,
    write_debug_json,
    write_scope_artifacts,
    write_top_level_artifacts,
    page_scope_info,
    _derive_hierarchy_page_scope,
    _scope_manifest,
    _serialize_scope_skeletons,
    _serialize_skeletons,
)


def _resolve_scope_processing_pages(
    *,
    scope_meta: dict[str, Any],
    skeletons: list[Any],
    page_count: int,
    toc_pages: list[int],
) -> tuple[list[int], list[int]]:
    from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc

    processing_pages = [
        int(page) for page in (scope_meta.get("processing_pages") or [])
    ]
    if not processing_pages:
        processing_pages = pages_excluding_toc(
            _derive_hierarchy_page_scope(
                skeletons=skeletons,
                page_count=page_count,
            ),
            toc_pages,
        )
    excluded_toc_pages = [
        int(page) for page in (scope_meta.get("excluded_toc_pages") or [])
    ]
    if not excluded_toc_pages:
        start = int(scope_meta.get("start_page") or 1)
        end = int(scope_meta.get("end_page") or page_count)
        excluded_toc_pages = sorted(
            page for page in toc_pages if start <= int(page) <= end
        )
    return processing_pages, excluded_toc_pages


def _run_fine_hierarchy_for_scope(
    *,
    scope_id: str,
    scope_dir: Path,
    out_dir: Path,
    pdf_path: str,
    page_count: int,
    page_texts: dict[int, str],
    page_features: list[Any],
    page_labels: list[Any],
    vlm_model: str | None,
    next_title_by_path: dict[str, str | None],
    toc_pages: list[int],
    page_memory_config: Any,
    token_cost_tracker: TokenCostTracker | None = None,
) -> ScopeResult:
    """Per-scope path aligned with production ``_run_hierarchy_scope`` (no C5)."""
    from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc
    from app.services.page_memory.fine_hierarchy import (
        compute_fat_leaf_pages,
        refine_fat_leaf_skeletons,
    )
    from app.services.page_memory.memory_service import (
        _resolve_hierarchy_model,
        _summarize_tag_scope,
        _summarize_tags,
    )
    from app.services.page_memory.page_plan import derive_page_processing_plan
    from app.services.page_memory.page_renderer import render_document_pages
    from app.services.page_memory.page_tagger import (
        PageTagResult,
        tag_page_titles,
        tag_pages,
    )

    scope_stages: list[dict[str, Any]] = []

    skel_path = scope_dir / "skeletons.json"
    require_file(skel_path, hint=f"Stage 3 should have created {skel_path}")
    scope_meta, active_skeletons = load_scope_skeletons_artifact(skel_path)
    active_skeletons = sort_skeletons(active_skeletons)
    strategy = str(scope_meta.get("strategy") or "coarse_scope")
    processing_pages, excluded_toc_pages = _resolve_scope_processing_pages(
        scope_meta=scope_meta,
        skeletons=active_skeletons,
        page_count=page_count,
        toc_pages=toc_pages,
    )

    scope_manifest = _scope_manifest(
        scope_id=scope_id,
        skeletons=active_skeletons,
        page_count=page_count,
        strategy=strategy,
    )

    logger.info(
        "🔬 [scope {}] {} skeletons  p{}-{} processing={} excluded_toc={}",
        scope_id,
        len(active_skeletons),
        scope_meta.get("start_page", "?"),
        scope_meta.get("end_page", "?"),
        processing_pages,
        excluded_toc_pages,
    )

    fine_min = page_memory_config.fine_min_pages
    fat_leaf_pages = compute_fat_leaf_pages(
        active_skeletons,
        min_pages=fine_min,
        toc_pages=toc_pages,
    )
    if fat_leaf_pages:
        title_pages = sorted(fat_leaf_pages)
        title_rendered = render_document_pages(
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=str(out_dir),
            scope_id=scope_id,
            pages=title_pages,
            page_features=page_features,
            page_texts=page_texts,
        )
        title_tags = [
            PageTagResult(
                page_index=page,
                summary="",
                keywords=[],
                strategy_used="title_detection_only",
            )
            for page in title_pages
        ]
        title_tags = tag_page_titles(
            pages=title_rendered,
            tag_results=title_tags,
            fat_leaf_pages=fat_leaf_pages,
            vlm_model=vlm_model,
            scan_direction=page_memory_config.scan_direction,
            max_concurrent=page_memory_config.title_detection_concurrency,
        )
        record_stage(
            scope_stages,
            "C3b.title_detection",
            page_info=page_scope_info(title_pages),
            variables={
                "scope_id": scope_id,
                "tags": _summarize_tags(title_tags),
            },
        )
        if token_cost_tracker is not None:
            token_cost_tracker.snapshot_stage(f"C3b.title_detection:{scope_id}")
        active_skeletons = refine_fat_leaf_skeletons(
            coarse_skeletons=active_skeletons,
            tag_results=title_tags,
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

    final_pages = pages_excluding_toc(
        _derive_hierarchy_page_scope(
            skeletons=active_skeletons,
            page_count=page_count,
        ),
        toc_pages,
    )
    final_scope_summary = _summarize_tag_scope(
        skeletons=active_skeletons,
        page_count=page_count,
        pages=final_pages,
    )
    rendered = render_document_pages(
        pdf_path=pdf_path,
        page_count=page_count,
        output_dir=str(out_dir),
        scope_id=scope_id,
        pages=final_pages,
        page_features=page_features,
        page_texts=page_texts,
    )
    record_stage(
        scope_stages,
        "C1.render_pages",
        page_info=page_scope_info([item.page_index for item in rendered]),
        variables={
            "scope_id": scope_id,
            "rendered_count": len(rendered),
            "tag_scope": final_scope_summary,
        },
    )

    plans = derive_page_processing_plan(
        page_count=page_count,
        page_labels=page_labels,
        page_features=page_features,
    )
    final_page_set = set(final_pages)
    plans = [plan for plan in plans if plan.page_index in final_page_set]
    record_stage(
        scope_stages,
        "C2.page_plan",
        page_info=page_scope_info([getattr(plan, "page_index", None) for plan in plans]),
        variables={"scope_id": scope_id, "plan_count": len(plans)},
    )

    tags = tag_pages(
        pages=rendered,
        plans=plans,
        vlm_model=vlm_model,
        max_concurrent=page_memory_config.tag_concurrency,
    )
    record_stage(
        scope_stages,
        "C3.page_tagger",
        page_info=page_scope_info([tag.page_index for tag in tags]),
        variables={"scope_id": scope_id, "tags": _summarize_tags(tags)},
    )
    if token_cost_tracker is not None:
        token_cost_tracker.snapshot_stage(f"C3.page_tagger:{scope_id}")

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
        final_pages=final_pages,
        scope_manifest=scope_manifest,
        trace_stages=scope_stages,
    )


def main() -> int:
    parser = base_argparser("Stage 3: Coarse scopes + fine hierarchy")
    add_scope_selection_args(parser)
    parser.add_argument(
        "--max-workers", type=int, default=5,
        help="Concurrent workers for scope fine hierarchy (default=5)",
    )
    args = parser.parse_args()

    from app.services.document_agent.pdf_text import read_page_texts
    from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc
    from app.services.page_memory.fine_hierarchy import build_next_title_by_path
    from app.services.page_memory.skeleton_extractor import SectionSkeleton
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    state_path = pipeline_state_path(out_dir)
    scopes_dir = out_dir / "scopes"

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
    page_features = anatomy.page_features if anatomy else []
    page_labels = anatomy.page_labels if anatomy else []
    toc_result = getattr(anatomy, "toc_result", None)
    toc_pages = list(getattr(toc_result, "toc_pages", None) or [])
    page_memory_config = PageMemoryConfig.default()
    skeletons = load_pipeline_skeletons(state_path)

    logger.info("█" * 70)
    logger.info(f"  STAGE 3: SCOPE + FINE HIERARCHY — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

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
            evidence={"source": "fallback_root"},
        )
        coarse_scopes = [
            {
                "scope_id": scope_id_for_pages(1, page_count),
                "skeletons": [root_skel],
                "start_page": 1,
                "end_page": page_count,
                "strategy": "fallback_root",
                "processing_pages": pages_excluding_toc(
                    list(range(1, page_count + 1)),
                    toc_pages,
                ),
                "excluded_toc_pages": sorted(
                    {int(page) for page in toc_pages}
                ),
            }
        ]
        logger.info("   no skeleton hierarchy → fallback Root scope p1-{}", page_count)

    if args.list_scopes:
        logger.info("Available scopes ({}):", len(coarse_scopes))
        for scope in coarse_scopes:
            start = int(scope["start_page"])
            end = int(scope["end_page"])
            logger.info(
                "  {}  p{}-{}  pages={}  skeletons={}  {}",
                scope["scope_id"],
                start,
                end,
                max(end - start + 1, 0),
                len(scope["skeletons"]),
                scope.get("strategy") or "",
            )
        raise SystemExit(0)

    # ── Scope selection (same priority as resolve_debug_scope_ids) ──
    if args.scope_id:
        requested = [
            part.strip() for part in str(args.scope_id).split(",") if part.strip()
        ]
        by_id = {str(scope["scope_id"]): scope for scope in coarse_scopes}
        missing = [sid for sid in requested if sid not in by_id]
        if missing:
            logger.error("❌ Unknown scope-id(s): {}", ", ".join(missing))
            logger.error(
                "   Available: {}",
                ", ".join(str(scope["scope_id"]) for scope in coarse_scopes),
            )
            raise SystemExit(1)
        selected_scopes = [by_id[sid] for sid in requested]
    elif args.fat_only:
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
                "processing_pages": pages_excluding_toc(
                    requested_pages,
                    toc_pages,
                ),
                "excluded_toc_pages": sorted(
                    page
                    for page in toc_pages
                    if pr_start <= int(page) <= pr_end
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
                "scope_id" if args.scope_id
                else "fat_only" if args.fat_only
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
        write_debug_json(scope_dir / "page_tags.json", [])
        write_debug_json(scope_dir / "assets.json", [])

    scope_ids = [str(s["scope_id"]) for s in selected_scopes]
    partial_run = len(scope_ids) < len(list_scope_dirs(scopes_dir))
    logger.info(f"  SCOPES ({len(scope_ids)}): {scope_ids}")
    if partial_run:
        logger.info("  MODE: partial — will not overwrite top-level hierarchy.json")

    next_title_by_path = build_next_title_by_path(skeletons)
    logger.info(
        "   next_title_by_path: {} paths ({} with tail anchor)",
        len(next_title_by_path),
        sum(1 for title in next_title_by_path.values() if title),
    )

    scope_payloads: list[tuple[str, Path]] = []
    pages_needed: set[int] = set()
    for sid in scope_ids:
        scope_dir = scopes_dir / sid
        _scope_meta, scope_skeletons = load_scope_skeletons_artifact(
            scope_dir / "skeletons.json"
        )
        pages_needed.update(
            pages_excluding_toc(
                _derive_hierarchy_page_scope(
                    skeletons=scope_skeletons,
                    page_count=page_count,
                ),
                toc_pages,
            )
        )
        scope_payloads.append((sid, scope_dir))

    page_texts = read_page_texts(
        pdf_path,
        sorted(pages_needed) or list(range(1, page_count + 1)),
        timeout=300,
    )
    logger.info(
        "   page texts ready: {} pages (of {}), tag_concurrency={}",
        len(page_texts),
        page_count,
        page_memory_config.tag_concurrency,
    )

    vlm_model = getattr(args, "vlm_model", None) or os.environ.get("IMAGE_MODEL")

    def _run_selected_scope(scope_id: str, scope_dir: Path) -> ScopeResult:
        return _run_fine_hierarchy_for_scope(
            scope_id=scope_id,
            scope_dir=scope_dir,
            out_dir=out_dir,
            pdf_path=str(pdf_path),
            page_count=page_count,
            page_texts=page_texts,
            page_features=page_features,
            page_labels=page_labels,
            vlm_model=vlm_model,
            next_title_by_path=next_title_by_path,
            toc_pages=toc_pages,
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
            for sid, scope_dir in scope_payloads
        ]
        gevent.joinall(greenlets, raise_error=True)
        scope_results = [cast(ScopeResult, g.value) for g in greenlets]
    else:
        logger.info("   serial fine hierarchy: {} scope(s)", len(scope_ids))
        scope_results = [
            _run_selected_scope(sid, scope_dir)
            for sid, scope_dir in scope_payloads
        ]

    for sr in scope_results:
        trace_stages.extend(sr.trace_stages)

    merged_skeletons = sort_skeletons(
        [skel for sr in scope_results for skel in sr.skeletons]
    )
    tags_by_page: dict[int, Any] = {}
    for sr in scope_results:
        for tag in sr.tags:
            tags_by_page[int(tag.page_index)] = tag
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
    logger.info(f"✅ Stage 3 done in {elapsed:.1f}s")
    logger.info(
        f"   {len(scope_results)} scopes processed, "
        f"{len(merged_skeletons)} skeletons this run, "
        f"{len(merged_tags)} unique page tags"
    )
    for sid in scope_ids:
        logger.info(f"   → {scopes_dir / sid / 'fine_hierarchy.json'}")

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
                "scope_id"
                if args.scope_id
                else "fat_only"
                if args.fat_only
                else "page_range"
                if args.page_range
                else "all_scopes"
            ),
            "partial_run": partial_run,
            "total_scope_count": len(coarse_scopes),
            "selected_scope_count": len(selected_scopes),
            "scopes": scope_rows,
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
    (out_dir / "coarse_scopes.json").unlink(missing_ok=True)

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="fine_hierarchy",
        page_count=page_count,
        pipeline_stage=3,
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
