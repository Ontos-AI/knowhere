"""VLM-driven progressive TOC boundary detection + mineru local MD + toc_parser reuse."""

from __future__ import annotations

import gc
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import (
    TocAnchorPage,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# -- Constants -----------------------------------------------------------------

BOUNDARY_STEP_PAGES = 5
MAX_BOUNDARY_ROUNDS = 6
MAX_TOC_PAGES = BOUNDARY_STEP_PAGES * MAX_BOUNDARY_ROUNDS  # 30
MINERU_TIMEOUT_SECONDS = 180


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
) -> list[TocAnchorPage]:
    """Phase 1: send all anchor PNGs to VLM, ask which are real TOC starts."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    if not anchor_pages:
        return []

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

    messages = [{"role": "user", "content": content_parts}]

    try:
        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=messages,
            model=model,
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
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
        return confirmed
    except Exception as exc:
        logger.warning(
            "[extract.toc] VLM anchor confirmation failed: {}, "
            "falling back to no confirmed anchors (safe degradation)",
            exc,
        )
        return []


def _vlm_check_boundary_page(
    png_path: str,
    page_num: int,
    model: str,
) -> bool:
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

    try:
        client = get_openai_client(model=model)
        raw, _ = client.chat_completion_with_usage(
            messages=[{"role": "user", "content": content_parts}],
            model=model,
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
        return bool(data.get("still_toc"))
    except Exception as exc:
        logger.warning(
            "[extract.toc] boundary VLM check failed for page {}: {}", page_num, exc
        )
        # Conservative: stop expansion on failure
        return False


# -- Progressive boundary detection --------------------------------------------


def _detect_toc_range_for_anchor(
    *,
    anchor_page: int,
    pdf_path: str,
    page_count: int,
    output_dir: str,
    dpi: int,
    model: str,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Progressively expand from anchor_page to find the TOC end boundary.

    Returns:
        (start_page, end_page, trace_rounds) -- all 1-based inclusive.
    """
    start_page = anchor_page
    current_end = min(anchor_page + BOUNDARY_STEP_PAGES - 1, page_count)
    trace_rounds: list[dict[str, Any]] = []

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

        still_toc = _vlm_check_boundary_page(png_path, check_page, model)
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
            # Boundary page is NOT TOC; TOC ends at previous page
            current_end = max(check_page - 1, start_page)
            break

        next_end = min(current_end + BOUNDARY_STEP_PAGES, page_count)
        if next_end == current_end:
            break
        current_end = next_end

    return start_page, current_end, trace_rounds


# -- MinerU local extraction ---------------------------------------------------


def _run_mineru_local(
    pdf_path: str,
    start_page_0based: int,
    end_page_0based: int,
    output_dir: str,
) -> list[str]:
    """Run mineru CLI on a page range and return the resulting markdown lines."""
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "mineru",
        "-p",
        pdf_path,
        "-o",
        output_dir,
        "-s",
        str(start_page_0based),
        "-e",
        str(end_page_0based),
        "-t",
        "false",  # skip table parsing for speed
        "-b",
        "pipeline",
        "-m",
        "txt",
    ]
    logger.info(
        "[extract.toc] mineru local: pages {}-{}, cmd: {}",
        start_page_0based,
        end_page_0based,
        " ".join(cmd),
    )

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=MINERU_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            logger.error(
                "[extract.toc] mineru failed (code={}): {}",
                proc.returncode,
                proc.stderr[-500:] if proc.stderr else "",
            )
            return []
    except subprocess.TimeoutExpired:
        logger.error("[extract.toc] mineru timed out after {}s", MINERU_TIMEOUT_SECONDS)
        return []

    md_lines: list[str] = []
    output_path = Path(output_dir)
    for md_file in sorted(output_path.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8", errors="replace")
        md_lines.extend(content.splitlines())

    logger.info("[extract.toc] mineru produced {} markdown lines", len(md_lines))
    return md_lines


# -- Main tool -----------------------------------------------------------------


@register_tool(
    name="extract.toc_with_boundaries",
    description=(
        "VLM-confirms TOC anchor pages, progressively detects TOC boundaries, "
        "runs mineru local extraction, then reuses toc_parser for hierarchy."
    ),
    allowed_states={DocumentAgentState.CLASSIFIED},
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

    model = ctx.settings.get("vlm_model") or os.environ.get(
        "IMAGE_MODEL", "qwen3.5-flash"
    )
    dpi = int(ctx.settings.get("toc_png_dpi", "144"))
    page_count = ctx.blackboard.page_count
    output_dir = str(
        Path(ctx.output_dir or os.path.expanduser("~/.knowhere/_debug_profile"))
        / "toc_pages"
    )
    os.makedirs(output_dir, exist_ok=True)

    # -- Phase 1: VLM confirm anchors -----------------------------------------
    confirmed = _vlm_confirm_anchors(anchors, model)
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
        toc_start, toc_end, trace_rounds = _detect_toc_range_for_anchor(
            anchor_page=anchor.page,
            pdf_path=ctx.pdf_path,
            page_count=page_count,
            output_dir=output_dir,
            dpi=dpi,
            model=model,
        )
        toc_ranges.append((toc_start, toc_end))
        all_trace_rounds.extend(trace_rounds)
        logger.info(
            "[extract.toc] TOC region: pages {}-{}", toc_start, toc_end
        )

    debug_info["phase2_ranges"] = toc_ranges
    debug_info["phase2_trace_rounds"] = all_trace_rounds

    # -- Phase 3: mineru local extraction --------------------------------------
    all_md_lines: list[str] = []
    for i, (toc_start, toc_end) in enumerate(toc_ranges):
        region_dir = os.path.join(output_dir, f"mineru_region_{i}")
        md_lines = _run_mineru_local(
            pdf_path=ctx.pdf_path,
            start_page_0based=toc_start - 1,  # mineru uses 0-based
            end_page_0based=toc_end - 1,
            output_dir=region_dir,
        )
        if md_lines:
            all_md_lines.extend(md_lines)
        else:
            warnings.append(
                f"mineru produced no output for region {i} (pages {toc_start}-{toc_end})"
            )

    debug_info["phase3_md_line_count"] = len(all_md_lines)

    if not all_md_lines:
        ctx.blackboard.toc_result = TocResult(
            toc_pages=[p for s, e in toc_ranges for p in range(s, e + 1)],
            method="vlm_progressive",
            notes="VLM detected TOC ranges but mineru produced no markdown",
        )
        warnings.append("mineru produced no markdown for any TOC region")
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            warnings=warnings,
            debug=debug_info,
        )

    # -- Phase 4: toc_parser reuse ---------------------------------------------
    try:
        from app.services.document_parser.structure.toc_parser import (
            detect_tocs_in_texts,
        )

        hierarchy_model = ctx.settings.get("model") or os.environ.get(
            "HIERARCHY_LLM_MODEL"
        ) or os.environ.get("NORMOL_MODEL")

        toc_hierarchies, _filtered = detect_tocs_in_texts(
            all_md_lines,
            model_name=hierarchy_model,
            hierarchy_model_name=hierarchy_model,
            branch="normal",
            limit_=150,
        )
    except Exception as exc:
        logger.error("[extract.toc] toc_parser failed: {}", exc)
        toc_hierarchies = None
        warnings.append(f"toc_parser failed: {exc}")

    # -- Write results to blackboard -------------------------------------------
    all_toc_pages = sorted(
        {p for s, e in toc_ranges for p in range(s, e + 1)}
    )

    ctx.blackboard.toc_result = TocResult(
        toc_pages=all_toc_pages,
        method="vlm_progressive",
        notes=(
            f"VLM confirmed {len(confirmed)} TOC starts, "
            f"expanded to {len(toc_ranges)} ranges: {toc_ranges}"
        ),
    )
    ctx.blackboard.toc_hierarchies = toc_hierarchies if toc_hierarchies else None

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
