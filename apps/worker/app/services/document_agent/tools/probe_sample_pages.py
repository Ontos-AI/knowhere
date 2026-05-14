"""Feature-page sampling for the Phase 1 split agent."""

from __future__ import annotations

import gc
import statistics
from typing import Any, Literal

from app.services.document_parser.pymupdf_subprocess import run_in_child_process, worker

SampleStrategy = Literal["stratified", "uniform", "key_pages"]


def _choose_sample_indices(
    page_count: int,
    *,
    strategy: SampleStrategy = "stratified",
    max_samples: int = 25,
) -> list[int]:
    if page_count <= 0:
        return []
    if max_samples <= 0:
        return []
    if page_count <= max_samples:
        return list(range(page_count))

    if strategy == "key_pages":
        candidates = [0, 1, 2, 3, 4, page_count - 5, page_count - 4, page_count - 3, page_count - 2, page_count - 1]
        return sorted({idx for idx in candidates if 0 <= idx < page_count})[:max_samples]

    if strategy == "uniform":
        if max_samples == 1:
            return [0]
        return sorted(
            {
                round(i * (page_count - 1) / (max_samples - 1))
                for i in range(max_samples)
            }
        )

    edge_each = min(5, max_samples // 3)
    edge_indices = list(range(edge_each)) + list(range(page_count - edge_each, page_count))
    remaining = max_samples - len(set(edge_indices))
    middle_start = edge_each
    middle_end = page_count - edge_each - 1
    middle_indices: list[int] = []
    if remaining > 0 and middle_start <= middle_end:
        if remaining == 1:
            middle_indices = [(middle_start + middle_end) // 2]
        else:
            middle_indices = [
                round(middle_start + i * (middle_end - middle_start) / (remaining - 1))
                for i in range(remaining)
            ]
    return sorted({idx for idx in edge_indices + middle_indices if 0 <= idx < page_count})


def _rect_area(rect: Any) -> float:
    return max(float(getattr(rect, "width", 0.0) or 0.0), 0.0) * max(
        float(getattr(rect, "height", 0.0) or 0.0),
        0.0,
    )


def _measure_image_coverage(page: Any, page_area: float) -> tuple[float, int]:
    if page_area <= 0:
        return 0.0, 0
    image_area = 0.0
    images = page.get_images(full=True) or []
    seen_rects: set[tuple[float, float, float, float]] = set()
    for image in images:
        if not image:
            continue
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
            if key in seen_rects:
                continue
            seen_rects.add(key)
            image_area += _rect_area(rect)
    return min(image_area / page_area, 1.0), len(images)


def _font_stats(page: Any) -> dict[str, float | int]:
    sizes: list[float] = []
    try:
        text_dict = page.get_text("dict") or {}
    except Exception:
        text_dict = {}
    for block in text_dict.get("blocks", []) or []:
        for line in block.get("lines", []) or []:
            for span in line.get("spans", []) or []:
                size = float(span.get("size") or 0.0)
                if size > 0:
                    sizes.append(size)
    if not sizes:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0, "count": 0}
    return {
        "min": min(sizes),
        "max": max(sizes),
        "mean": statistics.fmean(sizes),
        "median": statistics.median(sizes),
        "count": len(sizes),
    }


def _table_count(page: Any) -> int:
    try:
        finder = page.find_tables()
        return len(getattr(finder, "tables", []) or [])
    except Exception:
        return 0


def _extract_page_features(page: Any, page_index: int) -> dict[str, Any]:
    rect = page.rect
    page_area = max(_rect_area(rect), 1.0)
    text = page.get_text() or ""
    text_len = len(text.strip())
    image_coverage, image_count = _measure_image_coverage(page, page_area)
    try:
        drawings_count = len(page.get_drawings() or [])
    except Exception:
        drawings_count = 0
    orientation = "landscape" if float(rect.width) > float(rect.height) else "portrait"
    text_density = text_len / page_area * 10000
    table_count = _table_count(page)
    is_blank_like = text_len < 20 and image_coverage < 0.02 and drawings_count < 5

    return {
        "page_index": page_index,
        "page_number": page_index + 1,
        "width": float(rect.width),
        "height": float(rect.height),
        "orientation": orientation,
        "text_length": text_len,
        "text_density": round(text_density, 4),
        "image_count": image_count,
        "image_coverage": round(image_coverage, 4),
        "table_count": table_count,
        "drawings_count": drawings_count,
        "font_size_stats": _font_stats(page),
        "is_blank_like": is_blank_like,
        "text_preview": " ".join(text.split())[:500],
    }


@worker
def _sample_pages_worker(
    queue,
    pdf_path: str,
    strategy: str,
    max_samples: int,
) -> None:
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        page_count = int(doc.page_count)
        indices = _choose_sample_indices(
            page_count,
            strategy=strategy if strategy in {"stratified", "uniform", "key_pages"} else "stratified",
            max_samples=max_samples,
        )
        sampled_pages = [_extract_page_features(doc[idx], idx) for idx in indices]
    finally:
        doc.close()
        gc.collect()

    queue.put(
        {
            "ok": True,
            "page_count": page_count,
            "sample_indices": indices,
            "sampled_pages": sampled_pages,
        }
    )


def sample_pages(
    pdf_path: str,
    *,
    strategy: SampleStrategy = "stratified",
    max_samples: int = 25,
    timeout: int = 120,
) -> dict[str, Any]:
    """Sample structural page features in an isolated PyMuPDF child process."""
    result = run_in_child_process(
        _sample_pages_worker,
        pdf_path,
        strategy,
        max_samples,
        timeout=timeout,
    )
    return {
        "page_count": int(result.get("page_count") or 0),
        "sample_indices": list(result.get("sample_indices") or []),
        "sampled_pages": list(result.get("sampled_pages") or []),
    }
