"""Two-TOC VLM checks: paired duplicates (proximity gated) and nesting."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.anchoring_primitives import (
    toc_range_end,
    toc_range_start,
)
from app.services.document_agent.visual import render_pages

_LOG_PREFIX = "[profile.toc_pairing]"
_HEAD_TAIL_EDGE = 2
_TOC_EXPAND_PNG = re.compile(r"^toc_a\d+_p(\d+)\.png$")
_TOC_ANCHOR_PNG = re.compile(r"^toc_anchor_page_(\d+)\.png$")
_TOC_PAIR_PNG = re.compile(r"^toc_pair_page_(\d+)\.png$")


@dataclass(frozen=True)
class PairedTocPattern:
    code: str
    title: str
    description: str


@dataclass(frozen=True)
class PairedTocMatch:
    is_paired: bool
    pattern: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class NestingAnswer:
    inside: bool | None
    """Whether TOC B lies inside TOC A; ``None`` when the check did not answer."""
    reason: str = ""


PAIRED_TOC_PATTERNS: tuple[PairedTocPattern, ...] = (
    PairedTocPattern(
        code="bilingual_mirror",
        title="Bilingual / Multi-language Mirror TOC",
        description=(
            "TOC B is a translated version of TOC A (e.g. Chinese '目次/目录' "
            "followed by English 'Contents') listing the same document sections "
            "and pointing to the same body page numbers."
        ),
    ),
    # Additional paired patterns (e.g. summary-vs-detailed duplicate front TOC)
    # can be registered here.
)


def toc_pages_from_range(hierarchy: dict[str, Any]) -> list[int]:
    start = toc_range_start(hierarchy)
    end = toc_range_end(hierarchy)
    if start is None or end is None or end < start:
        return []
    return list(range(start, end + 1))


def gap_pages_between(toc_a: dict[str, Any], toc_b: dict[str, Any]) -> int | None:
    end_a = toc_range_end(toc_a)
    start_b = toc_range_start(toc_b)
    if end_a is None or start_b is None:
        return None
    return max(0, start_b - end_a - 1)


def proximity_allows_pair_check(
    toc_a: dict[str, Any],
    toc_b: dict[str, Any],
) -> bool:
    pages_a = toc_pages_from_range(toc_a)
    gap_pages = gap_pages_between(toc_a, toc_b)
    if not pages_a or gap_pages is None:
        return False
    return gap_pages < len(pages_a)


def sample_head_tail_pages(
    pages: list[int],
    *,
    max_edge: int = _HEAD_TAIL_EDGE,
) -> list[int]:
    ordered = sorted({int(page) for page in pages if int(page) >= 1})
    if len(ordered) <= max_edge * 2:
        return ordered
    sampled: list[int] = []
    for page in [*ordered[:max_edge], *ordered[-max_edge:]]:
        if page not in sampled:
            sampled.append(page)
    return sampled


def check_paired_toc(
    toc_a: dict[str, Any],
    toc_b: dict[str, Any],
    *,
    ctx: ToolContext,
    model: str | None,
) -> PairedTocMatch:
    """Return whether ``toc_b`` is a paired duplicate of ``toc_a``.

    VLM runs only when the proximity gate holds. A failed or incomplete check
    is ``is_paired=False`` so the caller continues on the calibrate path.
    """
    if not proximity_allows_pair_check(toc_a, toc_b):
        return PairedTocMatch(
            is_paired=False,
            reason="proximity gate not met",
        )
    if not model:
        return PairedTocMatch(
            is_paired=False,
            reason="no VLM model configured",
        )

    pages_a = sample_head_tail_pages(toc_pages_from_range(toc_a))
    pages_b = sample_head_tail_pages(toc_pages_from_range(toc_b))
    if not pages_a or not pages_b:
        return PairedTocMatch(
            is_paired=False,
            reason="missing TOC page range",
        )

    try:
        pngs_a = _resolve_toc_page_pngs(ctx, pages_a)
        pngs_b = _resolve_toc_page_pngs(ctx, pages_b)
    except Exception as exc:
        logger.warning("{} resolve TOC page images failed: {}", _LOG_PREFIX, exc)
        return PairedTocMatch(
            is_paired=False,
            reason=f"resolve page images failed: {exc}",
        )

    return _vlm_pair_check(
        pages_a=pages_a,
        pages_b=pages_b,
        pngs_a=pngs_a,
        pngs_b=pngs_b,
        model=model,
    )


def _toc_pages_dir(ctx: ToolContext) -> Path:
    return Path(ctx.output_dir or os.path.expanduser("~/.knowhere/_debug_profile")) / (
        "toc_pages"
    )


def _index_existing_toc_pngs(toc_dir: Path) -> dict[int, str]:
    found: dict[int, str] = {}
    if not toc_dir.is_dir():
        return found
    for path in toc_dir.iterdir():
        if not path.is_file():
            continue
        page: int | None = None
        for pattern in (_TOC_EXPAND_PNG, _TOC_ANCHOR_PNG, _TOC_PAIR_PNG):
            matched = pattern.match(path.name)
            if matched:
                page = int(matched.group(1))
                break
        if page is None:
            continue
        found.setdefault(page, str(path))
    return found


def _resolve_toc_page_pngs(ctx: ToolContext, pages: list[int]) -> list[tuple[int, str]]:
    toc_dir = _toc_pages_dir(ctx)
    toc_dir.mkdir(parents=True, exist_ok=True)
    existing = _index_existing_toc_pngs(toc_dir)
    resolved: dict[int, str] = {}
    missing: list[int] = []
    for page in pages:
        png_path = existing.get(page)
        if png_path and os.path.isfile(png_path):
            resolved[page] = png_path
        else:
            missing.append(page)

    if missing:
        dpi = int(ctx.settings.get("toc_png_dpi", "144"))
        rendered = render_pages(
            ctx,
            missing,
            folder_name="toc_pages",
            prefix="toc_pair",
            dpi=dpi,
        )
        for item in rendered:
            if not isinstance(item, dict):
                continue
            page = int(item["page"])
            png_path = str(item.get("png_path") or "")
            if png_path and os.path.isfile(png_path):
                resolved[page] = png_path

    page_pngs: list[tuple[int, str]] = []
    unresolved: list[int] = []
    for page in pages:
        png_path = resolved.get(page)
        if not png_path:
            unresolved.append(page)
            continue
        page_pngs.append((page, png_path))
    if unresolved:
        raise RuntimeError(f"paired TOC page images missing: {unresolved}")
    return page_pngs


def _format_paired_patterns() -> str:
    lines = []
    for pattern in PAIRED_TOC_PATTERNS:
        lines.append(f"- {pattern.code}: {pattern.title}. {pattern.description}")
    return "\n".join(lines)


def _build_paired_toc_prompt(pages_a: list[int], pages_b: list[int]) -> str:
    return (
        "You are a document structure analysis expert.\n"
        "You are given sample page screenshots (up to the first 2 pages and last 2 pages) "
        "from two consecutive Table of Contents regions in the same PDF:\n"
        f"- TOC A: pages {pages_a}\n"
        f"- TOC B: pages {pages_b}\n"
        "\n"
        "Determine whether TOC B is a **paired duplicate / parallel version** of TOC A "
        "covering the SAME body sections (so only TOC A needs to be kept as the "
        "structural skeleton), or an **independent TOC** for a different part of the "
        "document.\n"
        "\n"
        "Known paired duplicate patterns:\n"
        f"{_format_paired_patterns()}\n"
        "\n"
        " NOT a paired duplicate (return is_paired: false):\n"
        "- TOC B is a sub-table of contents for a later chapter, appendix, or volume "
        "with different sections.\n"
        "- TOC B is a List of Tables / List of Figures / Exhibit Index rather than a "
        "duplicate section TOC.\n"
        "- TOC B continues numbering from where TOC A left off (e.g. TOC A ends at "
        "Chapter 5 and TOC B starts at Chapter 6).\n"
        "\n"
        "Return strict json (no markdown fences):\n"
        '{"is_paired": true/false, "pattern": "<pattern_code or null>", '
        '"reason": "brief explanation"}'
    )


def _parse_pair_response(raw: str) -> PairedTocMatch:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("paired TOC VLM response is not a json object")
    pattern_raw = data.get("pattern")
    pattern: str | None
    if pattern_raw is None or pattern_raw == "":
        pattern = None
    else:
        pattern = str(pattern_raw)
        known = {item.code for item in PAIRED_TOC_PATTERNS}
        if pattern not in known:
            pattern = None
    return PairedTocMatch(
        is_paired=bool(data.get("is_paired")),
        pattern=pattern,
        reason=str(data.get("reason") or ""),
    )


def _vlm_pair_check(
    *,
    pages_a: list[int],
    pages_b: list[int],
    pngs_a: list[tuple[int, str]],
    pngs_b: list[tuple[int, str]],
    model: str,
) -> PairedTocMatch:
    try:
        raw = _ask_vlm_about_two_tocs(
            prompt=_build_paired_toc_prompt(pages_a, pages_b),
            pngs_a=pngs_a,
            pngs_b=pngs_b,
            model=model,
            usage_task="document_agent.toc_pair_check",
        )
        match = _parse_pair_response(raw)
        logger.info(
            "{} VLM pair check pages_a={} pages_b={} is_paired={} pattern={}",
            _LOG_PREFIX,
            pages_a,
            pages_b,
            match.is_paired,
            match.pattern,
        )
        return match
    except Exception as exc:
        logger.warning("{} VLM pair check failed: {}", _LOG_PREFIX, exc)
        return PairedTocMatch(
            is_paired=False,
            reason=f"VLM pair check failed: {exc}",
        )


def check_toc_nesting(
    toc_a: dict[str, Any],
    toc_b: dict[str, Any],
    *,
    ctx: ToolContext,
    model: str | None,
) -> NestingAnswer:
    """Return whether ``toc_b``, which comes later in the PDF, lies inside ``toc_a``.

    An unsure model answers no: keeping a nested TOC separate only ends a scope
    early, while a wrong nesting would stretch a scope over content its TOC does
    not list. ``inside`` is ``None`` when no answer was obtained.
    """
    if not model:
        return NestingAnswer(inside=None, reason="no VLM model configured")

    pages_a = sample_head_tail_pages(toc_pages_from_range(toc_a))
    pages_b = sample_head_tail_pages(toc_pages_from_range(toc_b))
    if not pages_a or not pages_b:
        return NestingAnswer(inside=None, reason="missing TOC page range")

    try:
        raw = _ask_vlm_about_two_tocs(
            prompt=_build_toc_nesting_prompt(pages_a, pages_b),
            pngs_a=_resolve_toc_page_pngs(ctx, pages_a),
            pngs_b=_resolve_toc_page_pngs(ctx, pages_b),
            model=model,
            usage_task="document_agent.toc_nesting_check",
        )
        nesting = _parse_nesting_response(raw)
    except Exception as exc:
        logger.warning("{} VLM nesting check failed: {}", _LOG_PREFIX, exc)
        return NestingAnswer(inside=None, reason=f"VLM nesting check failed: {exc}")
    logger.info(
        "{} VLM nesting check pages_a={} pages_b={} inside={}",
        _LOG_PREFIX,
        pages_a,
        pages_b,
        nesting.inside,
    )
    return nesting


def _build_toc_nesting_prompt(pages_a: list[int], pages_b: list[int]) -> str:
    return (
        "You are a document structure analysis expert.\n"
        "You are given sample page screenshots (up to the first 2 pages and last 2 pages) "
        "from two Table of Contents regions in the same PDF; TOC B comes after TOC A:\n"
        f"- TOC A: pages {pages_a}\n"
        f"- TOC B: pages {pages_b}\n"
        "\n"
        "Does TOC B lie inside the content that TOC A covers?\n"
        "- true: everything TOC B lists lies within the content TOC A covers, e.g. "
        "TOC B is the contents of one chapter, part, appendix or volume that TOC A "
        "lists. TOC B may number its pages from 1 again.\n"
        "- false: TOC B lists content outside what TOC A covers, e.g. a later part, "
        "a separate document, or another language edition of the whole document.\n"
        "\n"
        "If you are not sure, answer false.\n"
        "\n"
        "Return strict json (no markdown fences):\n"
        '{"inside": true | false, "reason": "brief explanation"}'
    )


def _parse_nesting_response(raw: str) -> NestingAnswer:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("TOC nesting VLM response is not a json object")
    inside = data.get("inside")
    if not isinstance(inside, bool):
        raise ValueError(f"TOC nesting answer is not a boolean: {inside!r}")
    return NestingAnswer(inside=inside, reason=str(data.get("reason") or ""))


def _ask_vlm_about_two_tocs(
    *,
    prompt: str,
    pngs_a: list[tuple[int, str]],
    pngs_b: list[tuple[int, str]],
    model: str,
    usage_task: str,
) -> str:
    """Send the prompt with TOC A and TOC B page images; return the raw json."""
    import base64

    from shared.core.config import settings
    from shared.services.ai.llm_overrides import get_vision_client

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for label, page_pngs in (("TOC A", pngs_a), ("TOC B", pngs_b)):
        for page_num, png_path in page_pngs:
            with open(png_path, "rb") as handle:
                img_b64 = base64.b64encode(handle.read()).decode()
            content_parts.append(
                {"type": "text", "text": f"\n--- {label} page {page_num} ---"}
            )
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                }
            )

    client, resolved_model = get_vision_client(requested_model=model)
    raw, _usage = client.chat_completion_with_usage(
        messages=cast(Any, [{"role": "user", "content": content_parts}]),
        model=resolved_model or model,
        temperature=0.0,
        max_tokens=settings.TOC_RELATION_VLM_MAX_TOKENS,
        response_format={"type": "json_object"},
        usage_task=usage_task,
    )
    return raw
