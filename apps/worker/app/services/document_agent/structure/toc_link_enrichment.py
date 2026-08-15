"""Attach TOC-page hyperlinks onto VLM entries before calibration.

PROFILE calls this after ``extract.toc_with_boundaries`` and before
``run_toc_anchoring``. Only runs when TOC pages actually contain internal
page hyperlinks (``page.get_links()``).

Page-number convention (all 1-based after normalize):
  - ``get_links()`` dest ``page``: already 1-based — do not add 1.
  - VLM / probe pages: already 1-based.
  - TODO(bookmarks): ``get_toc()`` ``meta.page`` is 0-based — add 1 when wired.

Matching (strict):
  - Walk VLM ``toc_with_level`` entries in order, once each.
  - ``heading.strip() in anchor_text.strip()``.
  - Attach only when exactly one link hits; zero or many → leave unmatched.
  - Cross-line / truncated anchors are not special-cased.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class TocPageLink:
    toc_page: int  # 1-based
    dest_physical_page: int  # 1-based
    anchor_text: str
    kind: int | None = None


@dataclass(frozen=True)
class TocLinkEnrichStats:
    toc_pages_scanned: list[int]
    links_raw: int
    links_internal: int
    entries_total: int
    entries_matched: int
    skipped_no_links: bool = False


def _toc_pages_from_hierarchy(hierarchy: dict[str, Any]) -> list[int]:
    """Physical pages that are actual TOC content (not VLM scan expansion)."""
    pages: set[int] = set()
    toc_range = hierarchy.get("toc_range")
    if isinstance(toc_range, (list, tuple)) and len(toc_range) >= 2:
        start, end = int(toc_range[0]), int(toc_range[1])
        if start > 0 and end >= start:
            pages.update(range(start, end + 1))
    # Do NOT include scan_range: that window often covers non-TOC body pages
    # used only for VLM boundary detection.
    return sorted(pages)


def _anchor_text_for_rect(page: Any, rect: Any) -> str:
    import fitz

    words = page.get_text("words") or []
    hit: list[tuple[float, float, str]] = []
    target = fitz.Rect(rect)
    # Slightly expand so thin link boxes still catch title glyphs.
    target = target + (-2, -2, 2, 2)
    for word in words:
        x0, y0, x1, y1, text = word[:5]
        if not str(text).strip():
            continue
        if fitz.Rect(x0, y0, x1, y1).intersects(target):
            hit.append((float(y0), float(x0), str(text)))
    hit.sort()
    return " ".join(part for _, _, part in hit).strip()


def _link_dest_physical_page(raw_page: Any) -> int:
    """Normalize ``page.get_links()`` destination to 1-based physical page.

    PyMuPDF page hyperlinks expose ``link["page"]`` already as 1-based (int or
    digit string). Do **not** add 1 here — that off-by-one sent every TOC link
    one page past the real click target.

    TODO(bookmarks): ``doc.get_toc()`` outline ``meta.page`` is 0-based. When
    bookmark signal is wired into calibration, convert that field with ``+1``
    (or use get_toc's 1-based display page) before merging with links / VLM.
    """
    return int(raw_page)


def collect_toc_page_links(pdf_path: str, toc_pages: list[int]) -> list[TocPageLink]:
    """Collect internal page hyperlinks on TOC pages with nearby anchor text."""
    import fitz

    if not toc_pages:
        return []

    out: list[TocPageLink] = []
    doc = fitz.open(pdf_path)
    try:
        for toc_page in toc_pages:
            if toc_page < 1 or toc_page > doc.page_count:
                continue
            page = doc[toc_page - 1]
            for link in page.get_links() or []:
                # Page hyperlinks only (NAMED/GOTO via get_links). Not bookmarks.
                dest_raw = link.get("page")
                if dest_raw is None:
                    continue
                try:
                    dest_physical = _link_dest_physical_page(dest_raw)
                except (TypeError, ValueError):
                    continue
                if dest_physical < 1 or dest_physical > doc.page_count:
                    continue
                rect = link.get("from")
                if rect is None:
                    continue
                anchor = _anchor_text_for_rect(page, rect)
                if not anchor:
                    continue
                kind = link.get("kind")
                out.append(
                    TocPageLink(
                        toc_page=toc_page,
                        dest_physical_page=dest_physical,
                        anchor_text=anchor,
                        kind=int(kind) if kind is not None else None,
                    )
                )
    finally:
        doc.close()
    return out


def match_toc_entries_to_links(
    entries: list[dict[str, Any]],
    links: list[TocPageLink],
) -> tuple[list[dict[str, Any]], int]:
    """Attach ``link`` when heading.strip() is in exactly one link anchor."""
    matched = 0
    enriched: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        new_entry = {
            "heading": entry.get("heading"),
            "level": entry.get("level"),
            "page_number": entry.get("page_number"),
        }
        for key, value in entry.items():
            if key in new_entry or key == "link":
                continue
            new_entry[key] = value

        heading = str(entry.get("heading") or "").strip()
        if not heading or not links:
            enriched.append(new_entry)
            continue

        hits = [
            link
            for link in links
            if heading in str(link.anchor_text or "").strip()
        ]
        if len(hits) != 1:
            enriched.append(new_entry)
            continue

        new_entry["link"] = {
            "physical_page": hits[0].dest_physical_page,
        }
        matched += 1
        enriched.append(new_entry)

    return enriched, matched


def enrich_toc_hierarchies_with_links(
    *,
    pdf_path: str,
    toc_hierarchies: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], TocLinkEnrichStats]:
    """Attach optional ``link`` fields onto matching TOC entries.

    If TOC pages have no internal links, hierarchies are returned unchanged.
    """
    hierarchies = [dict(h) for h in (toc_hierarchies or []) if isinstance(h, dict)]
    if not hierarchies:
        return [], TocLinkEnrichStats(
            toc_pages_scanned=[],
            links_raw=0,
            links_internal=0,
            entries_total=0,
            entries_matched=0,
            skipped_no_links=True,
        )

    toc_pages: list[int] = []
    seen: set[int] = set()
    for hierarchy in hierarchies:
        for page in _toc_pages_from_hierarchy(hierarchy):
            if page not in seen:
                seen.add(page)
                toc_pages.append(page)

    links = collect_toc_page_links(pdf_path, toc_pages)
    if not links:
        logger.info(
            "[toc_link_enrich] no internal links on TOC pages {}; skip",
            toc_pages,
        )
        return hierarchies, TocLinkEnrichStats(
            toc_pages_scanned=toc_pages,
            links_raw=0,
            links_internal=0,
            entries_total=sum(
                len(h.get("toc_with_level") or [])
                for h in hierarchies
                if isinstance(h.get("toc_with_level"), list)
            ),
            entries_matched=0,
            skipped_no_links=True,
        )

    total_entries = 0
    total_matched = 0
    out: list[dict[str, Any]] = []
    for hierarchy in hierarchies:
        entries = hierarchy.get("toc_with_level")
        if not isinstance(entries, list):
            out.append(hierarchy)
            continue
        enriched_entries, matched = match_toc_entries_to_links(entries, links)
        total_entries += len(enriched_entries)
        total_matched += matched
        new_hierarchy = dict(hierarchy)
        new_hierarchy["toc_with_level"] = enriched_entries
        out.append(new_hierarchy)

    stats = TocLinkEnrichStats(
        toc_pages_scanned=toc_pages,
        links_raw=len(links),
        links_internal=len(links),
        entries_total=total_entries,
        entries_matched=total_matched,
        skipped_no_links=False,
    )
    logger.info(
        "[toc_link_enrich] toc_pages={} links={} entries={}/{} matched",
        toc_pages,
        len(links),
        total_matched,
        total_entries,
    )
    return out, stats
