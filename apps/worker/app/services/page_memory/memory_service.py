from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.document_agent.pdf_text import coerce_page_text_cache
from app.services.document_agent.visual import (
    purge_debug_visual_dirs,
    visual_debug_enabled,
)
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import PARSER_DF_COLUMNS
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.page_memory.normalizer import normalize_to_pdf
from app.services.page_memory._utils import (
    CoarseScope,
    collapse_page_ranges,
    page_scope_info,
    sort_skeletons,
)
from app.services.page_memory._serialization import (
    derive_hierarchy_page_scope as _derive_hierarchy_page_scope,
    scope_manifest as _scope_manifest,
    write_scope_artifacts as _write_scope_artifacts,
    write_top_level_artifacts as _write_top_level_artifacts,
)
from app.services.page_memory.phase_a import (
    build_coarse_text_dataframe,
    build_phase_a,
    persist_m0_inputs,
)

from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.schemas.page_memory_config import PageMemoryConfig, resolve_asset_model
from app.services.page_memory.body_text_preview import build_body_text_preview


@dataclass(frozen=True)
class PageMemoryInput:
    file_path: str
    filename: str
    output_dir: str
    job_id: str | None = None
    internal_output_filename: str | None = None
    base_url: str = ""
    page_memory_config: PageMemoryConfig = field(
        default_factory=PageMemoryConfig.default,
    )
    ingestion_mode: str = "full"


@dataclass(frozen=True)
class _ScopeRunResult:
    scope_id: str
    hierarchy: list[Any]
    tags: list[Any]
    assets_by_page: dict[int, list[Any]]
    rendered: list[Any]
    final_pages: list[int]
    fine_text_by_page: dict[int, str] = field(default_factory=dict)


def run(request: PageMemoryInput) -> tuple[str, pd.DataFrame]:
    """Run the page-memory track.

    Supports two granularity verdicts:
    - ``whole_doc`` (≤5 pages, no TOC) → single whole-document chunk
    - ``page`` → leaf section nodes via the full C1-C7 pipeline
    """
    full_output_dir = _resolve_output_dir(request)
    page_memory_config = request.page_memory_config
    os.makedirs(full_output_dir, exist_ok=True)
    _cleanup_page_memory_artifacts(full_output_dir)
    trace_recorder: Any | None = None
    final_status = "failed"
    trace_summary: dict[str, Any] = {}
    try:
        with stage_timer("page_memory.normalize_to_pdf", filename=request.filename):
            pdf_path, pdf_filename = normalize_to_pdf(
                file_path=request.file_path,
                filename=request.filename,
                output_dir=full_output_dir,
                base_url=request.base_url,
            )
        _persist_source_pdf(pdf_path=pdf_path, output_dir=full_output_dir)
        is_progressive = str(request.ingestion_mode or "full").strip().lower() == "progressive"
        visual_m0_enabled = is_progressive and bool(
            getattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)
        )
        with stage_timer("page_memory.profile", filename=pdf_filename):
            profile = profile_document(
                pdf_path,
                pdf_filename,
                job_id=request.job_id,
                output_dir=full_output_dir,
                oversized_policy="page_memory",
                stop_after="asset_probe" if is_progressive else None,
            )
        trace_recorder = getattr(profile, "trace_recorder", None)
        page_count = max(int(profile.page_count or 0), 0)
        if page_count > page_memory_config.max_pages:
            raise ValidationException(
                user_message=(
                    f"Document too large: {page_count} pages exceeds the "
                    f"{page_memory_config.max_pages}-page limit for page-based processing."
                ),
                violations=[{
                    "field": "page_count",
                    "description": f"PDF has {page_count} pages, limit is {page_memory_config.max_pages}",
                }],
            )
        verdict = "page" if is_progressive else _decide_granularity(profile)
        trace_summary = {
            "page_count": page_count,
            "verdict": verdict,
            "ingestion_mode": "progressive" if is_progressive else "full",
        }

        if verdict == "whole_doc":
            parsed_df = _build_whole_doc_dataframe(
                filename=request.filename,
                page_count=page_count,
                verdict=verdict,
                trace_recorder=trace_recorder,
                page_texts=coerce_page_text_cache(
                    getattr(profile, "page_full_text_cache", None)
                ),
                body_text_by_page=getattr(profile, "page_body_text_cache", None),
            )
        elif is_progressive:
            page_texts = coerce_page_text_cache(
                getattr(profile, "page_full_text_cache", None)
            )
            body_text_by_page = coerce_page_text_cache(
                getattr(profile, "page_body_text_cache", None)
            )
            if visual_m0_enabled:
                parsed_df = _build_visual_m0_dataframe(
                    pdf_path=pdf_path,
                    filename=request.filename,
                    output_dir=full_output_dir,
                    page_count=page_count,
                    page_texts=page_texts,
                    body_text_by_page=body_text_by_page,
                )
            else:
                parsed_df = build_coarse_text_dataframe(
                    page_texts=page_texts,
                    body_text_by_page=body_text_by_page,
                    page_count=page_count,
                    filename=request.filename,
                )
            persist_m0_inputs(
                output_dir=full_output_dir,
                profile=profile,
            )
        else:
            parsed_df = _build_page_dataframe(
                pdf_path=pdf_path,
                filename=request.filename,
                output_dir=full_output_dir,
                profile=profile,
                verdict=verdict,
                page_memory_config=page_memory_config,
                trace_recorder=trace_recorder,
            )
        final_status = "success"
        trace_summary["rows_count"] = len(parsed_df)
        return full_output_dir, parsed_df
    except Exception as exc:
        trace_summary["error"] = str(exc)
        raise
    finally:
        if trace_recorder is not None:
            trace_recorder.write_trace_artifact(
                full_output_dir,
                final_status=final_status,
                summary=trace_summary | trace_recorder.summary(),
            )
            _remove_nested_doc_agent_trace(full_output_dir)
        if not visual_debug_enabled():
            purge_debug_visual_dirs(full_output_dir)


def _resolve_output_dir(request: PageMemoryInput) -> str:
    output_name = request.internal_output_filename or request.filename
    return os.path.join(request.output_dir, Path(output_name).stem)


def _persist_source_pdf(*, pdf_path: str, output_dir: str) -> str:
    source_pdf_path = os.path.join(output_dir, "source.pdf")
    if Path(pdf_path).resolve() != Path(source_pdf_path).resolve():
        shutil.copyfile(pdf_path, source_pdf_path)
    return source_pdf_path


def _build_visual_m0_dataframe(
    *,
    pdf_path: str,
    filename: str,
    output_dir: str,
    page_count: int,
    page_texts: dict[int, str],
    body_text_by_page: dict[int, str],
) -> pd.DataFrame:
    from app.services.page_memory.page_renderer import render_document_pages
    from shared.services.ai.embedding import embed_images

    pages = list(range(1, max(int(page_count or 0), 0) + 1))
    with stage_timer("page_memory.m0_render", filename=filename, page_count=len(pages)):
        rendered = render_document_pages(
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=output_dir,
            scope_id="m0",
            pages=pages,
            page_texts=page_texts,
        )
    image_path_by_page = {
        int(item.page_index): str(item.image_path)
        for item in rendered
        if item.image_path
    }
    ordered_pages = [page for page in pages if page in image_path_by_page]
    with stage_timer(
        "page_memory.m0_embed",
        filename=filename,
        page_count=len(ordered_pages),
    ):
        vectors = embed_images(
            [image_path_by_page[page] for page in ordered_pages],
            usage_task="page_memory.m0_embed",
        )
    embeddings_by_page = {}
    skipped_pages: list[int] = []
    for page, vec in zip(ordered_pages, vectors, strict=True):
        if isinstance(vec, list) and vec:
            embeddings_by_page[page] = vec
        else:
            skipped_pages.append(page)
    if skipped_pages:
        logger.warning(
            "Skipped page embeddings after retries; document continues: pages={}",
            skipped_pages,
        )
    return build_coarse_text_dataframe(
        page_texts=page_texts,
        body_text_by_page=body_text_by_page,
        page_count=page_count,
        filename=filename,
        image_path_by_page=image_path_by_page,
        embeddings_by_page=embeddings_by_page,
        output_dir=output_dir,
    )


def _decide_granularity(profile: Any) -> str:
    """Decide granularity for a page-memory document.

    Page-based track processes pages individually via VLM — no physical
    document splitting is needed regardless of page count.  The old
    ``shard_page`` verdict (>200 pages) was only meaningful when long
    PDFs were cut into physical sub-documents.
    """
    page_count = int(getattr(profile, "page_count", 0) or 0)
    toc = getattr(profile, "toc", None)
    has_toc = bool(getattr(toc, "has_toc", False))
    if page_count <= 5 and not has_toc:
        return "whole_doc"
    return "page"


def _resolve_hierarchy_model(page_memory_config: PageMemoryConfig) -> str | None:
    return (
        page_memory_config.hierarchy_model
        or os.environ.get("HIERARCHY_LLM_MODEL")
        or os.environ.get("NORMOL_MODEL")
    )


# ── page builder (C1→C2→C3→C4→C6→C7) ────────────────────────────────


def assemble_c7_node_rows(
    *,
    skeletons: list[Any],
    rendered: list[Any] | dict[int, Any],
    tags: list[Any],
    assets_by_page: dict[int, list[Any]],
    page_texts: dict[int, str],
    body_text_by_page: dict[int, str] | None,
    page_labels: list[Any],
    toc_pages: list[int],
    page_count: int,
    output_dir: str,
    filename: str,
    verdict: str,
    vlm_model: str | None,
    page_memory_config: PageMemoryConfig,
    fine_text_by_page: dict[int, str] | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Shared C7 node-row assembly used by whole-doc ``_build_page_dataframe`` and per-scope refinement."""
    from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc
    from app.services.page_memory.node_assembler import build_node_rows

    if isinstance(rendered, dict):
        render_map = rendered
    else:
        render_map = {item.page_index: item for item in rendered}
    tag_map = {t.page_index: t for t in tags}
    label_map: dict[int, str] = {}
    if page_labels:
        for lbl in page_labels:
            label_map[lbl.page] = lbl.kind

    final_pages = pages_excluding_toc(
        _derive_hierarchy_page_scope(
            skeletons=skeletons,
            page_count=page_count,
        ),
        toc_pages,
    )
    fine_text = fine_text_by_page or {}
    raw_text_by_page: dict[int, str] = {}
    image_path_by_page: dict[int, str] = {}
    for page in final_pages:
        rend = render_map.get(page)
        fallback = (rend.raw_text if rend else page_texts.get(page, "")) or ""
        raw_text_by_page[page] = (fine_text.get(page) or fallback)
        if rend and rend.image_path and os.path.exists(rend.image_path):
            image_path_by_page[page] = rend.image_path

    rows = build_node_rows(
        skeletons=skeletons,
        raw_text_by_page=raw_text_by_page,
        image_path_by_page=image_path_by_page,
        output_dir=output_dir,
        kind_by_page=label_map,
        tag_by_page=tag_map,
        filename=filename,
        verdict=verdict,
        vlm_model=vlm_model,
        page_assets_by_page=assets_by_page,
        node_summary_max_pages=page_memory_config.node_summary_max_pages,
        node_assembly_concurrency=page_memory_config.node_assembly_concurrency,
        body_text_by_page=body_text_by_page,
    )
    return rows, final_pages


def _build_page_dataframe(
    *,
    pdf_path: str,
    filename: str,
    output_dir: str,
    profile: Any,
    verdict: str,
    page_memory_config: PageMemoryConfig,
    trace_recorder: Any | None = None,
) -> pd.DataFrame:
    """Build per-page DataFrame via Phase A scopes then the full C1-C7 path.

    Steps:
      Phase A  build_phase_a → frozen scopes / next titles / asset budgets
      C1  page_renderer       → PageRenderResult[]
      C2  page_plan           → PagePlan[]
      C3  page_tagger         → PageTagResult[]
      C3b title detection     → observed_titles (reading-order, untrimmed)
      C4b fine_hierarchy      → start/end anchor trim + refined SectionSkeleton[]
      C5  page_assets          → assets anchored to refined hierarchy pages
      C7  assemble node-granularity DataFrame
    """
    from app.services.page_memory.skeleton_extractor import collapse_single_child_chains

    page_count = max(int(profile.page_count or 0), 0)
    if page_count <= 0:
        return pd.DataFrame(columns=pd.Index(PARSER_DF_COLUMNS))

    asset_extraction_enabled = page_memory_config.asset_extraction_enabled
    phase_a = build_phase_a(
        profile=profile,
        filename=filename,
        trace_recorder=trace_recorder,
    )
    page_texts = phase_a.page_texts
    coarse_scopes = phase_a.coarse_scopes
    next_title_by_path = phase_a.next_title_by_path
    toc_pages = phase_a.toc_pages
    anatomy = getattr(profile, "anatomy", None)
    page_features = anatomy.page_features if anatomy else []
    page_labels = anatomy.page_labels if anatomy else []
    vlm_model = os.environ.get("IMAGE_MODEL")

    scope_concurrency = page_memory_config.scope_concurrency
    logger.info(
        "[page_memory] processing {} hierarchy scopes (concurrency={})",
        len(coarse_scopes),
        scope_concurrency,
    )
    if toc_pages:
        logger.info(
            "[page_memory] excluding TOC pages from scope processing: {}",
            sorted({int(page) for page in toc_pages}),
        )
    scope_results: list[_ScopeRunResult] = []
    _scope_kwargs = dict(
        pdf_path=pdf_path,
        filename=filename,
        output_dir=output_dir,
        page_count=page_count,
        page_texts=page_texts,
        page_features=page_features,
        page_labels=page_labels,
        vlm_model=vlm_model,
        asset_extraction_enabled=asset_extraction_enabled,
        trace_recorder=trace_recorder,
        page_memory_config=page_memory_config,
        next_title_by_path=next_title_by_path,
        toc_pages=toc_pages,
    )

    if scope_concurrency <= 1 or len(coarse_scopes) <= 1:
        for index, scope in enumerate(coarse_scopes, start=1):
            scope_results.append(
                run_single_scope(
                    scope=scope,
                    scope_index=index,
                    scope_count=len(coarse_scopes),
                    **_scope_kwargs,
                )
            )
    else:
        import gevent
        from gevent.pool import Pool as GeventPool

        pool = GeventPool(
            size=min(scope_concurrency, len(coarse_scopes)),
        )
        greenlets = [
            pool.spawn(
                run_single_scope,
                scope=scope,
                scope_index=idx,
                scope_count=len(coarse_scopes),
                **_scope_kwargs,
            )
            for idx, scope in enumerate(coarse_scopes, start=1)
        ]
        gevent.joinall(greenlets, raise_error=True)
        scope_results = [g.value for g in greenlets if g.value is not None]

    skeletons = sort_skeletons(
        [
            skeleton
            for result in scope_results
            for skeleton in result.hierarchy
        ]
    )
    skeletons = collapse_single_child_chains(skeletons)
    tags = _merge_page_tags(result.tags for result in scope_results)
    page_assets_by_page = _merge_assets_by_page(
        result.assets_by_page for result in scope_results
    )
    rendered_map: dict[int, Any] = {}
    fine_text_by_page: dict[int, str] = {}
    for result in scope_results:
        for rendered_page in result.rendered:
            rendered_map.setdefault(rendered_page.page_index, rendered_page)
        for page, text in result.fine_text_by_page.items():
            fine_text_by_page.setdefault(int(page), text)

    _write_top_level_artifacts(
        output_dir=output_dir,
        hierarchy=skeletons,
        tags=tags,
        assets_by_page=page_assets_by_page,
    )

    # ── C7: assemble DataFrame rows ──────────────────────────────────
    with stage_timer("page_memory.node_assembly", page_count=page_count):
        rows, final_pages_scope = assemble_c7_node_rows(
            skeletons=skeletons,
            rendered=rendered_map,
            tags=tags,
            assets_by_page=page_assets_by_page,
            page_texts=page_texts,
            body_text_by_page=getattr(profile, "page_body_text_cache", None),
            page_labels=page_labels,
            toc_pages=toc_pages,
            page_count=page_count,
            output_dir=output_dir,
            filename=filename,
            verdict=verdict,
            vlm_model=vlm_model,
            page_memory_config=page_memory_config,
            fine_text_by_page=fine_text_by_page,
        )
    logger.info(
        "[page_memory] C7 assembled {} node rows (verdict={})",
        len(rows), verdict,
    )
    _record_trace_stage(
        trace_recorder,
        "C7.node_assembly",
        page_info=page_scope_info(final_pages_scope),
        variables={
            "row_count": len(rows),
            "scope_count": len(scope_results),
            "page_to_node": _page_to_node_map(rows),
        },
    )
    return pd.DataFrame(rows, columns=pd.Index(PARSER_DF_COLUMNS))


_SCOPE_RETRY_DELAY_SECONDS = 10.0


def run_single_scope(**kwargs: Any) -> _ScopeRunResult:
    """Run one frozen scope through the existing Stage-3/4 path."""
    return _run_scope_with_retry(**kwargs)


def _run_scope_with_retry(
    **kwargs: Any,
) -> _ScopeRunResult:
    """Execute a single scope, retrying once on transient LLM failures."""
    import gevent

    from shared.core.exceptions.domain_exceptions import LLMServiceException

    scope: CoarseScope = kwargs["scope"]
    scope_index: int = kwargs["scope_index"]
    scope_count: int = kwargs["scope_count"]

    for attempt in range(2):
        try:
            return _run_hierarchy_scope(**kwargs)
        except LLMServiceException as exc:
            if attempt == 0:
                logger.warning(
                    "[page_memory] scope {}/{} {} LLM failure, retrying after {}s: {}",
                    scope_index,
                    scope_count,
                    scope.scope_id,
                    _SCOPE_RETRY_DELAY_SECONDS,
                    exc,
                )
                gevent.sleep(_SCOPE_RETRY_DELAY_SECONDS)
            else:
                raise
    # Unreachable, but satisfies type-checker
    raise RuntimeError("unreachable")  # pragma: no cover


def _run_hierarchy_scope(
    *,
    scope: CoarseScope,
    scope_index: int,
    scope_count: int,
    pdf_path: str,
    filename: str,
    output_dir: str,
    page_count: int,
    page_texts: dict[int, str],
    page_features: list[Any],
    page_labels: list[Any],
    vlm_model: str | None,
    asset_extraction_enabled: bool,
    trace_recorder: Any | None,
    page_memory_config: PageMemoryConfig,
    next_title_by_path: dict[str, str | None] | None = None,
    toc_pages: list[int] | None = None,
) -> _ScopeRunResult:
    from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc
    from app.services.page_memory.fine_hierarchy import (
        compute_fat_leaf_pages,
        refine_fat_leaf_skeletons,
    )
    from app.services.page_memory.page_assets import extract_page_assets_from_renders
    from app.services.page_memory.page_plan import derive_page_processing_plan
    from app.services.page_memory.page_renderer import render_document_pages
    from app.services.page_memory.page_tagger import (
        PageTagResult,
        tag_page_titles,
        tag_pages,
    )

    scope_skeletons = sort_skeletons(scope.skeletons)
    toc_pages = sorted({int(page) for page in (toc_pages or [])})
    scope_manifest = _scope_manifest(
        scope_id=scope.scope_id,
        skeletons=scope_skeletons,
        page_count=page_count,
        strategy=scope.strategy,
    )
    coarse_pages = pages_excluding_toc(
        _derive_hierarchy_page_scope(
            skeletons=scope_skeletons,
            page_count=page_count,
        ),
        toc_pages,
    )
    logger.info(
        "[page_memory] scope {}/{} {} coarse ranges={}",
        scope_index,
        scope_count,
        scope.scope_id,
        collapse_page_ranges(coarse_pages),
    )
    _record_trace_stage(
        trace_recorder,
        "C4.coarse_scope",
        page_info=page_scope_info(coarse_pages),
        variables={
            "scope_index": scope_index,
            "scope_count": scope_count,
            "scope": scope_manifest,
        },
    )

    fine_min = page_memory_config.fine_min_pages
    fat_leaf_pages = compute_fat_leaf_pages(
        scope_skeletons,
        min_pages=fine_min,
        toc_pages=toc_pages,
    )
    if fat_leaf_pages:
        title_pages = sorted(fat_leaf_pages)
        with stage_timer("page_memory.title_render", page_count=len(title_pages)):
            title_rendered = render_document_pages(
                pdf_path=pdf_path,
                page_count=page_count,
                output_dir=output_dir,
                scope_id=scope.scope_id,
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
        with stage_timer("page_memory.title_detection", page_count=len(title_pages)):
            title_tags = tag_page_titles(
                pages=title_rendered,
                tag_results=title_tags,
                fat_leaf_pages=fat_leaf_pages,
                vlm_model=vlm_model,
                scan_direction=page_memory_config.scan_direction,
                max_concurrent=page_memory_config.title_detection_concurrency,
            )
        _record_trace_stage(
            trace_recorder,
            "C3b.title_detection",
            page_info=page_scope_info(title_pages),
            variables={
                "scope_id": scope.scope_id,
                "tags": _summarize_tags(title_tags),
            },
        )
        with stage_timer("page_memory.fine_hierarchy", page_count=len(fat_leaf_pages)):
            scope_skeletons = refine_fat_leaf_skeletons(
                coarse_skeletons=scope_skeletons,
                tag_results=title_tags,
                fat_leaf_pages=fat_leaf_pages,
                next_title_by_path=next_title_by_path,
                model_name=_resolve_hierarchy_model(page_memory_config),
                max_tokens=page_memory_config.hierarchy_max_tokens,
                max_depth=page_memory_config.max_heading_depth,
                trace_recorder=trace_recorder,
            )
    else:
        logger.info(
            "[page_memory] scope {} no fat leaves (min={}); skip fine hierarchy",
            scope.scope_id,
            fine_min,
        )

    scope_manifest = _scope_manifest(
        scope_id=scope.scope_id,
        skeletons=scope_skeletons,
        page_count=page_count,
        strategy=f"{scope.strategy}:refined",
    )
    _record_trace_stage(
        trace_recorder,
        "C4b.fine_hierarchy",
        page_info={"fat_leaf": page_scope_info(sorted(fat_leaf_pages))},
        variables={
            "scope_id": scope.scope_id,
            "sections": _summarize_skeletons(scope_skeletons),
            "scope": scope_manifest,
        },
    )

    final_pages = pages_excluding_toc(
        _derive_hierarchy_page_scope(
            skeletons=scope_skeletons,
            page_count=page_count,
        ),
        toc_pages,
    )
    final_scope_summary = _summarize_tag_scope(
        skeletons=scope_skeletons,
        page_count=page_count,
        pages=final_pages,
    )
    with stage_timer("page_memory.render", page_count=len(final_pages)):
        rendered = render_document_pages(
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=output_dir,
            scope_id=scope.scope_id,
            pages=final_pages,
            page_features=page_features,
            page_texts=page_texts,
        )
    from app.services.page_memory.page_text_extractor import extract_fine_page_text

    with stage_timer("page_memory.fine_text", page_count=len(final_pages)):
        fine_text_by_page = extract_fine_page_text(
            pdf_path=pdf_path,
            rendered=rendered,
            final_pages=final_pages,
            extractor=page_memory_config.fine_text_extractor,
            vlm_model=vlm_model,
            output_dir=os.path.join(output_dir, "_fine_text", scope.scope_id),
            concurrency=page_memory_config.fine_text_extraction_concurrency,
        )
    _record_trace_stage(
        trace_recorder,
        "C1.render_pages",
        page_info=page_scope_info([item.page_index for item in rendered]),
        variables={
            "scope_id": scope.scope_id,
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
    _record_trace_stage(
        trace_recorder,
        "C2.page_plan",
        page_info=page_scope_info([getattr(plan, "page_index", None) for plan in plans]),
        variables={"scope_id": scope.scope_id, "plan_count": len(plans)},
    )

    with stage_timer("page_memory.tag", page_count=len(rendered)):
        tags = tag_pages(
            pages=rendered,
            plans=plans,
            vlm_model=vlm_model,
            max_concurrent=page_memory_config.tag_concurrency,
        )
    _record_trace_stage(
        trace_recorder,
        "C3.page_tagger",
        page_info=page_scope_info([tag.page_index for tag in tags]),
        variables={"scope_id": scope.scope_id, "tags": _summarize_tags(tags)},
    )
    _write_scope_artifacts(
        output_dir=output_dir,
        scope_id=scope.scope_id,
        scope_manifest_data=scope_manifest,
        hierarchy=scope_skeletons,
        tags=tags,
    )

    assets_by_page: dict[int, list[Any]] = {}
    if asset_extraction_enabled:
        asset_rendered = _select_rendered_pages_with_assets(
            rendered, page_features
        )
        with stage_timer("page_memory.assets", page_count=len(asset_rendered)):
            assets_by_page = extract_page_assets_from_renders(
                pdf_path=pdf_path,
                rendered_pages=asset_rendered,
                output_dir=output_dir,
                model_name=page_memory_config.asset_model or resolve_asset_model(),
                confidence_threshold=page_memory_config.asset_confidence_threshold,
                summary_enabled=page_memory_config.asset_summary_enabled,
                summary_concurrency=page_memory_config.asset_summary_concurrency,
                table_engine=page_memory_config.table_engine,
                table_merge_enabled=page_memory_config.table_merge_enabled,
            )
    _record_trace_stage(
        trace_recorder,
        "C5.page_assets",
        page_info=page_scope_info(sorted(assets_by_page)),
        variables={
            "scope_id": scope.scope_id,
            "asset_count": sum(len(items) for items in assets_by_page.values()),
            "assets_by_page": {
                page: [asset.asset_id for asset in assets]
                for page, assets in assets_by_page.items()
            },
        },
    )
    # Refresh hierarchy/tags and write assets without wiping unrelated slots.
    _write_scope_artifacts(
        output_dir=output_dir,
        scope_id=scope.scope_id,
        scope_manifest_data=scope_manifest,
        hierarchy=scope_skeletons,
        tags=tags,
        assets_by_page=assets_by_page,
    )

    return _ScopeRunResult(
        scope_id=scope.scope_id,
        hierarchy=scope_skeletons,
        tags=tags,
        assets_by_page=assets_by_page,
        rendered=rendered,
        final_pages=final_pages,
        fine_text_by_page=fine_text_by_page,
    )


# ── helpers ───────────────────────────────────────────────────────────


def _select_rendered_pages_with_assets(
    rendered: list[Any],
    page_features: list[Any],
) -> list[Any]:
    """Keep only rendered pages that coarse profile marked ``has_asset``."""
    asset_pages = {
        int(getattr(feature, "page", 0) or 0)
        for feature in page_features
        if getattr(feature, "has_asset", False)
    }
    return [item for item in rendered if item.page_index in asset_pages]


# ── whole_doc builder (PR3, unchanged) ────────────────────────────────


def _build_whole_doc_dataframe(
    *,
    filename: str,
    page_count: int,
    verdict: str,
    trace_recorder: Any | None = None,
    page_texts: dict[int, str] | None = None,
    body_text_by_page: dict[int, str] | None = None,
) -> pd.DataFrame:
    pages = list(range(1, page_count + 1)) if page_count > 0 else [1]
    texts = coerce_page_text_cache(page_texts)
    raw_text = "\n\n".join(texts.get(page, "") for page in pages).strip()
    summary = _build_summary(filename=filename, page_count=page_count, raw_text=raw_text)
    content = f"[SUMMARY]\n{summary}\n\n[RAW]\n{raw_text}".strip()
    know_id = gen_str_codes(f"wholedoc::{filename}::{content}")
    row = {
        "content": content,
        "path": f"{filename}/Root",
        "type": "page",
        "length": len(content),
        "keywords": "",
        "summary": summary,
        "know_id": know_id,
        "tokens": "",
        "connectto": "",
        "addtime": get_str_time(),
        "page_nums": ",".join(str(page) for page in pages),
        "extra_metadata": {
            "body_text_preview": build_body_text_preview(
                (body_text_by_page or {}).get(page, "") for page in pages
            ),
        },
    }
    _record_trace_stage(
        trace_recorder,
        "whole_doc",
        page_info=page_scope_info(pages),
        variables={"summary": summary, "verdict": verdict},
    )
    return pd.DataFrame([row], columns=pd.Index(PARSER_DF_COLUMNS))


def _build_summary(*, filename: str, page_count: int, raw_text: str) -> str:
    prefix = f"{filename} whole-document memory ({page_count} pages)"
    preview = " ".join(raw_text.split())[:500]
    return f"{prefix}: {preview}" if preview else prefix


def _record_trace_stage(
    trace_recorder: Any | None,
    stage: str,
    *,
    page_info: dict[str, Any],
    variables: dict[str, Any],
) -> None:
    if trace_recorder is None or not hasattr(trace_recorder, "record_stage"):
        return
    trace_recorder.record_stage(stage, page_info=page_info, variables=variables)


def _cleanup_page_memory_artifacts(output_dir: str) -> None:
    root = Path(output_dir)
    stale_files = {
        "assets.json",
        "chunks.json",
        "coarse_scopes.json",
        "doc_nav.json",
        "hierarchy.json",
        "manifest.json",
        "node_rows.csv",
        "node_rows.json",
        "page_plans.json",
        "page_rendered.json",
        "page_tags.json",
        "report.md",
        "trace.json",
    }
    for name in stale_files:
        path = root / name
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            logger.debug("[page_memory] failed to cleanup artifact {}", path)
    for name in ("asset_annotate", "debug", "images", "pages", "phase_b", "scopes", "tables"):
        path = root / name
        try:
            if path.is_dir():
                shutil.rmtree(path)
        except Exception:
            logger.debug("[page_memory] failed to cleanup artifact dir {}", path)


def _remove_nested_doc_agent_trace(output_dir: str) -> None:
    try:
        (Path(output_dir) / "_doc_agent" / "trace.json").unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("[page_memory] failed to remove nested doc-agent trace: {}", exc)


def _merge_page_tags(tag_groups: Any) -> list[Any]:
    by_page: dict[int, Any] = {}
    for tags in tag_groups:
        for tag in tags:
            by_page[int(getattr(tag, "page_index", 0) or 0)] = tag
    return [by_page[page] for page in sorted(by_page) if page > 0]


def _merge_assets_by_page(asset_groups: Any) -> dict[int, list[Any]]:
    merged: dict[int, list[Any]] = {}
    seen_ids: set[str] = set()
    for assets_by_page in asset_groups:
        for page, assets in assets_by_page.items():
            for asset in assets:
                asset_id = str(getattr(asset, "asset_id", "") or "")
                if asset_id and asset_id in seen_ids:
                    continue
                if asset_id:
                    seen_ids.add(asset_id)
                merged.setdefault(int(page), []).append(asset)
    return {page: merged[page] for page in sorted(merged)}


def _summarize_skeletons(skeletons: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "path": item.section_path,
            "level": item.level,
            "start_page": item.start_page,
            "end_page": item.end_page,
        }
        for item in skeletons
    ]


def _summarize_tag_scope(
    *,
    skeletons: list[Any],
    page_count: int,
    pages: list[int],
) -> dict[str, Any]:
    return {
        "strategy": "hierarchy_anchored_pages",
        "document_page_count": page_count,
        "skeleton_count": len(skeletons),
        **page_scope_info(pages),
    }


def _summarize_tags(tags: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "page": item.page_index,
            "title": getattr(item, "title", ""),
            "summary": getattr(item, "summary", ""),
            "kind": getattr(item, "kind", ""),
        }
        for item in tags
    ]


def _page_to_node_map(rows: list[dict[str, Any]]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for row in rows:
        if row.get("type") != "page":
            continue
        path = str(row.get("path") or "")
        for raw_page in str(row.get("page_nums") or "").split(","):
            try:
                mapping[int(raw_page.strip())] = path
            except ValueError:
                continue
    return mapping
