#!/usr/bin/env python3
"""Standalone test: VLM-direct TOC extraction from page PNGs.

Usage:
    cd knowhereapi-main
    python apps/worker/app/services/document_agent/tools/test_vlm_toc_extract.py

Requires:
    - ALI_API_KEYS env var (or in apps/worker/.env)
    - pymupdf, openai installed
    - Test PDFs accessible on disk
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# VLM prompt — no hardcoded examples, no overfitting
# ---------------------------------------------------------------------------

VLM_TOC_EXTRACT_PROMPT = """\
You are analyzing a Table of Contents (TOC) page from a document.

Your task is to extract every TOC entry visible on this page.

Each entry consists of three parts:
1. **title** — the section or chapter name, copied verbatim from the page.
   - EXCLUDE any trailing dots (…·.), dashes (—-), or leader lines that connect the title to its page number.
   - If one entry's title wraps across multiple printed lines, combine them into a single string.
   - Include any numbering prefix that is part of the title text (e.g. "1.", "第三章", "(二)").
2. **page_number** — the page reference at the right side of the entry.
   - Use an integer when the reference is a plain number (e.g. 26).
   - Use a string when the reference is non-numeric (e.g. "iv", "F-1", "A-3").
   - Use null when no page reference is visible for that entry.
3. **level** — the hierarchy depth of the entry, determined by visual formatting cues:
   - level 1: top-level entries — no indentation, or the largest / boldest text.
   - level 2: sub-entries — indented under a level-1 entry, or in a noticeably smaller font.
   - level 3+: deeper indentation, if present.
   - Category headers or group labels that are visually distinct (centered, larger font, different style) and do NOT have a page number should be treated as level 1.

Additional rules:
- Extract ALL entries, even if the page only shows a partial continuation of the TOC.
- Do NOT include the TOC page's own heading (e.g. a "TABLE OF CONTENTS" or "目 录" title at the top) as an entry.
- Do NOT include column headers (e.g. a standalone "Page" label) as entries.
- Preserve the original language and wording of each title exactly.

Return strict JSON with no markdown fences:
{"entries": [{"title": "...", "page_number": ..., "level": ...}, ...]}
"""

# Mirror the production boundary step from extract_toc_with_boundaries.py
BOUNDARY_STEP_PAGES = 5

VLM_TOC_CONTINUATION_CONTEXT = """\

--- IMPORTANT: Continuation Context ---
This is a CONTINUATION page of a multi-page Table of Contents.
The previous page(s) already extracted the following entries:

{previous_summary}

The LAST active category/section before this page was:
  Level {last_l1_level}: "{last_l1_title}"

Entries on THIS page that visually continue as sub-items under that
category (same indentation, same numbering sequence) must keep their
correct subordinate level — do NOT promote them to level 1 just because
the parent heading is not visible on this page.
"""


# ---------------------------------------------------------------------------
# Test case definitions
# ---------------------------------------------------------------------------


@dataclass
class TocTestCase:
    """One test document for VLM TOC extraction."""

    name: str
    pdf_path: str | None  # None means PNGs are pre-rendered
    toc_page_nums: list[int]  # 1-based page numbers to extract
    output_dir: str
    pre_rendered_pngs: list[str] = field(default_factory=list)
    expected_entry_count_range: tuple[int, int] = (1, 999)
    description: str = ""
    toc_output_path: str | None = None  # Override toc_hierarchies.json output path


def _build_test_cases() -> list[TocTestCase]:
    debug_root = os.path.expanduser("~/.knowhere/_debug_profile")
    cases: list[TocTestCase] = []

    # Case 1: SpaceX S-1 — single TOC page, English
    spacex_pdf = "/Users/wuchengke/Downloads/spacex-s1.pdf"
    spacex_out = os.path.join(debug_root, "spacex-s1", "toc_pages")
    if os.path.exists(spacex_pdf):
        cases.append(
            TocTestCase(
                name="SpaceX S-1 (English)",
                pdf_path=spacex_pdf,
                toc_page_nums=[17],  # VLM-confirmed TOC start, boundary says 17 only
                output_dir=spacex_out,
                expected_entry_count_range=(20, 30),
                description="Single flat TOC page, all level-1 entries, page nums on right",
            )
        )
    else:
        # Fall back to pre-rendered PNG
        png17 = os.path.join(spacex_out, "toc_anchor_page_17.png")
        if os.path.exists(png17):
            cases.append(
                TocTestCase(
                    name="SpaceX S-1 (English, pre-rendered)",
                    pdf_path=None,
                    toc_page_nums=[17],
                    output_dir=spacex_out,
                    pre_rendered_pngs=[png17],
                    expected_entry_count_range=(20, 30),
                    description="Single flat TOC page from pre-rendered PNG",
                )
            )

    # Case 2: Chinese corporate regulations — multi-page TOC with categories
    cn_pdf = "/Users/wuchengke/Desktop/temp/test_docs/SJSYJ-SC-2024 企业制度汇编（上册）.pdf"
    cn_out = os.path.join(debug_root, "chinese_corp", "toc_pages")
    if os.path.exists(cn_pdf):
        cases.append(
            TocTestCase(
                name="企业制度汇编 (Chinese, multi-page TOC)",
                pdf_path=cn_pdf,
                toc_page_nums=[5, 6],  # TOC spans pages 5-6
                output_dir=cn_out,
                expected_entry_count_range=(25, 45),
                description=(
                    "Multi-page TOC with category headers (经营类/生产类/安全类/...), "
                    "subcategory codes (SJSYJ-SC101-2024), numbered entries, "
                    "multi-line wrapping, and Chinese dot leaders"
                ),
            )
        )

    return cases


# ---------------------------------------------------------------------------
# PNG rendering
# ---------------------------------------------------------------------------


def _render_page_png(pdf_path: str, page_num: int, output_dir: str, dpi: int = 144) -> str:
    """Render a single page to PNG. Returns the PNG path."""
    import pymupdf  # type: ignore[import]

    os.makedirs(output_dir, exist_ok=True)
    png_path = os.path.join(output_dir, f"toc_page_{page_num}.png")

    doc = pymupdf.open(pdf_path)
    try:
        idx = page_num - 1
        if 0 <= idx < doc.page_count:
            page = doc[idx]
            mat = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
            pix = page.get_pixmap(matrix=mat)
            pix.save(png_path)
        else:
            raise ValueError(f"Page {page_num} out of range (total: {doc.page_count})")
    finally:
        doc.close()

    return png_path


# ---------------------------------------------------------------------------
# VLM call
# ---------------------------------------------------------------------------


def _build_openai_client(model: str):
    """Build an OpenAI SDK client for the given model, without requiring AppConfig.

    Mirrors the routing logic in shared.services.ai.openai_compatible_client_sync
    but reads env vars directly so the test can run standalone.
    """
    from openai import OpenAI

    model_lower = model.lower()

    if "qwen" in model_lower:
        # Aliyun DashScope
        api_key = os.environ.get("ALI_API_KEYS", "").strip()
        # ALI_API_KEYS can be JSON array or comma-separated; grab the first one
        if api_key.startswith("["):
            import re
            keys = re.findall(r'"([^"]+)"', api_key)
            api_key = keys[0] if keys else ""
        elif "," in api_key:
            api_key = api_key.split(",")[0].strip()
        # Handle token_id=api_key format
        if "=" in api_key and not api_key.startswith("sk-"):
            api_key = api_key.split("=", 1)[1]
        base_url = os.environ.get(
            "ALI_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
    elif "deepseek" in model_lower:
        api_key = os.environ.get("DS_KEY", "")
        base_url = os.environ.get("DS_URL", "https://api.deepseek.com/v1")
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # Strip /chat/completions suffix if present
    if base_url.rstrip("/").endswith("/chat/completions"):
        base_url = base_url.rstrip("/").removesuffix("/chat/completions")

    if not api_key:
        raise RuntimeError(
            f"No API key found for model {model!r}. "
            "Set ALI_API_KEYS, DS_KEY, or OPENAI_API_KEY env var."
        )

    return OpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=2)


def _build_continuation_context(previous_entries: list[dict[str, Any]]) -> str:
    """Build a concise context string from previously extracted entries.

    Returns the continuation context to append to the VLM prompt,
    or empty string if no previous entries exist.
    """
    if not previous_entries:
        return ""

    # Build a compact summary: last N entries with their levels
    # Show the last 8 entries to give enough context
    tail = previous_entries[-8:]
    summary_lines = []
    for e in tail:
        lvl = e.get("level", "?")
        title = e.get("title", "?")
        pn = e.get("page_number")
        pn_str = f" → p.{pn}" if pn is not None else ""
        summary_lines.append(f"  L{lvl}: {title}{pn_str}")

    if len(previous_entries) > 8:
        summary_lines.insert(0, f"  ... ({len(previous_entries) - 8} earlier entries omitted)")

    previous_summary = "\n".join(summary_lines)

    # Find the last L1 entry (the active parent category)
    last_l1 = None
    for e in reversed(previous_entries):
        if e.get("level") == 1:
            last_l1 = e
            break

    if last_l1 is None:
        # No L1 found — still provide the summary but skip the "last active" part
        return f"\n\n--- IMPORTANT: Continuation Context ---\nThis is a CONTINUATION page. Previous entries:\n{previous_summary}\n"

    return VLM_TOC_CONTINUATION_CONTEXT.format(
        previous_summary=previous_summary,
        last_l1_level=last_l1.get("level", 1),
        last_l1_title=last_l1.get("title", "?"),
    )


def _vlm_extract_toc_entries(
    png_path: str,
    page_num: int,
    model: str,
    previous_entries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call VLM to extract TOC entries from a single page PNG.

    Args:
        png_path: path to the page PNG image.
        page_num: 1-based page number.
        model: VLM model name.
        previous_entries: entries already extracted from earlier TOC pages.
            Used to build continuation context so the model can correctly
            assign hierarchy levels on continuation pages.

    Returns:
        (entries, meta) where meta has token usage and timing info.
    """
    client = _build_openai_client(model)

    with open(png_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    # Build prompt: base + optional continuation context
    prompt_text = VLM_TOC_EXTRACT_PROMPT
    continuation_ctx = _build_continuation_context(previous_entries or [])
    if continuation_ctx:
        prompt_text += continuation_ctx

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt_text},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        },
    ]

    messages = [{"role": "user", "content": content_parts}]

    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=4096,
        response_format={"type": "json_object"},
        extra_body={"enable_thinking": False},
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    raw = response.choices[0].message.content or ""
    usage_obj = response.usage
    usage = {
        "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
    }

    meta = {
        "page": page_num,
        "model": model,
        "elapsed_ms": elapsed_ms,
        "usage": usage,
        "raw_response_length": len(raw),
        "has_continuation_context": bool(continuation_ctx),
    }

    # Parse JSON
    data = json.loads(raw)
    if isinstance(data, dict):
        entries = data.get("entries", [])
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    return entries, meta


# ---------------------------------------------------------------------------
# VLM entries → toc_hierarchies.json conversion
# ---------------------------------------------------------------------------


def _build_toc_tree(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a nested dict tree from VLM entries, same algorithm as
    toc_hierarchy.build_tree_tocs() but self-contained for standalone use.

    Args:
        entries: list of {"title": str, "level": int, "page_number": ...}

    Returns:
        Nested dict: {heading: {child_heading: {...}, ...}, ...}
    """
    if not entries:
        return {}

    root: dict[str, Any] = {}
    stack: list[tuple[dict[str, Any], int]] = [(root, 0)]

    positive_levels = [e["level"] for e in entries if isinstance(e.get("level"), int) and e["level"] > 0]
    level_for_minus_one = max(positive_levels) + 1 if positive_levels else 1

    for entry in entries:
        heading = entry.get("title", "").strip()
        if not heading:
            continue

        original_level = entry.get("level", 1)
        normalized_level = level_for_minus_one if original_level == -1 else original_level

        while len(stack) > 1 and stack[-1][1] >= normalized_level:
            stack.pop()

        parent_dict = stack[-1][0]
        parent_dict[heading] = {}
        stack.append((parent_dict[heading], normalized_level))

    return root


def _build_toc_with_level_md(entries: list[dict[str, Any]]) -> str:
    """Build a markdown table string compatible with existing toc_with_level format."""
    if not entries:
        return ""

    lines = ["| id | heading | level |"]
    lines.append("|----|---------|-------|")
    for i, e in enumerate(entries, 1):
        heading = e.get("title", "").strip().replace("|", "\\|")
        level = e.get("level", 1)
        # Pad heading for readability
        lines.append(f"| {i:<2} | {heading:<60} | {level:<5} |")
    return "\n".join(lines)


def vlm_entries_to_toc_hierarchies(
    all_entries: list[dict[str, Any]],
    toc_page_nums: list[int],
    scan_end_page: int | None = None,
    page_count: int | None = None,
) -> list[dict[str, Any]]:
    """Convert VLM extraction results to the standard toc_hierarchies.json schema.

    Args:
        all_entries: merged VLM entries from all TOC pages.
            Each entry: {"title": str, "page_number": int|str|None, "level": int}
        toc_page_nums: 1-based page numbers of the TOC pages.
        scan_end_page: 1-based page number of the lookahead boundary check.
            In production, this is `anchor_page + BOUNDARY_STEP_PAGES - 1`.
            If the boundary check page was NOT TOC, it still counts as part
            of the scan range (it was the page we "looked at" to decide).
            Defaults to `max(toc_page_nums) + BOUNDARY_STEP_PAGES - 1`.
        page_count: total pages in the document (used to clamp scan_end_page).

    Returns:
        List with one dict per TOC region (usually 1), matching the schema:
        {
            "toc_range": [start_page, end_page],
            "toc_range_unit": "page",
            "scan_range": [start_page, scan_end_page],
            "source": "vlm",
            "toc_with_level": [structured list],
            "toc_with_level_md": "| id | heading | level |\n...",
            "toc_tree": {nested dict}
        }
    """
    if not all_entries or not toc_page_nums:
        return []

    # Build structured toc_with_level list
    toc_with_level: list[dict[str, Any]] = []
    for i, entry in enumerate(all_entries, 1):
        item: dict[str, Any] = {
            "id": i,
            "heading": entry.get("title", "").strip(),
            "level": entry.get("level", 1),
        }
        # Include page_number from VLM extraction
        pn = entry.get("page_number")
        item["page_number"] = pn
        toc_with_level.append(item)

    # Build toc_tree from clean entries
    toc_tree = _build_toc_tree(all_entries)

    # Build markdown table for backward compatibility
    toc_with_level_md = _build_toc_with_level_md(all_entries)

    # Determine page ranges
    start_page = min(toc_page_nums)
    end_page = max(toc_page_nums)

    # scan_range: the full lookahead window used during boundary detection.
    # In production: anchor + BOUNDARY_STEP_PAGES - 1, clamped to page_count.
    # The anchor is start_page (not end_page), matching _detect_toc_range_for_anchor().
    if scan_end_page is None:
        scan_end_page = start_page + BOUNDARY_STEP_PAGES - 1
    if page_count is not None:
        scan_end_page = min(scan_end_page, page_count)

    return [
        {
            "toc_range": [start_page, end_page],
            "toc_range_unit": "page",
            "scan_range": [start_page, scan_end_page],
            "source": "vlm",
            "toc_with_level": toc_with_level,
            "toc_with_level_md": toc_with_level_md,
            "toc_tree": toc_tree,
        }
    ]


# ---------------------------------------------------------------------------
# Comparison with existing toc_hierarchies.json
# ---------------------------------------------------------------------------


def _compare_with_existing(
    vlm_entries: list[dict[str, Any]],
    test_case: TocTestCase,
) -> dict[str, Any]:
    """Compare VLM results with existing toc_hierarchies.json if available."""
    comparison: dict[str, Any] = {"available": False}

    # Try to find existing toc_hierarchies.json
    parent_dir = str(Path(test_case.output_dir).parent)
    existing_path = os.path.join(parent_dir, "toc_hierarchies.json")
    if not os.path.exists(existing_path):
        return comparison

    with open(existing_path, "r", encoding="utf-8") as f:
        existing = json.load(f)

    comparison["available"] = True
    comparison["existing_path"] = existing_path

    # Extract titles from existing toc_tree
    existing_titles: list[str] = []
    if isinstance(existing, list) and existing:
        tree = existing[0].get("toc_tree", {})
        for key, sub in tree.items():
            existing_titles.append(key)
            if isinstance(sub, dict):
                for subkey in sub:
                    existing_titles.append(subkey)

    vlm_titles = [e.get("title", "") for e in vlm_entries]

    # Quality checks
    issues: list[str] = []

    # Check 1: titles with residual page numbers (MinerU artifact)
    import re
    residual_pattern = re.compile(r"\s+\d+\s*$")
    existing_with_residual = [t for t in existing_titles if residual_pattern.search(t)]
    vlm_with_residual = [t for t in vlm_titles if residual_pattern.search(t)]

    comparison["existing_residual_page_nums"] = existing_with_residual
    comparison["vlm_residual_page_nums"] = vlm_with_residual

    if existing_with_residual and not vlm_with_residual:
        issues.append(
            f"✅ VLM fixed {len(existing_with_residual)} residual page numbers "
            f"in titles (e.g. '{existing_with_residual[0]}')"
        )
    elif vlm_with_residual:
        issues.append(
            f"⚠️ VLM still has {len(vlm_with_residual)} titles with residual "
            f"page numbers: {vlm_with_residual[:3]}"
        )

    # Check 2: broken multi-line titles
    # In existing data: "MANAGEMENT'S DISCUSSION..." and "OF OPERATIONS 74" are separate
    broken_line_keywords = ["OF OPERATIONS", "CLASS A COMMON STOCK"]
    existing_broken = [
        t for t in existing_titles
        if any(t.strip().startswith(kw) for kw in broken_line_keywords)
    ]
    vlm_broken = [
        t for t in vlm_titles
        if any(t.strip().startswith(kw) for kw in broken_line_keywords)
    ]

    if existing_broken and not vlm_broken:
        issues.append(
            f"✅ VLM merged {len(existing_broken)} broken multi-line titles "
            f"(e.g. '{existing_broken[0]}')"
        )
    elif vlm_broken:
        issues.append(
            f"⚠️ VLM still has {len(vlm_broken)} broken multi-line titles: "
            f"{vlm_broken}"
        )

    # Check 3: false positive entries (e.g. "Page" as a title)
    false_positive_patterns = {"Page", "页码"}
    existing_fp = [t for t in existing_titles if t.strip() in false_positive_patterns]
    vlm_fp = [
        e for e in vlm_entries
        if e.get("title", "").strip() in false_positive_patterns
    ]

    if existing_fp and not vlm_fp:
        issues.append(
            f"✅ VLM removed {len(existing_fp)} false positive entries "
            f"(e.g. '{existing_fp[0]}')"
        )
    elif vlm_fp:
        issues.append(
            f"⚠️ VLM still has false positive entries: "
            f"{[e['title'] for e in vlm_fp]}"
        )

    # Check 4: entry count comparison
    comparison["existing_entry_count"] = len(existing_titles)
    comparison["vlm_entry_count"] = len(vlm_entries)
    comparison["quality_checks"] = issues

    return comparison


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------


def run_test(test_case: TocTestCase, model: str) -> dict[str, Any]:
    """Run VLM TOC extraction for one test case."""
    print(f"\n{'='*70}")
    print(f"TEST: {test_case.name}")
    print(f"  {test_case.description}")
    print(f"  TOC pages: {test_case.toc_page_nums}")
    print(f"{'='*70}")

    os.makedirs(test_case.output_dir, exist_ok=True)

    # Detect page count for scan_range calculation
    pdf_page_count: int | None = None
    if test_case.pdf_path:
        try:
            import pymupdf
            with pymupdf.open(test_case.pdf_path) as doc:
                pdf_page_count = len(doc)
            print(f"  PDF page count: {pdf_page_count}")
        except Exception:
            pass

    # Step 1: Prepare PNGs
    png_paths: list[tuple[int, str]] = []

    if test_case.pre_rendered_pngs:
        for i, png in enumerate(test_case.pre_rendered_pngs):
            png_paths.append((test_case.toc_page_nums[i], png))
            print(f"  [png] Using pre-rendered: {png}")
    elif test_case.pdf_path:
        for page_num in test_case.toc_page_nums:
            png = _render_page_png(test_case.pdf_path, page_num, test_case.output_dir)
            png_paths.append((page_num, png))
            print(f"  [png] Rendered page {page_num}: {png}")

    # Step 2: VLM extraction per page
    all_entries: list[dict[str, Any]] = []
    all_meta: list[dict[str, Any]] = []
    total_elapsed_ms = 0

    for page_num, png_path in png_paths:
        is_continuation = len(all_entries) > 0
        ctx_label = " (with context)" if is_continuation else ""
        print(f"\n  [vlm] Extracting page {page_num}{ctx_label}...")
        try:
            entries, meta = _vlm_extract_toc_entries(
                png_path, page_num, model,
                previous_entries=all_entries if is_continuation else None,
            )
            all_entries.extend(entries)
            all_meta.append(meta)
            total_elapsed_ms += meta["elapsed_ms"]
            print(f"  [vlm] Page {page_num}: {len(entries)} entries, {meta['elapsed_ms']}ms")

            # Show first few entries
            for e in entries[:5]:
                title = e.get("title", "?")
                pn = e.get("page_number", "?")
                lv = e.get("level", "?")
                print(f"        L{lv}: {title!r} → p.{pn}")
            if len(entries) > 5:
                print(f"        ... ({len(entries)} total)")

        except Exception as exc:
            print(f"  [vlm] ❌ FAILED for page {page_num}: {exc}")
            all_meta.append({"page": page_num, "error": str(exc)})

    # Step 3: Summary
    print(f"\n  --- Summary ---")
    print(f"  Total entries: {len(all_entries)}")
    print(f"  Total VLM time: {total_elapsed_ms}ms")
    expected_lo, expected_hi = test_case.expected_entry_count_range
    count_ok = expected_lo <= len(all_entries) <= expected_hi
    print(
        f"  Entry count check: {len(all_entries)} "
        f"(expected {expected_lo}-{expected_hi}) → "
        f"{'✅' if count_ok else '⚠️ OUT OF RANGE'}"
    )

    # Step 4: Quality analysis
    level_dist = {}
    for e in all_entries:
        lv = e.get("level", "?")
        level_dist[lv] = level_dist.get(lv, 0) + 1
    print(f"  Level distribution: {level_dist}")

    entries_with_page = [e for e in all_entries if e.get("page_number") is not None]
    entries_no_page = [e for e in all_entries if e.get("page_number") is None]
    print(f"  Entries with page number: {len(entries_with_page)}")
    print(f"  Entries without page number: {len(entries_no_page)}")
    if entries_no_page:
        for e in entries_no_page[:5]:
            print(f"    → {e.get('title', '?')!r} (level={e.get('level')})")

    # Step 5: Compare with existing
    comparison = _compare_with_existing(all_entries, test_case)
    if comparison.get("available"):
        print(f"\n  --- Comparison with existing toc_hierarchies.json ---")
        print(f"  Existing entries: {comparison['existing_entry_count']}")
        print(f"  VLM entries: {comparison['vlm_entry_count']}")
        for check in comparison.get("quality_checks", []):
            print(f"  {check}")

    # Step 6: Save raw VLM results
    result = {
        "test_name": test_case.name,
        "model": model,
        "toc_pages": test_case.toc_page_nums,
        "total_entries": len(all_entries),
        "total_elapsed_ms": total_elapsed_ms,
        "entries": all_entries,
        "per_page_meta": all_meta,
        "level_distribution": level_dist,
        "comparison": comparison if comparison.get("available") else None,
    }

    parent_dir = str(Path(test_case.output_dir).parent)
    output_path = os.path.join(parent_dir, "vlm_toc_entries.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 Raw results saved to: {output_path}")

    # Step 7: Generate toc_hierarchies.json in production schema
    toc_hierarchies = vlm_entries_to_toc_hierarchies(
        all_entries, test_case.toc_page_nums,
        page_count=pdf_page_count,
    )

    toc_hier_path = test_case.toc_output_path or os.path.join(
        parent_dir, "toc_hierarchies.json"
    )
    os.makedirs(os.path.dirname(toc_hier_path), exist_ok=True)
    with open(toc_hier_path, "w", encoding="utf-8") as f:
        json.dump(toc_hierarchies, f, ensure_ascii=False, indent=2)
    print(f"  📄 toc_hierarchies.json saved to: {toc_hier_path}")

    # Print schema summary
    if toc_hierarchies:
        h = toc_hierarchies[0]
        print(f"  --- Schema Summary ---")
        print(f"  toc_range: {h['toc_range']}")
        print(f"  toc_range_unit: {h['toc_range_unit']}")
        print(f"  source: {h['source']}")
        print(f"  toc_with_level entries: {len(h['toc_with_level'])}")
        print(f"  toc_tree L1 keys: {list(h['toc_tree'].keys())[:8]}")

    # Step 8: Print full entry table
    print(f"\n  --- Full Entry Table ---")
    print(f"  {'#':>3} {'Lv':>3} {'Page':>6}  Title")
    print(f"  {'─'*3} {'─'*3} {'─'*6}  {'─'*50}")
    for i, e in enumerate(all_entries, 1):
        title = e.get("title", "?")
        pn = e.get("page_number", "—")
        lv = e.get("level", "?")
        # Truncate long titles for display
        disp_title = title if len(title) <= 60 else title[:57] + "..."
        print(f"  {i:>3} {lv:>3} {str(pn):>6}  {disp_title}")

    return result


def main() -> None:
    # Load env vars from worker/.env if available
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Walk up to find the repo root: test_vlm_toc_extract.py is at
    # apps/worker/app/services/document_agent/tools/
    repo_root = os.path.abspath(os.path.join(script_dir, "..", "..", "..", "..", "..", ".."))
    env_file = os.path.join(repo_root, "apps", "worker", ".env")
    if os.path.exists(env_file):
        print(f"Loading env from: {env_file}")
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:  # don't override existing
                        os.environ[key] = value

    model = os.environ.get("IMAGE_MODEL", "qwen3.5-flash")
    print(f"VLM model: {model}")

    test_cases = _build_test_cases()
    if not test_cases:
        print("No test cases found! Check PDF paths.")
        sys.exit(1)

    print(f"Found {len(test_cases)} test case(s)")

    results: list[dict[str, Any]] = []
    for tc in test_cases:
        try:
            result = run_test(tc, model)
            results.append(result)
        except Exception as exc:
            print(f"\n❌ Test '{tc.name}' FAILED: {exc}")
            import traceback
            traceback.print_exc()

    # Final summary
    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    for r in results:
        name = r["test_name"]
        count = r["total_entries"]
        ms = r["total_elapsed_ms"]
        print(f"  {name}: {count} entries, {ms}ms")


if __name__ == "__main__":
    main()
