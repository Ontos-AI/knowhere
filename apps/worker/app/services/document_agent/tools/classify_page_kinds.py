"""Rule-based page kind classification."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from app.services.document_agent.manifest import PageFeature, PageLabel, ToolContext, ToolResult


def _label_feature(feature: PageFeature) -> PageLabel:
    page = feature.page
    if (
        feature.raw_text_length < 80
        and feature.image_coverage < 0.02
        and feature.drawings_count < 5
    ):
        return PageLabel(
            page=page,
            kind="low_content",
            confidence=0.78,
            evidence={"signal": "low_text_image_drawings"},
        )
    if feature.orientation == "landscape":
        return PageLabel(
            page=page,
            kind="landscape",
            confidence=0.78,
            evidence={"width": feature.width, "height": feature.height},
        )
    if feature.image_coverage >= 0.35 and feature.raw_text_length < 250:
        return PageLabel(
            page=page,
            kind="image_heavy",
            confidence=0.84,
            evidence={"image_coverage": feature.image_coverage},
        )
    if feature.table_count > 0 or feature.drawings_count >= 80:
        return PageLabel(
            page=page,
            kind="table_heavy",
            confidence=0.72,
            evidence={
                "table_count": feature.table_count,
                "drawings_count": feature.drawings_count,
            },
        )
    return PageLabel(page=page, kind="normal", confidence=0.65, evidence={})


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
                "confidence": label.confidence,
                "evidence": label.evidence,
                "raw_text_length": feature.raw_text_length if feature else None,
                "image_coverage": feature.image_coverage if feature else None,
                "table_count": feature.table_count if feature else None,
                "drawings_count": feature.drawings_count if feature else None,
                "text_preview": (feature.text_lines_preview[:4] if feature else []),
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
