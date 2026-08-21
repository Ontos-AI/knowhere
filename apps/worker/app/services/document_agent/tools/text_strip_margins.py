"""Temporary search-view strip of stored header/footer page bands."""

from __future__ import annotations

import time
from typing import Any, Literal

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.pdf_text import (
    page_bands_map,
    page_content_map,
    strip_margin_text,
)
from app.services.document_agent.registry import (
    has_page_features,
    has_page_full_text,
    not_is_scanned,
    register_tool,
)


def _resolve_page_range(
    ctx: ToolContext, args: dict[str, Any]
) -> tuple[int, int] | ToolResult:
    start_page = max(1, int(args.get("start_page") or 1))
    end_page = int(args.get("end_page") or ctx.blackboard.page_count or 0)
    if end_page < start_page:
        return ToolResult(
            status="error",
            error="end_page must be >= start_page",
            latency_ms=0,
        )
    return start_page, end_page


def _apply_strip(
    ctx: ToolContext,
    *,
    which: Literal["header", "footer"],
    start_page: int,
    end_page: int,
) -> dict[str, Any]:
    bands = page_bands_map(ctx.blackboard.page_full_text_cache)
    view = ctx.blackboard.page_text_search_view
    if view is None:
        view = page_content_map(bands)
    else:
        view = dict(view)

    pages_updated = 0
    for page in range(start_page, end_page + 1):
        record = bands.get(page)
        if record is None:
            continue
        margin = record.header if which == "header" else record.footer
        before = view.get(page, record.content)
        after = strip_margin_text(before, margin)
        view[page] = after
        if after != before:
            pages_updated += 1

    ctx.blackboard.page_text_search_view = view
    return {
        "strip": which,
        "start_page": start_page,
        "end_page": end_page,
        "pages_updated": pages_updated,
        "view_active": True,
    }


def _strip_tool(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    which: Literal["header", "footer"],
) -> ToolResult:
    start = time.monotonic()
    resolved = _resolve_page_range(ctx, args)
    if isinstance(resolved, ToolResult):
        resolved.latency_ms = int((time.monotonic() - start) * 1000)
        return resolved
    start_page, end_page = resolved
    payload = _apply_strip(
        ctx, which=which, start_page=start_page, end_page=end_page
    )
    return ToolResult(
        status="ok",
        payload=payload,
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "strip": which,
            "pages_updated": payload["pages_updated"],
        },
    )


_STRIP_PARAMS = {
    "type": "object",
    "properties": {
        "start_page": {"type": "integer"},
        "end_page": {"type": "integer"},
    },
}


@register_tool(
    name="text.strip_header",
    description=(
        "Temporarily remove stored header-band text from the grep search view "
        "for the given page range. Does not mutate page_full_text_cache. "
        "Subsequent grep.text uses the stripped view until cleared."
    ),
    parameters=_STRIP_PARAMS,
    preconditions=(has_page_features, has_page_full_text, not_is_scanned),
)
def strip_header(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _strip_tool(ctx, args, which="header")


@register_tool(
    name="text.strip_footer",
    description=(
        "Temporarily remove stored footer-band text from the grep search view "
        "for the given page range. Does not mutate page_full_text_cache. "
        "Subsequent grep.text uses the stripped view until cleared."
    ),
    parameters=_STRIP_PARAMS,
    preconditions=(has_page_features, has_page_full_text, not_is_scanned),
)
def strip_footer(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _strip_tool(ctx, args, which="footer")
