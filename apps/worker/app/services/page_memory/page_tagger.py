"""Page tagger: VLM per-page annotation for summary and keywords.

For ``vlm_lite`` pages, sends the page PNG to the VLM and expects a JSON
response with ``summary`` and ``keywords``.
For ``text_only`` pages, calls the existing ``summary-full`` LLM prompt
to extract summary + keywords from raw text.
For ``skip_tagging`` pages, content is preserved but summary is omitted.

Budget is drawn from the ``page_tagging`` stage envelope.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

from loguru import logger

from app.services.document_agent.budget import BudgetTracker
from app.services.page_memory.page_plan import PagePlan, PageProcessingStrategy
from app.services.page_memory.page_renderer import PageRenderResult
from shared.utils.token_estimate import estimate_tokens


@dataclass
class PageTagResult:
    """Tagging output for a single page."""

    page_index: int
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    strategy_used: str = ""


# ── VLM prompt: outputs summary + keywords only ─────────────────────

_VLM_TAG_PROMPT = """\
You are annotating a single PDF page screenshot for a document memory system.
Return strict JSON with exactly these keys:

{
  "summary": "<1-3 sentence summary of the page content>",
  "keywords": "<keyword_1>;<keyword_2>;<keyword_3>"
}

Rules:
- "summary": describe the main content visible on the page in 1-3 sentences.
  If the page contains tables, mention the table topic and key columns.
  If the page contains figures or charts, describe what they depict.
- "keywords": extract the most important thematic keywords (up to 5),
  separated by semicolons ";". Keywords must be in the same language as
  the visible page content.
- Return ONLY the JSON object, no markdown fences or extra text.
"""

_BUDGET_STAGE = "page_tagging"
_MAX_JSON_RETRIES = 1
_RAW_TEXT_SUMMARY_LIMIT = 500


def tag_pages(
    *,
    pages: list[PageRenderResult],
    plans: list[PagePlan],
    budget: BudgetTracker | None = None,
    vlm_model: str | None = None,
) -> list[PageTagResult]:
    """Tag all pages according to their processing plan.

    Parameters
    ----------
    pages:
        Rendered page results (from ``page_renderer``).
    plans:
        Processing plans (from ``page_plan``).
    budget:
        Optional budget tracker with a ``page_tagging`` stage envelope.
    vlm_model:
        VLM model name; falls back to ``$IMAGE_MODEL``.

    Returns
    -------
    list[PageTagResult]
        One result per page, ordered by page_index.
    """
    plan_map = {plan.page_index: plan for plan in plans}
    model = vlm_model or os.environ.get("IMAGE_MODEL")

    results: list[PageTagResult] = []
    vlm_calls = 0

    for page in pages:
        plan = plan_map.get(page.page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_LITE

        if strategy == PageProcessingStrategy.SKIP_TAGGING:
            results.append(_tag_skip(page))
            continue

        if strategy == PageProcessingStrategy.TEXT_ONLY:
            results.append(_tag_text_only(page))
            continue

        # vlm_lite
        if not model:
            logger.warning(
                "[page_tagger] no VLM model for page {}; falling back to text_only",
                page.page_index,
            )
            results.append(_tag_text_only(page))
            continue

        tag = _tag_vlm_lite(page, model=model, budget=budget)
        results.append(tag)
        vlm_calls += 1

    logger.info(
        "[page_tagger] tagged {} pages ({} VLM calls, {} text_only, {} skipped)",
        len(results),
        vlm_calls,
        sum(1 for r in results if r.strategy_used == "text_only"),
        sum(1 for r in results if r.strategy_used == "skip_tagging"),
    )
    return results


def _tag_skip(page: PageRenderResult) -> PageTagResult:
    """Skip-tagging: preserve raw_text but no summary.

    If the page has no extractable text, mark as EMPTY.
    """
    raw = page.raw_text.strip()
    return PageTagResult(
        page_index=page.page_index,
        summary="" if raw else "EMPTY",
        keywords=[],
        strategy_used="skip_tagging",
    )


def _tag_text_only(page: PageRenderResult) -> PageTagResult:
    """Use existing ``summary-full`` LLM prompt to extract summary + keywords.

    Falls back to raw text truncation if LLM is not available or fails.
    """
    raw = page.raw_text.strip()
    if not raw:
        return PageTagResult(
            page_index=page.page_index,
            summary="EMPTY",
            keywords=[],
            strategy_used="text_only",
        )

    # Try the existing summary-full LLM call (same as text chunk pipeline)
    try:
        from shared.services.ai.prompt_service import build_prompt
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        text_model = os.environ.get("NORMOL_MODEL", "deepseek-v4-flash")
        prompt, temperature, top_p, max_tokens = build_prompt(
            "summary-full",
            raw[:3000],  # limit input to avoid token overflow
            "",
            paras={"max_tokens": 200, "kw_num": 5},
        )
        client = get_openai_client(model=text_model)
        raw_response, _ = client.chat_completion_with_usage(
            messages=[{"role": "user", "content": prompt}],
            model=text_model,
            temperature=temperature,
            max_tokens=max_tokens,
            usage_task="page_memory.text_only_summary",
        )

        if raw_response and raw_response.strip().lower() != "null":
            data = json.loads(raw_response)
            summary = str(data.get("summary", ""))
            kw_str = str(data.get("keywords", ""))
            keywords = [k.strip() for k in kw_str.split(";") if k.strip()]
            return PageTagResult(
                page_index=page.page_index,
                summary=summary,
                keywords=keywords,
                strategy_used="text_only",
            )
    except Exception as exc:
        logger.warning(
            "[page_tagger] summary-full LLM failed for page {}: {}; "
            "falling back to raw text truncation",
            page.page_index, exc,
        )

    # Fallback: raw text truncation
    summary = " ".join(raw.split())[:_RAW_TEXT_SUMMARY_LIMIT]
    return PageTagResult(
        page_index=page.page_index,
        summary=summary,
        keywords=[],
        strategy_used="text_only_fallback",
    )


def _tag_vlm_lite(
    page: PageRenderResult,
    *,
    model: str,
    budget: BudgetTracker | None,
) -> PageTagResult:
    """Send page PNG to VLM and parse JSON response."""
    est = estimate_tokens(_VLM_TAG_PROMPT) + 800  # ~800 tokens for image

    if budget is not None:
        if not budget.try_reserve("visual", est, stage=_BUDGET_STAGE):
            logger.warning(
                "[page_tagger] insufficient budget for page {}; text_only fallback",
                page.page_index,
            )
            result = _tag_text_only(page)
            result = PageTagResult(
                page_index=result.page_index,
                summary=result.summary,
                keywords=result.keywords,
                strategy_used="text_only_budget_fallback",
            )
            return result

    if not page.image_path or not os.path.exists(page.image_path):
        logger.warning(
            "[page_tagger] no PNG for page {}; text_only fallback",
            page.page_index,
        )
        if budget is not None:
            budget.refund("visual", est=est, stage=_BUDGET_STAGE)
        return _tag_text_only(page)

    try:
        with open(page.image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as exc:
        logger.warning(
            "[page_tagger] failed to read PNG for page {}: {}",
            page.page_index, exc,
        )
        if budget is not None:
            budget.refund("visual", est=est, stage=_BUDGET_STAGE)
        return _tag_text_only(page)

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": _VLM_TAG_PROMPT},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        },
    ]

    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    client = get_openai_client(model=model)

    for attempt in range(_MAX_JSON_RETRIES + 1):
        try:
            raw_response, usage = client.chat_completion_with_usage(
                messages=cast(Any, [{"role": "user", "content": content_parts}]),
                model=model,
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
                usage_task="page_memory.tag",
            )
            if budget is not None:
                budget.commit(
                    "visual",
                    actual=usage.get("total_tokens", est),
                    est=est,
                    stage=_BUDGET_STAGE,
                )

            data = json.loads(raw_response)
            kw_str = str(data.get("keywords", ""))
            keywords = [k.strip() for k in kw_str.split(";") if k.strip()]
            return PageTagResult(
                page_index=page.page_index,
                summary=str(data.get("summary", "")),
                keywords=keywords,
                strategy_used="vlm_lite",
            )
        except json.JSONDecodeError:
            if attempt < _MAX_JSON_RETRIES:
                logger.warning(
                    "[page_tagger] JSON parse failed for page {} (attempt {}/{}), retrying",
                    page.page_index, attempt + 1, _MAX_JSON_RETRIES + 1,
                )
                continue
            # Final attempt failed: fallback to text_only
            logger.warning(
                "[page_tagger] JSON retry exhausted for page {}; text_only fallback",
                page.page_index,
            )
            result = _tag_text_only(page)
            result = PageTagResult(
                page_index=result.page_index,
                summary=result.summary,
                keywords=result.keywords,
                strategy_used="vlm_lite_json_fallback",
            )
            return result
        except Exception as exc:
            logger.warning(
                "[page_tagger] VLM call failed for page {}: {}",
                page.page_index, exc,
            )
            if budget is not None:
                budget.refund("visual", est=est, stage=_BUDGET_STAGE)
            return _tag_text_only(page)

    # Should not reach here, but safety net
    return _tag_text_only(page)
