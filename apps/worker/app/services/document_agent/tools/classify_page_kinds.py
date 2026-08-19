"""Rule-based page kind classification."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from app.services.document_agent.manifest import PageFeature, PageLabel, ToolContext, ToolResult


def _label_feature(feature: PageFeature) -> PageLabel:
    page = feature.page
    if feature.orientation == "landscape":
        return PageLabel(
            page=page,
            kind="landscape",
            evidence={
                "source": "geometry",
                "rule": "width_gt_height",
                "width": feature.width,
                "height": feature.height,
            },
        )
    return PageLabel(
        page=page,
        kind="normal",
        evidence={"source": "geometry", "rule": "width_le_height"},
    )


def classify_page_kinds(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    labels = [_label_feature(feature) for feature in ctx.blackboard.page_features]
    ctx.blackboard.page_labels = labels
    counts = Counter(label.kind for label in labels)
    ctx.blackboard.global_signals["page_kind_counts"] = counts
    features_by_page = {feature.page: feature for feature in ctx.blackboard.page_features}
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for label in labels:
        if len(samples[label.kind]) >= 8:
            continue
        feature = features_by_page.get(label.page)
        samples[label.kind].append(
            {
                "page": label.page,
                "evidence": label.evidence,
                "raw_text_length": feature.raw_text_length if feature else None,
            }
        )
    return ToolResult(
        status="ok",
        payload={"page_kind_counts": dict(counts)},
        latency_ms=int((time.monotonic() - start) * 1000),
        input_summary={"page_count": ctx.blackboard.page_count},
        output_summary={
            "page_kind_counts": dict(counts),
            "sample_pages_by_kind": dict(samples),
        },
    )
