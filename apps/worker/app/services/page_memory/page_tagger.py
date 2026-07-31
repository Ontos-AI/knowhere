"""Dual-mode page annotation for the page-memory track.

``visual`` mode: one combined VLM call per physical page for titles, summary,
and entities.

``text`` mode: VLM extracts titles only; DeepSeek summarizes the page body text
and extracts entities. Both stages stay inside the tagging stage with separate
concurrency pools.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from loguru import logger

from app.services.page_memory._utils import slice_text_from_anchor
from app.services.page_memory.page_plan import PagePlan, PageProcessingStrategy
from app.services.page_memory.page_renderer import PageRenderResult
from shared.services.ai.prompt_service import allowed_entity_types, build_prompt
from shared.core.exceptions.domain_exceptions import UnavailableException

TaggingMode = Literal["visual", "text"]

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
    """Verbatim title candidates observed on this page.

    Each entry is ``{"text": str}`` (plus optional filter flags when present).
    Empty list means no titles were detected (or title detection was skipped).
    """
    tagging_mode: str = "visual"
    resolved_body_text: str | None = field(default=None, repr=False, compare=False)
    """Transient OCR cache for node assembly; never serialized to artifacts."""


_MAX_JSON_RETRIES = 1
# Dense pages can emit long combined JSON; escalate only on truncation.
_PAGE_TAG_TOKEN_BUDGETS: tuple[int, ...] = (800, 1200, 2000)
_TITLE_ONLY_TOKEN_BUDGETS: tuple[int, ...] = (600, 1000, 1600)


def normalize_entities(raw_entities: object) -> list[dict[str, str]]:
    """Validate typed entities against the configured allowed type set."""
    if not isinstance(raw_entities, list):
        return []
    allowed = {label.casefold() for label in allowed_entity_types()}
    entities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_entities:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        entity_type = str(item.get("type") or "").strip()
        if not text or not entity_type:
            continue
        if allowed and entity_type.casefold() not in allowed:
            continue
        key = (entity_type.casefold(), text.casefold())
        if key in seen:
            continue
        seen.add(key)
        entities.append({"text": text, "type": entity_type})
    return entities


def tag_pages(
    *,
    pages: list[PageRenderResult],
    plans: list[PagePlan],
    budget: Any | None = None,
    vlm_model: str | None = None,
    max_concurrent: int | None = None,
    body_start_by_page: dict[int, str] | None = None,
    scan_direction: str = "top_to_bottom_left_to_right",
    tagging_mode: TaggingMode = "visual",
    text_summary_concurrency: int | None = None,
    text_summary_model: str | None = None,
) -> list[PageTagResult]:
    """Tag all pages according to their processing plan and tagging mode."""
    del budget  # Deprecated call-site compatibility.
    from shared.core.config import settings

    plan_map = {plan.page_index: plan for plan in plans}
    model = vlm_model or os.environ.get("IMAGE_MODEL")
    anchors = body_start_by_page or {}
    mode: TaggingMode = "text" if tagging_mode == "text" else "visual"

    resolved_max_concurrent = max_concurrent or int(
        getattr(settings, "PAGE_MEMORY_TAG_CONCURRENCY", 5)
    )
    if not pages:
        return []

    if mode == "text":
        return _tag_pages_text_mode(
            pages=pages,
            plan_map=plan_map,
            vlm_model=model,
            max_concurrent=resolved_max_concurrent,
            body_start_by_page=anchors,
            scan_direction=scan_direction,
            text_summary_concurrency=text_summary_concurrency
            or int(getattr(settings, "PAGE_MEMORY_TEXT_SUMMARY_CONCURRENCY", 5)),
            text_summary_model=text_summary_model
            or os.environ.get("NORMOL_MODEL", "deepseek-v4-flash"),
        )

    return _tag_pages_visual_mode(
        pages=pages,
        plan_map=plan_map,
        vlm_model=model,
        max_concurrent=resolved_max_concurrent,
        body_start_by_page=anchors,
        scan_direction=scan_direction,
    )


def _tag_pages_visual_mode(
    *,
    pages: list[PageRenderResult],
    plan_map: dict[int, PagePlan],
    vlm_model: str | None,
    max_concurrent: int,
    body_start_by_page: dict[int, str],
    scan_direction: str,
) -> list[PageTagResult]:
    import gevent
    from gevent.pool import Pool as GeventPool

    total_pages = len(pages)
    completed = {"count": 0}

    def _tag_one(page: PageRenderResult) -> PageTagResult:
        plan = plan_map.get(page.page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_PAGE

        if strategy == PageProcessingStrategy.SKIP_TAGGING:
            result = _tag_skip(page, tagging_mode="visual")
        elif not vlm_model:
            logger.warning(
                "[page_tagger] no VLM model configured for page {}; skipping tag",
                page.page_index,
            )
            result = _tag_skip(page, tagging_mode="visual")
        else:
            result = _tag_vlm_combined(
                page,
                model=vlm_model,
                scan_direction=scan_direction,
                body_start_text=body_start_by_page.get(page.page_index, ""),
            )
        completed["count"] += 1
        logger.info(
            "[page_tagger] progress {}/{} page={} mode=visual",
            completed["count"],
            total_pages,
            page.page_index,
        )
        return result

    pool = GeventPool(size=min(max_concurrent, total_pages))
    greenlets = [pool.spawn(_tag_one, page) for page in pages]
    gevent.joinall(greenlets, raise_error=True)

    results = [cast(PageTagResult, g.value) for g in greenlets]
    logger.info(
        "[page_tagger] tagged {} pages ({} VLM calls, {} skipped) mode=visual concurrency={}",
        len(results),
        sum(1 for r in results if r.strategy_used == "vlm_page"),
        sum(1 for r in results if r.strategy_used == "skip_tagging"),
        max_concurrent,
    )
    return results


def _tag_pages_text_mode(
    *,
    pages: list[PageRenderResult],
    plan_map: dict[int, PagePlan],
    vlm_model: str | None,
    max_concurrent: int,
    body_start_by_page: dict[int, str],
    scan_direction: str,
    text_summary_concurrency: int,
    text_summary_model: str,
) -> list[PageTagResult]:
    import gevent
    from gevent.pool import Pool as GeventPool

    total_pages = len(pages)
    completed = {"count": 0}

    def _titles_one(page: PageRenderResult) -> PageTagResult:
        plan = plan_map.get(page.page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_PAGE
        if strategy == PageProcessingStrategy.SKIP_TAGGING or not vlm_model:
            result = _tag_skip(page, tagging_mode="text")
        else:
            result = _tag_vlm_titles(
                page,
                model=vlm_model,
                scan_direction=scan_direction,
                body_start_text=body_start_by_page.get(page.page_index, ""),
            )
        completed["count"] += 1
        logger.info(
            "[page_tagger] title progress {}/{} page={} mode=text",
            completed["count"],
            total_pages,
            page.page_index,
        )
        return result

    title_pool = GeventPool(size=min(max_concurrent, total_pages))
    title_greenlets = [title_pool.spawn(_titles_one, page) for page in pages]
    gevent.joinall(title_greenlets, raise_error=True)
    title_results = [cast(PageTagResult, g.value) for g in title_greenlets]
    by_page = {result.page_index: result for result in title_results}

    summary_completed = {"count": 0}

    def _summary_one(
        page: PageRenderResult,
    ) -> tuple[int, str, list[dict[str, str]], str | None]:
        plan = plan_map.get(page.page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_PAGE
        if strategy == PageProcessingStrategy.SKIP_TAGGING:
            return page.page_index, "", [], None
        try:
            resolved_text = _resolve_page_text(
                page,
                vlm_model=vlm_model,
            )
        except Exception as exc:
            logger.warning(
                "[page_tagger] text extraction failed for page {}: {}",
                page.page_index,
                exc,
            )
            return page.page_index, "", [], None
        body_text = _slice_page_body_text(
            page_index=page.page_index,
            text=resolved_text,
            body_start_text=body_start_by_page.get(page.page_index, ""),
        )
        summary, entities = _summarize_page_text(
            page,
            model=text_summary_model,
            text=body_text,
        )
        summary_completed["count"] += 1
        logger.info(
            "[page_tagger] text-summary progress {}/{} page={}",
            summary_completed["count"],
            total_pages,
            page.page_index,
        )
        return page.page_index, summary, entities, (
            resolved_text if not page.raw_text.strip() else None
        )

    summary_pool = GeventPool(
        size=min(max(1, text_summary_concurrency), total_pages)
    )
    summary_greenlets = [summary_pool.spawn(_summary_one, page) for page in pages]
    gevent.joinall(summary_greenlets, raise_error=True)

    for greenlet in summary_greenlets:
        page_index, summary, entities, resolved_body_text = cast(
            tuple[int, str, list[dict[str, str]], str | None],
            greenlet.value,
        )
        base = by_page[page_index]
        plan = plan_map.get(page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_PAGE
        if strategy == PageProcessingStrategy.SKIP_TAGGING:
            continue
        base.summary = summary
        base.entities = entities
        base.keywords = [entity["text"] for entity in entities]
        base.strategy_used = "text_page"
        base.tagging_mode = "text"
        base.resolved_body_text = resolved_body_text

    results = [by_page[page.page_index] for page in pages]
    logger.info(
        "[page_tagger] tagged {} pages mode=text "
        "(vlm_title_concurrency={}, text_summary_concurrency={})",
        len(results),
        max_concurrent,
        text_summary_concurrency,
    )
    return results


def _tag_skip(
    page: PageRenderResult,
    *,
    tagging_mode: TaggingMode = "visual",
) -> PageTagResult:
    """Skip-tagging: preserve raw_text but no summary."""
    raw = page.raw_text.strip()
    return PageTagResult(
        page_index=page.page_index,
        summary="" if raw else "EMPTY",
        keywords=[],
        strategy_used="skip_tagging",
        tagging_mode=tagging_mode,
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


def _parse_combined_page_tag_response(
    raw_response: str,
    *,
    page_index: int,
) -> PageTagResult:
    data = json.loads(raw_response)
    if not isinstance(data, dict):
        raise ValueError("page tag response must be a JSON object")

    entities = normalize_entities(data.get("entities"))
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
        tagging_mode="visual",
    )


def _parse_titles_only_response(
    raw_response: str,
    *,
    page_index: int,
) -> PageTagResult:
    data = json.loads(raw_response)
    if not isinstance(data, dict):
        raise ValueError("page title response must be a JSON object")
    return PageTagResult(
        page_index=page_index,
        observed_titles=_parse_observed_titles(
            raw_response,
            page_index=page_index,
        ),
        strategy_used="vlm_titles",
        tagging_mode="text",
    )


def _tag_vlm_combined(
    page: PageRenderResult,
    *,
    model: str,
    scan_direction: str = "top_to_bottom_left_to_right",
    body_start_text: str = "",
) -> PageTagResult:
    return _run_vlm_json_page(
        page,
        model=model,
        scan_direction=scan_direction,
        body_start_text=body_start_text,
        prompt_task="page-memory-vlm-page",
        token_budgets=_PAGE_TAG_TOKEN_BUDGETS,
        parse_response=_parse_combined_page_tag_response,
        usage_task="page_memory.page_tag",
        tagging_mode="visual",
    )


def _tag_vlm_titles(
    page: PageRenderResult,
    *,
    model: str,
    scan_direction: str = "top_to_bottom_left_to_right",
    body_start_text: str = "",
) -> PageTagResult:
    return _run_vlm_json_page(
        page,
        model=model,
        scan_direction=scan_direction,
        body_start_text=body_start_text,
        prompt_task="page-memory-vlm-titles",
        token_budgets=_TITLE_ONLY_TOKEN_BUDGETS,
        parse_response=_parse_titles_only_response,
        usage_task="page_memory.page_titles",
        tagging_mode="text",
    )


def _run_vlm_json_page(
    page: PageRenderResult,
    *,
    model: str,
    scan_direction: str,
    body_start_text: str,
    prompt_task: str,
    token_budgets: tuple[int, ...],
    parse_response: Any,
    usage_task: str,
    tagging_mode: TaggingMode,
) -> PageTagResult:
    if not page.image_path or not os.path.exists(page.image_path):
        logger.warning(
            "[page_tagger] no PNG for page {}; skipping tag",
            page.page_index,
        )
        return _tag_skip(page, tagging_mode=tagging_mode)

    paras: dict[str, Any] = {
        "max_tokens": token_budgets[0],
        "scan_direction": scan_direction,
    }
    if body_start_text:
        paras["body_start_text"] = body_start_text
    prompt, temperature, _top_p, _default_max_tokens = build_prompt(
        prompt_task,
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
        return _tag_skip(page, tagging_mode=tagging_mode)

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
    for budget_index, max_tokens in enumerate(token_budgets):
        for attempt in range(_MAX_JSON_RETRIES + 1):
            try:
                raw_response, usage = client.chat_completion_with_usage(
                    messages=cast(Any, [{"role": "user", "content": content_parts}]),
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    usage_task=usage_task,
                )
            except UnavailableException:
                raise
            except Exception as exc:
                logger.warning(
                    "[page_tagger] page VLM failed for page {}: {}",
                    page.page_index,
                    exc,
                )
                return _tag_skip(page, tagging_mode=tagging_mode)

            try:
                result = parse_response(
                    raw_response,
                    page_index=page.page_index,
                )
            except (json.JSONDecodeError, ValueError):
                truncated = _response_truncated(
                    raw_response,
                    usage=usage,
                    max_tokens=max_tokens,
                )
                if truncated and budget_index + 1 < len(token_budgets):
                    last_truncated = True
                    logger.info(
                        "[page_tagger] page JSON truncated on page {} "
                        "(budget={}, completion_tokens={}); escalating",
                        page.page_index,
                        max_tokens,
                        _completion_tokens(usage),
                    )
                    break
                if attempt < _MAX_JSON_RETRIES:
                    continue
                logger.warning(
                    "[page_tagger] page JSON retry exhausted for page {}",
                    page.page_index,
                )
                return _tag_skip(page, tagging_mode=tagging_mode)

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
            list(token_budgets),
        )
    return _tag_skip(page, tagging_mode=tagging_mode)


def _resolve_page_text(
    page: PageRenderResult,
    *,
    vlm_model: str | None,
) -> str:
    text = (page.raw_text or "").strip()
    if not text and vlm_model and page.image_path and os.path.exists(page.image_path):
        from shared.services.ai.summary.engine import transcribe

        text = transcribe(
            image_paths=[page.image_path],
            model=vlm_model,
            max_tokens=1500,
            usage_task="page_memory.page_tag_ocr",
        ).strip()
    return text


def _slice_page_body_text(
    *,
    page_index: int,
    text: str,
    body_start_text: str,
) -> str:
    if not body_start_text:
        return text
    sliced, matched = slice_text_from_anchor(text, body_start_text)
    if not matched:
        logger.warning(
            "[page_tagger] body_start_text not found on page {}; keeping full text",
            page_index,
        )
        return text
    return sliced


def _summarize_page_text(
    page: PageRenderResult,
    *,
    model: str,
    text: str,
) -> tuple[str, list[dict[str, str]]]:
    from shared.services.ai.summary.engine import summarize

    if not text:
        return "", []

    try:
        result = summarize(
            mode="text",
            text=text,
            model=model,
            usage_task="page_memory.page_text_summary",
            prompt_task="page-memory-text-page",
            prompt_paras={"max_tokens": 600},
        )
    except Exception as exc:
        logger.warning(
            "[page_tagger] text summary failed for page {}: {}",
            page.page_index,
            exc,
        )
        return "", []

    entities = normalize_entities([entity.to_dict() for entity in result.entities])
    return (result.summary or "").strip(), entities
