"""Full-page structural probing: text pass, then optional asset pass."""

from __future__ import annotations

import gc
import math
import time
from collections import defaultdict
from typing import Any

from app.services.document_agent.manifest import PageFeature, ToolContext, ToolResult
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# Cluster nearby drawing strokes into one figure region (PDF points).
_FIGURE_CLUSTER_GAP = 18.0
# Drop clusters smaller than this many strokes (noise).
_FIGURE_CLUSTER_MIN_PATHS = 3
# Drop aggregated figures smaller than this area (PDF pt²); catches most logos.
_FIGURE_MIN_AREA = 5000.0
# Near-full-page sparse stroke frames are treated as borders, not figures.
_FIGURE_FULLPAGE_AREA_RATIO = 0.92
_FIGURE_FULLPAGE_MAX_PATHS = 25


def _rect_area(rect: Any) -> float:
    width = max(float(getattr(rect, "width", 0.0) or 0.0), 0.0)
    height = max(float(getattr(rect, "height", 0.0) or 0.0), 0.0)
    return width * height


def _clip_bbox(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    clip: Any,
) -> list[float]:
    return [
        round(max(x0, float(clip.x0)), 2),
        round(max(y0, float(clip.y0)), 2),
        round(min(x1, float(clip.x1)), 2),
        round(min(y1, float(clip.y1)), 2),
    ]


def _valid_bbox(box: list[float]) -> bool:
    return len(box) == 4 and box[2] > box[0] and box[3] > box[1]


def _outside_content_band(
    y0: float,
    y1: float,
    *,
    page_h: float,
    header_y: float | None,
    footer_y: float | None,
) -> bool:
    """True if any vertical edge of an aggregated figure leaves the content band.

    ``header_y`` / ``footer_y`` are fractions of page height (top origin, y down).
    """
    if header_y is not None and y0 < header_y * page_h:
        return True
    if footer_y is not None and y1 > footer_y * page_h:
        return True
    return False


def _rects_near(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    gap: float,
) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (
        ax1 + gap < bx0
        or bx1 + gap < ax0
        or ay1 + gap < by0
        or by1 + gap < ay0
    )


def _cluster_indices(rects: list[tuple[float, float, float, float]], gap: float) -> list[list[int]]:
    """Union-find cluster of nearby AABBs via spatial grid (avg ~O(n))."""
    n = len(rects)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    cell = max(gap, 1.0)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)

    def cell_range(
        x0: float, y0: float, x1: float, y1: float
    ) -> tuple[int, int, int, int]:
        return (
            math.floor(x0 / cell),
            math.floor(y0 / cell),
            math.floor(x1 / cell),
            math.floor(y1 / cell),
        )

    for i, (x0, y0, x1, y1) in enumerate(rects):
        ix0, iy0, ix1, iy1 = cell_range(x0, y0, x1, y1)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid[(ix, iy)].append(i)

    for i, (x0, y0, x1, y1) in enumerate(rects):
        ix0, iy0, ix1, iy1 = cell_range(x0 - gap, y0 - gap, x1 + gap, y1 + gap)
        seen: set[int] = set()
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for j in grid.get((ix, iy), ()):
                    if j <= i or j in seen:
                        continue
                    seen.add(j)
                    if _rects_near(rects[i], rects[j], gap):
                        union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())

def _figure_bboxes_from_drawings(
    drawings: list[Any],
    *,
    page_rect: Any,
    page_area: float,
    header_y: float | None,
    footer_y: float | None,
) -> list[dict[str, Any]]:
    """Cluster drawing strokes, then filter aggregated figure bboxes only."""
    page_h = float(getattr(page_rect, "height", 0.0) or 0.0)
    kept_rects: list[tuple[float, float, float, float]] = []
    for drawing in drawings:
        if not isinstance(drawing, dict):
            continue
        rect = drawing.get("rect")
        if rect is None:
            continue
        x0 = float(getattr(rect, "x0", 0.0) or 0.0)
        y0 = float(getattr(rect, "y0", 0.0) or 0.0)
        x1 = float(getattr(rect, "x1", 0.0) or 0.0)
        y1 = float(getattr(rect, "y1", 0.0) or 0.0)
        if x1 <= x0 or y1 <= y0:
            continue
        kept_rects.append((x0, y0, x1, y1))

    if not kept_rects:
        return []

    figures: list[dict[str, Any]] = []
    for idxs in _cluster_indices(kept_rects, _FIGURE_CLUSTER_GAP):
        if len(idxs) < _FIGURE_CLUSTER_MIN_PATHS:
            continue
        xs0 = [kept_rects[i][0] for i in idxs]
        ys0 = [kept_rects[i][1] for i in idxs]
        xs1 = [kept_rects[i][2] for i in idxs]
        ys1 = [kept_rects[i][3] for i in idxs]
        box = _clip_bbox(min(xs0), min(ys0), max(xs1), max(ys1), clip=page_rect)
        if not _valid_bbox(box):
            continue
        area = max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0)
        if area < _FIGURE_MIN_AREA:
            continue
        if _outside_content_band(
            box[1], box[3], page_h=page_h, header_y=header_y, footer_y=footer_y
        ):
            continue
        area_ratio = area / max(page_area, 1.0)
        if (
            area_ratio > _FIGURE_FULLPAGE_AREA_RATIO
            and len(idxs) < _FIGURE_FULLPAGE_MAX_PATHS
        ):
            continue
        figures.append({"kind": "figure", "bbox": box})
    return figures


def _probe_visual_assets(
    page: Any,
    page_area: float,
    *,
    header_y: float | None,
    footer_y: float | None,
) -> dict[str, Any]:
    """Collect counts + coarse asset bboxes from images / tables / drawings."""
    image_area = 0.0
    bboxes: list[dict[str, Any]] = []
    seen_image_rects: set[tuple[float, float, float, float]] = set()
    page_rect = page.rect

    images = page.get_images(full=True) or []
    image_count = len(images)
    for image in images:
        xref = image[0]
        try:
            rects = page.get_image_rects(xref) or []
        except Exception:
            rects = []
        for rect in rects:
            box = _clip_bbox(
                float(getattr(rect, "x0", 0.0) or 0.0),
                float(getattr(rect, "y0", 0.0) or 0.0),
                float(getattr(rect, "x1", 0.0) or 0.0),
                float(getattr(rect, "y1", 0.0) or 0.0),
                clip=page_rect,
            )
            if not _valid_bbox(box):
                continue
            key = (box[0], box[1], box[2], box[3])
            if key in seen_image_rects:
                continue
            seen_image_rects.add(key)
            image_area += max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0)
            bboxes.append({"kind": "image", "bbox": box})

    table_count = 0
    try:
        finder = page.find_tables()
        tables = getattr(finder, "tables", []) or []
        table_count = len(tables)
        for table in tables:
            raw = getattr(table, "bbox", None)
            if raw is None:
                continue
            box = _clip_bbox(
                float(raw[0]),
                float(raw[1]),
                float(raw[2]),
                float(raw[3]),
                clip=page_rect,
            )
            if _valid_bbox(box):
                bboxes.append({"kind": "table", "bbox": box})
    except Exception:
        table_count = 0

    try:
        drawings = page.get_drawings() or []
    except Exception:
        drawings = []
    drawings_count = len(drawings)
    bboxes.extend(
        _figure_bboxes_from_drawings(
            drawings,
            page_rect=page_rect,
            page_area=page_area,
            header_y=header_y,
            footer_y=footer_y,
        )
    )

    coverage = min(image_area / page_area, 1.0) if page_area > 0 else 0.0
    return {
        "image_coverage": round(coverage, 4),
        "image_count": image_count,
        "table_count": table_count,
        "drawings_count": drawings_count,
        "asset_bboxes": bboxes or None,
    }


def _probe_text_one(page: Any, page_number: int) -> dict[str, Any]:
    rect = page.rect
    area = max(_rect_area(rect), 1.0)
    text = page.get_text() or ""
    raw_text_length = len(text.strip())
    orientation = "landscape" if float(rect.width) > float(rect.height) else "portrait"
    return {
        "page": page_number,
        "raw_text_length": raw_text_length,
        "text_density": round(raw_text_length / area * 10000, 4),
        "orientation": orientation,
        "width": round(float(rect.width), 2),
        "height": round(float(rect.height), 2),
        "is_blank_like": raw_text_length < 50,
    }


@worker
def _probe_text_worker(queue, pdf_path: str) -> None:
    import pymupdf  # type: ignore[import]

    features: list[dict[str, Any]] = []
    page_count = 0
    try:
        doc = pymupdf.open(pdf_path)
        page_count = int(doc.page_count)
        for idx in range(page_count):
            features.append(_probe_text_one(doc[idx], idx + 1))
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "page_count": page_count, "features": features})


@worker
def _probe_assets_worker(
    queue,
    pdf_path: str,
    header_y: float | None,
    footer_y: float | None,
) -> None:
    import pymupdf  # type: ignore[import]

    assets: list[dict[str, Any]] = []
    try:
        doc = pymupdf.open(pdf_path)
        for idx in range(int(doc.page_count)):
            page = doc[idx]
            area = max(_rect_area(page.rect), 1.0)
            visual = _probe_visual_assets(
                page, area, header_y=header_y, footer_y=footer_y
            )
            assets.append({"page": idx + 1, **visual})
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "assets": assets})


def probe_page_features(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    """Text-only page probe used before coarse VLM classification."""
    start = time.monotonic()
    try:
        result = run_in_child_process(_probe_text_worker, ctx.pdf_path, timeout=300)
        features = [
            PageFeature(
                page=int(item["page"]),
                raw_text_length=int(item.get("raw_text_length") or 0),
                text_density=float(item.get("text_density") or 0.0),
                image_coverage=0.0,
                image_count=0,
                table_count=0,
                drawings_count=0,
                orientation=str(item.get("orientation") or "portrait"),  # type: ignore[arg-type]
                width=float(item.get("width") or 0.0),
                height=float(item.get("height") or 0.0),
                has_asset=False,
                is_blank_like=bool(item.get("is_blank_like")),
                asset_bboxes=None,
            )
            for item in (result.get("features") or [])
        ]
        ctx.blackboard.page_features = sorted(features, key=lambda f: f.page)
        ctx.blackboard.page_count = int(result.get("page_count") or len(features))
        ctx.blackboard.global_signals["total_pages"] = ctx.blackboard.page_count
        ctx.blackboard.global_signals["assets_probed"] = False
        logger.info("[document_agent] probed text on {} pages", ctx.blackboard.page_count)
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


def probe_page_assets(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    """Asset coarse extraction after coarse VLM; drawings honor margin band."""
    start = time.monotonic()
    if not ctx.blackboard.page_features:
        return ToolResult(
            status="error",
            error="page_features missing; run text probe first",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    profile = ctx.blackboard.document_profile
    header_y = getattr(profile, "header_y", None) if profile else None
    footer_y = getattr(profile, "footer_y", None) if profile else None
    try:
        result = run_in_child_process(
            _probe_assets_worker,
            ctx.pdf_path,
            header_y,
            footer_y,
            timeout=300,
        )
        by_page = {
            int(item["page"]): item for item in (result.get("assets") or [])
        }
        updated: list[PageFeature] = []
        for feature in ctx.blackboard.page_features:
            visual = by_page.get(feature.page) or {}
            has_asset = visual.get("asset_bboxes") is not None
            updated.append(
                PageFeature(
                    page=feature.page,
                    raw_text_length=feature.raw_text_length,
                    text_density=feature.text_density,
                    image_coverage=float(visual.get("image_coverage") or 0.0),
                    image_count=int(visual.get("image_count") or 0),
                    table_count=int(visual.get("table_count") or 0),
                    drawings_count=int(visual.get("drawings_count") or 0),
                    orientation=feature.orientation,
                    width=feature.width,
                    height=feature.height,
                    has_asset=has_asset,
                    is_blank_like=feature.raw_text_length < 50 and not has_asset,
                    asset_bboxes=(
                        list(visual["asset_bboxes"])
                        if isinstance(visual.get("asset_bboxes"), list)
                        else None
                    ),
                )
            )
        ctx.blackboard.page_features = sorted(updated, key=lambda f: f.page)
        ctx.blackboard.global_signals["assets_probed"] = True
        ctx.blackboard.global_signals["content_margins"] = {
            "header_y": header_y,
            "footer_y": footer_y,
        }
        logger.info(
            "[document_agent] probed assets on {} pages (header_y={}, footer_y={})",
            ctx.blackboard.page_count,
            header_y,
            footer_y,
        )
        return ToolResult(
            status="ok",
            payload={
                "page_count": ctx.blackboard.page_count,
                "header_y": header_y,
                "footer_y": footer_y,
            },
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return ToolResult(
            status="error",
            error=str(exc),
            latency_ms=int((time.monotonic() - start) * 1000),
        )
