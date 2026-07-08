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
from shared.services.ai.summary.engine import summarize
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

    Each entry is ``{"text": str, "prominence": float | None}``.
    Empty list means no titles were detected (or title detection was skipped).
    """


_MAX_JSON_RETRIES = 1


@dataclass
class PageTitleContext:
    """Spatial context for position-aware VLM title detection."""

    coarse_title: str
    is_first_page: bool
    is_last_page: bool
    next_sibling_title: str | None = None
    scan_direction: str = "top_to_bottom_left_to_right"
_DEFAULT_FINE_MIN_PAGES = 4


def tag_pages(
    *,
    pages: list[PageRenderResult],
    plans: list[PagePlan],
    budget: Any | None = None,
    vlm_model: str | None = None,
    max_concurrent: int | None = None,
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

    resolved_max_concurrent = max_concurrent or int(
        getattr(settings, "SUMMARY_LLM_MAX_CONCURRENT", 4)
    )
    if not pages:
        return []

    def _tag_one(page: PageRenderResult) -> PageTagResult:
        plan = plan_map.get(page.page_index)
        strategy = plan.strategy if plan else PageProcessingStrategy.VLM_LITE

        if strategy == PageProcessingStrategy.SKIP_TAGGING:
            return _tag_skip(page)

        if strategy == PageProcessingStrategy.TEXT_ONLY:
            return _tag_text_only(page)

        if not model:
            logger.warning(
                "[page_tagger] no VLM model configured for page {}; using text_only",
                page.page_index,
            )
            return _tag_text_only(page)

        return _tag_vlm_lite(page, model=model)

    pool = GeventPool(size=min(resolved_max_concurrent, len(pages)))
    greenlets = [pool.spawn(_tag_one, page) for page in pages]
    gevent.joinall(greenlets, raise_error=True)

    results = [cast(PageTagResult, g.value) for g in greenlets]
    vlm_calls = sum(1 for r in results if r.strategy_used == "vlm_lite")
    logger.info(
        "[page_tagger] tagged {} pages ({} VLM calls, {} text_only, {} skipped, {} failed) concurrency={}",
        len(results),
        vlm_calls,
        sum(1 for r in results if r.strategy_used == "text_only"),
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


def _tag_text_only(
    page: PageRenderResult,
) -> PageTagResult:
    """Extract summary + entities from raw page text via page-memory-text-tag.

    Uses the same output spec ({summary, entities}) as the VLM path so
    downstream consumers need no special-casing. Returns an EMPTY-marked
    result when the page has no extractable text.
    """
    raw = page.raw_text.strip()
    if not raw:
        return PageTagResult(
            page_index=page.page_index,
            summary="EMPTY",
            keywords=[],
            strategy_used="text_only",
        )

    model = os.environ.get("NORMOL_MODEL", "deepseek-v4-flash")
    text_input = raw[:4000]  # cap to avoid token overflow
    prompt, temperature, top_p, max_tokens = build_prompt(
        "page-memory-text-tag",
        "",
        "",
        paras={"max_tokens": 600, "page_text": text_input},
    )

    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=model)
        raw_response, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": prompt}]),
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            usage_task="page_memory.text_tag",
        )
    except UnavailableException:
        raise
    except Exception as exc:
        logger.warning(
            "[page_tagger] text_only LLM failed for page {}: {}",
            page.page_index,
            exc,
        )
        return PageTagResult(
            page_index=page.page_index,
            summary="",
            keywords=[],
            strategy_used="text_only",
        )

    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError:
        return PageTagResult(
            page_index=page.page_index,
            summary="",
            keywords=[],
            strategy_used="text_only",
        )

    entities_raw = data.get("entities") or []
    entities = [
        e for e in entities_raw
        if isinstance(e, dict) and e.get("text")
    ]
    return PageTagResult(
        page_index=page.page_index,
        summary=str(data.get("summary") or "").strip(),
        keywords=[str(e["text"]) for e in entities],
        entities=entities,
        strategy_used="text_only",
    )


def _tag_vlm_lite(
    page: PageRenderResult,
    *,
    model: str,
) -> PageTagResult:
    """Send page PNG to the VLM via the unified engine."""
    if not page.image_path or not os.path.exists(page.image_path):
        logger.warning(
            "[page_tagger] no PNG for page {}; text_only fallback",
            page.page_index,
        )
        return _tag_text_only(page)

    result = summarize(
        mode="page",
        image_paths=[page.image_path],
        model=model,
        usage_task="page_memory.tag",
    )
    return PageTagResult(
        page_index=page.page_index,
        summary=result.summary,
        keywords=[e.text for e in result.entities],
        entities=[e.to_dict() for e in result.entities],
        strategy_used="vlm_lite",
    )


# ── Step 2: Independent title candidate extraction ───────────────────


def get_fine_min_pages() -> int:
    """Default fat-leaf gating threshold."""
    return _DEFAULT_FINE_MIN_PAGES


def tag_page_titles(
    *,
    pages: list[PageRenderResult],
    tag_results: list[PageTagResult],
    fat_leaf_pages: set[int],
    budget: Any | None = None,
    vlm_model: str | None = None,
    coarse_skeletons: list[Any] | None = None,
    scan_direction: str = "top_to_bottom_left_to_right",
    max_concurrent: int | None = None,
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
        (those with more than the configured fine-min-page threshold).
    budget:
        Deprecated, ignored. Kept for call-site compatibility.
    vlm_model:
        VLM model name; falls back to ``$IMAGE_MODEL``.
    coarse_skeletons:
        Sorted list of coarse skeletons for this scope. When provided,
        enables position-aware spatial constraints on first/last pages.
    scan_direction:
        Reading direction for boundary constraint wording.
    max_concurrent:
        Maximum concurrent title-detection calls.

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

    ctx_map: dict[int, PageTitleContext] = {}
    if coarse_skeletons:
        ctx_map = _build_page_title_context_map(
            coarse_skeletons, fat_leaf_pages, scan_direction,
        )

    tag_map = {t.page_index: t for t in tag_results}
    page_map = {p.page_index: p for p in pages}
    work_items = [
        (page_idx, page, tag_map[page_idx])
        for page_idx in sorted(fat_leaf_pages)
        if (page := page_map.get(page_idx)) is not None
        and page_idx in tag_map
        and page.image_path
        and os.path.exists(page.image_path)
    ]
    if not work_items:
        return tag_results

    from shared.core.config import settings

    resolved_max_concurrent = max_concurrent or int(
        getattr(settings, "PAGE_MEMORY_TITLE_DETECTION_CONCURRENCY", 3)
    )

    def _detect_one(
        page_idx: int,
        page: PageRenderResult,
    ) -> tuple[int, list[dict[str, Any]]]:
        return page_idx, _tag_vlm_titles(page, model=model, title_ctx=ctx_map.get(page_idx))

    import gevent
    from gevent.pool import Pool as GeventPool

    pool = GeventPool(size=min(resolved_max_concurrent, len(work_items)))
    greenlets = [
        pool.spawn(_detect_one, page_idx, page)
        for page_idx, page, _tag in work_items
    ]
    gevent.joinall(greenlets, raise_error=True)

    title_pairs = [
        cast(tuple[int, list[dict[str, Any]]], greenlet.value)
        for greenlet in greenlets
    ]
    titles_by_page: dict[int, list[dict[str, Any]]] = dict(title_pairs)
    for page_idx, _page, tag in work_items:
        observed = titles_by_page.get(page_idx, [])
        tag.observed_titles = observed

    vlm_calls = len(work_items)
    titles_found = sum(len(titles) for titles in titles_by_page.values())

    logger.info(
        "[page_tagger] title detection: {} VLM calls on {} fat-leaf pages, {} titles found concurrency={}",
        vlm_calls,
        len(fat_leaf_pages),
        titles_found,
        resolved_max_concurrent,
    )
    return tag_results


def _build_page_title_context_map(
    coarse_skeletons: list[Any],
    fat_leaf_pages: set[int],
    scan_direction: str,
) -> dict[int, PageTitleContext]:
    """Map each fat-leaf page to its spatial position context."""
    ctx_map: dict[int, PageTitleContext] = {}
    for idx, skel in enumerate(coarse_skeletons):
        exclusive_end = _title_exclusive_end(coarse_skeletons, idx)
        next_title = (
            coarse_skeletons[idx + 1].title
            if idx + 1 < len(coarse_skeletons)
            else None
        )
        for page in range(skel.start_page, exclusive_end + 1):
            if page not in fat_leaf_pages:
                continue
            ctx_map[page] = PageTitleContext(
                coarse_title=skel.title,
                is_first_page=(page == skel.start_page),
                is_last_page=(page == exclusive_end),
                next_sibling_title=next_title,
                scan_direction=scan_direction,
            )
    return ctx_map


def _title_exclusive_end(skeletons: list[Any], index: int) -> int:
    """Last page owned by a skeleton before the next sibling starts."""
    skeleton = skeletons[index]
    later_starts = [
        skel.start_page for skel in skeletons if skel.start_page > skeleton.start_page
    ]
    if not later_starts:
        return skeleton.end_page
    return min(skeleton.end_page, min(later_starts) - 1)


def _tag_vlm_titles(
    page: PageRenderResult,
    *,
    model: str,
    title_ctx: PageTitleContext | None = None,
) -> list[dict[str, Any]]:
    """Send page PNG to VLM with the title-only prompt and parse results."""
    paras: dict[str, Any] = {"max_tokens": 300}
    if title_ctx is not None:
        paras["coarse_title"] = title_ctx.coarse_title
        paras["is_first_page"] = title_ctx.is_first_page
        paras["is_last_page"] = title_ctx.is_last_page
        paras["next_sibling_title"] = title_ctx.next_sibling_title
        paras["scan_direction"] = title_ctx.scan_direction
    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-vlm-title",
        "",
        "",
        paras=paras,
    )

    try:
        with open(page.image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as exc:
        logger.warning(
            "[page_tagger] failed to read PNG for title detection page {}: {}",
            page.page_index, exc,
        )
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
                        except (TypeError, ValueError) as exc:
                            logger.debug(
                                "[page_tagger] ignored non-numeric title prominence {}: {}",
                                item.get("prominence"),
                                exc,
                            )
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
        except UnavailableException:
            raise
        except Exception as exc:
            logger.warning(
                "[page_tagger] title VLM failed for page {}: {}",
                page.page_index, exc,
            )
            return []

    return []
