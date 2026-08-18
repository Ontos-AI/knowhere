"""probe.links: collect internal page hyperlinks with noise markers.

Reads ``page.get_links()`` only. Does not attach or enrich TOC hierarchies.

Page-number convention (all 1-based after normalize):
  - ``get_links()`` dest ``page``: ``int`` is 0-based (+1); digit ``str`` is
    already 1-based (unresolved URI parse).
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import has_page_features, register_tool

_PURE_PAGE_ANCHOR = re.compile(
    r"^("
    r"[\d\s.\-–—/]+|"
    r"第?\s*\d+\s*[页頁]|"
    r"p\.?\s*\d+"
    r")$",
    re.IGNORECASE,
)


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


def _is_pure_page_anchor(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return True
    return bool(_PURE_PAGE_ANCHOR.match(cleaned))


def _is_header_zone(link: PageLink) -> bool:
    if link.from_y0 is None or link.page_height is None or link.page_height <= 0:
        return False
    return float(link.from_y0) <= float(link.page_height) * 0.12


def annotate_link_noise(links: list[PageLink]) -> list[dict[str, Any]]:
    """Mark pure page-number anchors, header-zone links, and repeated destinations."""
    dest_counts = Counter(link.dest_physical_page for link in links)
    out: list[dict[str, Any]] = []
    for link in links:
        noise: list[str] = []
        if _is_pure_page_anchor(link.anchor_text):
            noise.append("pure_page_number")
        if _is_header_zone(link):
            noise.append("header_zone")
        if dest_counts[link.dest_physical_page] >= 3:
            noise.append("repeated_dest")
        out.append(
            {
                "source_page": link.source_page,
                "dest_physical_page": link.dest_physical_page,
                "anchor_text": link.anchor_text,
                "kind": link.kind,
                "noise": noise,
                "is_noise": bool(noise),
            }
        )
    return out


@register_tool(
    name="probe.links",
    description=(
        "Collect internal PDF page hyperlinks on the given pages. "
        "Returns anchor text, source page, destination physical page, kind, and noise flags."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "1-based physical pages to scan for links",
            },
        },
        "required": ["pages"],
    },
    preconditions=(has_page_features,),
)
def probe_links(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    raw_pages = args.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        return ToolResult(
            status="error",
            error="probe.links requires pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    pages: list[int] = []
    for item in raw_pages:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if page not in pages:
            pages.append(page)
    if not pages:
        return ToolResult(
            status="error",
            error="probe.links requires valid pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    links = collect_page_links(ctx.pdf_path, pages)
    annotated = annotate_link_noise(links)
    noise_count = sum(1 for item in annotated if item["is_noise"])
    return ToolResult(
        status="ok",
        payload={
            "source": "pdf_links",
            "pages": pages,
            "links": annotated,
            "link_count": len(annotated),
            "noise_count": noise_count,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "pages": pages,
            "link_count": len(annotated),
            "noise_count": noise_count,
        },
    )
