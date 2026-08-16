"""Tools for the calibration SubAgent (local registry, not PROFILE gates)."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.agents.calibration.types import (
    FAILURE_NO_OFFSET,
    calibration_result_from_dict,
)
from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import ToolRegistry, ToolSpec
from app.services.document_agent.tools.inspect_pages import inspect_pages


def build_calibration_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="inspect.pages",
            description=(
                "Open one or more physical PDF pages, render them, and answer "
                "the given question about those pages."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pages": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "1-based physical page numbers",
                    },
                    "question": {
                        "type": "string",
                        "description": "Question to answer from the rendered pages",
                    },
                },
                "required": ["pages", "question"],
            },
            preconditions=(),
            handler=_calibration_inspect_pages,
        )
    )
    registry.register(
        ToolSpec(
            name="calibration.submit",
            description=(
                "Submit the Phase-1 result and finish. Only the fields below are "
                "read; Phase-2 recomputes everything else."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "result": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["ok", "failed"],
                            },
                            "regimes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "description": (
                                                "Page-numbering system of this "
                                                "regime's printed labels"
                                            ),
                                        },
                                        "offset": {
                                            "type": "integer",
                                            "description": "physical - printed",
                                        },
                                        "entry_indices": {
                                            "type": "array",
                                            "items": {"type": "integer"},
                                            "description": (
                                                "0-based indices into "
                                                "toc_region.entries; omit when the "
                                                "regime is exactly the entries "
                                                "whose label shape matches kind"
                                            ),
                                        },
                                        "samples": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "title": {"type": "string"},
                                                    "physical": {"type": "integer"},
                                                },
                                                "required": ["title", "physical"],
                                            },
                                            "description": (
                                                "Anchors you confirmed with "
                                                "inspect.pages, so Phase-2 does not "
                                                "re-verify them"
                                            ),
                                        },
                                    },
                                    "required": ["kind", "offset"],
                                },
                            },
                            "notes": {
                                "type": "string",
                                "description": "One short sentence: why this result",
                            },
                        },
                        "required": ["status", "regimes"],
                    },
                },
                "required": ["result"],
            },
            preconditions=(),
            handler=calibration_submit,
        )
    )
    return registry


def _calibration_inspect_pages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    merged = dict(args)
    merged.setdefault("folder_name", "calibration_inspect")
    merged.setdefault("prefix", "calib")
    merged.setdefault("usage_task", "calibration.inspect_pages")
    merged.setdefault("visual_stage", "calibration")
    return inspect_pages(ctx, merged)


def calibration_submit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    raw = args.get("result")
    if not isinstance(raw, dict):
        if any(key in args for key in ("status", "regimes")):
            raw = {
                key: args.get(key)
                for key in ("status", "regimes", "notes")
                if key in args
            }
        else:
            return ToolResult(
                status="error",
                error="calibration.submit requires result object",
                latency_ms=int((time.monotonic() - start) * 1000),
            )
    result = calibration_result_from_dict(raw)
    if not result.regimes and result.status == "ok":
        return ToolResult(
            status="error",
            error="ok result must include regimes",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    if not result.regimes:
        result.failure_kind = FAILURE_NO_OFFSET
    tool_calls = int(ctx.blackboard.global_signals.get("calibration_tool_calls") or 0)
    result.tool_calls = tool_calls
    region_index = ctx.blackboard.global_signals.get("calibration_region_index")
    if region_index is not None:
        result.region_index = int(region_index)
    ctx.blackboard.global_signals["calibration_result"] = result.to_dict()
    ctx.blackboard.global_signals["calibration_done"] = True
    return ToolResult(
        status="ok",
        payload=result.to_dict(),
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={"status": result.status, "regimes": len(result.regimes)},
    )


def strip_toc_links(hierarchies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return hierarchies with entry ``link`` keys removed."""
    out: list[dict[str, Any]] = []
    for hierarchy in hierarchies:
        if not isinstance(hierarchy, dict):
            continue
        cloned = dict(hierarchy)
        entries = hierarchy.get("toc_with_level")
        if isinstance(entries, list):
            new_entries: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                item.pop("link", None)
                new_entries.append(item)
            cloned["toc_with_level"] = new_entries
        out.append(cloned)
    return out
