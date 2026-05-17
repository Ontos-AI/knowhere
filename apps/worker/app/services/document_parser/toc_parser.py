# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportOperatorIssue=false, reportReturnType=false
"""
TOC (Table of Contents) Parser Module
Migrated from sxjg/app/kbs/toc_parser.py

Provides functionality for:
- Detecting TOC (Table of Contents) candidates in markdown documents
- Using LLM to determine precise TOC boundaries
"""

import re

import gevent
import pandas as pd
from app.services.document_parser.toc_hierarchy import eval_toc_levels
from app.services.document_parser.text_helpers import normalize_md, truncate_text_by_tokens
from app.services.document_parser.stage_profiler import stage_timer
from app.services.document_parser.table_parser import df2md
from gevent.pool import Pool as GeventPool
from loguru import logger

from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.response_process_service import eval_response
from shared.utils.OpenAICompatibleClientSync import get_openai_client


# ==================== Markdown TOC Detection Functions ====================


def is_content_line(line: str) -> bool:
    """
    Check if a line is valid content for TOC candidates.
    Filter out table tags and LaTeX formulas.

    Args:
        line: the line to check

    Returns:
        True if the line is valid content, False if it should be filtered out
    """
    stripped = line.strip().lower()

    # Filter out page markers (e.g. <!-- page 10 -->)
    if re.match(r"^<!--\s*page\s+\d+\s*-->", stripped):
        return False

    # Filter out html lines
    # TODO if table extract it as lines but keep the id span as 1
    if stripped.startswith("<table>"):
        return False

    if stripped.startswith("!["):
        return False

    # Filter out lines containing LaTeX patterns
    latex_patterns = [
        r"\\mathrm\b",
        r"\\frac\b",
        r"\\sum\b",
        r"\\int\b",
        r"\\sqrt\b",
        r"\\alpha\b",
        r"\\beta\b",
        r"\\gamma\b",
        r"\\delta\b",
        r"\\theta\b",
        r"\\lambda\b",
        r"\\sigma\b",
        r"\\omega\b",
        r"\\partial\b",
        r"\\infty\b",
        r"\\left\b",
        r"\\right\b",
        r"\\begin\{",
        r"\\end\{",
        r"\\text\b",
        r"\\mathbf\b",
        r"\\mathit\b",
        r"\\times\b",
        r"\\cdot\b",
        r"\\leq\b",
        r"\\geq\b",
        r"\\neq\b",
        r"\\approx\b",
    ]

    for pattern in latex_patterns:
        if re.search(pattern, line, re.IGNORECASE):
            return False
    return True


def detect_toc_candidates(md_lines: list, limit_: int = 100) -> tuple:
    """
    Detect TOC candidates (support multiple TOC areas)

    Strategy: scan forward and collect exactly ``limit_`` valid lines while
    also recording the full raw range ``[start_idx, end_idx]`` for later
    filtering of the TOC area.

    Args:
        md_lines: markdown lines list
        limit_: Target number of valid lines, not the maximum scan length.

    Returns:
        Tuple of (candidates, invalid_ids_by_area, area_ranges)
        - candidates: List[(start_idx, candidate_lines_with_indices)]
          - start_idx: start index of the candidate area
          - candidate_lines_with_indices: [(line_idx, line_content), ...] exactly limit_ valid lines
        - invalid_ids_by_area: List[list] - each list contains invalid line indices for that area
        - area_ranges: List[(start_idx, end_idx)] - full raw md_lines ranges for later filtering
    """

    toc_keywords = {"目录", "目次", "tableofcontents", "contents"}

    # Step 1: find all TOC keywords
    start_indices = []
    for i, line in enumerate(md_lines):
        if normalize_md(line) in toc_keywords:
            start_indices.append(i)

    # Step 2: if no TOC keywords found, use the first line as the candidate area
    if not start_indices:
        logger.info("No TOC keywords found, using the first line as the candidate area")
        start_indices = [0]
    else:
        logger.info(f"{len(start_indices)} TOC keywords found")
        for idx in start_indices:
            logger.debug(f"  - line {idx}: {md_lines[idx].strip()}")

    # Step 3: build candidate areas for each start index
    candidates = []
    invalid_ids_by_area = []
    area_ranges = []

    other_toc_starts = set(start_indices)  # for quick lookup

    for start_idx in start_indices:
        candidate_lines_with_indices = []
        invalid_ids = []
        current_idx = start_idx

        while len(candidate_lines_with_indices) < limit_ and current_idx < len(
            md_lines
        ):
            # Stop if we've reached another TOC keyword's start
            if current_idx != start_idx and current_idx in other_toc_starts:
                logger.debug(
                    f"Scan for area starting at {start_idx} stopped at {current_idx} (another TOC keyword)"
                )
                break

            line = md_lines[current_idx]

            if line.strip():
                if is_content_line(line):
                    candidate_lines_with_indices.append((current_idx, line))
                else:
                    invalid_ids.append(current_idx)
            current_idx += 1

        end_idx = current_idx - 1 if current_idx > start_idx else start_idx

        valid_count = len(candidate_lines_with_indices)
        invalid_count = len(invalid_ids)
        logger.debug(
            f"Candidate area #{len(candidates) + 1}: line {start_idx}-{end_idx}, "
            f"valid lines: {valid_count}, invalid lines: {invalid_count}, "
            f"scanned: {end_idx - start_idx + 1} lines"
        )

        candidates.append((start_idx, candidate_lines_with_indices))
        invalid_ids_by_area.append(invalid_ids)
        area_ranges.append((start_idx, end_idx))

    return candidates, invalid_ids_by_area, area_ranges


def llm_judge_toc_range(
    html_table: str,
    lines_: list,
    model_name: str = None,
    use_reindex: bool = False,
    total_lines: int = 0,
) -> tuple:
    """
    Use LLM to determine the start and end indices of the TOC content

    Args:
        html_table: HTML table of candidate lines
        lines_: [(line_idx, line_content), ...] non-empty lines list
        model_name: model name (optional, uses default if not specified)
        use_reindex: if True, LLM receives 0-based consecutive ids
        total_lines: total number of lines (used when use_reindex=True)

    Returns:
        (toc_start_idx, toc_end_idx) or None
        - If use_reindex=True, returns 0-based indices that need to be mapped back
    """
    if not lines_:
        return None

    if use_reindex:
        # Use 0-based consecutive indexing
        start_idx = 0
        end_idx = total_lines - 1
    else:
        start_idx = lines_[0][0]
        end_idx = lines_[-1][0]

    total_candidates = len(lines_)

    paras = {
        "start_idx": start_idx,
        "end_idx": end_idx,
        "total_candidates": total_candidates,
    }
    prompt, temperature, top_p, max_tokens = build_prompt(
        task="detect-toc-range", texts=html_table, query="", paras=paras
    )

    messages = [
        {"role": "system", "content": "You are a document analysis expert"},
        {"role": "user", "content": prompt},
    ]

    try:
        with stage_timer(
            "toc.detect_range_llm",
            model_name=model_name,
            total_candidates=total_candidates,
        ):
            answer = get_openai_client(model=model_name).chat_completion(
                messages=messages,
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            result = eval_response(answer)
        toc_start = result.get("toc_start")
        toc_end = result.get("toc_end")
        confidence = result.get("confidence", "low")
        logger.info(
            f"LLM judge: toc_start={toc_start}, toc_end={toc_end}, confidence={confidence}"
        )

        if toc_start is not None and toc_end is not None:
            # Validate: indices should be within valid range
            if (
                0 <= toc_start <= end_idx
                and 0 <= toc_end <= end_idx
                and toc_end >= toc_start
            ):
                return toc_start, toc_end
            else:
                logger.warning(
                    f"IDs returned by LLM are out of range [0, {end_idx}], got [{toc_start}, {toc_end}]"
                )
                return None
        else:
            return None

    except Exception as e:
        logger.error(f"LLM parse failed: {e}")
        return None


def detect_tocs_llm(md_lines: list, model_name: str = None, limit_: int = 100) -> list:
    """
    Use LLM to detect TOC, support multiple TOC areas.
    When multiple candidate areas exist, LLM calls are parallelized via gevent.

    Args:
        md_lines: markdown lines list
        model_name: model name (optional)
        limit_: max lines to consider for each candidate area

    Returns:
        List[(start_idx, end_idx, toc_lines, area_end)] or empty list
        - toc_lines only contains valid content lines (table/LaTeX/images filtered out)
        may return multiple TOC areas
    """
    # Step 1: detect candidate areas (may be multiple)
    candidates, invalid_ids_by_area, area_ranges = detect_toc_candidates(
        md_lines, limit_
    )
    if not candidates:
        logger.info("No TOC candidates detected")
        return []

    # Step 2: evaluate each candidate area with LLM (parallel when multiple)
    def _judge_single_area(idx, lines_, invalid_ids, area_start, area_end):
        """Judge a single candidate area. Returns (idx, result_tuple) or (idx, None)."""
        try:
            reindex_to_original = {i: abs_idx for i, (abs_idx, _) in enumerate(lines_)}

            df = pd.DataFrame(
                [
                    {
                        "id": i,
                        "content": truncate_text_by_tokens(
                            re.sub(r"^#+\s*", "", line.strip()), 30, 10
                        ),
                    }
                    for i, (_, line) in enumerate(lines_)
                ]
            )

            md_table = df2md(df, index=False)
            llm_result = llm_judge_toc_range(
                md_table, lines_, model_name, use_reindex=True, total_lines=len(lines_)
            )

            if llm_result is not None:
                reindex_start, reindex_end = llm_result
                toc_start = reindex_to_original.get(reindex_start)
                toc_end = reindex_to_original.get(reindex_end)

                if toc_start is not None and toc_end is not None:
                    toc_lines = [
                        line
                        for i, line in enumerate(
                            md_lines[toc_start : toc_end + 1], start=toc_start
                        )
                        if i not in invalid_ids and line.strip()
                    ]
                    return idx, (toc_start, toc_end, toc_lines, area_end)
                else:
                    logger.warning(
                        f"TOC area #{idx + 1}: failed to map reindex back to original"
                    )
                    return idx, None
            else:
                logger.info(f"TOC area #{idx + 1} not detected")
                return idx, None
        except Exception as e:
            logger.error(f"TOC area #{idx + 1} LLM judge failed: {e}")
            return idx, None

    if len(candidates) == 1:
        # Single candidate: no need for parallel overhead
        (_, lines_), invalid_ids, (area_start, area_end) = (
            candidates[0],
            invalid_ids_by_area[0],
            area_ranges[0],
        )
        _, result = _judge_single_area(0, lines_, invalid_ids, area_start, area_end)
        return [result] if result else []

    # Multiple candidates: parallel LLM calls
    logger.info(
        f"Parallelizing TOC range detection for {len(candidates)} candidate areas"
    )
    pool = GeventPool(size=len(candidates))
    greenlets = []
    for idx, ((_, lines_), invalid_ids, (area_start, area_end)) in enumerate(
        zip(candidates, invalid_ids_by_area, area_ranges)
    ):
        greenlets.append(
            pool.spawn(
                _judge_single_area, idx, lines_, invalid_ids, area_start, area_end
            )
        )
    gevent.joinall(greenlets)

    # Collect results, sorted by original index to maintain order
    results = [g.value for g in greenlets if g.value is not None]
    results.sort(key=lambda r: r[0])

    toc_ranges = [result for _, result in results if result is not None]
    return toc_ranges


def detect_tocs_in_texts(
    md_lines: list,
    model_name: str = None,
    hierarchy_model_name: str = None,
    branch: str = "normal",
    limit_: int = 150,
):
    """
    Detect and analyze TOC in texts

    Args:
        md_lines: markdown lines list
        model_name: TOC range detection model name (optional)
        hierarchy_model_name: hierarchy LLM model name (optional)
        branch: "normal" or "plus-ocr"
        limit_: max lines to consider for each candidate area

    Returns:
        Tuple of (toc_hierarchies, filtered_md_lines)
        - toc_hierarchies: List[dict] containing:
            {
                "toc_range": (start_idx, end_idx),
                "toc_with_level": [...],
                "toc_tree": {...}
            }
        - filtered_md_lines: md_lines with TOC sections removed
    """

    logger.info("-" * 60)
    logger.info(f"Step 1: detect TOC lines, with {branch} method")
    logger.info("-" * 60)

    if branch == "normal":
        toc_area = detect_tocs_llm(md_lines, model_name, limit_)
    elif branch == "plus-ocr":
        # TODO implement OCR branch, if implement, direct yield toc-tree
        return None, md_lines
    else:
        logger.error(f"Not supported branch: {branch}")
        return None, md_lines

    if not toc_area or len(toc_area) == 0:
        logger.warning("TOC detection failed, no TOC area detected")
        return None, md_lines

    # ── Step 1.5: Merge consecutive TOC areas ──
    # Adjacent TOCs with ≤ CONSECUTIVE_TOC_GAP non-empty lines between them
    # are treated as one (e.g. Chinese TOC + English translation).
    # Only the first TOC in each merged group will get a hierarchy LLM call.
    CONSECUTIVE_TOC_GAP = 5  # max non-empty lines between consecutive TOCs

    if len(toc_area) > 1:
        merged_toc_area = [toc_area[0]]
        for current in toc_area[1:]:
            prev = merged_toc_area[-1]
            prev_end = prev[1]  # toc_end of previous
            curr_start = current[0]  # toc_start of current

            # Count non-empty lines in the gap (prev_end+1 .. curr_start-1)
            gap_non_empty = sum(
                1
                for i in range(prev_end + 1, curr_start)
                if i < len(md_lines) and md_lines[i].strip()
            )

            if gap_non_empty <= CONSECUTIVE_TOC_GAP:
                # Merge: extend range, keep first TOC's toc_lines, extend area_end
                merged_toc_area[-1] = (
                    prev[0],  # toc_start: keep first
                    current[1],  # toc_end: extend to current's end
                    prev[2],  # toc_lines: keep first's content for LLM
                    max(prev[3], current[3]),  # area_end: take the larger
                )
                logger.info(
                    f"📎 Merged consecutive TOC: [{prev[0]},{prev[1]}] + [{current[0]},{current[1]}] "
                    f"(gap={gap_non_empty} non-empty lines) → [{prev[0]},{current[1]}]"
                )
            else:
                merged_toc_area.append(current)

        if len(merged_toc_area) < len(toc_area):
            logger.info(f"TOC merge: {len(toc_area)} → {len(merged_toc_area)} areas")
        toc_area = merged_toc_area

    logger.info("Step 2: analyze TOC hierarchy")
    logger.info(f"{len(toc_area)} TOC areas detected")

    toc_hierarchies = []
    ranges_to_remove = []

    def _analyze_single_toc(idx, toc_start, toc_end, toc_lines, area_end):
        """Analyze hierarchy for a single TOC area. Returns (idx, result_dict)."""
        logger.info(
            f"Analyzing TOC area #{idx + 1}: TOC range [{toc_start}, {toc_end}], scan range ends at {area_end}"
        )
        try:
            toc_with_level, toc_tree = eval_toc_levels(
                toc_lines, hierarchy_model_name, max_depth=6
            )
            return idx, {
                "toc_range": (toc_start, toc_end),
                "scan_range": (toc_start, area_end),
                "toc_with_level": toc_with_level,
                "toc_tree": toc_tree,
            }
        except Exception as e:
            logger.error(f"TOC area #{idx + 1} hierarchy analysis failed: {e}")
            return idx, None

    if len(toc_area) == 1:
        # Single TOC: no parallel overhead
        toc_start, toc_end, toc_lines, area_end = toc_area[0]
        _, result = _analyze_single_toc(0, toc_start, toc_end, toc_lines, area_end)
        if result:
            toc_hierarchies.append(result)
            ranges_to_remove.append((toc_start, toc_end))
    else:
        # Multiple TOCs: parallel hierarchy analysis
        logger.info(f"Parallelizing TOC hierarchy analysis for {len(toc_area)} areas")
        pool = GeventPool(size=len(toc_area))
        greenlets = [
            pool.spawn(
                _analyze_single_toc, idx, toc_start, toc_end, toc_lines, area_end
            )
            for idx, (toc_start, toc_end, toc_lines, area_end) in enumerate(toc_area)
        ]
        gevent.joinall(greenlets)

        # Collect results sorted by index to maintain document order
        results = sorted(
            [g.value for g in greenlets if g.value is not None], key=lambda r: r[0]
        )
        for _, result in results:
            if result:
                toc_hierarchies.append(result)
                ranges_to_remove.append(result["toc_range"])

    # Step 3: Remove TOC lines from md_lines (process in reverse order to correct indices)
    # Sort ranges by start index descending just to be safe
    ranges_to_remove.sort(key=lambda x: x[0], reverse=True)

    for start, end in ranges_to_remove:
        # Slicing removal: [:start] + [end+1:]
        md_lines = md_lines[:start] + md_lines[end + 1 :]

    logger.info(f"Removed TOC lines, remaining {len(md_lines)} lines")
    return toc_hierarchies, md_lines


# ==================== TOC Utility Functions ====================


def load_toc_hierarchies(json_path: str) -> list:
    import json

    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)
