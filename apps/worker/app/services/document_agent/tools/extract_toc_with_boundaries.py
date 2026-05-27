"""VLM-driven TOC anchor, boundary, and entry extraction."""

from __future__ import annotations

import gc
import json
import os
import time
from pathlib import Path
from typing import Any, cast

from shared.utils.token_estimate import estimate_tokens

from app.services.document_agent.manifest import (
    TocAnchorPage,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import register_tool
from app.services.document_agent.tools.vlm_toc_extractor import (
    vlm_entries_to_toc_hierarchies,
    vlm_extract_toc_entries,
)
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# -- Constants -----------------------------------------------------------------

BOUNDARY_STEP_PAGES = 5
MAX_BOUNDARY_ROUNDS = 6
MAX_TOC_PAGES = BOUNDARY_STEP_PAGES * MAX_BOUNDARY_ROUNDS  # 30


# -- PyMuPDF workers (must be top-level for multiprocessing pickle) ------------


@worker
def _render_single_page_worker(
    queue, pdf_path: str, page_num: int, output_path: str, dpi: int
) -> None:
    import pymupdf  # type: ignore[import]

    try:
        doc = pymupdf.open(pdf_path)
        idx = page_num - 1
        if 0 <= idx < doc.page_count:
            page = doc[idx]
            mat = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
            pix = page.get_pixmap(matrix=mat)
            pix.save(output_path)
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "png_path": output_path})


# -- VLM helpers ---------------------------------------------------------------


def _vlm_confirm_anchors(
    anchor_pages: list[TocAnchorPage],
    model: str,
    budget: Any | None = None,
) -> tuple[list[TocAnchorPage], bool]:
    """Phase 1: send all anchor PNGs to VLM, ask which are real TOC starts."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    if not anchor_pages:
        return [], False

    import base64

    # Build multi-image message
    content_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "You are a document structure analysis expert. "
                "Below are screenshot(s) of candidate pages extracted from a PDF. "
                "These pages contained keywords such as 'Table of Contents' / 'Contents' "
                "during a text scan.\n\n"
                "For each page, determine whether it is truly the **start page** of a "
                "Table of Contents (TOC).\n\n"
                "Criteria for a real TOC page:\n"
                "- Contains a list of section titles paired with page numbers\n"
                "- Titles are connected to page numbers via dots, ellipses, or spaces\n"
                "- Titles have a systematic numbering scheme (e.g. 1. / 1.1 / Chapter 1)\n\n"
                "NOT a TOC page:\n"
                "- Body text that casually mentions 'contents'\n"
                "- A page with only a 'Contents' heading but body text below\n\n"
                "Return a strict JSON array (no markdown fences):\n"
                '[{"page": <page_number>, "is_toc_start": true/false, "reason": "brief reason"}]'
            ),
        }
    ]

    for anchor in anchor_pages:
        with open(anchor.png_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content_parts.append(
            {
                "type": "text",
                "text": f"\n--- Page {anchor.page} ---",
            }
        )
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )

    messages = cast(Any, [{"role": "user", "content": content_parts}])
    est = estimate_tokens(str(content_parts[0]["text"])) + len(anchor_pages) * 800
    if budget and not budget.try_reserve("visual", est):
        logger.warning("[extract.toc] insufficient visual budget for anchor confirmation")
        return [], True

    try:
        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=messages,
            model=model,
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        if budget:
            budget.commit("visual", actual=usage.get("total_tokens", est), est=est)
        data = json.loads(raw)
        if isinstance(data, dict):
            items = data.get("pages") or data.get("results") or data.get("data") or []
            if not items and len(data) == 1:
                items = list(data.values())[0]
        elif isinstance(data, list):
            items = data
        else:
            items = []

        confirmed_pages: set[int] = set()
        for item in items:
            if isinstance(item, dict) and item.get("is_toc_start"):
                confirmed_pages.add(int(item["page"]))

        confirmed = [a for a in anchor_pages if a.page in confirmed_pages]
        rejected = [a.page for a in anchor_pages if a.page not in confirmed_pages]
        logger.info(
            "[extract.toc] VLM confirmed {} TOC starts, rejected pages: {}",
            len(confirmed),
            rejected,
        )
        return confirmed, False
    except Exception as exc:
        if budget:
            budget.refund("visual", est=est)
        logger.warning(
            "[extract.toc] VLM anchor confirmation failed: {}, "
            "falling back to no confirmed anchors (safe degradation)",
            exc,
        )
        return [], True


def _vlm_check_boundary_page(
    png_path: str,
    page_num: int,
    model: str,
    budget: Any | None = None,
) -> tuple[bool, bool]:
    """Phase 2: check if a single page still contains TOC content."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    import base64

    with open(png_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    content_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "You are a document structure analysis expert. "
                "Below is a screenshot of one page from a PDF. "
                "I am determining the boundary of a Table of Contents (TOC) region.\n\n"
                "Does this page still contain TOC content?\n\n"
                "TOC content characteristics:\n"
                "- Entry titles paired with page numbers\n"
                "- Dots (...), leader lines (.....), or spaces connecting titles to page numbers\n"
                "- Systematic numbering (e.g. 1. / 1.1 / Chapter 1 / (1))\n\n"
                "NOT TOC content:\n"
                "- Body text paragraphs\n"
                "- Data tables\n"
                "- Image-heavy pages\n"
                "- A single heading with no page-number listing\n\n"
                "Return strict JSON (no markdown fences):\n"
                '{"still_toc": true/false, "confidence": "high"/"medium"/"low", '
                '"reason": "brief reason"}'
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        },
    ]

    est = estimate_tokens(str(content_parts[0]["text"])) + 800
    if budget and not budget.try_reserve("visual", est):
        logger.warning("[extract.toc] insufficient visual budget for boundary check p{}", page_num)
        return False, True

    try:
        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=model,
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        if budget:
            budget.commit("visual", actual=usage.get("total_tokens", est), est=est)
        data = json.loads(raw)
        return bool(data.get("still_toc")), False
    except Exception as exc:
        if budget:
            budget.refund("visual", est=est)
        logger.warning(
            "[extract.toc] boundary VLM check failed for page {}: {}", page_num, exc
        )
        # Conservative: stop expansion on failure
        return False, True


# -- Progressive boundary detection --------------------------------------------


def _detect_toc_range_for_anchor(
    *,
    anchor_page: int,
    pdf_path: str,
    page_count: int,
    output_dir: str,
    dpi: int,
    model: str,
    budget: Any | None = None,
) -> tuple[int, int, list[dict[str, Any]], list[str]]:
    """Progressively expand from anchor_page to find the TOC end boundary.

    Returns:
        (start_page, end_page, trace_rounds) -- all 1-based inclusive.
    """
    start_page = anchor_page
    current_end = min(anchor_page + BOUNDARY_STEP_PAGES - 1, page_count)
    trace_rounds: list[dict[str, Any]] = []
    warnings: list[str] = []

    for round_idx in range(MAX_BOUNDARY_ROUNDS):
        check_page = current_end
        png_path = os.path.join(output_dir, f"toc_boundary_p{check_page}.png")
        run_in_child_process(
            _render_single_page_worker,
            pdf_path,
            check_page,
            png_path,
            dpi,
            timeout=60,
        )

        still_toc, failed = _vlm_check_boundary_page(png_path, check_page, model, budget=budget)
        if failed:
            warnings.append(f"vlm_boundary_check_failed:p{check_page}")
        trace_rounds.append(
            {
                "round": round_idx,
                "check_page": check_page,
                "window": [start_page, current_end],
                "still_toc": still_toc,
            }
        )
        logger.info(
            "[extract.toc] round {}: page {} still_toc={}",
            round_idx,
            check_page,
            still_toc,
        )

        if not still_toc:
            # Boundary page is NOT TOC; TOC ends at previous page.
            current_end = max(check_page - 1, start_page)
            break

        next_end = min(current_end + BOUNDARY_STEP_PAGES, page_count)
        if next_end == current_end:
            break
        current_end = next_end

    return start_page, current_end, trace_rounds, warnings


# -- Main tool -----------------------------------------------------------------


@register_tool(
    name="extract.toc_with_boundaries",
    description=(
        "VLM-confirms TOC anchor pages, progressively detects TOC boundaries, "
        "then extracts TOC entries directly from rendered pages with VLM."
    ),
)
def extract_toc_with_boundaries(
    ctx: ToolContext, _args: dict[str, Any]
) -> ToolResult:
    start = time.monotonic()
    anchors = ctx.blackboard.toc_anchor_pages
    warnings: list[str] = []
    debug_info: dict[str, Any] = {}

    if not anchors:
        logger.info("[extract.toc] no anchor pages, skipping")
        ctx.blackboard.toc_result = TocResult(
            method="none",
            notes="No TOC anchor pages found by find.toc_anchor_pages",
        )
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    if not model:
        logger.warning("[extract.toc] no VLM model configured; skipping TOC extraction")
        ctx.blackboard.toc_result = TocResult(
            method="none",
            notes="No VLM model configured for TOC extraction",
        )
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            warnings=["No VLM model configured; skipping TOC extraction."],
        )
    dpi = int(ctx.settings.get("toc_png_dpi", "144"))
    page_count = ctx.blackboard.page_count
    output_dir = str(
        Path(ctx.output_dir or os.path.expanduser("~/.knowhere/_debug_profile"))
        / "toc_pages"
    )
    os.makedirs(output_dir, exist_ok=True)

    # -- Phase 1: VLM confirm anchors -----------------------------------------
    confirmed, confirm_failed = _vlm_confirm_anchors(anchors, model, budget=ctx.budget)
    if confirm_failed:
        warnings.append("vlm_anchor_confirmation_failed")
    debug_info["phase1_confirmed"] = [a.page for a in confirmed]
    debug_info["phase1_rejected"] = [
        a.page for a in anchors if a not in confirmed
    ]

    if not confirmed:
        ctx.blackboard.toc_result = TocResult(
            method="none",
            notes="VLM rejected all TOC anchor candidates",
        )
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            warnings=["VLM rejected all anchor pages"],
            debug=debug_info,
        )

    # -- Phase 2: progressive boundary detection -------------------------------
    toc_ranges: list[tuple[int, int]] = []
    all_trace_rounds: list[dict[str, Any]] = []

    for anchor in confirmed:
        toc_start, toc_end, trace_rounds, boundary_warnings = _detect_toc_range_for_anchor(
            anchor_page=anchor.page,
            pdf_path=ctx.pdf_path,
            page_count=page_count,
            output_dir=output_dir,
            dpi=dpi,
            model=model,
            budget=ctx.budget,
        )
        warnings.extend(boundary_warnings)
        toc_ranges.append((toc_start, toc_end))
        all_trace_rounds.extend(trace_rounds)
        logger.info(
            "[extract.toc] TOC region: pages {}-{}", toc_start, toc_end
        )

    debug_info["phase2_ranges"] = toc_ranges
    debug_info["phase2_trace_rounds"] = all_trace_rounds

    # -- Phase 3: VLM entry extraction -----------------------------------------
    all_toc_pages = sorted(
        {p for s, e in toc_ranges for p in range(s, e + 1)}
    )
    all_entries: list[dict[str, Any]] = []
    per_page_meta: list[dict[str, Any]] = []
    rendered_pages: list[dict[str, Any]] = []
    for page_num in all_toc_pages:
        png_path = os.path.join(output_dir, f"toc_page_{page_num}.png")
        render_result = run_in_child_process(
            _render_single_page_worker,
            ctx.pdf_path,
            page_num,
            png_path,
            dpi,
            timeout=60,
        )
        rendered_pages.append(render_result)
        entries, meta = vlm_extract_toc_entries(
            png_path=str(render_result.get("png_path") or png_path),
            page_num=page_num,
            model=model,
            previous_entries=all_entries,
        )
        all_entries.extend(entries)
        per_page_meta.append(meta)

    if not all_entries:
        raise RuntimeError("VLM TOC extractor returned no entries for confirmed TOC pages")

    scan_end_page = max(
        (round_info.get("check_page", 0) for round_info in all_trace_rounds),
        default=max(all_toc_pages),
    )
    toc_hierarchies = vlm_entries_to_toc_hierarchies(
        all_entries,
        toc_page_nums=all_toc_pages,
        scan_end_page=int(scan_end_page),
        page_count=page_count,
    )
    debug_info["phase3_vlm_entry_count"] = len(all_entries)
    debug_info["phase3_vlm_per_page_meta"] = per_page_meta
    debug_info["phase3_rendered_pages"] = rendered_pages

    ctx.blackboard.toc_result = TocResult(
        toc_pages=all_toc_pages,
        method="vlm_progressive",
        notes=(
            f"VLM confirmed {len(confirmed)} TOC starts, "
            f"expanded to {len(toc_ranges)} ranges: {toc_ranges}"
        ),
    )
    ctx.blackboard.toc_hierarchies = toc_hierarchies if toc_hierarchies else None
    ctx.blackboard.global_signals["vlm_toc_entries"] = {
        "model": model,
        "toc_pages": all_toc_pages,
        "total_entries": len(all_entries),
        "entries": all_entries,
        "per_page_meta": per_page_meta,
    }

    # Persist toc_hierarchies to disk for inspection / downstream reuse
    if toc_hierarchies and ctx.output_dir:
        toc_json_path = os.path.join(ctx.output_dir, "toc_hierarchies.json")
        try:
            with open(toc_json_path, "w", encoding="utf-8") as f:
                json.dump(toc_hierarchies, f, ensure_ascii=False, indent=2)
            logger.info("[extract.toc] wrote toc_hierarchies to {}", toc_json_path)
        except Exception as exc:
            logger.warning("[extract.toc] failed to write toc_hierarchies: {}", exc)

    toc_summary: dict[str, Any] = {
        "toc_ranges": toc_ranges,
        "toc_page_count": len(all_toc_pages),
        "toc_entry_count": len(all_entries),
        "toc_source": "vlm",
    }
    if toc_hierarchies:
        for i, hier in enumerate(toc_hierarchies):
            tree = hier.get("toc_tree", {})
            toc_summary[f"region_{i}_level1_count"] = len(tree)
            toc_summary[f"region_{i}_level1_titles"] = list(tree.keys())[:10]

    return ToolResult(
        status="ok",
        payload={
            "toc_count": len(toc_hierarchies) if toc_hierarchies else 0,
            "toc_page_count": len(all_toc_pages),
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary=toc_summary,
        warnings=warnings,
        debug=debug_info,
    )
