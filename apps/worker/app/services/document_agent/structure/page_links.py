"""PDF internal page-link helpers for ``probe.links``.

Reads ``page.get_links()`` only. Does not attach or enrich TOC hierarchies;
TOC extraction stays on the VLM / keyword path.

Page-number convention (all 1-based after normalize):
  - ``get_links()`` dest ``page``: ``int`` is 0-based (+1); digit ``str`` is
    already 1-based (unresolved URI parse).
  - Bookmark outline (``doc.get_toc()``) is handled by ``probe.outline``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PageLink:
    source_page: int  # 1-based
    dest_physical_page: int  # 1-based
    anchor_text: str
    kind: int | None = None
    from_y0: float | None = None
    page_height: float | None = None


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

    PyMuPDF exposes two shapes for the same field:
      - ``int``: resolved name-tree / GOTO path → 0-based → add 1
      - digit ``str``: unresolved URI parse (``uri_to_dict``) → already 1-based
    """
    if isinstance(raw_page, int):
        return raw_page + 1
    return int(raw_page)


def collect_page_links(pdf_path: str, pages: list[int]) -> list[PageLink]:
    """Collect internal page hyperlinks on the given pages with nearby anchor text."""
    import fitz

    if not pages:
        return []

    out: list[PageLink] = []
    doc = fitz.open(pdf_path)
    try:
        for source_page in pages:
            if source_page < 1 or source_page > doc.page_count:
                continue
            page = doc[source_page - 1]
            page_height = float(page.rect.height) if page.rect is not None else None
            for link in page.get_links() or []:
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
                from_y0 = float(rect.y0) if hasattr(rect, "y0") else float(rect[1])
                out.append(
                    PageLink(
                        source_page=source_page,
                        dest_physical_page=dest_physical,
                        anchor_text=anchor,
                        kind=int(kind) if kind is not None else None,
                        from_y0=from_y0,
                        page_height=page_height,
                    )
                )
    finally:
        doc.close()
    return out
