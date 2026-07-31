from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.visual import (
    purge_debug_visual_dirs,
    visual_debug_enabled,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.support.identifiers import gen_str_codes
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.page_memory.normalizer import normalize_to_pdf
from app.services.page_memory._utils import (
    CoarseScope,
    build_hierarchy_scopes,
    collapse_page_ranges,
    page_scope_info,
    sort_skeletons,
)
from app.services.page_memory.toc_page_policy import TocPagePolicy
from app.services.page_memory._serialization import (
    derive_hierarchy_page_scope as _derive_hierarchy_page_scope,
    scope_manifest as _scope_manifest,
    write_scope_artifacts as _write_scope_artifacts,
    write_top_level_artifacts as _write_top_level_artifacts,
)

from loguru import logger

from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.services.chunks.canonical_chunk_builder import ChunkMetadata, ChunkPayload


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


_HierarchyScope = CoarseScope


@dataclass(frozen=True)
class _ScopeRunResult:
    scope_id: str
    hierarchy: list[Any]
    tags: list[Any]
    rendered: list[Any]
    final_pages: list[int]


def run(request: PageMemoryInput) -> tuple[str, list[ChunkPayload]]:
    """Run the page-memory track.

    Supports two granularity verdicts:
    - ``whole_doc`` (≤6 pages, no TOC) → single whole-document chunk
    - ``page`` → leaf-node chunks assembled from page-owned evidence via C1-C7
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
        with stage_timer("page_memory.profile", filename=pdf_filename):
            profile = profile_document(
                pdf_path,
                pdf_filename,
                job_id=request.job_id,
                output_dir=full_output_dir,
                skip_shard_plan=True,
                oversized_policy="page_memory",
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
        verdict = _decide_granularity(profile)
        trace_summary = {
            "page_count": page_count,
            "verdict": verdict,
        }

        if verdict == "whole_doc":
            chunks = _build_whole_doc_chunks(
                pdf_path=pdf_path,
                filename=request.filename,
                page_count=page_count,
                verdict=verdict,
                trace_recorder=trace_recorder,
            )
        else:
            chunks = _build_page_chunks(
                pdf_path=pdf_path,
                filename=request.filename,
                output_dir=full_output_dir,
                profile=profile,
                verdict=verdict,
                trace_recorder=trace_recorder,
                page_memory_config=page_memory_config,
            )
        final_status = "success"
        trace_summary["rows_count"] = len(chunks)
        return full_output_dir, chunks
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


def _decide_granularity(profile: Any) -> str:
    """Decide granularity for a page-memory document.

    Page-based track processes pages individually via VLM — no physical
    document splitting is needed regardless of page count.  The old
    ``shard_page`` verdict (>200 pages) was only meaningful for the
    MinerU batch API pipeline (``_parse_pdf_via_shards``) which splits
    long PDFs into physical sub-documents.
    """
    page_count = int(getattr(profile, "page_count", 0) or 0)
    toc = getattr(profile, "toc", None)
    has_toc = bool(getattr(toc, "has_toc", False))
    if page_count <= 6 and not has_toc:
        return "whole_doc"
    return "page"


def _resolve_asset_max_pages(
    page_count: int,
    page_memory_config: PageMemoryConfig,
) -> int:
    configured_limit = page_memory_config.asset_max_pages
    if configured_limit is None:
        return page_count
    return max(0, min(page_count, configured_limit))


def _resolve_hierarchy_model(page_memory_config: PageMemoryConfig) -> str | None:
    return (
        page_memory_config.hierarchy_model
        or os.environ.get("HIERARCHY_LLM_MODEL")
        or os.environ.get("NORMOL_MODEL")
    )


# ── page builder (C1→C2→C3→C4→C6→C7) ────────────────────────────────


def _build_page_chunks(
    *,
    pdf_path: str,
    filename: str,
    output_dir: str,
    profile: Any,
    verdict: str,
    page_memory_config: PageMemoryConfig,
    trace_recorder: Any | None = None,
) -> list[ChunkPayload]:
    """Build per-page canonical chunks via the full C1-C7 pipeline.

    Steps:
      C4  skeleton_extractor  → SectionSkeleton[]
      C1  page_renderer       → document-level PageRenderResult[] (deduped)
      C2  page_plan           → PagePlan[] for processing pages
      C3  page_tagger         → titles + summary + entities (global page pool)
      C4b fine_hierarchy      → per-scope start/end trim + refined skeletons
      C5  page_assets          → document-level unique-page extraction (∥ C3)
      C7  assemble node-granularity chunks
    """
    from app.services.page_memory.skeleton_extractor import (
        SectionSkeleton,
        collapse_single_child_chains,
        extract_section_skeletons,
    )
    from shared.services.chunks.path_segments import join_document_path
    anatomy = getattr(profile, "anatomy", None)
    page_count = max(int(profile.page_count or 0), 0)
    if page_count <= 0:
        return []

    asset_extraction_enabled = page_memory_config.asset_extraction_enabled

    # ── build ToolContext for sub-agent VLM calls ─────────────────────
    ctx = _build_page_ctx(
        pdf_path=pdf_path,
        job_id=filename,
        output_dir=output_dir,
        page_count=page_count,
        trace_recorder=trace_recorder,
    )

    # ── C4: skeleton (from profile anatomy) ───────────────────────────
    with stage_timer("page_memory.read_page_texts", page_count=page_count):
        page_texts = read_page_texts(
            pdf_path, list(range(1, page_count + 1)), timeout=300,
        )
    with stage_timer("page_memory.skeleton", page_count=page_count):
        if anatomy is not None:
            skeletons = extract_section_skeletons(
                anatomy=anatomy,
                filename=filename,
                page_texts=page_texts,
                ctx=ctx,
            )
        else:
            skeletons = []
    logger.info(
        "[page_memory] C4 skeleton: {} sections from anatomy",
        len(skeletons),
    )

    if not skeletons:
        skeletons = [
            SectionSkeleton(
                section_path=join_document_path([filename, "Root"]),
                level=1,
                start_page=1,
                end_page=page_count,
                title="Root",
                parent_path=filename,
                evidence={"source": "fallback_root", "reason": "no_hierarchy"},
            )
        ]

    from app.services.page_memory.fine_hierarchy import build_next_title_by_path

    next_title_by_path = build_next_title_by_path(skeletons)
    toc_policy = TocPagePolicy.from_anatomy(anatomy)
    coarse_scopes = _build_hierarchy_scopes(
        skeletons=skeletons,
        filename=filename,
        page_count=page_count,
        processing_pages=toc_policy.filter_processing_pages(
            list(range(1, page_count + 1))
        ),
        excluded_toc_pages=sorted(toc_policy.pure_toc_pages),
    )
    coarse_pages_scope = toc_policy.filter_processing_pages(
        _derive_hierarchy_page_scope(
            skeletons=skeletons,
            page_count=page_count,
        )
    )
    _record_trace_stage(
        trace_recorder,
        "C4.skeleton",
        page_info={
            "document_page_count": page_count,
            "coarse_scope": page_scope_info(coarse_pages_scope),
            "excluded_toc": page_scope_info(sorted(toc_policy.pure_toc_pages)),
        },
        variables={
            "sections": _summarize_skeletons(skeletons),
            "scope_count": len(coarse_scopes),
            "toc_regions": [region.to_dict() for region in toc_policy.regions],
            "scopes": [
                _scope_manifest(
                    scope_id=scope.scope_id,
                    skeletons=scope.skeletons,
                    page_count=page_count,
                    strategy=scope.strategy,
                    processing_pages=list(scope.processing_pages),
                    excluded_toc_pages=list(scope.excluded_toc_pages),
                )
                for scope in coarse_scopes
            ],
        },
    )

    page_features = anatomy.page_features if anatomy else []
    page_labels = anatomy.page_labels if anatomy else []
    vlm_model = os.environ.get("IMAGE_MODEL")

    processing_pages = _union_scope_processing_pages(coarse_scopes)
    rendered_by_page = _render_document_pages(
        pdf_path=pdf_path,
        output_dir=output_dir,
        page_count=page_count,
        processing_pages=processing_pages,
        page_texts=page_texts,
        page_features=page_features,
        trace_recorder=trace_recorder,
    )

    import gevent

    tag_job = gevent.spawn(
        _tag_document_pages,
        page_count=page_count,
        rendered_by_page=rendered_by_page,
        page_features=page_features,
        page_labels=page_labels,
        vlm_model=vlm_model,
        toc_policy=toc_policy,
        page_memory_config=page_memory_config,
        trace_recorder=trace_recorder,
    )
    asset_job = None
    if asset_extraction_enabled:
        asset_job = gevent.spawn(
            _extract_document_assets,
            pdf_path=pdf_path,
            output_dir=output_dir,
            rendered_by_page=rendered_by_page,
            page_features=page_features,
            page_count=page_count,
            page_memory_config=page_memory_config,
            trace_recorder=trace_recorder,
        )

    tags_by_page = tag_job.get()

    scope_concurrency = page_memory_config.scope_concurrency
    logger.info(
        "[page_memory] refining {} hierarchy scopes (scope_concurrency={})",
        len(coarse_scopes),
        scope_concurrency,
    )
    scope_results: list[_ScopeRunResult] = []

    _scope_kwargs = dict(
        output_dir=output_dir,
        page_count=page_count,
        rendered_by_page=rendered_by_page,
        tags_by_page=tags_by_page,
        trace_recorder=trace_recorder,
        page_memory_config=page_memory_config,
        next_title_by_path=next_title_by_path,
        toc_policy=toc_policy,
    )

    if scope_concurrency <= 1 or len(coarse_scopes) <= 1:
        for index, scope in enumerate(coarse_scopes, start=1):
            result = _run_scope_with_retry(
                scope=scope,
                scope_index=index,
                scope_count=len(coarse_scopes),
                **_scope_kwargs,
            )
            scope_results.append(result)
    else:
        from gevent.pool import Pool as GeventPool

        pool = GeventPool(
            size=min(scope_concurrency, len(coarse_scopes)),
        )
        greenlets = [
            pool.spawn(
                _run_scope_with_retry,
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
    tags = [tags_by_page[page] for page in sorted(tags_by_page)]
    tags = _merge_static_toc_tags(tags, toc_policy)
    page_assets_by_page = asset_job.get() if asset_job is not None else {}
    rendered_map = dict(rendered_by_page)

    nav_skeletons = _append_toc_nav_skeletons(
        body_skeletons=skeletons,
        anatomy=anatomy,
        filename=filename,
    )
    _write_top_level_artifacts(
        output_dir=output_dir,
        hierarchy=nav_skeletons,
        tags=tags,
        assets_by_page=page_assets_by_page,
        tagging_mode=page_memory_config.tagging_mode,
    )

    # ── C7: assemble node-granularity chunks ─────────────────────────
    tag_map = {t.page_index: t for t in tags}
    render_map = rendered_map

    # Shared per-page lookups for node-granularity assembly.
    raw_text_by_page: dict[int, str] = {}
    image_path_by_page: dict[int, str] = {}
    final_pages_scope = toc_policy.filter_processing_pages(
        _derive_hierarchy_page_scope(
            skeletons=skeletons,
            page_count=page_count,
        )
    )
    for page in final_pages_scope:
        rend = render_map.get(page)
        tag = tag_map.get(page)
        raw_text_by_page[page] = _resolve_assembly_page_text(
            rendered=rend,
            tag=tag,
            fallback_text=page_texts.get(page, ""),
        )
        if rend and rend.image_path and os.path.exists(rend.image_path):
            image_path_by_page[page] = rend.image_path

    from app.services.page_memory.node_assembler import (
        build_node_chunks,
        build_toc_node_chunks,
        merge_chunks_by_first_page,
    )

    with stage_timer("page_memory.node_assembly", page_count=len(final_pages_scope)):
        body_chunks = build_node_chunks(
            skeletons=skeletons,
            raw_text_by_page=raw_text_by_page,
            image_path_by_page=image_path_by_page,
            tag_by_page=tag_map,
            filename=filename,
            vlm_model=vlm_model,
            page_assets_by_page=page_assets_by_page,
            node_assembly_concurrency=page_memory_config.node_assembly_concurrency,
            body_start_by_page=toc_policy.body_start_by_page(),
        )
        toc_chunks = build_toc_node_chunks(
            anatomy=anatomy,
            filename=filename,
        )
        chunks = merge_chunks_by_first_page(toc_chunks, body_chunks)
    logger.info(
        "[page_memory] C7 assembled {} canonical chunks (verdict={})",
        len(chunks), verdict,
    )
    _record_trace_stage(
        trace_recorder,
        "C7.node_assembly",
        page_info=page_scope_info(final_pages_scope),
        variables={
            "chunk_count": len(chunks),
            "scope_count": len(scope_results),
            "page_to_node": _page_to_node_map(chunks),
        },
    )
    return chunks


def _build_page_ctx(
    *,
    pdf_path: str,
    job_id: str,
    output_dir: str,
    page_count: int,
    trace_recorder: Any | None = None,
) -> ToolContext:
    """Construct a ToolContext for C4 sub-agent and C3 tagger VLM calls."""
    blackboard = AgentBlackboard()
    blackboard.page_count = page_count
    vlm_model = os.environ.get("IMAGE_MODEL")
    reason_model = (
        os.environ.get("PAGE_LOCATE_REASON_MODEL")
        or os.environ.get("NORMOL_MODEL")
    )
    return ToolContext(
        pdf_path=pdf_path,
        job_id=job_id,
        blackboard=blackboard,
        budget=None,
        trace=trace_recorder,
        output_dir=output_dir,
        settings={
            "vlm_model": vlm_model,
            "model": reason_model,
        },
    )


def _build_hierarchy_scopes(
    *,
    skeletons: list[Any],
    filename: str,
    page_count: int,
    processing_pages: list[int] | None = None,
    excluded_toc_pages: list[int] | None = None,
) -> list[_HierarchyScope]:
    return build_hierarchy_scopes(
        skeletons=skeletons,
        filename=filename,
        page_count=page_count,
        processing_pages=processing_pages,
        excluded_toc_pages=excluded_toc_pages,
    )


def _union_scope_processing_pages(scopes: list[_HierarchyScope]) -> list[int]:
    """Deduplicate processing pages across coarse scopes (stable ascending)."""
    pages: set[int] = set()
    for scope in scopes:
        pages.update(int(page) for page in scope.processing_pages)
    return sorted(pages)


def _resolve_assembly_page_text(
    *,
    rendered: Any | None,
    tag: Any | None,
    fallback_text: str,
) -> str:
    """Prefer extracted text, then transient tagging OCR, without repeating OCR."""
    return str(
        (getattr(rendered, "raw_text", "") if rendered is not None else "")
        or getattr(tag, "resolved_body_text", None)
        or fallback_text
        or ""
    )


def _render_document_pages(
    *,
    pdf_path: str,
    output_dir: str,
    page_count: int,
    processing_pages: list[int],
    page_texts: dict[int, str],
    page_features: list[Any],
    trace_recorder: Any | None = None,
) -> dict[int, Any]:
    """Render each processing page once at document level (C1)."""
    from app.services.page_memory.page_renderer import render_document_pages

    pages = [int(page) for page in processing_pages]
    if not pages:
        return {}

    logger.info("[page_memory] document render stage: {} pages", len(pages))
    with stage_timer("page_memory.render", page_count=len(pages)):
        rendered = render_document_pages(
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=output_dir,
            pages=pages,
            page_features=page_features,
            page_texts=page_texts,
        )
    _record_trace_stage(
        trace_recorder,
        "C1.render_pages",
        page_info=page_scope_info([item.page_index for item in rendered]),
        variables={"rendered_count": len(rendered)},
    )
    return {item.page_index: item for item in rendered}


def _tag_document_pages(
    *,
    page_count: int,
    rendered_by_page: dict[int, Any],
    page_features: list[Any],
    page_labels: list[Any],
    vlm_model: str | None,
    toc_policy: TocPagePolicy,
    page_memory_config: PageMemoryConfig,
    trace_recorder: Any | None = None,
) -> dict[int, Any]:
    """Tag rendered pages once at document level (C3)."""
    from app.services.page_memory.page_plan import derive_page_processing_plan
    from app.services.page_memory.page_tagger import tag_pages

    if not rendered_by_page:
        return {}

    pages = sorted(rendered_by_page)
    rendered = [rendered_by_page[page] for page in pages]
    plans = [
        plan
        for plan in derive_page_processing_plan(
            page_count=page_count,
            page_labels=page_labels,
            page_features=page_features,
        )
        if plan.page_index in set(pages)
    ]
    _record_trace_stage(
        trace_recorder,
        "C2.page_plan",
        page_info=page_scope_info([plan.page_index for plan in plans]),
        variables={"plan_count": len(plans)},
    )

    logger.info(
        "[page_memory] document tag stage: {} pages (tag_concurrency={})",
        len(rendered),
        page_memory_config.tag_concurrency,
    )
    with stage_timer("page_memory.tag", page_count=len(rendered)):
        branch_stats: dict[str, Any] = {}
        tags = tag_pages(
            pages=rendered,
            plans=plans,
            budget=None,
            vlm_model=vlm_model,
            max_concurrent=page_memory_config.tag_concurrency,
            body_start_by_page=toc_policy.body_start_by_page(),
            scan_direction=page_memory_config.scan_direction,
            tagging_mode=page_memory_config.tagging_mode,
            text_summary_concurrency=page_memory_config.text_summary_concurrency,
            text_summary_model=page_memory_config.text_summary_model,
            branch_stats=branch_stats,
        )
    _record_trace_stage(
        trace_recorder,
        "C3.page_tagger",
        page_info=page_scope_info([tag.page_index for tag in tags]),
        variables={"tags": _summarize_tags(tags), "branch_stats": branch_stats},
    )
    return {tag.page_index: tag for tag in tags}


def _extract_document_assets(
    *,
    pdf_path: str,
    output_dir: str,
    rendered_by_page: dict[int, Any],
    page_features: list[Any],
    page_count: int,
    page_memory_config: PageMemoryConfig,
    trace_recorder: Any | None = None,
) -> dict[int, list[Any]]:
    """Extract assets once for the document-level unique render set (C5)."""
    from app.services.page_memory.page_assets import extract_page_assets_from_renders

    if not page_memory_config.asset_extraction_enabled or not rendered_by_page:
        return {}

    asset_max_pages = _resolve_asset_max_pages(page_count, page_memory_config)
    if asset_max_pages <= 0:
        return {}

    rendered = [rendered_by_page[page] for page in sorted(rendered_by_page)]
    asset_rendered = _select_rendered_pages_with_assets(rendered, page_features)
    logger.info(
        "[page_memory] document asset stage: {}/{} rendered pages have has_asset "
        "(asset_max_pages={})",
        len(asset_rendered),
        len(rendered),
        asset_max_pages,
    )
    with stage_timer("page_memory.assets", page_count=min(len(asset_rendered), asset_max_pages)):
        assets_by_page = extract_page_assets_from_renders(
            pdf_path=pdf_path,
            rendered_pages=asset_rendered,
            output_dir=output_dir,
            model_name=page_memory_config.asset_model,
            budget=None,
            max_pages=asset_max_pages,
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
            "asset_count": sum(len(items) for items in assets_by_page.values()),
            "assets_by_page": {
                page: [asset.asset_id for asset in assets]
                for page, assets in assets_by_page.items()
            },
        },
    )
    return assets_by_page


def _render_and_tag_document_pages(
    *,
    pdf_path: str,
    output_dir: str,
    page_count: int,
    processing_pages: list[int],
    page_texts: dict[int, str],
    page_features: list[Any],
    page_labels: list[Any],
    vlm_model: str | None,
    toc_policy: TocPagePolicy,
    page_memory_config: PageMemoryConfig,
    trace_recorder: Any | None = None,
) -> tuple[dict[int, Any], dict[int, Any]]:
    """Render and combined-tag each processing page once at document level."""
    rendered_by_page = _render_document_pages(
        pdf_path=pdf_path,
        output_dir=output_dir,
        page_count=page_count,
        processing_pages=processing_pages,
        page_texts=page_texts,
        page_features=page_features,
        trace_recorder=trace_recorder,
    )
    tags_by_page = _tag_document_pages(
        page_count=page_count,
        rendered_by_page=rendered_by_page,
        page_features=page_features,
        page_labels=page_labels,
        vlm_model=vlm_model,
        toc_policy=toc_policy,
        page_memory_config=page_memory_config,
        trace_recorder=trace_recorder,
    )
    return rendered_by_page, tags_by_page


_SCOPE_RETRY_DELAY_SECONDS = 10.0


def _run_scope_with_retry(
    **kwargs: Any,
) -> _ScopeRunResult:
    """Execute a single scope, retrying once on transient LLM failures."""
    import gevent

    from shared.core.exceptions.domain_exceptions import LLMServiceException

    scope: _HierarchyScope = kwargs["scope"]
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
    scope: _HierarchyScope,
    scope_index: int,
    scope_count: int,
    output_dir: str,
    page_count: int,
    rendered_by_page: dict[int, Any],
    tags_by_page: dict[int, Any],
    trace_recorder: Any | None,
    page_memory_config: PageMemoryConfig,
    next_title_by_path: dict[str, str | None] | None = None,
    toc_policy: TocPagePolicy | None = None,
) -> _ScopeRunResult:
    from app.services.page_memory.fine_hierarchy import (
        compute_fat_leaf_pages,
        refine_fat_leaf_skeletons,
    )

    policy = toc_policy or TocPagePolicy(frozenset(), {}, ())
    scope_skeletons = sort_skeletons(scope.skeletons)
    processing_pages = [int(page) for page in scope.processing_pages]
    scope_manifest = _scope_manifest(
        scope_id=scope.scope_id,
        skeletons=scope_skeletons,
        page_count=page_count,
        strategy=scope.strategy,
        processing_pages=processing_pages,
        excluded_toc_pages=list(scope.excluded_toc_pages),
    )
    logger.info(
        "[page_memory] scope {}/{} {} coarse ranges={} excluded_toc={}",
        scope_index,
        scope_count,
        scope.scope_id,
        collapse_page_ranges(processing_pages),
        collapse_page_ranges(list(scope.excluded_toc_pages)),
    )
    _record_trace_stage(
        trace_recorder,
        "C4.coarse_scope",
        page_info=page_scope_info(processing_pages),
        variables={
            "scope_index": scope_index,
            "scope_count": scope_count,
            "scope": scope_manifest,
        },
    )

    if not processing_pages:
        logger.info(
            "[page_memory] scope {} has no processing pages after TOC exclusion; skip",
            scope.scope_id,
        )
        return _ScopeRunResult(
            scope_id=scope.scope_id,
            hierarchy=scope_skeletons,
            tags=[],
            rendered=[],
            final_pages=[],
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
        scope_skeletons,
        min_pages=fine_min,
        exclude_pages=policy.pure_toc_pages,
    )
    if fat_leaf_pages:
        with stage_timer("page_memory.fine_hierarchy", page_count=len(fat_leaf_pages)):
            scope_skeletons = refine_fat_leaf_skeletons(
                coarse_skeletons=scope_skeletons,
                tag_results=tags,
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
        processing_pages=processing_pages,
        excluded_toc_pages=list(scope.excluded_toc_pages),
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
    _write_scope_artifacts(
        output_dir=output_dir,
        scope_id=scope.scope_id,
        scope_manifest_data=scope_manifest,
        hierarchy=scope_skeletons,
        tags=tags,
        tagging_mode=page_memory_config.tagging_mode,
    )

    return _ScopeRunResult(
        scope_id=scope.scope_id,
        hierarchy=scope_skeletons,
        tags=tags,
        rendered=rendered,
        final_pages=processing_pages,
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


def _project_assets_for_pages(
    assets_by_page: dict[int, list[Any]],
    pages: set[int],
) -> dict[int, list[Any]]:
    """Project document assets onto a page set without re-extracting."""
    from app.services.page_memory.page_assets import asset_source_pages

    if not pages or not assets_by_page:
        return {}
    projected: dict[int, list[Any]] = {}
    seen_ids: set[str] = set()
    for page_index in sorted(assets_by_page):
        for asset in assets_by_page[page_index]:
            asset_id = str(getattr(asset, "asset_id", "") or "")
            if asset_id and asset_id in seen_ids:
                continue
            source_pages = set(asset_source_pages(asset))
            if not source_pages.intersection(pages):
                continue
            if asset_id:
                seen_ids.add(asset_id)
            projected.setdefault(int(page_index), []).append(asset)
    return projected


def _merge_assets_by_page(asset_groups: Any) -> dict[int, list[Any]]:
    """Merge asset groups with ``asset_id`` dedupe (legacy scope fallback)."""
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


def _page_to_node_map(chunks: list[ChunkPayload]) -> dict[int, str]:
    """Map each owned physical page to its owner node path."""
    mapping: dict[int, str] = {}
    for chunk in chunks:
        if chunk["type"] != "page":
            continue
        path = chunk["path"]
        owned_pages = chunk["metadata"].get("owned_page_nums")
        pages = (
            owned_pages
            if isinstance(owned_pages, list)
            else chunk["metadata"].get("page_nums", [])
        )
        for page in pages:
            mapping[int(page)] = path
    return mapping


def _build_whole_doc_chunks(
    *,
    pdf_path: str,
    filename: str,
    page_count: int,
    verdict: str,
    trace_recorder: Any | None = None,
) -> list[ChunkPayload]:
    pages = list(range(1, page_count + 1)) if page_count > 0 else [1]
    page_texts = read_page_texts(pdf_path, pages)
    raw_text = "\n\n".join(page_texts.get(page, "") for page in pages).strip()
    summary = _build_summary(filename=filename, page_count=page_count, raw_text=raw_text)
    content = f"[SUMMARY]\n{summary}\n\n[RAW]\n{raw_text}".strip()
    chunk_id = gen_str_codes(f"wholedoc::{filename}::{content}")
    metadata: ChunkMetadata = {
        "length": len(content),
        "keywords": [],
        "summary": summary,
        "connect_to": [],
        "page_nums": pages,
        "owned_page_nums": pages,
    }
    chunk: ChunkPayload = {
        "chunk_id": chunk_id,
        "type": "page",
        "content": content,
        "path": f"{filename}/Root",
        "metadata": metadata,
        "order": 0,
    }
    _record_trace_stage(
        trace_recorder,
        "whole_doc",
        page_info=page_scope_info(pages),
        variables={"summary": summary, "verdict": verdict},
    )
    return [chunk]


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
    for name in ("asset_annotate", "debug", "images", "pages", "scopes", "tables"):
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


def _merge_static_toc_tags(tags: list[Any], toc_policy: TocPagePolicy) -> list[Any]:
    from app.services.page_memory.page_tagger import PageTagResult

    by_page = {
        int(getattr(tag, "page_index", 0) or 0): tag
        for tag in tags
        if int(getattr(tag, "page_index", 0) or 0) > 0
    }
    for page in sorted(toc_policy.pure_toc_pages):
        by_page[page] = PageTagResult(
            page_index=page,
            summary="Table of Contents",
            keywords=[],
            entities=[],
            strategy_used="toc_static",
        )
    return [by_page[page] for page in sorted(by_page)]


def _append_toc_nav_skeletons(
    *,
    body_skeletons: list[Any],
    anatomy: Any | None,
    filename: str,
) -> list[Any]:
    from app.services.page_memory.node_assembler import build_toc_nav_skeletons

    toc_skeletons = build_toc_nav_skeletons(anatomy=anatomy, filename=filename)
    if not toc_skeletons:
        return body_skeletons
    return sort_skeletons([*toc_skeletons, *body_skeletons])


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
