from __future__ import annotations

import pandas as pd
from app.services.document_parser.layout_parser import hiearchy_llm
from app.services.document_parser.stage_profiler import stage_timer
from app.services.document_parser.table_text_parser import df2md
from app.services.document_parser.text_helpers import normalize_md
from loguru import logger
from pandas import Index

from shared.core.config import settings


def resolve_hierarchy_model_name(model_name: str | None = None) -> str:
    return model_name or settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL


def parse_toc_hierarchy(
    toc_df: pd.DataFrame, max_depth: int = 6, model_name: str | None = None
) -> list[dict]:
    resolved_model_name = resolve_hierarchy_model_name(model_name)
    try:
        with stage_timer(
            "toc.parse_hierarchy_llm",
            model_name=resolved_model_name,
            heading_count=len(toc_df),
            max_depth=max_depth,
        ):
            toc_hierarchy = hiearchy_llm(
                toc_df,
                model_name=resolved_model_name,
                max_depth=max_depth,
                task="eval-toc-headings",
            )
        id_to_level = {item["id"]: item["level"] for item in toc_hierarchy}

        toc_with_level = []
        for _, row in toc_df.iterrows():
            line_id = row["id"]
            heading = row["heading"]
            level = id_to_level.get(line_id, 1)
            toc_with_level.append({"id": line_id, "heading": heading, "level": level})
        return toc_with_level

    except Exception as exc:
        logger.error(f"LLM hierarchy analysis failed: {exc}")
        return []


def build_tree_tocs(toc_with_level: list[dict]) -> dict:
    if not toc_with_level:
        return {}

    positive_levels = [item["level"] for item in toc_with_level if item["level"] > 0]
    level_for_minus_one = max(positive_levels) + 1 if positive_levels else 1

    root = {}
    stack = [(root, 0)]

    for item in toc_with_level:
        heading = item["heading"]
        original_level = item["level"]
        normalized_level = (
            level_for_minus_one if original_level == -1 else original_level
        )
        while len(stack) > 1 and stack[-1][1] >= normalized_level:
            stack.pop()

        parent_dict = stack[-1][0]
        parent_dict[heading] = {}
        stack.append((parent_dict[heading], normalized_level))
    return root


def build_toc_hierarchy_payload(
    toc_entries: list[dict],
    toc_range: tuple | None = None,
    scan_range: tuple | None = None,
) -> dict | None:
    valid_entries = []
    for entry in toc_entries:
        heading = str(entry.get("heading", "")).strip()
        level = entry.get("level")
        if not heading or not isinstance(level, int) or level <= 0:
            continue

        valid_entries.append(
            {
                "id": entry.get("id"),
                "heading": heading,
                "level": level,
            }
        )

    if not valid_entries:
        return None

    toc_df = pd.DataFrame(valid_entries, columns=Index(["id", "heading", "level"]))
    payload = {
        "toc_range": toc_range or (valid_entries[0]["id"], valid_entries[-1]["id"]),
        "toc_with_level": df2md(toc_df, index=False),
        "toc_tree": build_tree_tocs(valid_entries),
    }
    if scan_range is not None:
        payload["scan_range"] = scan_range
    return payload


def eval_toc_levels(
    toc_lines: list[str], model_name: str | None = None, max_depth: int = 6
) -> tuple[str, dict]:
    toc_title_keywords = {"目录", "目次", "tableofcontents", "contents"}
    valid_data = []

    for index, line in enumerate(toc_lines):
        heading = line.strip()
        if not heading:
            continue
        if normalize_md(heading) in toc_title_keywords:
            logger.debug(
                f"eval_toc_levels: skipping TOC keyword title line id={index}: {heading[:60]}"
            )
            continue

        valid_data.append({"id": index, "heading": heading, "level": "Not Sure"})

    toc_df = pd.DataFrame(valid_data)

    if toc_df.empty:
        logger.info("No valid TOC content, skip hierarchy analysis")
        return "", {}

    llm_result = parse_toc_hierarchy(toc_df, max_depth, model_name)
    id_to_level = {item["id"]: item["level"] for item in llm_result}

    valid_items_for_tree = []
    for data in valid_data:
        line_id = data["id"]
        heading = data["heading"]
        level = id_to_level.get(line_id, -1)
        if level > 0:
            valid_items_for_tree.append(
                {"id": line_id, "heading": heading, "level": level}
            )

    payload = build_toc_hierarchy_payload(valid_items_for_tree)
    if not payload:
        return "", {}
    return payload["toc_with_level"], payload["toc_tree"]
