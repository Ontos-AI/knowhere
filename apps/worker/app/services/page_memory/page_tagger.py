"""Page tagger: VLM per-page annotation for summary, keywords, and title candidates.

For ``vlm_lite`` pages, sends the page PNG to the VLM and expects a JSON
response with ``summary`` and ``keywords``.
For ``text_only`` pages, calls the existing ``summary-full`` LLM prompt
to extract summary + keywords from raw text.
For ``skip_tagging`` pages, content is preserved but summary is omitted.

Step 2 of page-memory native hierarchy adds:
- Independent VLM title candidate extraction (``observed_titles``)
- Fat-leaf gating: only pages in TOC leaves with > N pages trigger title detection
- Title extraction uses a dedicated verbatim-only prompt (temp=0, small max_tokens)

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
from shared.services.ai.prompt_service import build_prompt
from shared.utils.token_estimate import estimate_tokens


@dataclass
class PageTagResult:
    """Tagging output for a single page."""

    page_index: int
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    strategy_used: str = ""
    observed_titles: list[dict[str, Any]] = field(default_factory=list)
    """Step 2: verbatim title candidates observed on this page.

    Each entry is ``{"text": str, "prominence": float | None}``.
    Empty list means no titles were detected (or title detection was skipped).
    """


_BUDGET_STAGE = "page_tagging"
_BUDGET_STAGE_TITLES = "page_title_detection"
_MAX_JSON_RETRIES = 1
_RAW_TEXT_SUMMARY_LIMIT = 500
_DEFAULT_FINE_MIN_PAGES = 4


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
    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-vlm-tag",
        "",
        "",
        paras={"max_tokens": 600},
    )
    est = estimate_tokens(prompt) + 800  # ~800 tokens for image

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
        {"type": "text", "text": prompt},
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
                temperature=temperature,
                max_tokens=max_tokens,
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


# ── Step 2: Independent title candidate extraction ───────────────────


def get_fine_min_pages() -> int:
    """Fat-leaf gating threshold from env ``PAGE_MEMORY_FINE_MIN_PAGES``."""
    return int(os.environ.get("PAGE_MEMORY_FINE_MIN_PAGES", str(_DEFAULT_FINE_MIN_PAGES)))


def tag_page_titles(
    *,
    pages: list[PageRenderResult],
    tag_results: list[PageTagResult],
    fat_leaf_pages: set[int],
    budget: BudgetTracker | None = None,
    vlm_model: str | None = None,
) -> list[PageTagResult]:
    """Run independent VLM title detection on fat-leaf pages.

    Parameters
    ----------
    pages:
        Rendered page results.
    tag_results:
        Existing tag results from ``tag_pages()`` (will be updated in-place).
    fat_leaf_pages:
        Set of page indices belonging to fat-leaf TOC sections
        (those with > ``PAGE_MEMORY_FINE_MIN_PAGES`` pages).
    budget:
        Optional budget tracker.
    vlm_model:
        VLM model name; falls back to ``$IMAGE_MODEL``.

    Returns
    -------
    list[PageTagResult]
        Updated tag results with ``observed_titles`` populated for fat-leaf pages.
    """
    if not fat_leaf_pages:
        return tag_results

    model = vlm_model or os.environ.get("IMAGE_MODEL")
    if not model:
        logger.warning("[page_tagger] no VLM model for title detection; skipping")
        return tag_results

    tag_map = {t.page_index: t for t in tag_results}
    page_map = {p.page_index: p for p in pages}
    vlm_calls = 0
    titles_found = 0

    for page_idx in sorted(fat_leaf_pages):
        page = page_map.get(page_idx)
        tag = tag_map.get(page_idx)
        if page is None or tag is None:
            continue

        # Skip pages without images (text_only / skip)
        if not page.image_path or not os.path.exists(page.image_path):
            continue

        observed = _tag_vlm_titles(page, model=model, budget=budget)
        tag.observed_titles = observed
        vlm_calls += 1
        titles_found += len(observed)

    logger.info(
        "[page_tagger] title detection: {} VLM calls on {} fat-leaf pages, {} titles found",
        vlm_calls,
        len(fat_leaf_pages),
        titles_found,
    )
    return tag_results


def _tag_vlm_titles(
    page: PageRenderResult,
    *,
    model: str,
    budget: BudgetTracker | None,
) -> list[dict[str, Any]]:
    """Send page PNG to VLM with the title-only prompt and parse results."""
    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-vlm-title",
        "",
        "",
        paras={"max_tokens": 300},
    )
    est = estimate_tokens(prompt) + 800  # ~800 tokens for image

    if budget is not None:
        if not budget.try_reserve("visual", est, stage=_BUDGET_STAGE_TITLES):
            logger.debug(
                "[page_tagger] title budget exhausted for page {}",
                page.page_index,
            )
            return []

    try:
        with open(page.image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as exc:
        logger.warning(
            "[page_tagger] failed to read PNG for title detection page {}: {}",
            page.page_index, exc,
        )
        if budget is not None:
            budget.refund("visual", est=est, stage=_BUDGET_STAGE_TITLES)
        return []

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
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
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                usage_task="page_memory.title_detection",
            )
            if budget is not None:
                budget.commit(
                    "visual",
                    actual=usage.get("total_tokens", est),
                    est=est,
                    stage=_BUDGET_STAGE_TITLES,
                )

            data = json.loads(raw_response)
            titles_raw = data.get("titles", [])
            if not isinstance(titles_raw, list):
                return []

            observed: list[dict[str, Any]] = []
            for item in titles_raw:
                if isinstance(item, dict) and item.get("text"):
                    text = str(item["text"]).strip()
                    
                    is_table = item.get("is_in_table") is True
                    is_header = item.get("is_in_header_footer") is True
                    
                    if is_table or is_header:
                        logger.debug(
                            "[page_tagger] filtered CoT title on page {}: '{}' (table={}, header={})",
                            page.page_index, text, is_table, is_header
                        )
                        continue

                    if text:
                        prominence = None
                        try:
                            prominence = float(item.get("prominence", 0.5))
                        except (TypeError, ValueError):
                            pass
                        observed.append({
                            "text": text,
                            "prominence": prominence,
                            "is_in_table": is_table,
                            "is_in_header_footer": is_header
                        })
            return observed

        except json.JSONDecodeError:
            if attempt < _MAX_JSON_RETRIES:
                continue
            logger.warning(
                "[page_tagger] title JSON retry exhausted for page {}",
                page.page_index,
            )
            return []
        except Exception as exc:
            logger.warning(
                "[page_tagger] title VLM failed for page {}: {}",
                page.page_index, exc,
            )
            if budget is not None:
                budget.refund("visual", est=est, stage=_BUDGET_STAGE_TITLES)
            return []

    return []
