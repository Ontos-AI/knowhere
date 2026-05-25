"""VLM-native TOC entry extraction and hierarchy conversion."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, cast


VLM_TOC_EXTRACT_PROMPT = """\
You are analyzing a Table of Contents (TOC) page from a document.

Your task is to extract every TOC entry visible on this page.

Each entry consists of three parts:
1. title — the section or chapter name, copied verbatim from the page.
   - EXCLUDE any trailing dots, dashes, or leader lines that connect the title to its page number.
   - If one entry's title wraps across multiple printed lines, combine them into a single string.
   - Include any numbering prefix that is part of the title text.
2. page_number — the page reference at the right side of the entry.
   - Use an integer when the reference is a plain number.
   - Use a string when the reference is non-numeric, such as iv, F-1, or A-3.
   - Use null when no page reference is visible for that entry.
3. level — the hierarchy depth of the entry, determined by visual formatting cues:
   - level 1: top-level entries with no indentation, or the largest / boldest text.
   - level 2: sub-entries indented under a level-1 entry, or in a noticeably smaller font.
   - level 3+: deeper indentation, if present.
   - Category headers or group labels that are visually distinct and do NOT have a page number should be treated as level 1.

Additional rules:
- Extract ALL entries, even if the page only shows a partial continuation of the TOC.
- Do NOT include the TOC page's own heading, such as TABLE OF CONTENTS, 目录, or 目 录.
- Do NOT include column headers, such as a standalone Page or 页码 label.
- Preserve the original language and wording of each title exactly.
- If this screenshot is not actually a TOC page, return {"entries": []}.

Return strict JSON with no markdown fences:
{"entries": [{"title": "...", "page_number": ..., "level": ...}, ...]}
"""

VLM_TOC_CONTINUATION_CONTEXT = """\

--- IMPORTANT: Continuation Context ---
This is a CONTINUATION page of a multi-page Table of Contents.
The previous page(s) already extracted the following entries:

{previous_summary}

The LAST active category/section before this page was:
  Level {last_l1_level}: "{last_l1_title}"

Entries on THIS page that visually continue as sub-items under that
category must keep their correct subordinate level.
"""


def _build_continuation_context(previous_entries: list[dict[str, Any]]) -> str:
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
        summary_lines.insert(0, f"  ... ({len(previous_entries) - 8} earlier entries omitted)")

    previous_summary = "\n".join(summary_lines)
    last_l1 = None
    for entry in reversed(previous_entries):
        if entry.get("level") == 1:
            last_l1 = entry
            break

    if last_l1 is None:
        return (
            "\n\n--- IMPORTANT: Continuation Context ---\n"
            f"This is a CONTINUATION page. Previous entries:\n{previous_summary}\n"
        )

    return VLM_TOC_CONTINUATION_CONTEXT.format(
        previous_summary=previous_summary,
        last_l1_level=last_l1.get("level", 1),
        last_l1_title=last_l1.get("title", "?"),
    )


def vlm_extract_toc_entries(
    *,
    png_path: str,
    page_num: int,
    model: str,
    previous_entries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract visible TOC entries from one rendered page screenshot."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    with open(png_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    prompt_text = VLM_TOC_EXTRACT_PROMPT + _build_continuation_context(
        previous_entries or []
    )
    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt_text},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        },
    ]
    start = time.monotonic()
    client = get_openai_client(model=model)
    raw, usage = client.chat_completion_with_usage(
        messages=cast(Any, [{"role": "user", "content": content_parts}]),
        model=model,
        temperature=0.1,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    data = json.loads(raw)
    if isinstance(data, dict):
        raw_entries = data.get("entries", [])
    elif isinstance(data, list):
        raw_entries = data
    else:
        raw_entries = []

    entries: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        try:
            level = int(item.get("level") or 1)
        except (TypeError, ValueError):
            level = 1
        entries.append(
            {
                "title": title,
                "page_number": item.get("page_number"),
                "level": level,
            }
        )

    return entries, {
        "page": page_num,
        "model": model,
        "elapsed_ms": elapsed_ms,
        "usage": dict(usage),
        "raw_response_length": len(raw),
        "has_continuation_context": bool(previous_entries),
    }


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
    if not entries:
        return ""
    lines = ["| id | heading | level |", "|----|---------|-------|"]
    for index, entry in enumerate(entries, 1):
        heading = str(entry.get("title") or "").strip().replace("|", "\\|")
        level = entry.get("level", 1)
        lines.append(f"| {index:<2} | {heading:<60} | {level:<5} |")
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
    for index, entry in enumerate(entries, 1):
        toc_with_level.append(
            {
                "id": index,
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
            "toc_with_level_md": build_toc_with_level_md(entries),
            "toc_tree": build_toc_tree(entries),
        }
    ]
