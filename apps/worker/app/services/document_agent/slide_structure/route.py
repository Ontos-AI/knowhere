"""Slide-structure route: the structure of a slide deck, instead of TOC extraction.

Writes the same blackboard state as the PDF outline route (sections with
physical pages, no calibration), so skeletons, scopes and FINE downstream are
unchanged. No structural slide found means no hierarchy: the deck stays flat.
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import TocResult, ToolContext, ToolResult
from app.services.document_agent.pdf_text import page_content_map
from app.services.document_agent.slide_structure.config import SlideStructureConfig
from app.services.document_agent.slide_structure.contact_sheet import ContactSheets
from app.services.document_agent.slide_structure.pipeline import (
    SlideSection,
    SlideStructure,
    VlmCall,
    detect_slide_structure,
)
from app.services.document_agent.structure.toc_anchoring import (
    write_physical_page_skeleton,
)
from app.services.document_parser.structure.body_boundary import normalize_heading_label

SLIDE_STRUCTURE_SOURCE = "slide_structure"
_LOG_PREFIX = "[profile.slide_structure]"


def run_slide_structure_route(ctx: ToolContext) -> ToolResult:
    """Detect the deck structure and anchor it onto the blackboard.

    A failed detection degrades like a failed TOC extraction: no hierarchy,
    ``failure_kind="degraded"``, and the profile goes on with the deck flat.
    """
    started = time.monotonic()
    config = SlideStructureConfig.from_settings()
    usage = {"calls": 0, "tokens": 0}
    page_count = int(ctx.blackboard.page_count or 0)
    try:
        structure = detect_slide_structure(
            page_count=page_count,
            grids=ContactSheets(ctx),
            config=config,
            vlm=_vision_call(ctx, config, usage),
        )
    except Exception as exc:
        logger.warning("{} detection failed, deck stays flat: {}", _LOG_PREFIX, exc)
        ctx.blackboard.toc_result = TocResult(
            method="none",
            notes=f"slide structure detection failed: {exc}",
            failure_kind="degraded",
        )
        return ToolResult(
            status="ok",
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_used=usage["tokens"],
            output_summary={"vlm_calls": usage["calls"]},
            warnings=[f"Slide structure detection failed: {exc}"],
        )
    entries = toc_entries(structure.sections)
    hierarchy = {"source": SLIDE_STRUCTURE_SOURCE, "toc_with_level": entries}
    anchored = write_physical_page_skeleton(
        ctx,
        hierarchies=[hierarchy],
        page_texts=page_content_map(ctx.blackboard.page_full_text_cache),
        body_pages=list(range(1, page_count + 1)),
        page_count=page_count,
        toc_pages=[],
        source=SLIDE_STRUCTURE_SOURCE,
    )
    if anchored:
        ctx.blackboard.toc_hierarchies = [hierarchy]
    ctx.blackboard.toc_result = TocResult(
        method=SLIDE_STRUCTURE_SOURCE if anchored else "none",
        notes=(
            f"slide structure: {len(structure.candidates)} candidate slides, "
            f"{len(entries)} sections"
        ),
    )
    logger.info(
        "{} candidates={} sections={} gap_groups={} calls={} tokens={}",
        _LOG_PREFIX,
        structure.candidates,
        len(entries),
        len(structure.gap_groups),
        usage["calls"],
        usage["tokens"],
    )
    return ToolResult(
        status="ok",
        payload=_report(structure, entries),
        latency_ms=int((time.monotonic() - started) * 1000),
        tokens_used=usage["tokens"],
        output_summary={
            "candidates": len(structure.candidates),
            "sections": len(entries),
            "gap_groups": len(structure.gap_groups),
            "vlm_calls": usage["calls"],
        },
    )


def toc_entries(sections: list[SlideSection]) -> list[dict[str, Any]]:
    """Outline-shaped rows in reading order, one row per title path.

    The anchoring keys overrides by title path, so a second section with the
    same path under the same parent would overwrite the first; it is dropped.
    """
    entries: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    stack: list[tuple[int, str]] = []
    for section in sections:
        title = normalize_heading_label(section.title)
        if len(title) < 2:
            # Same rule as the anchoring's title tree, which skips such titles.
            continue
        while stack and stack[-1][0] >= section.level:
            stack.pop()
        path = (*(item[1] for item in stack), title)
        if path in seen_paths:
            continue
        seen_paths.add(path)
        stack.append((section.level, title))
        entries.append(
            {
                "heading": section.title,
                "level": section.level,
                "physical_page": section.start_slide,
            }
        )
    return entries


def _vision_call(
    ctx: ToolContext, config: SlideStructureConfig, usage: dict[str, int]
) -> VlmCall:
    from shared.services.ai.llm_overrides import get_vision_client

    client, model = get_vision_client(requested_model=ctx.settings.get("vlm_model"))

    def call(prompt: str, images: list[str], task: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image}"},
            }
            for image in images
        )
        raw, call_usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content}]),
            model=model,
            temperature=0.0,
            max_tokens=config.vlm_max_tokens,
            response_format={"type": "json_object"},
            usage_task=f"document_agent.slide_structure.{task}",
        )
        usage["calls"] += 1
        usage["tokens"] += int(call_usage.get("total_tokens", 0) or 0)
        answer = json.loads(raw)
        if not isinstance(answer, dict):
            raise ValueError(f"slide structure {task} output must be a JSON object")
        return answer

    return call


def _report(structure: SlideStructure, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": structure.candidates,
        "ignored_slides": structure.ignored_slides,
        "sections": [
            {
                "title": section.title,
                "level": section.level,
                "start_slide": section.start_slide,
                "filled": section.filled,
            }
            for section in structure.sections
        ],
        "gap_groups": [
            {
                "slides": [group["first"], group["last"]],
                "expected": [
                    {"title": title, "level": level}
                    for title, level in group["expected"].items()
                ],
                "found": group["found"],
                "accepted": group["accepted"],
            }
            for group in structure.gap_groups
        ],
        "toc_with_level": entries,
    }
