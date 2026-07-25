"""Aggregate page-feature statistics for VLM profile planning."""

from __future__ import annotations

import statistics
import time
from typing import Any

from app.services.document_agent.manifest import PageFeature, ToolContext, ToolResult

# Coarse sampling extrema are text-only. Low text / density already proxy
# chart-heavy or asset-heavy pages; table/drawing counts are not used to
# nominate extrema pages for VLM coarse classification.
PROFILE_METRICS = (
    "raw_text_length",
    "text_density",
)

EXTREMA_ROLES = {
    "raw_text_length": ("min", "max"),
    "text_density": ("min", "max"),
}

EXTREMA_LABELS = {
    "raw_text_length": "text_length",
    "text_density": "text_density",
}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _metric_value(feature: PageFeature, metric: str) -> float:
    return float(getattr(feature, metric))


def aggregate_doc_stats(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    features = list(ctx.blackboard.page_features)
    stats: dict[str, Any] = {}
    page_count = len(features)
    extrema_pages: list[int] = []
    extrema_samples: list[dict[str, Any]] = []
    for metric in PROFILE_METRICS:
        pairs = [(feature.page, _metric_value(feature, metric)) for feature in features]
        values = [value for _, value in pairs]
        if not pairs:
            stats[metric] = {
                "mean": 0.0,
                "p50": 0.0,
                "p90": 0.0,
                "min": {"page": None, "value": 0.0},
                "max": {"page": None, "value": 0.0},
            }
            continue
        min_page, min_value = min(pairs, key=lambda item: (item[1], item[0]))
        max_page, max_value = max(pairs, key=lambda item: (item[1], -item[0]))
        stats[metric] = {
            "mean": round(statistics.fmean(values), 4),
            "p50": round(_percentile(values, 0.5), 4),
            "p90": round(_percentile(values, 0.9), 4),
            "min": {"page": min_page, "value": round(min_value, 4)},
            "max": {"page": max_page, "value": round(max_value, 4)},
        }
        extrema_by_role = {
            "min": (min_page, min_value),
            "max": (max_page, max_value),
        }
        for role in EXTREMA_ROLES[metric]:
            page, value = extrema_by_role[role]
            extrema_pages.append(page)
            extrema_samples.append(
                {
                    "page": page,
                    "metric": metric,
                    "label": EXTREMA_LABELS[metric],
                    "role": role,
                    "value": round(value, 4),
                }
            )

    deduped_extrema = sorted(set(extrema_pages))
    landscape_pages = sum(1 for feature in features if feature.orientation == "landscape")
    doc_shape: dict[str, Any] = {
        "page_count": page_count,
        "landscape_pages": landscape_pages,
        "landscape_ratio": round(landscape_pages / page_count, 4)
        if page_count
        else 0.0,
    }
    if ctx.blackboard.global_signals.get("assets_probed"):
        scan_like_pages = sum(
            1
            for feature in features
            if feature.raw_text_length < 50 and feature.image_coverage >= 0.5
        )
        asset_pages = sum(1 for feature in features if feature.has_asset)
        doc_shape.update(
            {
                "scan_like_pages": scan_like_pages,
                "scan_like_ratio": round(scan_like_pages / page_count, 4)
                if page_count
                else 0.0,
                "asset_pages": asset_pages,
                "asset_ratio": round(asset_pages / page_count, 4) if page_count else 0.0,
            }
        )
    ctx.blackboard.doc_stats = stats
    ctx.blackboard.extrema_pages = deduped_extrema
    ctx.blackboard.global_signals["doc_stats"] = stats
    ctx.blackboard.global_signals["doc_shape"] = doc_shape
    ctx.blackboard.global_signals["extrema_pages"] = deduped_extrema
    ctx.blackboard.global_signals["extrema_samples"] = extrema_samples
    return ToolResult(
        status="ok",
        payload={
            "metric_count": len(PROFILE_METRICS),
            "extrema_pages": deduped_extrema,
            "extrema_samples": extrema_samples,
            "doc_shape": doc_shape,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "doc_stats": stats,
            "doc_shape": doc_shape,
            "extrema_pages": deduped_extrema,
            "extrema_samples": extrema_samples,
        },
    )


__all__ = ["PROFILE_METRICS", "aggregate_doc_stats"]
