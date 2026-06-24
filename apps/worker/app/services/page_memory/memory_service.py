from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.visual import render_pages
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import PARSER_ROW_COLUMNS
from app.services.page_memory.normalizer import normalize_to_pdf

from loguru import logger


@dataclass(frozen=True)
class PageMemoryInput:
    file_path: str
    filename: str
    output_dir: str
    job_id: str | None = None
    internal_output_filename: str | None = None
    base_url: str = ""


def run(request: PageMemoryInput) -> tuple[str, pd.DataFrame]:
    """Run the page-memory track.

    Supports two granularity verdicts:
    - ``whole_doc`` (≤6 pages, no TOC) → single whole-document chunk
    - ``page`` → per-page chunks via the full C1-C7 pipeline
    """
    full_output_dir = _resolve_output_dir(request)
    os.makedirs(full_output_dir, exist_ok=True)
    pdf_path, pdf_filename = normalize_to_pdf(
        file_path=request.file_path,
        filename=request.filename,
        output_dir=full_output_dir,
        base_url=request.base_url,
    )
    profile = profile_document(
        pdf_path,
        pdf_filename,
        job_id=request.job_id,
        output_dir=full_output_dir,
        skip_shard_plan=True,
        oversized_policy="page_memory",
    )
    verdict = _decide_granularity(profile)

    if verdict == "whole_doc":
        return full_output_dir, _build_whole_doc_dataframe(
            pdf_path=pdf_path,
            filename=request.filename,
            output_dir=full_output_dir,
            page_count=max(int(profile.page_count or 0), 0),
            verdict=verdict,
        )

    # page → per-page pipeline
    return full_output_dir, _build_page_dataframe(
        pdf_path=pdf_path,
        filename=request.filename,
        output_dir=full_output_dir,
        profile=profile,
        verdict=verdict,
    )


def _resolve_output_dir(request: PageMemoryInput) -> str:
    output_name = request.internal_output_filename or request.filename
    return os.path.join(request.output_dir, Path(output_name).stem)


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
) -> pd.DataFrame:
    """Build per-page DataFrame via the full C1-C7 pipeline.

    Steps:
      C4  skeleton_extractor  → SectionSkeleton[]
      C1  page_renderer       → PageRenderResult[]
      C2  page_plan           → PagePlan[]
      C3  page_tagger         → PageTagResult[]
      C3b title detection     → observed_titles
      C4b fine_hierarchy      → refined SectionSkeleton[]
      C7  assemble node-granularity DataFrame
    """
    from app.services.document_agent.budget import BudgetTracker, StageEnvelope
    from app.services.page_memory.page_plan import derive_page_processing_plan
    from app.services.page_memory.page_renderer import render_document_pages
    from app.services.page_memory.page_tagger import tag_pages
    from app.services.page_memory.skeleton_extractor import (
        SectionSkeleton,
        extract_section_skeletons,
    )
    from app.services.page_memory.page_assets import (
        extract_page_assets_from_renders,
        get_asset_budget,
        get_asset_confidence_threshold,
        get_asset_max_pages,
        get_asset_model,
        page_asset_extraction_enabled,
    )

    anatomy = getattr(profile, "anatomy", None)
    page_count = max(int(profile.page_count or 0), 0)
    if page_count <= 0:
        return pd.DataFrame(columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))

    # ── unified budget (page_locate + page_tagging + title_detection) ──
    page_tagging_budget = int(
        os.environ.get("PAGE_MEMORY_TAG_BUDGET", str(page_count * 1200))
    )
    page_locate_budget = int(
        os.environ.get("PAGE_MEMORY_LOCATE_BUDGET", str(min(page_count * 1600, 2_000_000)))
    )
    title_detection_budget = int(
        os.environ.get("PAGE_MEMORY_TITLE_BUDGET", str(page_count * 800))
    )
    asset_extraction_enabled = page_asset_extraction_enabled()
    asset_extraction_budget = (
        get_asset_budget(page_count) if asset_extraction_enabled else 0
    )
    total_visual = (
        page_tagging_budget
        + page_locate_budget
        + title_detection_budget
        + asset_extraction_budget
    )

    # plan_budget powers the LLM decision loop inside PageLocateSubAgent.
    # Without it the sub-agent falls back to a deterministic path that
    # cannot rewrite queries (e.g. drop trailing doc-reference codes),
    # causing grep to miss titles whose TOC text differs from body text.
    plan_budget = int(
        os.environ.get("PAGE_MEMORY_PLAN_BUDGET", str(min(page_count * 800, 2_000_000)))
    )

    stage_envelopes = {
        "page_locate": StageEnvelope(
            min_guarantee=page_locate_budget,
            cap=None,
        ),
        "page_tagging": StageEnvelope(
            min_guarantee=page_tagging_budget,
            cap=None,
        ),
    }
    stage_envelopes["page_title_detection"] = StageEnvelope(
        min_guarantee=title_detection_budget,
        cap=None,
    )
    if asset_extraction_enabled:
        stage_envelopes["page_asset_extraction"] = StageEnvelope(
            min_guarantee=asset_extraction_budget,
            cap=None,
        )
    budget = BudgetTracker(
        plan_budget=plan_budget,
        visual_budget=total_visual,
        visual_stage_envelopes=stage_envelopes,
    )

    # ── build ToolContext for sub-agent VLM calls ─────────────────────
    ctx = _build_page_ctx(
        pdf_path=pdf_path,
        job_id=filename,
        output_dir=output_dir,
        page_count=page_count,
        budget=budget,
    )

    # ── C4: skeleton (from profile anatomy) ───────────────────────────
    page_texts = read_page_texts(
        pdf_path, list(range(1, page_count + 1)), timeout=300,
    )
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

    # ── C1: render pages ──────────────────────────────────────────────
    page_features = anatomy.page_features if anatomy else []
    rendered = render_document_pages(
        pdf_path=pdf_path,
        page_count=page_count,
        output_dir=output_dir,
        page_features=page_features,
        page_texts=page_texts,
    )

    page_assets_by_page: dict[int, list[Any]] = {}
    if asset_extraction_enabled:
        page_assets_by_page = extract_page_assets_from_renders(
            pdf_path=pdf_path,
            rendered_pages=rendered,
            output_dir=output_dir,
            model_name=get_asset_model(),
            budget=budget,
            max_pages=get_asset_max_pages(page_count),
            confidence_threshold=get_asset_confidence_threshold(),
        )

    # ── C2: page plan ─────────────────────────────────────────────────
    page_labels = anatomy.page_labels if anatomy else []
    plans = derive_page_processing_plan(
        page_count=page_count,
        page_labels=page_labels,
        page_features=page_features,
    )

    # ── C3: page tagger ──────────────────────────────────────────────
    vlm_model = os.environ.get("IMAGE_MODEL")
    tags = tag_pages(
        pages=rendered,
        plans=plans,
        budget=budget,
        vlm_model=vlm_model,
    )

    # ── C3b + C4b: page-native hierarchy refinement ─────────────────
    if skeletons:
        from app.services.page_memory.fine_hierarchy import (
            compute_fat_leaf_pages,
            refine_fat_leaf_skeletons,
        )
        from app.services.page_memory.page_tagger import (
            get_fine_min_pages,
            tag_page_titles,
        )

        fine_min = get_fine_min_pages()
        fat_leaf_pages = compute_fat_leaf_pages(skeletons, min_pages=fine_min)
        logger.info(
            "[page_memory] native hierarchy: {} fat-leaf pages (min={})",
            len(fat_leaf_pages), fine_min,
        )

        # Step 2: VLM title detection on fat-leaf pages
        if fat_leaf_pages:
            tags = tag_page_titles(
                pages=rendered,
                tag_results=tags,
                fat_leaf_pages=fat_leaf_pages,
                budget=budget,
                vlm_model=vlm_model,
            )

            # Step 3: Refine coarse skeletons with LLM hierarchy
            skeletons = refine_fat_leaf_skeletons(
                coarse_skeletons=skeletons,
                tag_results=tags,
                fat_leaf_pages=fat_leaf_pages,
                model_name=os.environ.get(
                    "PAGE_MEMORY_HIERARCHY_MODEL",
                    os.environ.get(
                        "HIERARCHY_LLM_MODEL",
                        os.environ.get("NORMOL_MODEL"),
                    ),
                ),
                output_dir=output_dir,
            )
            logger.info(
                "[page_memory] C4b refined: {} sections after fine hierarchy",
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
            )
        ]

    # ── C7: assemble DataFrame rows ──────────────────────────────────
    tag_map = {t.page_index: t for t in tags}
    render_map = {r.page_index: r for r in rendered}

    # Build page → PageLabel.kind lookup
    label_map: dict[int, str] = {}
    if page_labels:
        for lbl in page_labels:
            label_map[lbl.page] = lbl.kind

    # Shared per-page lookups for node-granularity assembly.
    raw_text_by_page: dict[int, str] = {}
    image_uri_by_page: dict[int, str] = {}
    image_path_by_page: dict[int, str] = {}
    for page in range(1, page_count + 1):
        rend = render_map.get(page)
        raw_text_by_page[page] = (rend.raw_text if rend else page_texts.get(page, "")) or ""
        if rend and rend.image_path and os.path.exists(rend.image_path):
            image_path_by_page[page] = rend.image_path
            image_uri_by_page[page] = str(
                Path(rend.image_path).relative_to(output_dir)
            )

    from app.services.page_memory.node_assembler import build_node_rows

    rows = build_node_rows(
        skeletons=skeletons,
        raw_text_by_page=raw_text_by_page,
        image_uri_by_page=image_uri_by_page,
        image_path_by_page=image_path_by_page,
        kind_by_page=label_map,
        tag_by_page=tag_map,
        filename=filename,
        verdict=verdict,
        budget=budget,
        vlm_model=vlm_model,
        page_assets_by_page=page_assets_by_page,
    )
    logger.info(
        "[page_memory] C7 assembled {} node rows (verdict={})",
        len(rows), verdict,
    )
    return pd.DataFrame(rows, columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))


def _build_page_ctx(
    *,
    pdf_path: str,
    job_id: str,
    output_dir: str,
    page_count: int,
    budget: Any,
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
        budget=budget,
        trace=None,
        output_dir=output_dir,
        settings={
            "vlm_model": vlm_model,
            "model": reason_model,
        },
    )

# ── whole_doc builder (PR3, unchanged) ────────────────────────────────


def _build_whole_doc_dataframe(
    *,
    pdf_path: str,
    filename: str,
    output_dir: str,
    page_count: int,
    verdict: str,
) -> pd.DataFrame:
    pages = list(range(1, page_count + 1)) if page_count > 0 else [1]
    page_texts = read_page_texts(pdf_path, pages)
    raw_text = "\n\n".join(page_texts.get(page, "") for page in pages).strip()
    summary = _build_summary(filename=filename, page_count=page_count, raw_text=raw_text)
    page_image_uris = _render_page_images(
        pdf_path=pdf_path,
        output_dir=output_dir,
        page_count=page_count,
        pages=pages,
    )
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
            "page_image_uris": page_image_uris,
        },
    }
    return pd.DataFrame([row], columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))


def _build_summary(*, filename: str, page_count: int, raw_text: str) -> str:
    prefix = f"{filename} whole-document memory ({page_count} pages)"
    preview = " ".join(raw_text.split())[:500]
    return f"{prefix}: {preview}" if preview else prefix


def _render_page_images(
    *,
    pdf_path: str,
    output_dir: str,
    page_count: int,
    pages: list[int],
) -> list[str]:
    if page_count <= 0:
        return []
    blackboard = AgentBlackboard()
    blackboard.page_count = page_count
    ctx = ToolContext(
        pdf_path=pdf_path,
        job_id="page_memory_render",
        blackboard=blackboard,
        budget=None,
        trace=None,
        output_dir=output_dir,
        settings={},
    )
    rendered = render_pages(ctx, pages, folder_name="pages", prefix="page", timeout=180)
    return [
        str(Path(item["png_path"]).relative_to(output_dir))
        for item in rendered
        if item.get("png_path")
    ]
