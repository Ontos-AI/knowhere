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


# -- Main tool -----------------------------------------------------------------


@register_tool(
    name="extract.toc_with_boundaries",
    description=(
        "VLM-confirms TOC anchor pages, then batch-classifies and extracts "
        "TOC entries from rendered page windows using VLM."
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

    # -- Phase 2+3 (unified): batch classify + extract ---------------------------
    # Instead of separate boundary detection (Phase 2) then per-page extraction
    # (Phase 3), we send batches of BOUNDARY_STEP_PAGES images to VLM in one
    # call.  The VLM classifies each page (TOC vs non-TOC) AND extracts entries
    # from TOC pages simultaneously.  If the last page in a batch is still TOC,
    # we expand the window and use prior entries as continuation context.
    from app.services.document_agent.tools.vlm_toc_extractor import (
        vlm_extract_toc_batch,
    )

    all_entries: list[dict[str, Any]] = []
    all_toc_pages: list[int] = []
    toc_hierarchies: list[dict[str, Any]] = []
    batch_meta: list[dict[str, Any]] = []
    batch_trace: list[dict[str, Any]] = []

    for anchor in confirmed:
        anchor_page = anchor.page
        region_entries: list[dict[str, Any]] = []
        region_toc_pages: list[int] = []
        region_scan_end = anchor_page

        for round_idx in range(MAX_BOUNDARY_ROUNDS):
            batch_start = anchor_page + round_idx * BOUNDARY_STEP_PAGES
            batch_end = min(
                batch_start + BOUNDARY_STEP_PAGES - 1, page_count
            )
            if batch_start > page_count:
                break

            batch_pages = list(range(batch_start, batch_end + 1))
            logger.info(
                "[extract.toc] batch round {}: pages {}-{} for anchor {}",
                round_idx, batch_start, batch_end, anchor_page,
            )

            # Render all pages in this batch
            page_pngs: list[tuple[int, str]] = []
            for page_num in batch_pages:
                png_path = os.path.join(output_dir, f"toc_page_{page_num}.png")
                run_in_child_process(
                    _render_single_page_worker,
                    ctx.pdf_path,
                    page_num,
                    png_path,
                    dpi,
                    timeout=60,
                )
                page_pngs.append((page_num, png_path))

            # Send batch to VLM — classify + extract in one call
            batch_result = vlm_extract_toc_batch(
                page_pngs=page_pngs,
                model=model,
                previous_entries=region_entries if region_entries else None,
            )
            batch_meta.append(batch_result.meta)

            # Collect results
            region_entries.extend(batch_result.all_entries)
            region_toc_pages.extend(batch_result.toc_pages)
            region_scan_end = batch_end

            batch_trace.append({
                "anchor": anchor_page,
                "round": round_idx,
                "batch_pages": batch_pages,
                "toc_pages": batch_result.toc_pages,
                "non_toc_pages": batch_result.non_toc_pages,
                "entries_found": len(batch_result.all_entries),
            })

            # Determine if we need to continue expanding
            # If the last page in the batch is NOT TOC, boundary found
            last_page_is_toc = (
                batch_result.page_results
                and batch_result.page_results[-1].is_toc
            )
            if not last_page_is_toc:
                logger.info(
                    "[extract.toc] boundary found: last page {} is not TOC",
                    batch_end,
                )
                break

            # Last page is still TOC — continue expanding
            if batch_end >= page_count:
                break
            logger.info(
                "[extract.toc] last page {} still TOC, expanding window",
                batch_end,
            )

        all_entries.extend(region_entries)
        all_toc_pages.extend(region_toc_pages)

        if region_entries:
            region_hierarchies = vlm_entries_to_toc_hierarchies(
                region_entries,
                toc_page_nums=region_toc_pages,
                scan_end_page=region_scan_end,
                page_count=page_count,
            )
            toc_hierarchies.extend(region_hierarchies)
        else:
            logger.warning(
                "[extract.toc] anchor {} produced no TOC entries",
                anchor_page,
            )

    if not all_entries:
        raise RuntimeError(
            "VLM TOC extractor returned no entries for confirmed TOC pages"
        )

    debug_info["batch_trace"] = batch_trace
    debug_info["batch_meta"] = batch_meta
    debug_info["vlm_entry_count"] = len(all_entries)

    all_toc_pages_sorted = sorted(set(all_toc_pages))
    toc_region_count = len(toc_hierarchies)

    ctx.blackboard.toc_result = TocResult(
        toc_pages=all_toc_pages_sorted,
        method="vlm_batch",
        notes=(
            f"VLM confirmed {len(confirmed)} TOC starts, "
            f"batch classify+extract found {toc_region_count} regions, "
            f"toc_pages={all_toc_pages_sorted}"
        ),
    )
    ctx.blackboard.toc_hierarchies = toc_hierarchies if toc_hierarchies else None
    ctx.blackboard.global_signals["vlm_toc_entries"] = {
        "model": model,
        "toc_pages": all_toc_pages_sorted,
        "total_entries": len(all_entries),
        "entries": all_entries,
        "batch_meta": batch_meta,
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

    # Build toc_ranges from confirmed TOC pages for summary
    toc_ranges_out: list[list[int]] = []
    if toc_hierarchies:
        for hier in toc_hierarchies:
            toc_ranges_out.append(hier.get("toc_range", []))

    toc_summary: dict[str, Any] = {
        "toc_ranges": toc_ranges_out,
        "toc_page_count": len(all_toc_pages_sorted),
        "toc_entry_count": len(all_entries),
        "toc_region_count": toc_region_count,
        "toc_source": "vlm_batch",
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
            "toc_page_count": len(all_toc_pages_sorted),
            "toc_region_count": toc_region_count,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary=toc_summary,
        warnings=warnings,
        debug=debug_info,
    )

