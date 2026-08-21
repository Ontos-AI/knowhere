"""Scan PDF text for TOC anchor pages and render their PNGs for VLM inspection."""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import TocAnchorPage, ToolContext, ToolResult
from app.services.document_agent.pdf_text import page_content_map
from app.services.document_agent.registry import has_page_full_text, has_page_labels, register_tool
from app.services.document_parser.structure.body_boundary import normalize_match_text
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# CJK and English TOC keywords used for first-pass anchor detection.
TOC_KEYWORDS = frozenset({"目录", "目次", "contents", "table of contents"})

# If a TOC keyword fingerprint appears on more than this fraction of total
# pages, it is treated as a recurring navigation element (header/footer link)
# rather than real TOC content.
RECURRING_ELEMENT_THRESHOLD = 0.30

# Hard cap on the number of candidate anchor pages sent to VLM.  A real
# document never has more than ~30 TOC start pages.
MAX_ANCHOR_CANDIDATES = 30

# Upper bound on consecutive lines joined when repairing a keyword split by
# newlines (e.g. 目\\n录). Derived from the longest keyword character length.
_MAX_KEYWORD_SPLIT_LINES = max(len(keyword) for keyword in TOC_KEYWORDS)


def _match_toc_keyword_parts(parts: list[str]) -> str | None:
    candidates = {normalize_match_text(parts[0])} if parts else set()
    for part in parts[1:]:
        next_candidates: set[str] = set()
        for prefix in candidates:
            for separator in ("", " "):
                candidate = normalize_match_text(f"{prefix}{separator}{part}")
                if any(keyword.startswith(candidate) for keyword in TOC_KEYWORDS):
                    next_candidates.add(candidate)
        candidates = next_candidates
        if not candidates:
            return None
    return next((candidate for candidate in candidates if candidate in TOC_KEYWORDS), None)


def _meaningful_text_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.split()]


def _merge_keyword_split_lines(
    lines: list[str],
) -> list[tuple[str, int, int]]:
    """Merge consecutive lines that together exactly equal one TOC keyword.

    Only repairs newlines inside known keywords (e.g. 目+录, Table of+Contents).
    Does not join arbitrary page text windows.
    """
    merged: list[tuple[str, int, int]] = []
    index = 0
    while index < len(lines):
        joined: tuple[str, int, int] | None = None
        upper = min(_MAX_KEYWORD_SPLIT_LINES, len(lines) - index)
        for part_count in range(upper, 1, -1):
            parts = [lines[index + offset].strip() for offset in range(part_count)]
            keyword = _match_toc_keyword_parts(parts)
            if keyword is None:
                continue
            joined = (keyword, index, index + part_count - 1)
            break
        if joined is not None:
            merged.append(joined)
            index = joined[2] + 1
            continue
        merged.append((lines[index], index, index))
        index += 1
    return merged


def _find_toc_text_matches(lines: list[str]) -> list[dict[str, Any]]:
    """Match TOC keywords only as whole lines after keyword-split repair."""
    matches: list[dict[str, Any]] = []
    for raw_line, start_idx, end_idx in _merge_keyword_split_lines(lines):
        keyword = normalize_match_text(raw_line)
        if keyword not in TOC_KEYWORDS:
            continue
        matches.append(
            {
                "raw_line": raw_line.strip(),
                "line_index": start_idx,
                "line_end_index": end_idx,
                "match_kind": f"keyword:{keyword}",
            }
        )
    return matches


def _scan_toc_from_page_texts(
    page_texts: dict[int, str],
    *,
    page_count: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for page_num in range(1, page_count + 1):
        text = page_texts.get(page_num, "")
        lines = _meaningful_text_lines(text)
        for match in _find_toc_text_matches(lines):
            matches.append({"page": page_num, **match})
    return matches


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
    matches: list[dict[str, Any]],
    total_pages: int,
) -> set[int]:
    """Remove pages whose TOC keyword pattern is a recurring navigation element.

    Each match contains ``page``, ``raw_line``, ``line_index``, and optionally
    ``line_end_index``.  We build a **composite
    fingerprint** per page by joining all its matches as
    ``"raw_line@line_idx-line_end\\n..."``.  If the same composite fingerprint appears
    on more than ``RECURRING_ELEMENT_THRESHOLD`` of all pages, those pages are
    header/footer false-positives.

    Example (SpaceX S-1):
      - page 17 → ``"Table of Contents@0\\nTABLE OF CONTENTS@1"`` (unique → keeps)
      - page 18 → ``"Table of Contents@1"`` (376 pages → recurring → filtered)
      - page 401 → ``"Table of Contents@0"`` (~31 pages → VLM decides)
    """
    # Collect all matches per page
    page_matches: dict[int, list[tuple[str, int, int]]] = {}
    for match in matches:
        page = int(match["page"])
        raw_line = str(match.get("raw_line") or "").strip()
        line_idx = int(match.get("line_index") or 0)
        line_end_idx = int(match.get("line_end_index") or line_idx)
        page_matches.setdefault(page, []).append((raw_line, line_idx, line_end_idx))

    # Build composite fingerprint per page (sorted by line_idx for stability)
    page_fingerprints: dict[int, str] = {}
    for page, hits in page_matches.items():
        hits_sorted = sorted(hits, key=lambda h: h[1])
        page_fingerprints[page] = "\n".join(
            f"{raw}@{start_idx}-{end_idx}"
            for raw, start_idx, end_idx in hits_sorted
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
        "Scan full PDF page text for whole-line TOC keywords, filter recurring "
        "navigation elements, then render candidate PNGs for VLM confirmation."
    ),
    preconditions=(has_page_labels, has_page_full_text),
)
def find_toc_anchor_pages(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    total_pages = ctx.blackboard.page_count

    keyword_matches = _scan_toc_from_page_texts(
        page_content_map(ctx.blackboard.page_full_text_cache),
        page_count=total_pages,
    )
    raw_hit_pages = {int(match["page"]) for match in keyword_matches}

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
