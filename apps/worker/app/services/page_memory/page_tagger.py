"""Single-pass VLM page annotation.

Each non-blank processing page produces summary, typed entities, and verbatim
title candidates in one call. Fine hierarchy consumes ``observed_titles``;
node assembly consumes summary/entities from the same result.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

from loguru import logger

from app.services.page_memory.page_plan import PagePlan, PageProcessingStrategy
from app.services.page_memory.page_renderer import PageRenderResult
from shared.services.ai.prompt_service import build_prompt
from shared.core.exceptions.domain_exceptions import UnavailableException


@dataclass
class PageTagResult:
    """Tagging output for a single page."""

    page_index: int
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    strategy_used: str = ""
    entities: list[dict[str, str]] = field(default_factory=list)
    """Typed entities (§4.4): ``{"text","type"}`` dicts. ``keywords`` is kept as
    the flattened surface-form view for transitional keyword-overlap consumers."""
    observed_titles: list[dict[str, Any]] = field(default_factory=list)
    """Step 2: verbatim title candidates observed on this page.

    Each entry is ``{"text": str}`` (plus optional filter flags when present).
    Empty list means no titles were detected (or title detection was skipped).
    """


_MAX_JSON_RETRIES = 1
# Dense pages can emit long combined JSON; escalate only on truncation.
_PAGE_TAG_TOKEN_BUDGETS: tuple[int, ...] = (800, 1200, 2000)


def tag_pages(
    *,
    pages: list[PageRenderResult],
    plans: list[PagePlan],
    budget: Any | None = None,
    vlm_model: str | None = None,
    max_concurrent: int | None = None,
    body_start_by_page: dict[int, str] | None = None,
    scan_direction: str = "top_to_bottom_left_to_right",
) -> list[PageTagResult]:
    """Tag all pages according to their processing plan.

    Parameters
    ----------
    pages:
        Rendered page results (from ``page_renderer``).
    plans:
        Processing plans (from ``page_plan``).
    budget:
        Deprecated, ignored. Kept for call-site compatibility.
    vlm_model:
        VLM model name; falls back to ``$IMAGE_MODEL``.
    max_concurrent:
        Maximum concurrent page-tagging calls.
    body_start_by_page:
        Optional mixed-page body-start anchors keyed by page index.

    Returns
    -------
    list[PageTagResult]
        One result per page, ordered by page_index.
    """
    import gevent
    from gevent.pool import Pool as GeventPool

    from shared.core.config import settings

    plan_map = {plan.page_index: plan for plan in plans}
    model = vlm_model or os.environ.get("IMAGE_MODEL")
    anchors = body_start_by_page or {}

    resolved_max_concurrent = max_concurrent or int(
        getattr(settings, "SUMMARY_LLM_MAX_CONCURRENT", 4)
    )
    if not pages:
        return []

    def _tag_one(page: PageRenderResult) -> PageTagResult:
        plan = plan_map.get(page.page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_PAGE

        if strategy == PageProcessingStrategy.SKIP_TAGGING:
            return _tag_skip(page)

        if not model:
            logger.warning(
                "[page_tagger] no VLM model configured for page {}; skipping tag",
                page.page_index,
            )
            return _tag_skip(page)

        return _tag_vlm_page(
            page,
            model=model,
            scan_direction=scan_direction,
            body_start_text=anchors.get(page.page_index, ""),
        )

    pool = GeventPool(size=min(resolved_max_concurrent, len(pages)))
    greenlets = [pool.spawn(_tag_one, page) for page in pages]
    gevent.joinall(greenlets, raise_error=True)

    results = [cast(PageTagResult, g.value) for g in greenlets]
    vlm_calls = sum(1 for r in results if r.strategy_used == "vlm_page")
    logger.info(
        "[page_tagger] tagged {} pages ({} VLM calls, {} skipped, {} failed) concurrency={}",
        len(results),
        vlm_calls,
        sum(1 for r in results if r.strategy_used == "skip_tagging"),
        len(greenlets) - len(results),
        resolved_max_concurrent,
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


def _completion_tokens(usage: Any) -> int:
    if isinstance(usage, dict):
        return int(usage.get("completion_tokens") or 0)
    return int(getattr(usage, "completion_tokens", 0) or 0)


def _response_truncated(
    raw_response: str,
    *,
    usage: Any,
    max_tokens: int,
) -> bool:
    """True when the completion likely hit the budget mid-JSON."""
    if max_tokens > 0 and _completion_tokens(usage) >= max_tokens:
        return True
    stripped = (raw_response or "").rstrip()
    if not stripped:
        return False
    return not stripped.endswith("}")


def _parse_observed_titles(
    raw_response: str,
    *,
    page_index: int,
) -> list[dict[str, Any]]:
    data = json.loads(raw_response)
    titles_raw = data.get("titles", [])
    if not isinstance(titles_raw, list):
        return []

    observed: list[dict[str, Any]] = []
    for item in titles_raw:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        text = str(item["text"]).strip()
        is_table = item.get("is_in_table") is True
        is_header = item.get("is_in_header_footer") is True
        if is_table or is_header:
            logger.debug(
                "[page_tagger] filtered CoT title on page {}: '{}' (table={}, header={})",
                page_index,
                text,
                is_table,
                is_header,
            )
            continue
        if not text:
            continue
        observed.append(
            {
                "text": text,
                "is_in_table": is_table,
                "is_in_header_footer": is_header,
            }
        )
    return observed


def _parse_page_tag_response(
    raw_response: str,
    *,
    page_index: int,
) -> PageTagResult:
    data = json.loads(raw_response)
    if not isinstance(data, dict):
        raise ValueError("page tag response must be a JSON object")

    entities: list[dict[str, str]] = []
    entities_raw = data.get("entities")
    if isinstance(entities_raw, list):
        for item in entities_raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            entities.append(
                {
                    "text": text,
                    "type": str(item.get("type") or "").strip(),
                }
            )

    return PageTagResult(
        page_index=page_index,
        summary=str(data.get("summary") or "").strip(),
        keywords=[entity["text"] for entity in entities],
        entities=entities,
        observed_titles=_parse_observed_titles(
            raw_response,
            page_index=page_index,
        ),
        strategy_used="vlm_page",
    )


def _tag_vlm_page(
    page: PageRenderResult,
    *,
    model: str,
    scan_direction: str = "top_to_bottom_left_to_right",
    body_start_text: str = "",
) -> PageTagResult:
    """Extract titles, summary, and entities from one rendered page.

    Completion budget escalates only when the combined JSON is truncated.
    """
    if not page.image_path or not os.path.exists(page.image_path):
        logger.warning(
            "[page_tagger] no PNG for page {}; skipping tag",
            page.page_index,
        )
        return _tag_skip(page)

    paras: dict[str, Any] = {
        "max_tokens": _PAGE_TAG_TOKEN_BUDGETS[0],
        "scan_direction": scan_direction,
    }
    if body_start_text:
        paras["body_start_text"] = body_start_text
    prompt, temperature, _top_p, _default_max_tokens = build_prompt(
        "page-memory-vlm-page",
        "",
        "",
        paras=paras,
    )

    try:
        with open(page.image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as exc:
        logger.warning(
            "[page_tagger] failed to read PNG for page {}: {}",
            page.page_index, exc,
        )
        return _tag_skip(page)

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        },
    ]

    from shared.services.ai.llm_overrides import get_vision_client

    client, resolved_model = get_vision_client(requested_model=model)
    model = resolved_model or model

    last_truncated = False
    for budget_index, max_tokens in enumerate(_PAGE_TAG_TOKEN_BUDGETS):
        for attempt in range(_MAX_JSON_RETRIES + 1):
            try:
                raw_response, usage = client.chat_completion_with_usage(
                    messages=cast(Any, [{"role": "user", "content": content_parts}]),
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    usage_task="page_memory.page_tag",
                )
            except UnavailableException:
                raise
            except Exception as exc:
                logger.warning(
                    "[page_tagger] page VLM failed for page {}: {}",
                    page.page_index,
                    exc,
                )
                return _tag_skip(page)

            try:
                result = _parse_page_tag_response(
                    raw_response,
                    page_index=page.page_index,
                )
            except (json.JSONDecodeError, ValueError):
                truncated = _response_truncated(
                    raw_response,
                    usage=usage,
                    max_tokens=max_tokens,
                )
                if truncated and budget_index + 1 < len(_PAGE_TAG_TOKEN_BUDGETS):
                    last_truncated = True
                    logger.info(
                        "[page_tagger] page JSON truncated on page {} "
                        "(budget={}, completion_tokens={}); escalating",
                        page.page_index,
                        max_tokens,
                        _completion_tokens(usage),
                    )
                    break  # next budget
                if attempt < _MAX_JSON_RETRIES:
                    continue
                logger.warning(
                    "[page_tagger] page JSON retry exhausted for page {}",
                    page.page_index,
                )
                return _tag_skip(page)

            if last_truncated:
                logger.info(
                    "[page_tagger] page tagging recovered on page {} with budget={}",
                    page.page_index,
                    max_tokens,
                )
            return result
        else:
            continue

    if last_truncated:
        logger.warning(
            "[page_tagger] page JSON still truncated on page {} after budgets {}",
            page.page_index,
            list(_PAGE_TAG_TOKEN_BUDGETS),
        )
    return _tag_skip(page)
