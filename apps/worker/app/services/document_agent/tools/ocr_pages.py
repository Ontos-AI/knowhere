"""OCR specified PDF pages with RapidOCR and persist page text on the blackboard."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import has_page_features, register_tool
from app.services.document_agent.visual import render_pages


def _line_text(item: Any) -> str:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return str(item[1] or "")
    return ""


def _line_box(item: Any) -> Any:
    if isinstance(item, (list, tuple)) and len(item) >= 1:
        return item[0]
    return None


def _line_score(item: Any) -> float:
    if isinstance(item, (list, tuple)) and len(item) >= 3:
        try:
            return float(item[2])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


@register_tool(
    name="ocr.pages",
    description="Run RapidOCR on specified pages and return positioned text lines.",
    parameters={
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {"type": "integer"},
            },
        },
        "required": ["pages"],
    },
    preconditions=(has_page_features,),
)
def ocr_pages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    raw_pages = args.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        return ToolResult(
            status="error",
            error="ocr.pages requires pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    pages = [int(page) for page in raw_pages]
    pngs = render_pages(
        ctx,
        pages,
        folder_name="ocr_pages",
        prefix="ocr",
        timeout=300,
    )
    png_by_page = {
        int(item["page"]): str(item["png_path"])
        for item in pngs
        if item.get("page") is not None and item.get("png_path")
    }

    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    page_texts: dict[int, str] = {}
    page_lines: dict[int, list[dict[str, Any]]] = {}
    for page in pages:
        image_path = png_by_page.get(page)
        lines: list[dict[str, Any]] = []
        if image_path:
            result, _elapse = engine(image_path)
            for item in result or []:
                text = _line_text(item)
                lines.append(
                    {
                        "box": _line_box(item),
                        "text": text,
                        "score": _line_score(item),
                    }
                )
        page_lines[page] = lines
        page_texts[page] = "\n".join(line["text"] for line in lines if line["text"])

    cache = dict(ctx.blackboard.page_full_text_cache)
    cache.update(page_texts)
    ctx.blackboard.page_full_text_cache = cache
    return ToolResult(
        status="ok",
        payload={"page_texts": page_texts, "page_lines": page_lines},
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={"page_count": len(page_texts)},
    )
