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

    Supports three granularity verdicts:
    - ``whole_doc`` (≤6 pages, no TOC) → single whole-document chunk
    - ``page`` / ``shard_page`` → per-page chunks via the full C1-C7 pipeline
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

    # page / shard_page → unified per-page pipeline
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
    page_count = int(getattr(profile, "page_count", 0) or 0)
    toc = getattr(profile, "toc", None)
    has_toc = bool(getattr(toc, "has_toc", False))
    if page_count > 200:
        return "shard_page"
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
      C6  page_section_mapper → PageSectionMapping[]
      C7  assemble DataFrame
    """
    from app.services.document_agent.budget import BudgetTracker, StageEnvelope
    from app.services.page_memory.page_plan import derive_page_processing_plan
    from app.services.page_memory.page_renderer import render_document_pages
    from app.services.page_memory.page_section_mapper import map_pages_to_sections
    from app.services.page_memory.page_tagger import tag_pages
    from app.services.page_memory.skeleton_extractor import extract_section_skeletons

    anatomy = getattr(profile, "anatomy", None)
    page_count = max(int(profile.page_count or 0), 0)
    if page_count <= 0:
        return pd.DataFrame(columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))

    # ── unified budget (page_locate + page_tagging share one tracker) ──
    page_tagging_budget = int(
        os.environ.get("PAGE_MEMORY_TAG_BUDGET", str(page_count * 1200))
    )
    page_locate_budget = int(
        os.environ.get("PAGE_MEMORY_LOCATE_BUDGET", str(min(page_count * 1600, 2_000_000)))
    )
    budget = BudgetTracker(
        plan_budget=0,
        visual_budget=page_tagging_budget + page_locate_budget,
        visual_stage_envelopes={
            "page_locate": StageEnvelope(
                min_guarantee=page_locate_budget,
                cap=None,
            ),
            "page_tagging": StageEnvelope(
                min_guarantee=page_tagging_budget,
                cap=None,
            ),
        },
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

    # ── C6: page → section mapping ───────────────────────────────────
    mappings = map_pages_to_sections(
        page_count=page_count,
        skeletons=skeletons,
        filename=filename,
    )

    # ── C7: assemble DataFrame rows ──────────────────────────────────
    tag_map = {t.page_index: t for t in tags}
    render_map = {r.page_index: r for r in rendered}

    # Build page → PageLabel.kind lookup
    label_map: dict[int, str] = {}
    if page_labels:
        for lbl in page_labels:
            label_map[lbl.page] = lbl.kind

    # Build page → observed_titles from C4 skeleton (primary sections)
    skeleton_titles: dict[int, list[str]] = {}
    for skel in skeletons:
        titles = skeleton_titles.setdefault(skel.start_page, [])
        if skel.title and skel.title not in titles:
            titles.append(skel.title)

    rows: list[dict[str, Any]] = []
    for mapping in mappings:
        page = mapping.page_index
        tag = tag_map.get(page)
        rend = render_map.get(page)

        raw_text = rend.raw_text if rend else page_texts.get(page, "")
        content = raw_text.strip()
        summary = tag.summary if tag else ""
        keywords_list = tag.keywords if tag else []
        keywords_str = ";".join(keywords_list)

        doc_hash = gen_str_codes(f"{filename}::{page}")
        know_id = f"page_{doc_hash}"

        image_uri = ""
        if rend and rend.image_path and os.path.exists(rend.image_path):
            image_uri = str(
                Path(rend.image_path).relative_to(output_dir)
            )

        rows.append({
            "content": content,
            "path": mapping.section_path,
            "type": "page",
            "length": len(content),
            "keywords": keywords_str,
            "summary": summary,
            "know_id": know_id,
            "tokens": "",
            "connectto": "",
            "addtime": get_str_time(),
            "page_nums": str(page),
            "extra_metadata": {
                "granularity": "page",
                "page_index": page,
                "page_image_uri": image_uri,
                "strategy_used": tag.strategy_used if tag else "",
                "kind": label_map.get(page, "normal"),
                "observed_titles": skeleton_titles.get(page, []),
                "section_roles": mapping.section_roles,
                "source_verdict": verdict,
            },
        })

    logger.info(
        "[page_memory] C7 assembled {} page rows (verdict={})",
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
            "granularity": "whole_doc",
            "strategy_used": "whole_doc" if verdict == "whole_doc" else "whole_doc_fallback",
            "source_verdict": verdict,
            "page_index": None,
            "page_image_uris": page_image_uris,
            "status": "clear",
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

