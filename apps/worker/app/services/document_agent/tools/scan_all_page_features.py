"""scan_all_page_features — full-page structural feature extraction.

Performs a **full-page traversal** (not sampling) of a PDF, extracting
structural features for every page.  Runs inside an isolated PyMuPDF child
process to ensure memory is freed after extraction.

For a 200-page A4 PDF the child process typically completes in < 8 s.
"""

from __future__ import annotations

import gc
import statistics
from typing import Any

from app.services.document_agent.page_map import PageFeature
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger


# ── Low-level helpers ─────────────────────────────────────────────────────────


def _rect_area(rect: Any) -> float:
    w = max(float(getattr(rect, "width", 0.0) or 0.0), 0.0)
    h = max(float(getattr(rect, "height", 0.0) or 0.0), 0.0)
    return w * h


def _measure_image_coverage(page: Any, page_area: float) -> tuple[float, int]:
    if page_area <= 0:
        return 0.0, 0
    image_area = 0.0
    images = page.get_images(full=True) or []
    seen_rects: set[tuple] = set()
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


def _table_count(page: Any) -> int:
    try:
        finder = page.find_tables()
        return len(getattr(finder, "tables", []) or [])
    except Exception:
        return 0


def _extract_single_page_feature(page: Any, page_index: int) -> dict[str, Any]:
    """Extract features for one page; returns a plain dict for IPC."""
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
        "page": page_index + 1,          # 1-based
        "text_length": text_len,
        "text_density": round(text_density, 4),
        "image_coverage": round(image_coverage, 4),
        "image_count": image_count,
        "table_count": table_count,
        "drawings_count": drawings_count,
        "orientation": orientation,
        "width": round(float(rect.width), 2),
        "height": round(float(rect.height), 2),
        "is_blank_like": is_blank_like,
        "text_preview": " ".join(text.split())[:300],
    }


# ── Child-process worker ──────────────────────────────────────────────────────


@worker
def _scan_all_pages_worker(
    queue,
    pdf_path: str,
    page_start: int,  # 0-based inclusive
    page_end: int,    # 0-based inclusive (-1 = all)
) -> None:
    """Scan all pages (or a subrange) and put feature list onto the queue."""
    import pymupdf  # type: ignore[import]

    features: list[dict[str, Any]] = []
    page_count = 0

    try:
        doc = pymupdf.open(pdf_path)
        page_count = int(doc.page_count)
        end = page_count - 1 if page_end < 0 else min(page_end, page_count - 1)
        start = max(0, page_start)

        for idx in range(start, end + 1):
            try:
                feat = _extract_single_page_feature(doc[idx], idx)
                features.append(feat)
            except Exception as exc:
                # Log and continue; partial feature is better than crash
                features.append(
                    {
                        "page": idx + 1,
                        "text_length": 0,
                        "text_density": 0.0,
                        "image_coverage": 0.0,
                        "image_count": 0,
                        "table_count": 0,
                        "drawings_count": 0,
                        "orientation": "portrait",
                        "width": 0.0,
                        "height": 0.0,
                        "is_blank_like": True,
                        "text_preview": "",
                        "_error": str(exc),
                    }
                )
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()

    queue.put({"ok": True, "page_count": page_count, "features": features})


# ── Public API ────────────────────────────────────────────────────────────────


def scan_all_page_features(
    pdf_path: str,
    *,
    page_start: int = 0,
    page_end: int = -1,
    timeout: int = 300,
) -> list[PageFeature]:
    """Return a ``PageFeature`` for every page in the PDF (or a subrange).

    Args:
        pdf_path:   Absolute path to the PDF file.
        page_start: 0-based index of first page to scan (default 0 = first).
        page_end:   0-based index of last page to scan, inclusive
                    (default -1 = last page).
        timeout:    Child process timeout in seconds.  Allow 300 s for large docs.

    Returns:
        Ordered list of ``PageFeature``, one per page, sorted by page number.
        Never raises; returns empty list on subprocess failure.
    """
    try:
        result = run_in_child_process(
            _scan_all_pages_worker,
            pdf_path,
            page_start,
            page_end,
            timeout=timeout,
        )
    except Exception as exc:
        logger.error(f"[scan_all_page_features] subprocess failed: {exc}")
        return []

    raw_features: list[dict] = result.get("features") or []
    page_features: list[PageFeature] = []

    # Compute median page dimensions for landscape anomaly detection
    widths = [f["width"] for f in raw_features if f.get("width", 0) > 0]
    heights = [f["height"] for f in raw_features if f.get("height", 0) > 0]
    median_w = statistics.median(widths) if widths else 595.0  # A4 width in pt
    median_h = statistics.median(heights) if heights else 842.0

    for raw in raw_features:
        page_num = int(raw.get("page") or 0)
        if page_num <= 0:
            continue

        # Refine orientation: a "portrait" page that is much wider than the
        # document median is flagged as a layout anomaly (landscape-rotated page
        # that PyMuPDF may not detect via width > height alone).
        orientation = raw.get("orientation", "portrait")
        w, h = float(raw.get("width") or 0), float(raw.get("height") or 0)
        if orientation == "portrait" and median_h > 0 and w > 0:
            # If this page's aspect ratio deviates by > 40% vs median, flag it
            doc_ratio = median_w / median_h
            page_ratio = w / h if h > 0 else 1.0
            if page_ratio > doc_ratio * 1.4:
                orientation = "landscape"

        page_features.append(
            PageFeature(
                page=page_num,
                text_length=int(raw.get("text_length") or 0),
                text_density=float(raw.get("text_density") or 0.0),
                image_coverage=float(raw.get("image_coverage") or 0.0),
                image_count=int(raw.get("image_count") or 0),
                table_count=int(raw.get("table_count") or 0),
                drawings_count=int(raw.get("drawings_count") or 0),
                orientation=orientation,
                width=w,
                height=float(raw.get("height") or 0.0),
                is_blank_like=bool(raw.get("is_blank_like")),
                text_preview=str(raw.get("text_preview") or "")[:300],
                embedding_ref=None,
            )
        )

    page_features.sort(key=lambda pf: pf.page)
    logger.info(
        f"[scan_all_page_features] scanned {len(page_features)} pages "
        f"from '{pdf_path}'"
    )
    return page_features
