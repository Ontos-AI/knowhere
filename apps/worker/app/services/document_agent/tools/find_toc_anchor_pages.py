"""Scan for TOC anchor pages and render their PNGs for VLM inspection."""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import TocAnchorPage, ToolContext, ToolResult
from app.services.document_agent.registry import has_page_labels, register_tool
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# CJK and English TOC keywords used for anchor detection.
TOC_KEYWORDS = {"目录", "目次", "contents", "tableofcontents", "table of contents"}

# If a TOC keyword fingerprint appears on more than this fraction of total
# pages, it is treated as a recurring navigation element (header/footer link)
# rather than real TOC content.
RECURRING_ELEMENT_THRESHOLD = 0.30

# Hard cap on the number of candidate anchor pages sent to VLM.  A real
# document never has more than ~30 TOC start pages.
MAX_ANCHOR_CANDIDATES = 30


def _normalize_for_toc(text: str) -> str:
    """Collapse whitespace for keyword matching."""
    return text.replace(" ", "").replace("\u3000", "").lower()


@worker
def _render_pages_worker(
    queue, pdf_path: str, pages: list[int], output_dir: str, dpi: int
) -> None:
    import pymupdf  # type: ignore[import]

    results: list[dict[str, Any]] = []
    try:
        doc = pymupdf.open(pdf_path)
        for page_num in pages:
            idx = page_num - 1
            if 0 <= idx < doc.page_count:
                page = doc[idx]
                mat = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
                pix = page.get_pixmap(matrix=mat)
                png_name = f"toc_anchor_page_{page_num}.png"
                png_path = os.path.join(output_dir, png_name)
                pix.save(png_path)
                results.append({"page": page_num, "png_path": png_path})
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "results": results})


def _filter_recurring_elements(
    matches: list[tuple[int, str, int]],
    total_pages: int,
) -> set[int]:
    """Remove pages whose TOC keyword pattern is a recurring navigation element.

    Each *match* is ``(page, raw_line, line_index)``.  We build a **composite
    fingerprint** per page by joining all its matches as
    ``"raw_line@line_idx\\n..."``.  If the same composite fingerprint appears
    on more than ``RECURRING_ELEMENT_THRESHOLD`` of all pages, those pages are
    header/footer false-positives.

    Example (SpaceX S-1):
      - page 17 → ``"Table of Contents@0\\nTABLE OF CONTENTS@1"`` (unique → keeps)
      - page 18 → ``"Table of Contents@1"`` (376 pages → recurring → filtered)
      - page 401 → ``"Table of Contents@0"`` (~31 pages → VLM decides)
    """
    # Collect all matches per page
    page_matches: dict[int, list[tuple[str, int]]] = {}
    for page, raw_line, line_idx in matches:
        page_matches.setdefault(page, []).append((raw_line, line_idx))

    # Build composite fingerprint per page (sorted by line_idx for stability)
    page_fingerprints: dict[int, str] = {}
    for page, hits in page_matches.items():
        hits_sorted = sorted(hits, key=lambda h: h[1])
        page_fingerprints[page] = "\n".join(
            f"{raw}@{idx}" for raw, idx in hits_sorted
        )

    # Group pages by composite fingerprint
    fp_groups: dict[str, list[int]] = {}
    for page, fp in page_fingerprints.items():
        fp_groups.setdefault(fp, []).append(page)

    threshold = max(int(total_pages * RECURRING_ELEMENT_THRESHOLD), 1)

    surviving: set[int] = set()
    for fp, pages in fp_groups.items():
        if len(pages) > threshold:
            logger.info(
                "[find.toc_anchor_pages] recurring pattern filtered: "
                "{!r} appears on {}/{} pages",
                fp[:60],
                len(pages),
                total_pages,
            )
        else:
            surviving.update(pages)

    return surviving


@register_tool(
    name="find.toc_anchor_pages",
    description=(
        "Scan page text previews for TOC keywords, filter recurring "
        "navigation elements, then render candidate PNGs for VLM confirmation."
    ),
    preconditions=(has_page_labels,),
)
def find_toc_anchor_pages(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    total_pages = ctx.blackboard.page_count

    # Scan text previews for TOC keywords and record per-line matches.
    # Each entry: (page, raw_line_text, line_index)
    # We use raw (original) text for fingerprinting so that casing
    # differences (e.g. "Table of Contents" vs "TABLE OF CONTENTS")
    # naturally produce distinct composite fingerprints.
    keyword_matches: list[tuple[int, str, int]] = []
    raw_hit_pages: set[int] = set()

    for feature in ctx.blackboard.page_features:
        page_matched = False
        for line_idx, raw_line in enumerate(feature.text_lines_preview):
            norm_line = _normalize_for_toc(raw_line)
            for keyword in TOC_KEYWORDS:
                if keyword in norm_line:
                    keyword_matches.append((feature.page, raw_line.strip(), line_idx))
                    raw_hit_pages.add(feature.page)
                    page_matched = True
                    break  # one match per line is enough

        # Fallback: check if a TOC keyword spans across adjacent lines.
        # PyMuPDF sometimes splits large headings across lines, e.g.
        # "目" + "录" or "Table of" + "Contents".  Join the first few
        # preview lines (where a page title would appear) and re-check
        # with the same keywords and normalisation.
        if not page_matched and feature.text_lines_preview:
            head = feature.text_lines_preview[:10]
            joined_head = _normalize_for_toc("".join(head))
            for keyword in TOC_KEYWORDS:
                if keyword in joined_head:
                    keyword_matches.append((feature.page, keyword, 0))
                    raw_hit_pages.add(feature.page)
                    logger.debug(
                        "[find.toc_anchor_pages] cross-line keyword '{}' "
                        "detected on page {} (head lines joined)",
                        keyword,
                        feature.page,
                    )
                    break

    # Apply recurring element fingerprint filter
    if keyword_matches:
        anchor_pages = _filter_recurring_elements(keyword_matches, total_pages)
    else:
        anchor_pages = set()

    logger.info(
        "[find.toc_anchor_pages] keyword scan: {} raw hits → {} after "
        "fingerprint filter",
        len(raw_hit_pages),
        len(anchor_pages),
    )

    # Hard cap: a document never has more than ~30 real TOC start candidates.
    if len(anchor_pages) > MAX_ANCHOR_CANDIDATES:
        logger.warning(
            "[find.toc_anchor_pages] {} candidates exceed cap of {}, truncating",
            len(anchor_pages),
            MAX_ANCHOR_CANDIDATES,
        )
        anchor_pages = set(sorted(anchor_pages)[:MAX_ANCHOR_CANDIDATES])

    if not anchor_pages:
        logger.info("[find.toc_anchor_pages] no TOC keyword pages found")
        ctx.blackboard.toc_anchor_pages = []
        return ToolResult(
            status="ok",
            payload={"anchor_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            output_summary={"anchor_count": 0, "pages": []},
        )

    # Render candidate pages as PNGs for downstream VLM confirmation
    sorted_pages = sorted(anchor_pages)
    output_dir = str(
        Path(ctx.output_dir or os.path.expanduser("~/.knowhere/_debug_profile"))
        / "toc_pages"
    )
    os.makedirs(output_dir, exist_ok=True)

    dpi = int(ctx.settings.get("toc_png_dpi", "144"))
    result = run_in_child_process(
        _render_pages_worker, ctx.pdf_path, sorted_pages, output_dir, dpi, timeout=120
    )

    anchors: list[TocAnchorPage] = []
    for item in result.get("results") or []:
        page = int(item["page"])
        anchors.append(
            TocAnchorPage(page=page, png_path=item["png_path"], source="text_scan")
        )

    ctx.blackboard.toc_anchor_pages = anchors
    logger.info(
        "[find.toc_anchor_pages] found {} anchor pages: {}",
        len(anchors),
        [a.page for a in anchors],
    )

    return ToolResult(
        status="ok",
        payload={"anchor_count": len(anchors)},
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "anchor_count": len(anchors),
            "pages": [a.to_dict() for a in anchors],
        },
    )

