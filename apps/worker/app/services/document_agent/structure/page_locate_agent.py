"""Section-start page verification via ``inspect.pages`` (Phase-2).

Confirms whether a candidate physical page is the true START of a TOC title.
Uses the same question schema as Phase-1 forward scan; no heuristic fallback.

``inspect_pages`` is imported lazily so ``anchoring_primitives`` → this module
does not race ``tools.__init__`` → ``propose_shard_plan`` → anchoring.
"""

from __future__ import annotations

from typing import Any

from app.services.document_agent.agents.calibration.prompts import (
    SECTION_START_ANSWER_KEYS,
    build_section_start_question,
    coerce_found,
    coerce_found_page,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import TitleMatch

VLM_CONFIRMED_DEFAULT_CONFIDENCE = 0.75


def verify_section_page_choice(
    *,
    ctx: ToolContext | None,
    title: str,
    candidate_matches: list[TitleMatch],
    candidate_page_cap: int,
) -> dict[str, Any]:
    candidates = candidate_matches[: max(candidate_page_cap, 1)]
    pages = [match.page for match in candidates]
    if not candidates:
        return {
            "selected_page": None,
            "candidate_pages": [],
            "confidence": 0.0,
            "source": "agent_vlm",
            "reason": "no candidates",
        }
    if ctx is None:
        return {
            "selected_page": None,
            "candidate_pages": pages,
            "confidence": 0.0,
            "source": "agent_vlm",
            "reason": "ctx missing",
        }

    from app.services.document_agent.tools.inspect_pages import inspect_pages

    result = inspect_pages(
        ctx,
        {
            "pages": pages,
            "page_cap": len(pages),
            "question": build_section_start_question(title),
            "answer_keys": SECTION_START_ANSWER_KEYS,
            "folder_name": "calibration_verify",
            "prefix": "verify",
            "usage_task": "calibration.verify_section_page",
            "visual_stage": "calibration",
        },
    )
    if result.status != "ok":
        return {
            "selected_page": None,
            "candidate_pages": pages,
            "confidence": 0.0,
            "source": "agent_vlm",
            "reason": result.error or "inspect.pages failed",
            "latency_ms": result.latency_ms,
            "tokens_used": result.tokens_used,
        }

    fields = (result.payload or {}).get("fields") or {}
    found_page = coerce_found_page(fields.get("found_page"), pages=pages)
    found = coerce_found(fields.get("found")) and found_page is not None
    if not found:
        return {
            "selected_page": None,
            "candidate_pages": pages,
            "confidence": 0.0,
            "source": "agent_vlm",
            "reason": str((result.payload or {}).get("answer") or "not found"),
            "latency_ms": result.latency_ms,
            "tokens_used": result.tokens_used,
        }
    return {
        "selected_page": found_page,
        "candidate_pages": pages,
        "confidence": VLM_CONFIRMED_DEFAULT_CONFIDENCE,
        "source": "agent_vlm",
        "reason": str((result.payload or {}).get("answer") or ""),
        "latency_ms": result.latency_ms,
        "tokens_used": result.tokens_used,
    }
