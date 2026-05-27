"""VLM-native TOC entry extraction and hierarchy conversion."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, cast


# ---------------------------------------------------------------------------
# Batch-mode prompt: send a window of candidate pages in one VLM call.
# The VLM first classifies each page, then extracts entries only from TOC pages.
# ---------------------------------------------------------------------------

VLM_TOC_BATCH_PROMPT = """\
You will receive {page_count} consecutive page screenshots from a document.
Some of these pages may be Table of Contents (TOC) pages, while others may be
regular body text, section dividers, blank pages, or other non-TOC content.

**Your task has two parts:**

### Part 1: Classify each page
For each page, decide whether it is a TOC page or not.

A page IS a TOC page when it shows a STRUCTURED LISTING of document sections,
recognizable by MOST of these visual patterns:
- Multiple entry lines, each pairing a section/chapter TITLE with a PAGE NUMBER
- Leader characters (dots "......", dashes "------", or whitespace) connecting
  titles on the left to page numbers aligned on the right
- Systematic numbering in the titles (1. / 1.1 / Chapter 1 / 一、 / 第一章, etc.)
- An explicit heading such as "Table of Contents", "Contents", "目录", or "目次"
  (may appear only on the first page of a multi-page TOC)

A page is NOT a TOC page when:
- It contains narrative paragraphs or body text, even if the text has numbered
  headings (e.g. "1.0.1  为建立并落实..." followed by explanatory sentences)
- It is a section divider / title page with only a single heading and no listing
- It is blank or nearly blank
- It shows data tables, charts, or images rather than a contents listing
- It has numbered definitions or terms with explanations (e.g. "2.0.3 风险 risk")
  — these are glossary/body content, NOT a TOC

The KEY distinction: TOC entries are SHORT titles pointing to page numbers.
Body text has EXPLANATORY content after the heading. If a numbered item is
followed by sentences of explanation, it is body text, not a TOC entry.

### Part 2: Extract entries from TOC pages only
For each page you classify as TOC, extract every entry with:
- title: the section/chapter name, verbatim, without trailing dots or leaders.
  Combine wrapped lines into one string. Include numbering prefixes.
- page_number: integer for plain numbers, string for non-numeric (iv, F-1),
  null when no page reference is visible.
- level: hierarchy depth from visual cues (1=top-level, 2=indented sub-entry, 3+=deeper).
  Category headers or group labels without page numbers → level 1.

Do NOT include the TOC heading itself ("Table of Contents", "目录", etc.) or
column labels ("Page", "页码").

Return strict JSON (no markdown fences):
{{
  "pages": [
    {{
      "page": <page_number>,
      "is_toc": true/false,
      "entries": [{{"title": "...", "page_number": ..., "level": ...}}, ...]
    }},
    ...
  ]
}}

For non-TOC pages, set "entries" to an empty array [].
"""

VLM_TOC_BATCH_CONTINUATION = """\

--- Continuation Context ---
Previous batch(es) already confirmed TOC pages and extracted these entries:

{previous_summary}

Last active section: Level {last_l1_level}: "{last_l1_title}"

Use this to maintain hierarchy consistency for any TOC pages in this batch.
"""


@dataclass
class BatchPageResult:
    """Result for a single page within a batch VLM call."""

    page: int
    is_toc: bool
    entries: list[dict[str, Any]]


@dataclass
class BatchTocResult:
    """Result from a batch VLM TOC extraction call."""

    page_results: list[BatchPageResult]
    toc_pages: list[int]  # pages classified as TOC
    non_toc_pages: list[int]  # pages classified as non-TOC
    all_entries: list[dict[str, Any]]  # entries from TOC pages only
    meta: dict[str, Any]


def _build_batch_continuation(previous_entries: list[dict[str, Any]]) -> str:
    """Build continuation context for batch mode."""
    if not previous_entries:
        return ""

    tail = previous_entries[-8:]
    summary_lines = []
    for entry in tail:
        level = entry.get("level", "?")
        title = entry.get("title", "?")
        page_number = entry.get("page_number")
        suffix = f" -> p.{page_number}" if page_number is not None else ""
        summary_lines.append(f"  L{level}: {title}{suffix}")

    if len(previous_entries) > 8:
        summary_lines.insert(
            0, f"  ... ({len(previous_entries) - 8} earlier entries omitted)"
        )

    previous_summary = "\n".join(summary_lines)
    last_l1 = None
    for entry in reversed(previous_entries):
        if entry.get("level") == 1:
            last_l1 = entry
            break

    if last_l1 is None:
        return (
            "\n\n--- Continuation Context ---\n"
            f"Previous batch extracted entries:\n{previous_summary}\n"
        )

    return VLM_TOC_BATCH_CONTINUATION.format(
        previous_summary=previous_summary,
        last_l1_level=last_l1.get("level", 1),
        last_l1_title=last_l1.get("title", "?"),
    )


def vlm_extract_toc_batch(
    *,
    page_pngs: list[tuple[int, str]],
    model: str,
    previous_entries: list[dict[str, Any]] | None = None,
) -> BatchTocResult:
    """Extract TOC entries from a batch of page images in a single VLM call.

    Args:
        page_pngs: list of (page_number, png_path) pairs, in page order.
        model: VLM model name.
        previous_entries: entries from prior batches, for continuation context.

    Returns:
        BatchTocResult with per-page classification and extracted entries.
    """
    from loguru import logger
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    if not page_pngs:
        return BatchTocResult(
            page_results=[], toc_pages=[], non_toc_pages=[],
            all_entries=[], meta={},
        )

    prompt_text = VLM_TOC_BATCH_PROMPT.format(page_count=len(page_pngs))
    prompt_text += _build_batch_continuation(previous_entries or [])

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt_text},
    ]
    for page_num, png_path in page_pngs:
        with open(png_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content_parts.append(
            {"type": "text", "text": f"\n--- Page {page_num} ---"}
        )
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )

    start = time.monotonic()
    client = get_openai_client(model=model)
    raw, usage = client.chat_completion_with_usage(
        messages=cast(Any, [{"role": "user", "content": content_parts}]),
        model=model,
        temperature=0.1,
        max_tokens=8192,
        response_format={"type": "json_object"},
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)

    data = json.loads(raw)
    raw_pages: list[dict[str, Any]] = []
    if isinstance(data, dict):
        raw_pages = data.get("pages", [])
    elif isinstance(data, list):
        raw_pages = data

    # Build lookup from VLM response
    page_lookup: dict[int, dict[str, Any]] = {}
    for item in raw_pages:
        if isinstance(item, dict) and "page" in item:
            page_lookup[int(item["page"])] = item

    # Process results for each page in the original order
    page_results: list[BatchPageResult] = []
    toc_pages: list[int] = []
    non_toc_pages: list[int] = []
    all_entries: list[dict[str, Any]] = []

    for page_num, _png_path in page_pngs:
        vlm_page = page_lookup.get(page_num, {})
        is_toc = bool(vlm_page.get("is_toc", False))
        raw_entries = vlm_page.get("entries", [])

        entries: list[dict[str, Any]] = []
        if is_toc:
            for entry_item in raw_entries:
                if not isinstance(entry_item, dict):
                    continue
                title = str(entry_item.get("title") or "").strip()
                if not title:
                    continue
                try:
                    level = int(entry_item.get("level") or 1)
                except (TypeError, ValueError):
                    level = 1
                entries.append(
                    {
                        "title": title,
                        "page_number": entry_item.get("page_number"),
                        "level": level,
                    }
                )
            toc_pages.append(page_num)
        else:
            non_toc_pages.append(page_num)

        page_results.append(
            BatchPageResult(page=page_num, is_toc=is_toc, entries=entries)
        )
        all_entries.extend(entries)

    logger.info(
        "[vlm_toc_batch] {} pages: toc={} non_toc={} entries={} elapsed={}ms",
        len(page_pngs),
        toc_pages,
        non_toc_pages,
        len(all_entries),
        elapsed_ms,
    )

    return BatchTocResult(
        page_results=page_results,
        toc_pages=toc_pages,
        non_toc_pages=non_toc_pages,
        all_entries=all_entries,
        meta={
            "pages_sent": [p for p, _ in page_pngs],
            "model": model,
            "elapsed_ms": elapsed_ms,
            "usage": dict(usage),
            "raw_response_length": len(raw),
            "has_continuation_context": bool(previous_entries),
        },
    )



def build_toc_tree(entries: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[dict[str, Any], int]] = [(root, 0)]
    positive_levels = [
        int(entry["level"])
        for entry in entries
        if isinstance(entry.get("level"), int) and entry["level"] > 0
    ]
    level_for_minus_one = max(positive_levels) + 1 if positive_levels else 1

    for entry in entries:
        heading = str(entry.get("title") or "").strip()
        if not heading:
            continue
        original_level = entry.get("level", 1)
        level = level_for_minus_one if original_level == -1 else int(original_level or 1)
        while len(stack) > 1 and stack[-1][1] >= level:
            stack.pop()
        parent = stack[-1][0]
        parent[heading] = {}
        stack.append((parent[heading], level))
    return root


def build_toc_with_level_md(entries: list[dict[str, Any]]) -> str:
    """Build a compact Markdown table of TOC entries (heading + level only).

    Accepts both raw VLM entries (key='title') and stored toc_with_level
    dicts (key='heading'). Call this dynamically at consumption time rather
    than persisting the MD string.
    """
    if not entries:
        return ""
    lines = ["| heading | level |", "|---------|-------|"]
    for entry in entries:
        heading = str(
            entry.get("heading") or entry.get("title") or ""
        ).strip().replace("|", "\\|")
        level = entry.get("level", 1)
        lines.append(f"| {heading} | {level} |")
    return "\n".join(lines)


def vlm_entries_to_toc_hierarchies(
    entries: list[dict[str, Any]],
    *,
    toc_page_nums: list[int],
    scan_end_page: int | None = None,
    page_count: int | None = None,
) -> list[dict[str, Any]]:
    if not entries or not toc_page_nums:
        return []

    toc_with_level = []
    for entry in entries:
        toc_with_level.append(
            {
                "heading": str(entry.get("title") or "").strip(),
                "level": entry.get("level", 1),
                "page_number": entry.get("page_number"),
            }
        )

    start_page = min(toc_page_nums)
    end_page = max(toc_page_nums)
    if scan_end_page is None:
        scan_end_page = start_page
    if page_count is not None:
        scan_end_page = min(scan_end_page, page_count)

    return [
        {
            "toc_range": [start_page, end_page],
            "toc_range_unit": "page",
            "scan_range": [start_page, scan_end_page],
            "source": "vlm",
            "toc_with_level": toc_with_level,
            "toc_tree": build_toc_tree(entries),
        }
    ]

