"""VLM page verification for page-memory offset calibration.

This module provides the deterministic-input, VLM-arbitrated helper used by the
page-memory skeleton calibration to confirm which candidate page starts a given
section title. The former residual ReAct sub-agent has been removed; calibration
now drives offset-guided bulk anchoring directly and only needs this verifier.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import TitleMatch

VLM_CONFIRMED_DEFAULT_CONFIDENCE = 0.75
GREP_ONLY_CONFIDENCE_CAP = 0.62
RENDER_FAILED_GREP_CONFIDENCE_CAP = 0.58
BUDGET_EXHAUSTED_GREP_CONFIDENCE_CAP = 0.56
VLM_FAILED_GREP_CONFIDENCE_CAP = 0.54


def verify_section_page_choice(
    *,
    ctx: ToolContext | None,
    title: str,
    candidate_matches: list[TitleMatch],
    candidate_page_cap: int,
) -> dict[str, Any]:
    candidates = candidate_matches[: max(candidate_page_cap, 1)]
    if not candidates:
        return {
            "selected_page": None,
            "confidence": 0.0,
            "source": "agent_heuristic",
            "reason": "no grep candidates",
        }

    model = None
    if ctx is not None:
        model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    pages = [match.page for match in candidates]
    if ctx is None or not model or ctx.budget is None:
        best = candidates[0]
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, GREP_ONLY_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": "VLM unavailable; selected top grep candidate",
        }

    from app.services.document_agent.visual import render_pages

    rendered = render_pages(
        ctx,
        pages,
        folder_name="page_locate_pages",
        prefix="locate",
        timeout=120,
    )
    if not rendered:
        best = candidates[0]
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, RENDER_FAILED_GREP_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": "render failed; selected top grep candidate",
        }

    prompt = _build_verify_prompt(title=title, candidates=candidates)
    est = 800 * len(rendered) + 800
    stage = "calibration"
    if not ctx.budget.try_reserve("visual", est, stage=stage):
        best = candidates[0]
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, BUDGET_EXHAUSTED_GREP_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": "calibration visual budget exhausted; selected top grep candidate",
        }

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in rendered:
        with open(str(item["png_path"]), "rb") as image_file:
            img_b64 = base64.b64encode(image_file.read()).decode()
        content_parts.append({"type": "text", "text": f"\n--- Page {item['page']} ---"})
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )

    start = time.monotonic()
    try:
        from shared.services.ai.llm_overrides import get_vision_client

        client, model = get_vision_client(requested_model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=model,
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
            usage_task="page_memory.page_locate",
        )
        ctx.budget.commit(
            "visual",
            actual=usage.get("total_tokens", est),
            est=est,
            stage=stage,
        )
        payload = json.loads(raw)
        selected_page = payload.get("selected_page")
        if selected_page is not None:
            selected_page = int(selected_page)
        if selected_page not in pages:
            selected_page = None
        return {
            "selected_page": selected_page,
            "candidate_pages": pages,
            "confidence": float(payload.get("confidence") or VLM_CONFIRMED_DEFAULT_CONFIDENCE),
            "source": "agent_vlm",
            "reason": str(payload.get("reason") or ""),
            "latency_ms": int((time.monotonic() - start) * 1000),
            "tokens_used": usage.get("total_tokens", 0),
        }
    except Exception as exc:
        ctx.budget.refund("visual", est=est, stage=stage)
        best = candidates[0]
        logger.warning("[page_locate.agent] VLM failed for title={!r}: {}", title, exc)
        return {
            "selected_page": best.page,
            "candidate_pages": pages,
            "confidence": min(best.confidence, VLM_FAILED_GREP_CONFIDENCE_CAP),
            "source": "agent_heuristic",
            "reason": f"VLM failed ({type(exc).__name__}); selected top grep candidate",
        }


def _build_verify_prompt(*, title: str, candidates: list[TitleMatch]) -> str:
    candidate_lines = "\n".join(
        f"- page {match.page}: source={match.source}, line={match.matched_line!r}"
        for match in candidates
    )
    return (
        "You are a page-location sub-agent for a PDF hierarchy parser.\n"
        "Choose which candidate page is the true START page of the section title, "
        "not a table-of-contents entry, page header, footer, or body-text mention.\n"
        f"Section title: {title!r}\n"
        f"Candidates:\n{candidate_lines}\n"
        "Return strict JSON: {\"selected_page\": number|null, "
        "\"confidence\": number, \"reason\": \"brief explanation\"}."
    )
