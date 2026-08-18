"""probe.links: collect internal page hyperlinks with noise markers."""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import has_page_features, register_tool
from app.services.document_agent.structure.page_links import (
    PageLink,
    collect_page_links,
)

_PURE_PAGE_ANCHOR = re.compile(
    r"^("
    r"[\d\s.\-–—/]+|"
    r"第?\s*\d+\s*[页頁]|"
    r"p\.?\s*\d+"
    r")$",
    re.IGNORECASE,
)


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
