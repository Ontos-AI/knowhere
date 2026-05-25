"""Full-page structural probing."""

from __future__ import annotations

import gc
import time
from typing import Any

from app.services.document_agent.manifest import PageFeature, ToolContext, ToolResult
from app.services.document_agent.pdf_text import top_lines
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger


def _rect_area(rect: Any) -> float:
    width = max(float(getattr(rect, "width", 0.0) or 0.0), 0.0)
    height = max(float(getattr(rect, "height", 0.0) or 0.0), 0.0)
    return width * height


def _image_coverage(page: Any, page_area: float) -> tuple[float, int]:
    if page_area <= 0:
        return 0.0, 0
    area = 0.0
    images = page.get_images(full=True) or []
    seen: set[tuple[float, float, float, float]] = set()
    for image in images:
        xref = image[0]
        try:
            rects = page.get_image_rects(xref) or []
        except Exception:
            rects = []
        for rect in rects:
            key = (
                round(float(getattr(rect, "x0", 0.0) or 0.0), 2),
                round(float(getattr(rect, "y0", 0.0) or 0.0), 2),
                round(float(getattr(rect, "x1", 0.0) or 0.0), 2),
                round(float(getattr(rect, "y1", 0.0) or 0.0), 2),
            )
            if key in seen:
                continue
            seen.add(key)
            area += _rect_area(rect)
    return min(area / page_area, 1.0), len(images)


def _table_count(page: Any) -> int:
    try:
        finder = page.find_tables()
        return len(getattr(finder, "tables", []) or [])
    except Exception:
        return 0


def _probe_one(page: Any, page_number: int) -> dict[str, Any]:
    rect = page.rect
    area = max(_rect_area(rect), 1.0)
    text = page.get_text() or ""
    raw_text_length = len(text.strip())
    image_coverage, image_count = _image_coverage(page, area)
    try:
        drawings_count = len(page.get_drawings() or [])
    except Exception:
        drawings_count = 0
    orientation = "landscape" if float(rect.width) > float(rect.height) else "portrait"
    return {
        "page": page_number,
        "raw_text_length": raw_text_length,
        "text_density": round(raw_text_length / area * 10000, 4),
        "image_coverage": round(image_coverage, 4),
        "image_count": image_count,
        "table_count": _table_count(page),
        "drawings_count": drawings_count,
        "orientation": orientation,
        "width": round(float(rect.width), 2),
        "height": round(float(rect.height), 2),
        "is_blank_like": raw_text_length < 20 and image_coverage < 0.02 and drawings_count < 5,
        "text_lines_preview": top_lines(text, max_lines=30),
    }


@worker
def _probe_worker(queue, pdf_path: str) -> None:
    import pymupdf  # type: ignore[import]

    features: list[dict[str, Any]] = []
    page_count = 0
    try:
        doc = pymupdf.open(pdf_path)
        page_count = int(doc.page_count)
        for idx in range(page_count):
            features.append(_probe_one(doc[idx], idx + 1))
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "page_count": page_count, "features": features})


def probe_page_features(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    try:
        result = run_in_child_process(_probe_worker, ctx.pdf_path, timeout=300)
        features = [
            PageFeature(
                page=int(item["page"]),
                raw_text_length=int(item.get("raw_text_length") or 0),
                text_density=float(item.get("text_density") or 0.0),
                image_coverage=float(item.get("image_coverage") or 0.0),
                image_count=int(item.get("image_count") or 0),
                table_count=int(item.get("table_count") or 0),
                drawings_count=int(item.get("drawings_count") or 0),
                orientation=str(item.get("orientation") or "portrait"),  # type: ignore[arg-type]
                width=float(item.get("width") or 0.0),
                height=float(item.get("height") or 0.0),
                is_blank_like=bool(item.get("is_blank_like")),
                text_lines_preview=list(item.get("text_lines_preview") or []),
            )
            for item in (result.get("features") or [])
        ]
        ctx.blackboard.page_features = sorted(features, key=lambda f: f.page)
        ctx.blackboard.page_count = int(result.get("page_count") or len(features))
        ctx.blackboard.global_signals["total_pages"] = ctx.blackboard.page_count
        logger.info("[document_agent] probed {} pages", ctx.blackboard.page_count)
        return ToolResult(
            status="ok",
            payload={"page_count": ctx.blackboard.page_count},
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return ToolResult(
            status="error",
            error=str(exc),
            latency_ms=int((time.monotonic() - start) * 1000),
        )
