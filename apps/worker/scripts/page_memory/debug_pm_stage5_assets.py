#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 5: Document-level page asset extraction (C5) — NO page tagging.

Unions processing pages from selected scopes, renders/extracts each unique page
once, writes top-level ``assets.json``, and projects references into scope dirs.
Does NOT run page tagging (C3) — Stage 4 already produced shared document-level tags.

Requires Stage 4 output: scopes/<id>/fine_hierarchy.json

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage5_assets.py --file /path/to/doc.pdf
  uv run python scripts/page_memory/debug_pm_stage5_assets.py --scope-id p1-100
  uv run python scripts/page_memory/debug_pm_stage5_assets.py --all-scopes
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

from _debug_pm_shared import *  # noqa: F401,F403
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from _debug_pm_shared import (
    TokenCostTracker,
    add_scope_selection_args,
    base_argparser,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    load_hierarchy_artifact,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_debug_scope_ids,
    resolve_paths,
    stop_with_trace,
    update_pipeline_state,
    write_scope_artifacts,
    page_scope_info,
    _scope_manifest,
    _derive_hierarchy_page_scope,
    _serialize_assets,
    write_debug_json,
)


@dataclass(frozen=True)
class _ScopeAssetContext:
    scope_id: str
    pages: list[int]
    skeletons: list[Any]
    scope_manifest: dict[str, Any]


def _load_scope_asset_context(
    *,
    scope_id: str,
    scope_dir: Path,
    page_count: int,
    toc_policy: Any,
) -> _ScopeAssetContext | None:
    fine_hierarchy_path = scope_dir / "fine_hierarchy.json"
    require_file(
        fine_hierarchy_path,
        hint=f"Run Stage 4 first to produce {fine_hierarchy_path}",
    )
    prior_scope, active_skeletons = load_hierarchy_artifact(fine_hierarchy_path)
    if not active_skeletons:
        logger.warning(
            "   [scope {}] no skeletons in fine_hierarchy.json — skipping",
            scope_id,
        )
        return None

    recorded_pages = prior_scope.get("processing_pages")
    final_pages = (
        [int(page) for page in recorded_pages]
        if isinstance(recorded_pages, list)
        else toc_policy.filter_processing_pages(
            _derive_hierarchy_page_scope(
                skeletons=active_skeletons,
                page_count=page_count,
            )
        )
    )
    recorded_excluded = prior_scope.get("excluded_toc_pages")
    excluded_toc_pages = (
        [int(page) for page in recorded_excluded]
        if isinstance(recorded_excluded, list)
        else sorted(toc_policy.pure_toc_pages)
    )
    scope_manifest = _scope_manifest(
        scope_id=scope_id,
        skeletons=active_skeletons,
        page_count=page_count,
        strategy="fine:assets",
        processing_pages=final_pages,
        excluded_toc_pages=excluded_toc_pages,
    )
    return _ScopeAssetContext(
        scope_id=scope_id,
        pages=final_pages,
        skeletons=active_skeletons,
        scope_manifest=scope_manifest,
    )


def main() -> int:
    parser = base_argparser("Stage 5: Document-level asset extraction (C5)")
    add_scope_selection_args(parser)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=5,
        help="Kept for CLI compatibility; Stage 5 extracts once at document level",
    )
    args = parser.parse_args()

    from app.services.document_agent.pdf_text import read_page_texts
    from app.services.page_memory.memory_service import (
        _project_assets_for_pages,
        _resolve_asset_max_pages,
        _select_rendered_pages_with_assets,
    )
    from app.services.page_memory.page_assets import (
        extract_page_assets_from_renders,
        get_asset_confidence_threshold,
        page_asset_summary_enabled,
    )
    from app.services.page_memory.page_renderer import render_document_pages
    from toc_page_policy import TocPagePolicy
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    state_path = pipeline_state_path(out_dir)
    scopes_dir = out_dir / "scopes"

    require_file(
        anatomy_cache,
        hint="Run Stage 1 first: uv run python scripts/page_memory/debug_pm_stage1_hierarchy.py --file ...",
    )

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = anatomy.page_count
    page_features = anatomy.page_features if anatomy else []
    toc_policy = TocPagePolicy.from_anatomy(anatomy)

    scope_ids = resolve_debug_scope_ids(
        scopes_dir=scopes_dir,
        scope_id=args.scope_id,
        page_range=args.page_range,
        fat_only=args.fat_only,
        all_scopes=args.all_scopes,
        list_scopes=args.list_scopes,
        require_file="fine_hierarchy.json",
        nonempty_json=True,
    )
    logger.info("█" * 70)
    logger.info(f"  STAGE 5: DOCUMENT ASSET EXTRACTION — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info(f"  SCOPES: {scope_ids}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    pages = list(range(1, page_count + 1))
    page_texts = read_page_texts(pdf_path, pages, timeout=300)
    logger.info(f"   read {len(page_texts)} pages")

    scope_contexts: list[_ScopeAssetContext] = []
    for scope_id in scope_ids:
        context = _load_scope_asset_context(
            scope_id=scope_id,
            scope_dir=scopes_dir / scope_id,
            page_count=page_count,
            toc_policy=toc_policy,
        )
        if context is not None:
            scope_contexts.append(context)

    union_pages = sorted(
        {page for context in scope_contexts for page in context.pages}
    )
    logger.info(
        "🔬 document C5: {} unique pages across {} scopes",
        len(union_pages),
        len(scope_contexts),
    )

    rendered = render_document_pages(
        pdf_path=pdf_path,
        page_count=page_count,
        output_dir=str(out_dir),
        pages=union_pages,
        page_features=page_features,
        page_texts=page_texts,
    )
    record_stage(
        trace_stages,
        "C1.render_pages",
        page_info=page_scope_info([item.page_index for item in rendered]),
        variables={"rendered_count": len(rendered)},
    )

    asset_model = (
        getattr(args, "vlm_model", None)
        or os.environ.get("PAGE_MEMORY_ASSET_MODEL")
        or os.environ.get("IMAGE_MODEL")
    )
    pm_config = PageMemoryConfig.default()
    asset_max_pages = _resolve_asset_max_pages(page_count, pm_config)
    asset_rendered = _select_rendered_pages_with_assets(rendered, page_features)
    summary_enabled = page_asset_summary_enabled()
    logger.info(
        "   C5: {}/{} rendered pages have coarse has_asset "
        "(asset_max_pages={} summary_enabled={} model={})",
        len(asset_rendered),
        len(rendered),
        asset_max_pages,
        summary_enabled,
        asset_model,
    )

    assets_by_page = extract_page_assets_from_renders(
        pdf_path=pdf_path,
        rendered_pages=asset_rendered,
        output_dir=str(out_dir),
        model_name=asset_model,
        budget=None,
        max_pages=asset_max_pages,
        confidence_threshold=get_asset_confidence_threshold(),
        summary_enabled=summary_enabled,
        summary_concurrency=pm_config.asset_summary_concurrency,
        table_engine=pm_config.table_engine,
        table_merge_enabled=pm_config.table_merge_enabled,
    )
    asset_count = sum(len(items) for items in assets_by_page.values())
    logger.info(
        "   C5: {} assets on {} pages (single document extraction)",
        asset_count,
        len(assets_by_page),
    )
    record_stage(
        trace_stages,
        "C5.page_assets",
        page_info=page_scope_info(sorted(assets_by_page)),
        variables={
            "asset_count": asset_count,
            "assets_by_page": {
                page: [asset.asset_id for asset in assets]
                for page, assets in assets_by_page.items()
            },
        },
    )
    token_cost_tracker.snapshot_stage("C5.page_assets")

    write_debug_json(out_dir / "assets.json", _serialize_assets(assets_by_page))
    for context in scope_contexts:
        projected = _project_assets_for_pages(assets_by_page, set(context.pages))
        write_scope_artifacts(
            out_dir=out_dir,
            scope_id=context.scope_id,
            scope_manifest=context.scope_manifest,
            hierarchy=context.skeletons,
            tags=None,
            assets_by_page=projected,
        )

    elapsed = time.time() - t_start
    logger.info(f"✅ Stage 5 done in {elapsed:.1f}s")
    logger.info(
        f"   {len(scope_contexts)} scopes, {asset_count} assets, "
        f"{len(union_pages)} unique pages"
    )
    update_pipeline_state(
        state_path,
        stage=5,
        payload={
            "processed_scope_ids": [context.scope_id for context in scope_contexts],
            "processed_scope_count": len(scope_contexts),
            "asset_count": asset_count,
            "asset_pages": sorted(assets_by_page),
            "unique_pages": union_pages,
            "document_assets": str(out_dir / "assets.json"),
            "scope_artifacts": [
                str(scopes_dir / context.scope_id / "assets.json")
                for context in scope_contexts
            ],
        },
    )

    return stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="assets",
        page_count=page_count,
        pipeline_stage=5,
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
    )


if __name__ == "__main__":
    raise SystemExit(main())
