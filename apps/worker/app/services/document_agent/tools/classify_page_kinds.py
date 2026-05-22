"""Rule-based page kind classification."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.manifest import PageFeature, PageLabel, ToolContext, ToolResult
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState


def _joined_preview(feature: PageFeature) -> str:
    return "\n".join(feature.text_lines_preview).lower()


def _label_feature(feature: PageFeature) -> PageLabel:
    preview = _joined_preview(feature)
    page = feature.page
    if any(marker in preview.replace(" ", "") for marker in ("目录", "目次", "contents")):
        return PageLabel(
            page=page,
            kind="toc",
            confidence=0.86,
            evidence={"signal": "toc_marker"},
        )
    if feature.is_blank_like:
        return PageLabel(
            page=page,
            kind="blank",
            confidence=0.92,
            evidence={"signal": "low_text_image_drawings"},
        )
    if feature.orientation == "landscape":
        return PageLabel(
            page=page,
            kind="landscape",
            confidence=0.78,
            evidence={"width": feature.width, "height": feature.height},
        )
    if feature.image_coverage >= 0.72 and feature.raw_text_length < 250:
        return PageLabel(
            page=page,
            kind="single_image",
            confidence=0.84,
            evidence={"image_coverage": feature.image_coverage},
        )
    if feature.raw_text_length < 50 and feature.image_coverage >= 0.35:
        return PageLabel(
            page=page,
            kind="scan_like",
            confidence=0.76,
            evidence={
                "raw_text_length": feature.raw_text_length,
                "image_coverage": feature.image_coverage,
            },
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
    if feature.image_coverage >= 0.35:
        return PageLabel(
            page=page,
            kind="image_heavy",
            confidence=0.72,
            evidence={"image_coverage": feature.image_coverage},
        )
    if feature.raw_text_length < 80:
        return PageLabel(
            page=page,
            kind="sparse",
            confidence=0.67,
            evidence={"raw_text_length": feature.raw_text_length},
        )
    return PageLabel(page=page, kind="normal", confidence=0.65, evidence={})


@register_tool(
    name="classify.page_kinds",
    description="Classify every probed page into structural page kinds using deterministic signals.",
    allowed_states={DocumentAgentState.PROBED},
)
def classify_page_kinds(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    labels = [_label_feature(feature) for feature in ctx.blackboard.page_features]
    ctx.blackboard.page_labels = labels
    counts: dict[str, int] = {}
    for label in labels:
        counts[label.kind] = counts.get(label.kind, 0) + 1
    ctx.blackboard.global_signals["page_kind_counts"] = counts
    return ToolResult(
        status="ok",
        payload={"page_kind_counts": counts},
        latency_ms=int((time.monotonic() - start) * 1000),
    )
