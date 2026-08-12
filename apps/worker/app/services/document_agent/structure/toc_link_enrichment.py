"""Optional TOC hyperlink enrichment after VLM title extraction.

Only runs when TOC pages actually contain internal links. A link is attached to
a ``toc_with_level`` entry only when anchor text character-matches the extracted
heading. Unmatched entries are left unchanged (no ``link`` field).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from loguru import logger

_APOSTROPHE_TRANS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u00b4": "'",
        "\u0060": "'",
    }
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_toc_heading(text: str) -> str:
    """Normalize heading / anchor text for strict character matching."""
    raw = unicodedata.normalize("NFKC", str(text or "")).translate(_APOSTROPHE_TRANS)
    raw = raw.replace("\xa0", " ").strip().lower()
    return _NON_ALNUM.sub(" ", raw).strip()


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


def collect_toc_page_links(pdf_path: str, toc_pages: list[int]) -> list[TocPageLink]:
    """Collect internal goto links on TOC pages with nearby anchor text."""
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
                # PyMuPDF: LINK_GOTO=1, LINK_NAMED=4 commonly used for TOC.
                kind = link.get("kind")
                dest_idx = link.get("page")
                if dest_idx is None:
                    continue
                try:
                    dest_physical = int(dest_idx) + 1
                except (TypeError, ValueError):
                    continue
                if dest_physical < 1 or dest_physical > doc.page_count:
                    continue
                # Skip obvious self / header "back to TOC" loops to same/near page.
                if abs(dest_physical - toc_page) <= 1:
                    continue
                rect = link.get("from")
                if rect is None:
                    continue
                anchor = _anchor_text_for_rect(page, rect)
                if not anchor:
                    continue
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


def _is_page_number_label(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if t.isdigit():
        return True
    # Roman / folio labels: iv, xii, F-1
    if re.fullmatch(r"[ivxlcdm]+", t.lower()):
        return True
    if re.fullmatch(r"[A-Za-z]-?\d+", t):
        return True
    return False


def _headings_match(heading: str, anchor: str) -> bool:
    h = normalize_toc_heading(heading)
    a = normalize_toc_heading(anchor)
    if not h or not a:
        return False
    if h == a:
        return True
    # Anchor sometimes truncates long titles; require substantial prefix/containment.
    if len(h) >= 12 and (a.startswith(h) or h.startswith(a)):
        shorter, longer = (a, h) if len(a) <= len(h) else (h, a)
        if len(shorter) >= 12 and shorter in longer:
            return True
    return False


def match_toc_entries_to_links(
    entries: list[dict[str, Any]],
    links: list[TocPageLink],
) -> tuple[list[dict[str, Any]], int]:
    """Return new entry dicts; only matched ones gain a ``link`` object."""
    # Index title-like anchors (skip pure page-number chips).
    title_links = [
        link
        for link in links
        if not _is_page_number_label(link.anchor_text)
        and normalize_toc_heading(link.anchor_text)
        and normalize_toc_heading(link.anchor_text) not in {"table of contents", "contents"}
    ]

    matched = 0
    enriched: list[dict[str, Any]] = []
    used_dest_for_heading: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        new_entry = {
            "heading": entry.get("heading"),
            "level": entry.get("level"),
            "page_number": entry.get("page_number"),
        }
        # Preserve unknown keys except stale link from a prior run.
        for key, value in entry.items():
            if key in new_entry or key == "link":
                continue
            new_entry[key] = value

        heading = str(entry.get("heading") or "").strip()
        if not heading or not title_links:
            enriched.append(new_entry)
            continue

        hits = [link for link in title_links if _headings_match(heading, link.anchor_text)]
        if not hits:
            enriched.append(new_entry)
            continue

        # Prefer unique dest; if multiple dests, refuse (ambiguous).
        dests = {link.dest_physical_page for link in hits}
        if len(dests) != 1:
            logger.info(
                "[toc_link_enrich] ambiguous link for heading={!r} dests={}",
                heading,
                sorted(dests),
            )
            enriched.append(new_entry)
            continue

        chosen = hits[0]
        heading_key = normalize_toc_heading(heading)
        # One heading → one link attachment (first wins if duplicates).
        if heading_key in used_dest_for_heading:
            enriched.append(new_entry)
            continue
        used_dest_for_heading.add(heading_key)

        new_entry["link"] = {
            "physical_page": chosen.dest_physical_page,
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
