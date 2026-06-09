"""Initial VLM profile planner for the document profile agent."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import (
    DocumentProfile,
    ReflexionDecision,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.planner.prompts import PLANNER_INSTRUCTIONS
from app.services.document_agent.visual import render_pages
from shared.utils.token_estimate import estimate_tokens

PAGE_KIND_DEFINITIONS = {
    "normal": (
        "Page with enough extractable native text and no dominant table/image "
        "structure."
    ),
    "table_heavy": (
        "Page with detected tables or many vector drawings, often financial "
        "tables or dense tabular layout."
    ),
    "image_heavy": (
        "Page dominated by image coverage with little extractable native text; "
        "may be scanned, infographic, photo, or rendered page."
    ),
    "low_content": (
        "Page with very little extractable text and little visual/table content; "
        "may be blank, separator, short heading page, or sparse transition page."
    ),
    "landscape": "Landscape-oriented page, often wide tables, drawings, slides, or diagrams.",
}


def _feature_rows(ctx: ToolContext, pages: list[int]) -> list[dict[str, Any]]:
    labels_by_page = {label.page: label for label in ctx.blackboard.page_labels}
    selected = []
    for feature in ctx.blackboard.page_features:
        if feature.page not in pages:
            continue
        label = labels_by_page.get(feature.page)
        selected.append(
            {
                "page": feature.page,
                "kind": label.kind if label else None,
                "confidence": label.confidence if label else None,
                "raw_text_length": feature.raw_text_length,
                "text_density": feature.text_density,
                "image_coverage": feature.image_coverage,
                "image_count": feature.image_count,
                "table_count": feature.table_count,
                "drawings_count": feature.drawings_count,
                "orientation": feature.orientation,
                "is_blank_like": feature.is_blank_like,
            }
        )
    return selected


def _segment_sample(candidates: list[int], count: int) -> list[int]:
    if count <= 0 or not candidates:
        return []
    if len(candidates) <= count:
        return candidates
    if count == 1:
        return [candidates[len(candidates) // 2]]
    step = (len(candidates) - 1) / (count - 1)
    return [candidates[round(index * step)] for index in range(count)]


def _sample_pages(
    page_count: int,
    extrema_pages: list[int],
    exclude_pages: set[int] | None = None,
) -> list[int]:
    """Select representative pages for VLM profiling.

    Args:
        page_count: Total number of pages.
        extrema_pages: Pages with statistical extrema (min/max text, tables, etc.).
        exclude_pages: Pages to skip entirely (e.g. TOC pages already detected
            by the TOC pipeline). These inflate text-density metrics without
            adding profiling value.
    """
    if page_count <= 0:
        return []
    skip = exclude_pages or set()
    extrema = [page for page in extrema_pages if 1 <= page <= page_count and page not in skip]
    pool = [page for page in range(1, page_count + 1) if page not in set(extrema) and page not in skip]
    if not pool:
        return sorted(set(extrema))
    third = max(len(pool) // 3, 1)
    front = pool[:third]
    middle = pool[third : third * 2]
    back = pool[third * 2 :]
    sampled = (
        _segment_sample(front, 4)
        + _segment_sample(middle or pool, 3)
        + _segment_sample(back or pool, 3)
    )
    ordered = []
    for page in extrema + sampled:
        if page not in ordered:
            ordered.append(page)
    return ordered[:20]


def _parse_profile_and_decision(raw: str) -> tuple[DocumentProfile, ReflexionDecision]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("planner output must be a JSON object")
    category = " ".join(str(data.get("category") or "unknown document").split()[:5])
    raw_is_scanned = data.get("is_scanned")
    if isinstance(raw_is_scanned, bool):
        is_scanned = raw_is_scanned
    elif isinstance(raw_is_scanned, str):
        is_scanned = raw_is_scanned.strip().lower() in {"true", "yes", "1", "scanned"}
    else:
        is_scanned = bool(raw_is_scanned)
    profile = DocumentProfile(
        is_scanned=is_scanned,
        category=category or "unknown document",
        category_rationale=str(data.get("category_rationale") or ""),
        language=str(data.get("language") or "unknown"),
        rationale=str(data.get("rationale") or ""),
    )
    next_action = str(data.get("next_action") or "ready_to_shard")
    tool_name: str | None = None
    tool_args: dict[str, Any] = {}
    if next_action == "inspect_more":
        pages = [int(page) for page in (data.get("inspect_pages") or [])]
        tool_name = "inspect.pages"
        tool_args = {
            "pages": pages[:10],
            "question": "Clarify the document structure and whether these pages change the profile or sharding strategy.",
        }
    elif next_action == "grep_text" and not profile.is_scanned:
        query = str(data.get("grep_query") or "").strip()
        if query:
            tool_name = "grep.text"
            tool_args = {"query": query, "max_results": 20}
    elif next_action == "verdict_now":
        return profile, ReflexionDecision(
            action="verdict_now",
            rationale=profile.rationale,
            verdict=None,
        )
    if tool_name:
        return profile, ReflexionDecision(
            action="tool_call",
            rationale=profile.rationale,
            tool_name=tool_name,
            tool_args=tool_args,
        )
    return profile, ReflexionDecision(
        action="tool_call",
        rationale=profile.rationale,
        tool_name="propose.shard_plan",
        tool_args={},
    )


class ProfilePlanner:
    """One-shot VLM planner that profiles the document and proposes the first action."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def propose(self) -> tuple[DocumentProfile, ReflexionDecision, ToolResult]:
        start = time.monotonic()
        model = (
            self.ctx.settings.get("planner_model")
            or self.ctx.settings.get("vlm_model")
            or os.environ.get("IMAGE_MODEL")
        )
        toc_pages = set(
            self.ctx.blackboard.toc_result.toc_pages
            if self.ctx.blackboard.toc_result
            else []
        )
        pages = _sample_pages(
            self.ctx.blackboard.page_count,
            self.ctx.blackboard.extrema_pages,
            exclude_pages=toc_pages,
        )
        if not model:
            profile = DocumentProfile(
                is_scanned=False,
                category="unknown document",
                rationale="No planner model configured.",
            )
            decision = ReflexionDecision(
                action="tool_call",
                rationale=profile.rationale,
                tool_name="propose.shard_plan",
                tool_args={},
            )
            return profile, decision, ToolResult(
                status="ok",
                payload={"source": "deterministic", "sampled_pages": pages},
                latency_ms=int((time.monotonic() - start) * 1000),
                warnings=["No planner model configured; using conservative profile."],
                input_summary={"page_count": self.ctx.blackboard.page_count},
                output_summary={"profile": profile.to_dict(), "decision": decision.to_dict()},
            )
        pngs = render_pages(
            self.ctx,
            pages,
            folder_name="planner_pages",
            prefix="planner",
            timeout=180,
        )
        feature_summary = _feature_rows(self.ctx, pages)
        payload = {
            "page_count": self.ctx.blackboard.page_count,
            "page_kind_counts": self.ctx.blackboard.global_signals.get(
                "page_kind_counts",
                {},
            ),
            "page_kind_definitions": PAGE_KIND_DEFINITIONS,
            "doc_stats": self.ctx.blackboard.doc_stats,
            "extrema_samples": self.ctx.blackboard.global_signals.get(
                "extrema_samples",
                [],
            ),
            "sampled_page_features": feature_summary,
            "toc_pages": self.ctx.blackboard.toc_result.toc_pages
            if self.ctx.blackboard.toc_result
            else [],
            "h1_pages": [
                {"title": item.title, "page": item.page}
                for item in (
                    self.ctx.blackboard.h1_result.h1_candidates
                    if self.ctx.blackboard.h1_result
                    else []
                )
            ],
            "available_actions": [
                "inspect.pages",
                "grep.text",
                "propose.shard_plan",
                "validate.anatomy_map",
                "verdict",
            ],
        }
        prompt_text = PLANNER_INSTRUCTIONS + "\nPayload:\n" + json.dumps(
            payload,
            ensure_ascii=False,
        )
        prompt_tokens_est = estimate_tokens(prompt_text) + len(pngs) * 800
        if not self.ctx.budget.try_reserve("visual", prompt_tokens_est):
            raise RuntimeError("Insufficient visual budget for profile planning.")

        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for item in pngs:
            try:
                with open(str(item["png_path"]), "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                content_parts.append(
                    {"type": "text", "text": f"\n--- Page {item['page']} ---"}
                )
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    }
                )
            except Exception as exc:
                logger.warning("[document_agent] planner png attach failed: {}", exc)

        try:
            from shared.services.ai.openai_compatible_client_sync import get_openai_client

            client = get_openai_client(model=model)
            raw, usage = client.chat_completion_with_usage(
                messages=cast(Any, [{"role": "user", "content": content_parts}]),
                model=model,
                temperature=0.0,
                max_tokens=1800,
                response_format={"type": "json_object"},
            )
            self.ctx.budget.commit(
                "visual",
                actual=usage.get("total_tokens", prompt_tokens_est),
                est=prompt_tokens_est,
            )
            profile, decision = _parse_profile_and_decision(raw)
            return profile, decision, ToolResult(
                status="ok",
                payload={
                    "source": "llm",
                    "sampled_pages": pages,
                    "first_action": decision.tool_name,
                },
                latency_ms=int((time.monotonic() - start) * 1000),
                tokens_used=usage.get("total_tokens", 0),
                input_summary=payload,
                output_summary={"profile": profile.to_dict(), "decision": decision.to_dict()},
                debug={
                    "prompt_text": prompt_text,
                    "sampled_pngs": pngs,
                    "raw_response": raw,
                },
            )
        except Exception:
            self.ctx.budget.refund("visual", est=prompt_tokens_est)
            raise


