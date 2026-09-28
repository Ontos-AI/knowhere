"""Scan PDF text for TOC anchor pages and render their PNGs for VLM inspection."""

from __future__ import annotations

import gc
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import TocAnchorPage, ToolContext, ToolResult
from app.services.document_agent.pdf_text import (
    PageTextBands,
    line_fingerprint,
    page_bands_map,
)
from app.services.document_agent.registry import has_page_full_text, has_page_labels, register_tool
from app.services.document_parser.structure.body_boundary import normalize_match_text
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# CJK and English TOC keywords used for first-pass anchor detection.
TOC_KEYWORDS = frozenset({"目录", "目次", "contents", "table of contents"})

MAX_ANCHOR_CANDIDATES = 20
# English keywords must sit at a line edge once candidates overflow; CJK 目录/目次 are exempt.
EDGE_ANCHORED_KEYWORDS = frozenset({"contents", "table of contents"})
MIN_CONSECUTIVE_DUPLICATE_RUN = 2

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


def _match_toc_keyword_in_line(normalized_line: str) -> str | None:
    """Return the longest TOC keyword contained in a normalized line."""
    if not normalized_line:
        return None
    return next(
        (
            keyword
            for keyword in sorted(TOC_KEYWORDS, key=len, reverse=True)
            if keyword in normalized_line
        ),
        None,
    )


def _find_toc_text_matches(lines: list[str]) -> list[dict[str, Any]]:
    """Match TOC keywords as line-level containment after keyword-split repair.

    Cross-line handling stays keyword-internal only (e.g. 目+录). Hit rule is
    ``keyword in normalized_line`` (longest match wins), not whole-line equality.
    """
    matches: list[dict[str, Any]] = []
    for raw_line, start_idx, end_idx in _merge_keyword_split_lines(lines):
        keyword = _match_toc_keyword_in_line(normalize_match_text(raw_line))
        if keyword is None:
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


def _scan_toc_matches(
    bands_by_page: dict[int, PageTextBands],
    *,
    page_count: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for page in range(1, page_count + 1):
        bands = bands_by_page.get(page)
        if bands is None:
            continue
        texts = [" ".join(line.text.split()) for line in bands.lines]
        for match in _find_toc_text_matches(texts):
            covered = bands.lines[match["line_index"] : match["line_end_index"] + 1]
            matches.append(
                {
                    "page": page,
                    **match,
                    "region": covered[0].region,
                    "fingerprint": "\n".join(
                        line_fingerprint(line) for line in covered
                    ),
                }
            )
    return matches


def _match_pages(matches: list[dict[str, Any]]) -> set[int]:
    return {int(match["page"]) for match in matches}


def _consecutive_runs(pages: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for page in pages:
        if runs and page == runs[-1][-1] + 1:
            runs[-1].append(page)
        else:
            runs.append([page])
    return runs


def _drop_consecutive_duplicate_pages(
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop pages whose hit signature repeats on consecutive page numbers.

    A TOC start page is followed by TOC continuation or body pages, neither of
    which repeats the same heading line with the same region, paragraph height
    and font. Identical signatures on adjacent pages are running headers or
    footers (e.g. a "Table of Contents" link printed on every page).
    """
    fingerprints_by_page: dict[int, list[str]] = defaultdict(list)
    for match in matches:
        fingerprints_by_page[int(match["page"])].append(str(match["fingerprint"]))

    pages_by_signature: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for page, fingerprints in fingerprints_by_page.items():
        pages_by_signature[tuple(sorted(fingerprints))].append(page)

    dropped: set[int] = set()
    for signature, pages in pages_by_signature.items():
        for run in _consecutive_runs(sorted(pages)):
            if len(run) >= MIN_CONSECUTIVE_DUPLICATE_RUN:
                dropped.update(run)
                logger.info(
                    "[find.toc_anchor_pages] consecutive duplicate signature "
                    "{!r} on pages {}-{} dropped",
                    signature[0],
                    run[0],
                    run[-1],
                )
    return [match for match in matches if int(match["page"]) not in dropped]


def _keyword_at_line_edge(raw_line: str, keyword: str) -> bool:
    normalized = normalize_match_text(raw_line)
    return normalized.startswith(keyword) or normalized.endswith(keyword)


def _filter_edge_anchored(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for match in matches:
        keyword = str(match["match_kind"]).removeprefix("keyword:")
        if keyword not in EDGE_ANCHORED_KEYWORDS or _keyword_at_line_edge(
            str(match["raw_line"]), keyword
        ):
            kept.append(match)
    return kept


def _filter_body_region(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [match for match in matches if match["region"] == "body"]


def _select_anchor_pages(
    matches: list[dict[str, Any]],
) -> tuple[set[int], dict[str, Any]]:
    """Dedup unconditionally; narrow only while over the cap; never truncate."""
    narrowing: dict[str, Any] = {"raw": len(_match_pages(matches))}
    matches = _drop_consecutive_duplicate_pages(matches)
    narrowing["after_dedup"] = len(_match_pages(matches))
    if len(_match_pages(matches)) > MAX_ANCHOR_CANDIDATES:
        matches = _filter_edge_anchored(matches)
        narrowing["after_edge"] = len(_match_pages(matches))
    if len(_match_pages(matches)) > MAX_ANCHOR_CANDIDATES:
        matches = _filter_body_region(matches)
        narrowing["after_body"] = len(_match_pages(matches))
    if len(_match_pages(matches)) > MAX_ANCHOR_CANDIDATES:
        narrowing["gave_up"] = True
        return set(), narrowing
    return _match_pages(matches), narrowing


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


@register_tool(
    name="find.toc_anchor_pages",
    description=(
        "Scan Stage-0 line records for TOC keywords, drop consecutive-page "
        "duplicate signatures, narrow by edge anchoring and body region only "
        "when over the cap, then render candidate PNGs for VLM confirmation."
    ),
    preconditions=(has_page_labels, has_page_full_text),
)
def find_toc_anchor_pages(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    total_pages = ctx.blackboard.page_count

    keyword_matches = _scan_toc_matches(
        page_bands_map(ctx.blackboard.page_full_text_cache),
        page_count=total_pages,
    )
    anchor_pages, narrowing = _select_anchor_pages(keyword_matches)
    logger.info("[find.toc_anchor_pages] candidate narrowing: {}", narrowing)
    if narrowing.get("gave_up"):
        logger.warning(
            "[find.toc_anchor_pages] still > {} candidates after all narrowing; "
            "treating document as having no TOC",
            MAX_ANCHOR_CANDIDATES,
        )

    if not anchor_pages:
        ctx.blackboard.toc_anchor_pages = []
        return ToolResult(
            status="ok",
            payload={"anchor_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            output_summary={"anchor_count": 0, "pages": [], "narrowing": narrowing},
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
            "narrowing": narrowing,
        },
    )
