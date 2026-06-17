"""Page tagger: VLM per-page annotation for summary/status/observed_titles.

For ``vlm_lite`` pages, sends the page PNG to the VLM and expects a JSON
response.  ``text_only`` pages get a rules-based summary from raw text.
``skip_tagging`` pages are left empty.

Budget is drawn from the ``page_tagging`` stage envelope.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from loguru import logger

from app.services.document_agent.budget import BudgetTracker
from app.services.page_memory.page_plan import PagePlan, PageProcessingStrategy
from app.services.page_memory.page_renderer import PageRenderResult
from shared.utils.token_estimate import estimate_tokens


PageStatus = Literal["clear", "blurry", "skipped"]


@dataclass
class PageTagResult:
    """Tagging output for a single page."""

    page_index: int
    summary: str = ""
    status: PageStatus = "clear"
    observed_titles: list[str] = field(default_factory=list)
    strategy_used: str = ""


# ── prompt placeholder — NEEDS USER CONFIRMATION ─────────────────────

_VLM_TAG_PROMPT = """\
You are annotating a single PDF page screenshot for a document memory system.
Return strict JSON with exactly these keys:

{
  "summary": "<1-3 sentence summary of the page content>",
  "status": "clear" | "blurry",
  "observed_titles": ["<heading or section title visible on this page>", ...]
}

Rules:
- "summary" should describe the main content visible on the page.
- "status" should be "clear" if the page is legible, "blurry" if the text is
  unreadable or the image is too low quality to summarize.
- "observed_titles" should list any headings, section titles, or chapter titles
  visible on the page.  Return an empty array if none are visible.
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
            results.append(
                PageTagResult(
                    page_index=page.page_index,
                    status="skipped",
                    strategy_used="skip_tagging",
                )
            )
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
        sum(1 for r in results if r.status == "skipped"),
    )
    return results


def _tag_text_only(page: PageRenderResult) -> PageTagResult:
    """Rules-based tag from raw text (no VLM)."""
    raw = page.raw_text.strip()
    summary = " ".join(raw.split())[:_RAW_TEXT_SUMMARY_LIMIT]
    if not summary:
        summary = f"Page {page.page_index} (no extractable text)"

    # Simple heuristic: lines that look like headings (short, no trailing punct)
    observed: list[str] = []
    for line in raw.splitlines()[:30]:
        stripped = line.strip()
        if (
            stripped
            and 3 < len(stripped) < 100
            and not stripped.endswith((".", "。", ",", "，", ";", "；"))
            and not stripped[0].isdigit()
            and stripped[0].isupper() or not stripped[0].isascii()
        ):
            # Very rough heuristic; the real heading detection is in skeleton
            pass
    # For text_only mode, we don't attempt heading detection
    return PageTagResult(
        page_index=page.page_index,
        summary=summary,
        status="clear",
        observed_titles=observed,
        strategy_used="text_only",
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
                status=result.status,
                observed_titles=result.observed_titles,
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
            return PageTagResult(
                page_index=page.page_index,
                summary=str(data.get("summary", "")),
                status="clear" if data.get("status") != "blurry" else "blurry",
                observed_titles=list(data.get("observed_titles") or []),
                strategy_used="vlm_lite",
            )
        except json.JSONDecodeError:
            if attempt < _MAX_JSON_RETRIES:
                logger.warning(
                    "[page_tagger] JSON parse failed for page {} (attempt {}/{}), retrying",
                    page.page_index, attempt + 1, _MAX_JSON_RETRIES + 1,
                )
                continue
            # Final attempt failed: blurry degradation
            logger.warning(
                "[page_tagger] JSON retry exhausted for page {}; blurry fallback",
                page.page_index,
            )
            raw_summary = " ".join(page.raw_text.split())[:_RAW_TEXT_SUMMARY_LIMIT]
            return PageTagResult(
                page_index=page.page_index,
                summary=raw_summary or f"Page {page.page_index} (VLM parse failed)",
                status="blurry",
                observed_titles=[],
                strategy_used="vlm_lite_blurry_fallback",
            )
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
