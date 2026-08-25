#!/usr/bin/env python3
# ruff: noqa: E402
"""Stage 5: Canonical chunk assembly (C7) + finalize (C9).

Loads the combined page tags produced by Stage 3, assembles canonical chunks,
and optionally produces chunks.json / doc_nav.json / manifest.json.

Requires Stage 3 output: scopes/<id>/fine_hierarchy.json
Prefer Stage 4 document assets: assets.json
Fallback: scopes/<id>/assets.json (deduped by asset_id)

Usage:
  cd apps/worker
  uv run python scripts/page_memory/debug_pm_stage5_tagging_finalize.py --file /path/to/doc.pdf
  uv run python scripts/page_memory/debug_pm_stage5_tagging_finalize.py --all-scopes --finalize
  uv run python scripts/page_memory/debug_pm_stage5_tagging_finalize.py --scope-id p1-100 --finalize --run-db
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

from loguru import logger

from _debug_pm_shared import (
    ScopeResult,
    TokenCostTracker,
    add_scope_selection_args,
    aggregate_stage_costs,
    base_argparser,
    build_production_job_metadata_from_stage_costs,
    load_anatomy_cache,
    resolve_anatomy_cache_path,
    load_assets_artifact,
    load_page_tags_artifact,
    load_hierarchy_artifact,
    load_stage_costs,
    pipeline_state_path,
    record_stage,
    require_file,
    resolve_debug_scope_ids,
    resolve_paths,
    sort_skeletons,
    walk,
    write_scope_artifacts,
    write_top_level_artifacts,
    stop_with_trace,
    update_pipeline_state,
    page_scope_info,
    _scope_manifest,
    _derive_hierarchy_page_scope,
    _build_hierarchy_from_skeletons,
)


def _run_tagging_for_scope(
    *,
    scope_id: str,
    scope_dir: Path,
    pdf_path: str,
    filename: str,
    out_dir: Path,
    page_count: int,
    page_texts: dict[int, str],
    page_features: list[Any],
    toc_pages: list[int],
    args: Any,
    token_cost_tracker: TokenCostTracker | None = None,
) -> ScopeResult:
    """Load Stage-3 combined tags and rehydrate renders for final assembly."""
    from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc
    from app.services.page_memory.page_renderer import render_document_pages

    scope_stages: list[dict[str, Any]] = []
    if token_cost_tracker is not None:
        token_cost_tracker.register_child_thread()

    fine_hierarchy_path = scope_dir / "fine_hierarchy.json"
    require_file(fine_hierarchy_path, hint=f"Run Stage 3 to produce {fine_hierarchy_path}")
    prior_scope, active_skeletons = load_hierarchy_artifact(fine_hierarchy_path)
    if not active_skeletons:
        logger.warning("   [scope {}] no skeletons — skipping", scope_id)
        return ScopeResult(
            scope_id=scope_id, skeletons=[], tags=[], assets_by_page={},
            rendered=[], final_pages=[], scope_manifest={}, trace_stages=scope_stages,
        )

    tags_path = scope_dir / "page_tags.json"
    require_file(tags_path, hint=f"Run Stage 3 to produce {tags_path}")
    tags = load_page_tags_artifact(tags_path)

    # Load existing assets if available
    assets_path = scope_dir / "assets.json"
    assets_by_page: dict[int, list[Any]] = {}
    if assets_path.exists():
        try:
            assets_by_page = load_assets_artifact(assets_path)
        except Exception as exc:
            logger.warning(
                "   failed to load {}; continuing without assets: {}",
                assets_path,
                exc,
            )

    # Reuse Stage-3's exact scope contract. Fall back only for older artifacts.
    recorded_pages = prior_scope.get("processing_pages")
    final_pages = (
        [int(page) for page in recorded_pages]
        if isinstance(recorded_pages, list)
        else pages_excluding_toc(
            _derive_hierarchy_page_scope(
                skeletons=active_skeletons,
                page_count=page_count,
            ),
            toc_pages,
        )
    )
    final_pages = pages_excluding_toc(final_pages, toc_pages)
    scope_manifest = _scope_manifest(
        scope_id=scope_id,
        skeletons=active_skeletons,
        page_count=page_count,
        strategy="fine:finalize",
    )
    logger.info(
        "🔬 [scope {}] loaded {} combined tags for {} processing pages",
        scope_id,
        len(tags),
        len(final_pages),
    )

    # A separate debug process rehydrates the deterministic production renders.
    rendered = render_document_pages(
        pdf_path=pdf_path,
        page_count=page_count,
        output_dir=str(out_dir),
        pages=final_pages,
        page_features=page_features,
        page_texts=page_texts,
    )
    record_stage(
        scope_stages, "C1.render_pages_rehydrated",
        page_info=page_scope_info([r.page_index for r in rendered]),
        variables={
            "scope_id": scope_id,
            "rendered_count": len(rendered),
            "tag_count": len(tags),
        },
    )

    # Preserve Stage-3 tags while attaching Stage-4 assets.
    write_scope_artifacts(
        out_dir=out_dir,
        scope_id=scope_id,
        scope_manifest=scope_manifest,
        hierarchy=active_skeletons,
        tags=tags,
        assets_by_page=assets_by_page if assets_by_page else None,
    )

    return ScopeResult(
        scope_id=scope_id,
        skeletons=active_skeletons,
        tags=tags,
        assets_by_page=assets_by_page,
        rendered=rendered,
        final_pages=final_pages,
        scope_manifest=scope_manifest,
        trace_stages=scope_stages,
    )


def _build_report(
    *,
    filename: str,
    anatomy,
    toc_nodes: list,
    skeletons: list,
    tags: list,
    chunks: list,
    rendered: list,
    elapsed: float,
    token_cost_stages: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = []
    ap = lines.append

    ap(f"# Page-Memory E2E Report — {filename}\n")
    ap(f"- elapsed: **{elapsed:.1f}s**")
    ap(f"- page_count: **{anatomy.page_count}**")
    ap(f"- toc_pages: `{anatomy.toc_result.toc_pages}`")

    toc_node_rows = walk(toc_nodes)
    toc_leaf_count = sum(1 for _, n in toc_node_rows if not n.children)
    ap(f"- TitleNode: total **{len(toc_node_rows)}**, leaves **{toc_leaf_count}**\n")

    ap("## 1. Skeleton\n")
    located = [s for s in skeletons if s.evidence.get("source") not in (None, "unlocated", "fallback_root")]
    source_dist = Counter(s.evidence.get("source", "?") for s in skeletons)
    ap(f"- leaf skeletons: **{len(skeletons)}**")
    ap(f"- located: **{len(located)}** ({len(located)*100//max(len(skeletons),1)}%)")
    ap(f"- source dist: `{dict(source_dist)}`\n")

    ap("## 2. Page Tags\n")
    strategy_dist = Counter(t.strategy_used for t in tags)
    ap(f"- total tagged: **{len(tags)}**")
    ap(f"- strategy dist: `{dict(strategy_dist)}`\n")

    ap("## 3. Node Assembly\n")
    type_dist = Counter(str(chunk.get("type", "?")) for chunk in chunks)
    ap(f"- total chunks: **{len(chunks)}**")
    ap(f"- type dist: `{dict(type_dist)}`\n")

    if token_cost_stages:
        ap("\n## Token Cost\n")
        ap("| stage | prompt | completion | calls | cost(USD) |")
        ap("|-------|-------:|----------:|------:|----------:|")
        t_pt = t_ct = t_calls = 0
        t_cost = 0.0
        for s in token_cost_stages:
            pt = s.get("prompt_tokens", 0)
            ct = s.get("completion_tokens", 0)
            calls = s.get("calls", 0)
            cost = float((s.get("cost") or {}).get("total_cost", 0))
            t_pt += pt
            t_ct += ct
            t_calls += calls
            t_cost += cost
            if calls:
                ap(f"| {s['stage']} | {pt:,} | {ct:,} | {calls} | ${cost:.6f} |")
        ap(f"| **total** | **{t_pt:,}** | **{t_ct:,}** | **{t_calls}** | **${t_cost:.6f}** |\n")

    return "\n".join(lines)


def main() -> int:
    parser = base_argparser("Stage 5: Node assembly + finalize")
    add_scope_selection_args(parser)
    parser.add_argument(
        "--max-workers", type=int, default=5,
        help="Concurrent workers (default=5)",
    )
    parser.add_argument(
        "--finalize", action="store_true",
        help="Run C9: chunks.json / doc_nav.json / manifest.json",
    )
    parser.add_argument(
        "--run-db", action="store_true",
        help="Publish to local DB (implies --finalize)",
    )
    parser.add_argument(
        "--publish-job-id", default=None,
        help="Explicit job_id for --run-db",
    )
    parser.add_argument(
        "--skip-assets", action="store_true",
        help="With --run-db, skip asset upload",
    )
    args = parser.parse_args()
    if args.run_db and not args.finalize:
        args.finalize = True

    from app.services.document_agent.pdf_text import read_page_texts
    from app.services.page_memory.node_assembler import build_node_rows
    from app.services.page_memory.skeleton_extractor import collapse_single_child_chains
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    pdf_path, filename, out_dir = resolve_paths(args)
    anatomy_cache = resolve_anatomy_cache_path(out_dir)
    state_path = pipeline_state_path(out_dir)
    scopes_dir = out_dir / "scopes"

    require_file(
        anatomy_cache,
        hint="Run Stage 1 first.",
    )

    anatomy = load_anatomy_cache(anatomy_cache, pdf_path, filename)
    page_count = anatomy.page_count
    page_features = anatomy.page_features if anatomy else []
    page_labels = anatomy.page_labels if anatomy else []
    toc_result = getattr(anatomy, "toc_result", None)
    toc_pages = list(getattr(toc_result, "toc_pages", None) or [])

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
    logger.info(f"  STAGE 5: ASSEMBLY + FINALIZE — {filename}")
    logger.info(f"  OUTPUT: {out_dir}")
    logger.info(f"  SCOPES: {scope_ids}")
    logger.info("█" * 70)

    t_start = time.time()
    trace_stages: list[dict] = []
    token_cost_tracker = TokenCostTracker()

    # Read page texts
    pages = list(range(1, page_count + 1))
    page_texts = read_page_texts(pdf_path, pages, timeout=300)
    logger.info(f"   read {len(page_texts)} pages")

    # ── Load combined tags and renders per scope ──
    def _load_selected_scope(scope_id: str) -> ScopeResult:
        return _run_tagging_for_scope(
            scope_id=scope_id,
            scope_dir=scopes_dir / scope_id,
            pdf_path=pdf_path,
            filename=filename,
            out_dir=out_dir,
            page_count=page_count,
            page_texts=page_texts,
            page_features=page_features,
            toc_pages=toc_pages,
            args=args,
            token_cost_tracker=token_cost_tracker,
        )

    if args.max_workers > 1 and len(scope_ids) > 1:
        import gevent
        from gevent.pool import Pool as GeventPool

        logger.info(
            "   concurrent mode: {} workers × {} scopes",
            args.max_workers, len(scope_ids),
        )
        gpool = GeventPool(size=min(args.max_workers, len(scope_ids)))
        greenlets = [
            gpool.spawn(
                _load_selected_scope,
                sid,
            )
            for sid in scope_ids
        ]
        gevent.joinall(greenlets, raise_error=True)
        scope_results = [cast(ScopeResult, g.value) for g in greenlets]
    else:
        logger.info("   serial mode: {} scope(s)", len(scope_ids))
        scope_results = [
            _load_selected_scope(sid)
            for sid in scope_ids
        ]

    # Merge trace
    for sr in scope_results:
        trace_stages.extend(sr.trace_stages)

    # ── Global merge ──
    all_skeletons = sort_skeletons(
        [skel for sr in scope_results for skel in sr.skeletons]
    )
    tag_by_page: dict[int, Any] = {}
    for sr in scope_results:
        for t in sr.tags:
            tag_by_page[t.page_index] = t
    all_tags = sorted(tag_by_page.values(), key=lambda t: t.page_index)

    from app.services.page_memory.memory_service import _merge_assets_by_page

    document_assets_path = out_dir / "assets.json"
    if document_assets_path.exists():
        try:
            all_assets = load_assets_artifact(document_assets_path)
            logger.info(
                "   loaded document assets.json: {} assets on {} pages",
                sum(len(items) for items in all_assets.values()),
                len(all_assets),
            )
        except Exception as exc:
            logger.warning(
                "   failed to load document assets.json ({}); falling back to scope assets",
                exc,
            )
            all_assets = _merge_assets_by_page(
                sr.assets_by_page for sr in scope_results
            )
    else:
        all_assets = _merge_assets_by_page(sr.assets_by_page for sr in scope_results)
        if all_assets:
            logger.info(
                "   legacy fallback: merged {} scope asset groups → {} pages",
                len(scope_results),
                len(all_assets),
            )

    rendered_by_page: dict[int, Any] = {}
    for sr in scope_results:
        for rp in sr.rendered:
            rendered_by_page[rp.page_index] = rp
    all_rendered = sorted(rendered_by_page.values(), key=lambda rp: rp.page_index)

    active_pages = sorted({p for sr in scope_results for p in sr.final_pages})

    # ── C4c: global collapse ──
    pre_collapse = len(all_skeletons)
    all_skeletons = collapse_single_child_chains(all_skeletons)
    absorbed = pre_collapse - len(all_skeletons)
    if absorbed:
        logger.info(
            "🪢 C4c collapse: {} → {} ({} absorbed)",
            pre_collapse, len(all_skeletons), absorbed,
        )
    record_stage(
        trace_stages,
        "C4c.collapse_single_child_chains",
        variables={
            "pre_count": pre_collapse,
            "post_count": len(all_skeletons),
            "absorbed": absorbed,
        },
    )

    active_skeletons = all_skeletons
    tags = all_tags

    # Write top-level artifacts
    write_top_level_artifacts(
        out_dir=out_dir,
        hierarchy=active_skeletons,
        tags=tags,
        assets_by_page=all_assets if all_assets else None,
    )

    # ── C7: Node assembly (same entry as production memory_service) ──
    logger.info("=" * 70)
    logger.info("🧱 C7: assemble canonical chunks")
    logger.info("=" * 70)

    tag_map = {t.page_index: t for t in tags}
    render_map = {r.page_index: r for r in all_rendered}
    raw_text_by_page: dict[int, str] = {}
    image_path_by_page: dict[int, str] = {}
    for page in active_pages:
        rend = render_map.get(page)
        raw_text_by_page[page] = (
            rend.raw_text if rend else page_texts.get(page, "")
        ) or ""
        if rend and rend.image_path and os.path.exists(rend.image_path):
            image_path_by_page[page] = rend.image_path

    page_memory_config = PageMemoryConfig.default()
    label_map: dict[int, str] = {}
    if page_labels:
        for lbl in page_labels:
            label_map[int(lbl.page)] = str(lbl.kind)
    rows = build_node_rows(
        skeletons=active_skeletons,
        raw_text_by_page=raw_text_by_page,
        image_path_by_page=image_path_by_page,
        kind_by_page=label_map,
        tag_by_page=tag_map,
        filename=filename,
        verdict="page",
        vlm_model=args.vlm_model or os.environ.get("IMAGE_MODEL"),
        page_assets_by_page=all_assets if all_assets else None,
        node_summary_max_pages=page_memory_config.node_summary_max_pages,
        node_assembly_concurrency=page_memory_config.node_assembly_concurrency,
    )
    chunks = [
        {
            "chunk_id": str(row.get("know_id") or ""),
            "type": str(row.get("type") or "page"),
            "content": str(row.get("content") or ""),
            "path": str(row.get("path") or ""),
            "metadata": {
                "length": int(row.get("length") or 0),
                "summary": str(row.get("summary") or ""),
                "page_nums": [
                    int(part)
                    for part in str(row.get("page_nums") or "").split(",")
                    if str(part).strip().isdigit()
                ],
                "keywords": [
                    part.strip()
                    for part in str(row.get("keywords") or "").split(";")
                    if part.strip()
                ],
                **(row.get("extra_metadata") or {}),
            },
        }
        for row in rows
    ]
    logger.info(f"   C7: {len(chunks)} canonical chunks")
    record_stage(
        trace_stages,
        "C7.node_assembly",
        page_info=page_scope_info(active_pages),
        variables={"chunk_count": len(chunks)},
    )
    token_cost_tracker.snapshot_stage("C7.node_assembly")

    # ── C9: finalize ──
    doc_nav: dict[str, Any] = {}
    hierarchy_dict: dict[str, Any] = {}
    if args.finalize:
        logger.info("=" * 70)
        logger.info("📦 C9: finalize (chunks → doc_nav → manifest)")
        logger.info("=" * 70)

        from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder

        t_fin = time.time()
        type_dist = Counter(c.get("type", "?") for c in chunks)
        logger.info(f"   chunks: {len(chunks)} ({dict(type_dist)})")

        chunks_path = out_dir / "chunks.json"
        chunks_path.write_text(
            json.dumps({"chunks": chunks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        doc_nav = ZipDocNavigationBuilder().build_doc_nav(chunks, filename)
        doc_nav_path = out_dir / "doc_nav.json"
        doc_nav_path.write_text(
            json.dumps(doc_nav, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        hierarchy_dict = (
            _build_hierarchy_from_skeletons(active_skeletons)
            if active_skeletons
            else {}
        )
        try:
            from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries
            enrich_doc_nav_summaries(str(out_dir.parent), source_file=filename, use_llm=False)
        except Exception as exc:
            logger.warning(f"   enrich failed (non-fatal): {exc}")

        logger.info(f"   finalize done in {time.time() - t_fin:.1f}s")
        record_stage(
            trace_stages,
            "C9.finalize",
            page_info=page_scope_info(active_pages),
            variables={
                "chunk_count": len(chunks),
                "doc_nav_sections": len(doc_nav.get("sections", [])),
            },
        )
        token_cost_tracker.snapshot_stage("C9.finalize")

        if args.run_db:
            from scripts._debug_publish import publish_debug_result_dir

            publish_result = publish_debug_result_dir(
                result_dir=out_dir,
                source_file_name=filename,
                chunks=chunks,
                job_id=args.publish_job_id,
                parse_track="page_memory",
                upload_assets=not args.skip_assets,
            )
            record_stage(
                trace_stages,
                "C10.debug_publish",
                variables={"publish_result": publish_result.to_dict()},
            )

    # ── Final trace + cross-stage cost rollup ──
    toc_nodes = (
        extract_toc_nodes(anatomy.toc_hierarchies) if anatomy.toc_hierarchies else []
    )
    elapsed = time.time() - t_start
    stop_with_trace(
        out_dir=out_dir,
        stages=trace_stages,
        stop_at="finalize" if args.finalize else "assembly",
        page_count=page_count,
        pipeline_stage=5,
        elapsed_s=elapsed,
        token_cost_tracker=token_cost_tracker,
        final_status="success",
        extra_summary={
            "scope_ids": [sr.scope_id for sr in scope_results],
            "chunk_count": len(chunks),
        },
    )
    aggregated = aggregate_stage_costs(load_stage_costs(out_dir))
    report = _build_report(
        filename=filename,
        anatomy=anatomy,
        toc_nodes=toc_nodes,
        skeletons=active_skeletons,
        tags=tags,
        chunks=chunks,
        rendered=all_rendered,
        elapsed=float(aggregated.get("elapsed_s") or elapsed),
        token_cost_stages=list(
            ((aggregated.get("token_cost") or {}).get("by_stage") or [])
        ),
    )
    report_path = out_dir / "debug" / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    if args.finalize:
        from shared.services.storage.zip_manifest_schema import ZipManifestBuilder

        manifest = ZipManifestBuilder().generate_manifest(
            job_id=filename,
            data_id=None,
            source_file_name=filename,
            statistics=doc_nav.get("stats", {}),
            job_metadata=build_production_job_metadata_from_stage_costs(
                page_count=page_count,
                ledger=load_stage_costs(out_dir),
            ),
            hierarchy=hierarchy_dict,
        )
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Extra production ZIP: same members as the user-facing page_memory package.
        # Leaves the full debug workspace untouched.
        from shared.services.storage.zip_result_service import ZipResultService

        production_zip_name = "production_result.zip"
        zip_path, checksum, statistics, zip_size = ZipResultService().generate_zip_package(
            job_id=filename,
            chunks=chunks,
            add_dir=str(out_dir),
            source_file_name=filename,
            data_id=None,
            job_metadata=build_production_job_metadata_from_stage_costs(
                page_count=page_count,
                ledger=load_stage_costs(out_dir),
            ),
            temp_dir=str(out_dir),
        )
        desired_zip = out_dir / production_zip_name
        generated_zip = Path(zip_path)
        if generated_zip.resolve() != desired_zip.resolve():
            if desired_zip.exists():
                desired_zip.unlink()
            generated_zip.replace(desired_zip)
            zip_path = str(desired_zip)
        record_stage(
            trace_stages,
            "C9.production_zip",
            variables={
                "zip_path": zip_path,
                "zip_size": zip_size,
                "checksum": checksum,
                "statistics": statistics,
            },
        )
        logger.info(f"  production ZIP → {zip_path} ({zip_size} bytes)")

    update_pipeline_state(
        state_path,
        stage=5,
        payload={
            "processed_scope_ids": [sr.scope_id for sr in scope_results],
            "finalized": bool(args.finalize),
            "chunk_count": len(chunks) if chunks is not None else None,
            "published": bool(args.run_db),
            "elapsed_s": elapsed,
        },
    )

    logger.info("")
    logger.info("═" * 70)
    logger.info(f"  ✅ DONE in {elapsed:.1f}s → {out_dir}")
    logger.info(
        "  total pipeline: {:.1f}s / ${:.6f}",
        float(aggregated.get("elapsed_s") or elapsed),
        float(
            ((aggregated.get("token_cost") or {}).get("total") or {}).get("total_cost")
            or 0
        ),
    )
    logger.info(f"  report  → {report_path}")
    if args.finalize:
        logger.info(f"  chunks  → {out_dir / 'chunks.json'}")
        logger.info(f"  doc_nav → {out_dir / 'doc_nav.json'}")
        logger.info(f"  prod ZIP → {out_dir / 'production_result.zip'}")
    logger.info("═" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
