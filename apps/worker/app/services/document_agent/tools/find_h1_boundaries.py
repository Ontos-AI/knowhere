"""find_h1_boundaries — locate level-1 headings in a PDF via text search.

Why not use PyMuPDF ``doc.get_toc()`` page numbers?
---------------------------------------------------
PDF bookmark page references encode physical page offsets, while printed page
numbers in a TOC reflect logical numbering that often includes unnumbered front
matter.  For Chinese documents and scanned PDFs the offsets frequently disagree
by several pages or are entirely absent.

This tool instead:
1. Identifies TOC pages by detecting TOC-marker text in page features already
   gathered by ``scan_all_page_features``.
2. Reads those TOC pages in full to extract level-1 heading candidate strings.
3. Searches every page's full text for those candidates (fuzzy, after
   normalisation) to find where they actually appear in the body.

The result is a set of ``H1Match`` records linking each heading title to the
page where it physically starts — a reliable basis for shard cut decisions.
"""

from __future__ import annotations

import gc
import re
import unicodedata
from typing import Any

from app.services.document_agent.page_map import H1BoundaryResult, H1Match, PageFeature
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger


# ── Text normalisation helpers ─────────────────────────────────────────────────

# Patterns that prefix chapter/section numbers in various languages
_LEADING_NUMBER_RE = re.compile(
    r"""^
    (?:
        第\s*[零一二三四五六七八九十百千\d]+\s*[章节篇部分]  # 第X章/节
        | [零一二三四五六七八九十百千]+\s*[、。，,]           # 一、
        | [（(]\s*[零一二三四五六七八九十百千\d]+\s*[）)]    # （一）
        | \d+(?:\.\d+)*\.?\s*                                # 1. / 1.2 / 1.2.3.
        | [IVXLCDM]+\.?\s*                                   # Roman I. II.
        | [A-Za-z]\.\s*                                      # A. B.
        | Chapter\s+\w+\s*                                   # Chapter N
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _normalise_heading(text: str) -> str:
    """Strip leading numbers/labels and normalise whitespace for matching."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Strip leading numbering patterns
    stripped = _LEADING_NUMBER_RE.sub("", text).strip()
    # Keep the original if stripping removed everything (guard against over-stripping)
    return stripped if stripped else text


def _fuzzy_contains(needle: str, haystack: str, min_len: int = 4) -> bool:
    """Check whether ``needle`` (normalised heading) appears in ``haystack``.

    Matching strategy (in order of strictness):
    1. Exact substring after normalisation.
    2. Stripped-number variant of needle appears in normalised haystack.
    """
    if not needle or len(needle) < min_len:
        return False
    norm_hay = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", haystack))
    if needle in norm_hay:
        return True
    stripped = _normalise_heading(needle)
    if stripped and len(stripped) >= min_len and stripped in norm_hay:
        return True
    return False


# ── TOC page detection ─────────────────────────────────────────────────────────

_TOC_MARKERS = frozenset(["目录", "目次", "contents", "tableofcontents"])


def _is_toc_page(feature: PageFeature) -> bool:
    """Heuristic: is this page a Table of Contents page?"""
    text = re.sub(r"\s+", "", feature.text_preview.lower())
    return any(marker in text for marker in _TOC_MARKERS)


# ── Child-process worker: read full page texts ─────────────────────────────────


@worker
def _read_page_texts_worker(
    queue,
    pdf_path: str,
    page_indices: list[int],  # 0-based
) -> None:
    """Read full text for the requested pages; runs in an isolated process."""
    import pymupdf  # type: ignore[import]

    results: dict[int, str] = {}
    try:
        doc = pymupdf.open(pdf_path)
        for idx in page_indices:
            if 0 <= idx < doc.page_count:
                try:
                    results[idx] = doc[idx].get_text() or ""
                except Exception:
                    results[idx] = ""
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()

    queue.put({"ok": True, "texts": results})


def _load_full_page_texts(
    pdf_path: str,
    page_indices: list[int],
    timeout: int = 120,
) -> dict[int, str]:
    """Return {0-based-index: full_text} for the requested pages."""
    if not page_indices:
        return {}
    try:
        result = run_in_child_process(
            _read_page_texts_worker, pdf_path, page_indices, timeout=timeout
        )
        return {int(k): str(v) for k, v in (result.get("texts") or {}).items()}
    except Exception as exc:
        logger.warning(f"[find_h1_boundaries] full-text load failed: {exc}")
        return {}


# ── TOC text → h1 candidate extraction ────────────────────────────────────────

# Patterns that suggest a TOC line is a level-1 heading
# (numbered first-level or occupies a prominent position in the TOC)
_H1_TOC_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:
        第\s*[零一二三四五六七八九十百千\d]+\s*[章篇部]  # 第X章
        | [零一二三四五六七八九十百千]+\s*[、。]           # 一、
        | \d+\s*[\.\s]                                    # 1. / 1 (level-1 only)
        | Chapter\s+\w+                                   # Chapter …
        | [IVXLCDM]+\.?\s+\w                              # Roman numeral
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# A line is clearly a sub-heading if it starts with at least two level numbers
_SUBHEADING_RE = re.compile(r"^\s*\d+\.\d+", re.IGNORECASE)


def _extract_h1_candidates_from_toc_text(toc_text: str) -> list[str]:
    """Parse raw TOC page text and return likely level-1 heading strings."""
    candidates: list[str] = []
    seen: set[str] = set()

    for raw_line in toc_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip sub-headings (e.g. "1.2 something")
        if _SUBHEADING_RE.match(line):
            continue
        # Skip lines that look like page numbers only
        if re.fullmatch(r"[\d\s\.\-·…]+", line):
            continue
        # Must match a level-1 pattern
        if not _H1_TOC_LINE_RE.match(line):
            continue
        # Strip trailing page-number suffix common in TOCs: "  ……… 12"
        cleaned = re.sub(r"[\s\.\-·…]+\d+\s*$", "", line).strip()
        if not cleaned:
            continue
        norm = _normalise_heading(cleaned)
        if norm and norm not in seen and len(norm) >= 2:
            candidates.append(cleaned)  # keep original for matching fidelity
            seen.add(norm)

    return candidates


# ── Public API ─────────────────────────────────────────────────────────────────


def find_h1_boundaries(
    pdf_path: str,
    page_features: list[PageFeature],
    *,
    timeout: int = 180,
) -> H1BoundaryResult:
    """Locate level-1 headings in a PDF by grepping full page texts.

    Algorithm
    ---------
    1. Identify TOC pages from already-collected ``page_features``.
    2. Read those TOC pages in full; extract level-1 heading candidates.
    3. If no TOC found, fall back to grepping markdown-style headings
       (``# Title`` or lines that match level-1 numbering patterns) from
       each page's text_preview across all pages.
    4. For each candidate heading, search all pages' full text for the
       heading text (after normalisation).  Record the first matching page.

    Returns
    -------
    ``H1BoundaryResult`` — always non-raising; ``method="none"`` when
    nothing useful was found.
    """
    if not page_features:
        return H1BoundaryResult(toc_pages=[], h1_matches=[], method="none",
                                notes="no page features provided")

    # Step 1: find TOC pages
    toc_pages = [pf.page for pf in page_features if _is_toc_page(pf)]
    logger.info(f"[find_h1_boundaries] TOC pages (heuristic): {toc_pages}")

    # Step 2: extract h1 candidates from TOC page full text
    h1_candidates: list[str] = []
    if toc_pages:
        toc_indices = [p - 1 for p in toc_pages]  # 0-based
        toc_texts = _load_full_page_texts(pdf_path, toc_indices, timeout=timeout)
        for idx, text in sorted(toc_texts.items()):
            page_candidates = _extract_h1_candidates_from_toc_text(text)
            logger.info(
                f"[find_h1_boundaries] TOC page {idx + 1}: "
                f"{len(page_candidates)} h1 candidates"
            )
            h1_candidates.extend(page_candidates)

    # Step 3: fallback — grep heading-like lines from all page previews
    using_fallback = not h1_candidates
    if using_fallback:
        logger.info(
            "[find_h1_boundaries] no TOC or no candidates — "
            "grepping all page text_previews for heading-like lines"
        )
        for pf in page_features:
            for line in pf.text_preview.splitlines():
                line = line.strip()
                if _H1_TOC_LINE_RE.match(line) and not _SUBHEADING_RE.match(line):
                    cleaned = re.sub(r"[\s\.\-·…]+\d+\s*$", "", line).strip()
                    if cleaned and len(_normalise_heading(cleaned)) >= 2:
                        h1_candidates.append(cleaned)

    # Deduplicate candidates preserving order
    seen_norms: set[str] = set()
    unique_candidates: list[str] = []
    for c in h1_candidates:
        norm = _normalise_heading(c)
        if norm and norm not in seen_norms:
            unique_candidates.append(c)
            seen_norms.add(norm)
    h1_candidates = unique_candidates

    if not h1_candidates:
        return H1BoundaryResult(
            toc_pages=toc_pages,
            h1_matches=[],
            method="none",
            notes="no h1 candidates extracted",
        )

    logger.info(
        f"[find_h1_boundaries] {len(h1_candidates)} unique h1 candidates to search"
    )

    # Step 4: search all pages for each candidate
    # Load full text for all pages (excluding confirmed TOC pages to avoid
    # matching the TOC entry itself instead of the body heading)
    non_toc_indices = [
        pf.page - 1 for pf in page_features if pf.page not in set(toc_pages)
    ]
    all_texts = _load_full_page_texts(pdf_path, non_toc_indices, timeout=timeout)

    # Build a sorted list of (page_number, full_text) for ordered search
    page_text_pairs: list[tuple[int, str]] = sorted(
        ((idx + 1, text) for idx, text in all_texts.items()),
        key=lambda x: x[0],
    )

    h1_matches: list[H1Match] = []
    for candidate in h1_candidates:
        norm_candidate = _normalise_heading(candidate)
        matched_page: int | None = None
        match_text: str = ""
        confidence: float = 0.0

        for page_num, full_text in page_text_pairs:
            # Try exact match first (higher confidence)
            if candidate in full_text:
                matched_page = page_num
                match_text = candidate
                confidence = 1.0
                break
            # Try normalised match
            if norm_candidate and _fuzzy_contains(norm_candidate, full_text):
                matched_page = page_num
                match_text = norm_candidate
                confidence = 0.85
                break

        if matched_page is not None:
            h1_matches.append(
                H1Match(
                    title=candidate,
                    page=matched_page,
                    confidence=confidence,
                    match_text=match_text,
                )
            )
            logger.debug(
                f"[find_h1_boundaries] '{candidate[:40]}' → page {matched_page} "
                f"(conf={confidence})"
            )
        else:
            logger.debug(
                f"[find_h1_boundaries] '{candidate[:40]}' → not found in body"
            )

    method: str
    if h1_matches and not using_fallback:
        method = "toc_grep"
    elif h1_matches:
        method = "heading_grep"
    else:
        method = "none"

    logger.info(
        f"[find_h1_boundaries] result: method={method}, "
        f"{len(h1_matches)}/{len(h1_candidates)} headings matched"
    )
    return H1BoundaryResult(
        toc_pages=toc_pages,
        h1_matches=h1_matches,
        method=method,
        notes=(
            f"{len(h1_matches)} of {len(h1_candidates)} candidates matched; "
            f"fallback={using_fallback}"
        ),
    )
