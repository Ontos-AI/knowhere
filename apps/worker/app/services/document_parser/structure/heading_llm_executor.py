# pyright: reportArgumentType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalSubscript=false, reportReturnType=false
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pandas as pd
from app.services.document_parser.structure.metadata_extractor import clean_md_text_for_llm
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.document_parser.support.text_helpers import count_cn_en, truncate_text_by_tokens
from loguru import logger

PLACEHOLDER_REASON = "__PLACEHOLDER__"

HierarchyJudge = Callable[..., list[dict[str, Any]]]
FallbackHierarchy = Callable[[pd.DataFrame], pd.DataFrame]
SaveIntermediateCsv = Callable[[pd.DataFrame, str | None, str], None]


def compact_for_llm(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse consecutive body rows into placeholder rows before LLM chunking.

    The output DataFrame has columns [id, heading, reason].  The ``level``
    column is intentionally NOT forwarded to the LLM — preliminary estimates
    were found to mislead the model more often than they helped.  The naive-
    stage body-text detection (level == -1) is still used here to decide which
    rows become placeholders vs candidates.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["id", "heading", "reason"])

    rows: list[dict[str, Any]] = []
    index = 0
    row_count = len(df)
    while index < row_count:
        lvl_raw = df.iloc[index]["level"]
        try:
            lvl_int = int(lvl_raw)
        except (TypeError, ValueError):
            lvl_int = None

        if lvl_int == -1:
            end_index = index
            while end_index < row_count:
                try:
                    next_level = int(df.iloc[end_index]["level"])
                except (TypeError, ValueError):
                    break
                if next_level != -1:
                    break
                end_index += 1
            start_id = int(df.iloc[index]["id"])
            end_id = int(df.iloc[end_index - 1]["id"])
            run_length = end_index - index
            rows.append(
                {
                    "id": f"{start_id}-{end_id}",
                    "heading": f"[{run_length} BODY LINES]",
                    "reason": PLACEHOLDER_REASON,
                }
            )
            index = end_index
        else:
            row = df.iloc[index]
            rows.append(
                {
                    "id": int(row["id"]),
                    "heading": str(row["heading"]),
                    "reason": str(row.get("reason", "") or ""),
                }
            )
            index += 1

    return pd.DataFrame(rows, columns=["id", "heading", "reason"])


def split_heading_table(
    df: pd.DataFrame, threshold: int = 3000, max_start: int = 50, max_end: int = 10
) -> tuple[list[pd.DataFrame], list[str]]:
    raw_headings = df["heading"].tolist()
    working_df = df.copy()
    working_df["heading"] = working_df["heading"].apply(
        lambda heading: truncate_text_by_tokens(heading, max_start, max_end)
    )

    sub_dfs: list[pd.DataFrame] = []
    current_rows: list[list[Any]] = []
    current_len = 0
    for _, row in working_df.iterrows():
        # Drop internal-only columns before measuring token length
        row_filtered = row.drop(labels=["reason", "level"], errors="ignore")
        row_len = sum(count_cn_en(str(value)) for value in row_filtered.values)

        if current_len + row_len > threshold and current_rows:
            sub_dfs.append(pd.DataFrame(current_rows, columns=working_df.columns))
            current_rows = [row.tolist()]
            current_len = row_len
        else:
            current_rows.append(row.tolist())
            current_len += row_len

    if current_rows:
        sub_dfs.append(pd.DataFrame(current_rows, columns=working_df.columns))
    return sub_dfs, raw_headings


def execute_llm_heading_hierarchy(
    raw_preds: pd.DataFrame,
    prompt_limt: int,
    hierarchy_judge: HierarchyJudge,
    fallback_hierarchy: FallbackHierarchy,
    save_intermediate_csv: SaveIntermediateCsv,
    toc_hierarchies: Any | None = None,
    max_len: int = 30,
    max_depth: int = 6,
    model_name: str | None = None,
    output_dir: str | None = None,
    csv_suffix: str = "",
) -> pd.DataFrame:
    if len(raw_preds) == 0:
        return pd.DataFrame(columns=["id", "heading", "level", "reason"])

    compact_enabled = os.environ.get(
        "KB_LAYOUT_LLM_COMPACT_INPUT", "true"
    ).strip().lower() in ("true", "1", "yes", "on")
    preds_for_llm = compact_for_llm(raw_preds) if compact_enabled else raw_preds.copy()
    if compact_enabled:
        placeholder_count = int(preds_for_llm["reason"].eq(PLACEHOLDER_REASON).sum())
        logger.info(
            f"smart parse => compact input: {len(raw_preds)} -> {len(preds_for_llm)} rows "
            f"({placeholder_count} placeholder groups)"
        )

    non_placeholder = (
        preds_for_llm[preds_for_llm["reason"].astype(str) != PLACEHOLDER_REASON]
        if compact_enabled
        else preds_for_llm
    )
    if len(non_placeholder) == 0:
        logger.info(
            "smart parse => no heading candidates, skipping LLM hierarchy detection"
        )
        fallback = raw_preds.copy()[["id", "heading", "level", "reason"]]
        fallback["level"] = -1
        return fallback.sort_values("id").reset_index(drop=True)

    level_dfs, _raw_headings = split_heading_table(
        preds_for_llm, threshold=prompt_limt, max_start=max_len, max_end=5
    )
    chunk_sizes = [len(dataframe) for dataframe in level_dfs]
    logger.info(
        f"smart parse => {len(level_dfs)} chunk(s) | rows per chunk: {chunk_sizes} | "
        f"threshold={prompt_limt} | max_start={max_len}"
    )

    full_preds: pd.DataFrame | None = None
    try:
        with stage_timer(
            "heading.hierarchy_llm",
            chunk_count=len(level_dfs),
            compact_enabled=compact_enabled,
            source_row_count=len(raw_preds),
            model_name=model_name,
        ):
            # ── Per-chunk independent LLM calls ──
            llm_levels: dict[int, Any] = {}

            for chunk_idx, chunk_df in enumerate(level_dfs):
                # Skip chunks that contain only placeholders
                non_placeholder_mask = chunk_df["reason"].astype(str) != PLACEHOLDER_REASON
                if not non_placeholder_mask.any():
                    logger.debug(
                        f"smart parse => chunk {chunk_idx}: all placeholders, skipping"
                    )
                    continue

                df4llm = chunk_df.drop(columns=["reason", "level"], errors="ignore").copy()
                df4llm["heading"] = df4llm["heading"].apply(clean_md_text_for_llm)

                logger.info(
                    f"smart parse => chunk {chunk_idx}/{len(level_dfs)}: "
                    f"sending {len(df4llm)} rows to LLM"
                )
                chunk_result = hierarchy_judge(
                    df4llm, model_name, max_depth, toc_hierarchies, task="eval-headings"
                )

                if isinstance(chunk_result, list):
                    for item in chunk_result:
                        if isinstance(item, dict) and "id" in item and "level" in item:
                            try:
                                llm_levels[int(item["id"])] = item["level"]
                            except (TypeError, ValueError):
                                pass

                # Save per-chunk intermediate CSV
                chunk_preds = (
                    chunk_df[["id", "heading", "reason"]].copy().reset_index(drop=True)
                )
                chunk_preds.insert(
                    2, "level",
                    chunk_preds["id"].map(
                        lambda rid: llm_levels.get(
                            int(rid) if not isinstance(rid, str) or rid.isdigit() else -1, -1
                        )
                    ),
                )
                save_intermediate_csv(
                    chunk_preds, output_dir,
                    f"preds_llm{csv_suffix}_{chunk_idx}"
                )

            logger.info(
                f"smart parse => per-chunk LLM produced {len(llm_levels)} "
                f"id->level entries across {len(level_dfs)} chunks"
            )

            full_preds = raw_preds.copy()[["id", "heading", "level", "reason"]]

            def resolve_level(row_id: Any) -> int:
                try:
                    int_id = int(row_id)
                except (TypeError, ValueError):
                    return -1
                level = llm_levels.get(int_id, -1)
                try:
                    return int(level)
                except (TypeError, ValueError):
                    return -1
                    
            full_preds["level"] = full_preds["id"].map(resolve_level).astype(int)

    except Exception as exc:
        logger.warning(
            f"LLM-based parsing fails due to {exc}, using non-llm pipeline..."
        )
        full_preds = fallback_hierarchy(raw_preds.copy())
    return full_preds

