from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.visual import (
    purge_debug_visual_dirs,
    visual_debug_enabled,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import PARSER_ROW_COLUMNS
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.page_memory.normalizer import normalize_to_pdf
from app.services.page_memory._utils import (
    collapse_page_ranges,
    page_scope_info,
    scope_id_for_pages,
    sort_skeletons,
)
from app.services.page_memory._serialization import (
    derive_hierarchy_page_scope as _derive_hierarchy_page_scope,
    scope_manifest as _scope_manifest,
    write_scope_artifacts as _write_scope_artifacts,
    write_top_level_artifacts as _write_top_level_artifacts,
)

from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import ValidationException


@dataclass(frozen=True)
class PageMemoryInput:
    file_path: str
    filename: str
    output_dir: str
    job_id: str | None = None
    internal_output_filename: str | None = None
    base_url: str = ""


@dataclass(frozen=True)
class _HierarchyScope:
    scope_id: str
    skeletons: list[Any]
    strategy: str
    start_page: int
    end_page: int


@dataclass(frozen=True)
class _ScopeRunResult:
    scope_id: str
    hierarchy: list[Any]
    tags: list[Any]
    assets_by_page: dict[int, list[Any]]
    rendered: list[Any]
    final_pages: list[int]


def run(request: PageMemoryInput) -> tuple[str, pd.DataFrame]:
    """Run the page-memory track.

    Supports two granularity verdicts:
    - ``whole_doc`` (≤6 pages, no TOC) → single whole-document chunk
    - ``page`` → per-page chunks via the full C1-C7 pipeline
    """
    full_output_dir = _resolve_output_dir(request)
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
        if page_count > settings.PAGE_MEMORY_MAX_PAGES:
            raise ValidationException(
                user_message=(
                    f"Document too large: {page_count} pages exceeds the "
                    f"{settings.PAGE_MEMORY_MAX_PAGES}-page limit for page-based processing."
                ),
                violations=[{
                    "field": "page_count",
                    "description": f"PDF has {page_count} pages, limit is {settings.PAGE_MEMORY_MAX_PAGES}",
                }],
            )
        verdict = _decide_granularity(profile)
        trace_summary = {
            "page_count": page_count,
            "verdict": verdict,
        }

        if verdict == "whole_doc":
            parsed_df = _build_whole_doc_dataframe(
                pdf_path=pdf_path,
                filename=request.filename,
                page_count=page_count,
                verdict=verdict,
                trace_recorder=trace_recorder,
            )
        else:
            parsed_df = _build_page_dataframe(
                pdf_path=pdf_path,
                filename=request.filename,
                output_dir=full_output_dir,
                profile=profile,
                verdict=verdict,
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


# ── page builder (C1→C2→C3→C4→C6→C7) ────────────────────────────────


def _build_page_dataframe(
    *,
    pdf_path: str,
    filename: str,
    output_dir: str,
    profile: Any,
    verdict: str,
    trace_recorder: Any | None = None,
) -> pd.DataFrame:
    """Build per-page DataFrame via the full C1-C7 pipeline.

    Steps:
      C4  skeleton_extractor  → SectionSkeleton[]
      C1  page_renderer       → PageRenderResult[]
      C2  page_plan           → PagePlan[]
      C3  page_tagger         → PageTagResult[]
      C3b title detection     → observed_titles
      C4b fine_hierarchy      → refined SectionSkeleton[]
      C5  page_assets          → assets anchored to refined hierarchy pages
      C7  assemble node-granularity DataFrame
    """
    from app.services.page_memory.skeleton_extractor import (
        SectionSkeleton,
        collapse_single_child_chains,
        extract_section_skeletons,
    )
    from app.services.page_memory.page_assets import (
        get_asset_max_pages,
        page_asset_extraction_enabled,
    )

    anatomy = getattr(profile, "anatomy", None)
    page_count = max(int(profile.page_count or 0), 0)
    if page_count <= 0:
        return pd.DataFrame(columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))

    asset_extraction_enabled = page_asset_extraction_enabled()

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
                section_path=f"{filename}/Root",
                level=1,
                start_page=1,
                end_page=page_count,
                title="Root",
                parent_path=filename,
                evidence={"source": "fallback_root", "reason": "no_hierarchy"},
            )
        ]

    coarse_scopes = _build_hierarchy_scopes(
        skeletons=skeletons,
        filename=filename,
        page_count=page_count,
    )
    coarse_pages_scope = _derive_hierarchy_page_scope(
        skeletons=skeletons,
        page_count=page_count,
    )
    _record_trace_stage(
        trace_recorder,
        "C4.skeleton",
        page_info={
            "document_page_count": page_count,
            "coarse_scope": page_scope_info(coarse_pages_scope),
        },
        variables={
            "sections": _summarize_skeletons(skeletons),
            "scope_count": len(coarse_scopes),
            "scopes": [
                _scope_manifest(
                    scope_id=scope.scope_id,
                    skeletons=scope.skeletons,
                    page_count=page_count,
                    strategy=scope.strategy,
                )
                for scope in coarse_scopes
            ],
        },
    )

    page_features = anatomy.page_features if anatomy else []
    page_labels = anatomy.page_labels if anatomy else []
    vlm_model = os.environ.get("IMAGE_MODEL")

    scope_concurrency = int(
        os.environ.get("PAGE_MEMORY_SCOPE_CONCURRENCY", "4")
    )
    logger.info(
        "[page_memory] processing {} hierarchy scopes (concurrency={})",
        len(coarse_scopes),
        scope_concurrency,
    )
    scope_results: list[_ScopeRunResult] = []
    asset_pages_remaining = (
        get_asset_max_pages(page_count) if asset_extraction_enabled else 0
    )

    # Common keyword arguments shared by every scope invocation.
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
    )

    if scope_concurrency <= 1 or len(coarse_scopes) <= 1:
        # ── serial path (identical to previous behaviour) ─────────────
        for index, scope in enumerate(coarse_scopes, start=1):
            result = _run_scope_with_retry(
                scope=scope,
                scope_index=index,
                scope_count=len(coarse_scopes),
                asset_max_pages=asset_pages_remaining,
                **_scope_kwargs,
            )
            scope_results.append(result)
            if asset_extraction_enabled:
                asset_pages_remaining = max(
                    asset_pages_remaining - len(result.final_pages),
                    0,
                )
    else:
        # ── concurrent path (GeventPool) ──────────────────────────────
        import gevent
        from gevent.pool import Pool as GeventPool

        asset_allocations = _allocate_asset_pages(
            coarse_scopes, asset_pages_remaining,
        )
        pool = GeventPool(
            size=min(scope_concurrency, len(coarse_scopes)),
        )
        greenlets = [
            pool.spawn(
                _run_scope_with_retry,
                scope=scope,
                scope_index=idx,
                scope_count=len(coarse_scopes),
                asset_max_pages=asset_allocations[idx - 1],
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
    for result in scope_results:
        for rendered_page in result.rendered:
            rendered_map.setdefault(rendered_page.page_index, rendered_page)

    _write_top_level_artifacts(
        output_dir=output_dir,
        hierarchy=skeletons,
        tags=tags,
        assets_by_page=page_assets_by_page,
    )

    # ── C7: assemble DataFrame rows ──────────────────────────────────
    tag_map = {t.page_index: t for t in tags}
    render_map = rendered_map

    # Build page → PageLabel.kind lookup
    label_map: dict[int, str] = {}
    if page_labels:
        for lbl in page_labels:
            label_map[lbl.page] = lbl.kind

    # Shared per-page lookups for node-granularity assembly.
    raw_text_by_page: dict[int, str] = {}
    image_path_by_page: dict[int, str] = {}
    final_pages_scope = _derive_hierarchy_page_scope(
        skeletons=skeletons,
        page_count=page_count,
    )
    for page in final_pages_scope:
        rend = render_map.get(page)
        raw_text_by_page[page] = (rend.raw_text if rend else page_texts.get(page, "")) or ""
        if rend and rend.image_path and os.path.exists(rend.image_path):
            image_path_by_page[page] = rend.image_path

    from app.services.page_memory.node_assembler import build_node_rows

    with stage_timer("page_memory.node_assembly", page_count=len(final_pages_scope)):
        rows = build_node_rows(
            skeletons=skeletons,
            raw_text_by_page=raw_text_by_page,
            image_path_by_page=image_path_by_page,
            kind_by_page=label_map,
            tag_by_page=tag_map,
            filename=filename,
            verdict=verdict,
            budget=None,
            vlm_model=vlm_model,
            page_assets_by_page=page_assets_by_page,
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
    return pd.DataFrame(rows, columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))


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
) -> list[_HierarchyScope]:
    if not skeletons:
        return []

    root_fallback = all(
        getattr(item, "title", "") == "Root"
        or (getattr(item, "evidence", {}) or {}).get("source") == "fallback_root"
        for item in skeletons
    )
    if root_fallback:
        ordered = sort_skeletons(skeletons)
        return [
            _HierarchyScope(
                scope_id=scope_id_for_pages(1, page_count),
                skeletons=ordered,
                strategy="fallback_root",
                start_page=1,
                end_page=page_count,
            )
        ]

    min_level = min(int(getattr(item, "level", 0) or 0) for item in skeletons)
    top_nodes = [
        item
        for item in skeletons
        if int(getattr(item, "level", 0) or 0) == min_level
        or not str(getattr(item, "parent_path", "") or "").startswith(f"{filename}/")
    ]
    seen_top_paths: set[str] = set()
    unique_top_nodes: list[Any] = []
    for item in sort_skeletons(top_nodes):
        if item.section_path in seen_top_paths:
            continue
        seen_top_paths.add(item.section_path)
        unique_top_nodes.append(item)

    scopes: list[_HierarchyScope] = []
    for index, top in enumerate(unique_top_nodes):
        prefix = f"{top.section_path}/"
        members = [
            item
            for item in skeletons
            if item.section_path == top.section_path
            or str(item.section_path).startswith(prefix)
        ]
        if not members:
            continue
        start_page = max(1, int(getattr(top, "start_page", 1) or 1))
        next_top_start = (
            int(getattr(unique_top_nodes[index + 1], "start_page", page_count + 1) or page_count + 1)
            if index + 1 < len(unique_top_nodes)
            else page_count + 1
        )
        end_page = min(
            page_count,
            max(
                start_page,
                min(
                    int(getattr(top, "end_page", page_count) or page_count),
                    next_top_start - 1,
                ),
            ),
        )
        bounded_members = [
            replace(
                item,
                start_page=max(
                    start_page,
                    int(getattr(item, "start_page", start_page) or start_page),
                ),
                end_page=min(
                    end_page,
                    int(getattr(item, "end_page", end_page) or end_page),
                ),
            )
            for item in members
            if int(getattr(item, "start_page", 1) or 1) <= end_page
            and int(getattr(item, "end_page", end_page) or end_page) >= start_page
        ]
        bounded_members = sort_skeletons(bounded_members)
        scopes.append(
            _HierarchyScope(
                scope_id=scope_id_for_pages(start_page, end_page),
                skeletons=bounded_members,
                strategy=f"coarse_scope_{index + 1}",
                start_page=start_page,
                end_page=end_page,
            )
        )
    if scopes:
        return scopes

    ordered = sort_skeletons(skeletons)
    pages = _derive_hierarchy_page_scope(skeletons=ordered, page_count=page_count)
    return [
        _HierarchyScope(
            scope_id=scope_id_for_pages(
                pages[0] if pages else 1,
                pages[-1] if pages else page_count,
            ),
            skeletons=ordered,
            strategy="full_coarse_hierarchy",
            start_page=pages[0] if pages else 1,
            end_page=pages[-1] if pages else page_count,
        )
    ]


def _allocate_asset_pages(
    scopes: list[_HierarchyScope], total_budget: int,
) -> list[int]:
    """Pre-allocate asset page budget proportionally to avoid concurrency races."""
    if total_budget <= 0 or not scopes:
        return [0] * len(scopes)
    page_counts = [max(s.end_page - s.start_page + 1, 1) for s in scopes]
    total_pages = sum(page_counts)
    allocations: list[int] = []
    remaining = total_budget
    for i, count in enumerate(page_counts):
        if i == len(page_counts) - 1:
            allocations.append(remaining)
        else:
            share = min(int(total_budget * count / total_pages), remaining)
            allocations.append(share)
            remaining -= share
    return allocations


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
    pdf_path: str,
    filename: str,
    output_dir: str,
    page_count: int,
    page_texts: dict[int, str],
    page_features: list[Any],
    page_labels: list[Any],
    vlm_model: str | None,
    asset_extraction_enabled: bool,
    asset_max_pages: int,
    trace_recorder: Any | None,
) -> _ScopeRunResult:
    from app.services.page_memory.fine_hierarchy import (
        compute_fat_leaf_pages,
        refine_fat_leaf_skeletons,
    )
    from app.services.page_memory.page_assets import (
        extract_page_assets_from_renders,
        get_asset_confidence_threshold,
        get_asset_model,
    )
    from app.services.page_memory.page_plan import derive_page_processing_plan
    from app.services.page_memory.page_renderer import render_document_pages
    from app.services.page_memory.page_tagger import (
        PageTagResult,
        get_fine_min_pages,
        tag_page_titles,
        tag_pages,
    )

    scope_skeletons = sort_skeletons(scope.skeletons)
    scope_manifest = _scope_manifest(
        scope_id=scope.scope_id,
        skeletons=scope_skeletons,
        page_count=page_count,
        strategy=scope.strategy,
    )
    coarse_pages = _derive_hierarchy_page_scope(
        skeletons=scope_skeletons,
        page_count=page_count,
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

    fine_min = get_fine_min_pages()
    fat_leaf_pages = compute_fat_leaf_pages(scope_skeletons, min_pages=fine_min)
    if fat_leaf_pages:
        title_pages = sorted(fat_leaf_pages)
        title_rendered = render_document_pages(
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=output_dir,
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
                budget=None,
                vlm_model=vlm_model,
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
                model_name=os.environ.get(
                    "PAGE_MEMORY_HIERARCHY_MODEL",
                    os.environ.get(
                        "HIERARCHY_LLM_MODEL",
                        os.environ.get("NORMOL_MODEL"),
                    ),
                ),
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

    final_pages = _derive_hierarchy_page_scope(
        skeletons=scope_skeletons,
        page_count=page_count,
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
            pages=final_pages,
            page_features=page_features,
            page_texts=page_texts,
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
            budget=None,
            vlm_model=vlm_model,
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
    if asset_extraction_enabled and asset_max_pages > 0:
        with stage_timer("page_memory.assets", page_count=asset_max_pages):
            assets_by_page = extract_page_assets_from_renders(
                pdf_path=pdf_path,
                rendered_pages=rendered,
                output_dir=output_dir,
                model_name=get_asset_model(),
                budget=None,
                max_pages=asset_max_pages,
                confidence_threshold=get_asset_confidence_threshold(),
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
    )


# ── whole_doc builder (PR3, unchanged) ────────────────────────────────


def _build_whole_doc_dataframe(
    *,
    pdf_path: str,
    filename: str,
    page_count: int,
    verdict: str,
    trace_recorder: Any | None = None,
) -> pd.DataFrame:
    pages = list(range(1, page_count + 1)) if page_count > 0 else [1]
    page_texts = read_page_texts(pdf_path, pages)
    raw_text = "\n\n".join(page_texts.get(page, "") for page in pages).strip()
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
        "extra_metadata": {},
    }
    _record_trace_stage(
        trace_recorder,
        "whole_doc",
        page_info=page_scope_info(pages),
        variables={"summary": summary, "verdict": verdict},
    )
    return pd.DataFrame([row], columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))


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
    legacy_files = {
        "assets.json",
        "chunks.json",
        "coarse_tag_scope.json",
        "doc_nav.json",
        "hierarchy.json",
        "manifest.json",
        "node_rows.csv",
        "node_rows.json",
        "page_memory_fine_hierarchy.json",
        "page_plans.json",
        "page_rendered.json",
        "page_tags.json",
        "page_tags_after_titles.json",
        "page_tags_pre_hierarchy.json",
        "report.md",
        "skeletons.json",
        "tag_scope.json",
        "trace.json",
    }
    for name in legacy_files:
        path = root / name
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            logger.debug("[page_memory] failed to cleanup artifact {}", path)
    for name in ("asset_annotate", "debug", "fine_hierarchy", "images", "pages", "scopes", "tables"):
        path = root / name
        try:
            if path.is_dir():
                shutil.rmtree(path)
        except Exception:
            logger.debug("[page_memory] failed to cleanup artifact dir {}", path)


def _remove_nested_doc_agent_trace(output_dir: str) -> None:
    try:
        (Path(output_dir) / "_doc_agent" / "trace.json").unlink()
    except FileNotFoundError:
        pass
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
