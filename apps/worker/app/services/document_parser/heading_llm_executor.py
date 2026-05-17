# pyright: reportArgumentType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalSubscript=false, reportReturnType=false
from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

import pandas as pd
from app.services.document_parser.metadata_extractor import clean_md_text_for_llm
from app.services.document_parser.stage_profiler import stage_timer
from app.services.document_parser.text_helpers import count_cn_en, truncate_text_by_tokens
from loguru import logger

from shared.core.exceptions.domain_exceptions import WorkerHandlingException

PLACEHOLDER_REASON = "__PLACEHOLDER__"

HierarchyJudge = Callable[..., list[dict[str, Any]]]
FallbackHierarchy = Callable[[pd.DataFrame], pd.DataFrame]
SaveIntermediateCsv = Callable[[pd.DataFrame, str | None, str], None]


def build_level_mapping(
    df: pd.DataFrame, origin_lvls: list[int], mode: str = "max"
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    mapped_df = df.copy()
    mapped_df["origin_level"] = origin_lvls

    mapping = mapped_df.groupby("reason")["level"].apply(list).to_dict()

    processed_mapping: dict[str, dict[str, Any]] = {}
    for reason, lvls in mapping.items():
        positive_lvls = [lvl for lvl in lvls if lvl > -1]
        counts = Counter(lvls)

        if not positive_lvls:
            mapped_lvl = -1
        elif mode == "max":
            mapped_lvl = max(positive_lvls)
        elif mode == "freq":
            mapped_lvl = counts.most_common(1)[0][0]
        else:
            raise WorkerHandlingException(
                internal_message=f"wrong input mode: {mode}. Must be 'max' or 'freq'"
            )

        processed_mapping[reason] = {
            "lvls": lvls,
            "positive_lvls": positive_lvls,
            "freqs": dict(counts),
            "mapped_lvl": mapped_lvl,
        }
    return mapped_df, processed_mapping


def execute_level_mapping(
    df: pd.DataFrame, mapping: dict[str, dict[str, Any]]
) -> pd.DataFrame:
    def map_row(row: pd.Series) -> int:
        reason = row["reason"]
        if reason in mapping:
            return int(mapping[reason]["mapped_lvl"])
        return int(row["level"])

    mapped_df = df.copy()
    origin_est_lvls = mapped_df["level"].tolist()
    mapped_df["level"] = mapped_df.apply(map_row, axis=1)
    mapped_df["origin_level"] = origin_est_lvls
    return mapped_df


def extract_non_neg_code(reason_str: str) -> str:
    """Extract the non-NEG code from a heading reason string."""
    if not reason_str or not isinstance(reason_str, str):
        return ""
    neg_match = re.search(r"\s*NEG\s*\[[^\]]*\]", reason_str)
    if neg_match:
        before_neg = reason_str[: neg_match.start()]
        after_neg = reason_str[neg_match.end() :]
        return (before_neg + after_neg).strip()
    return reason_str.strip()


def build_non_neg_mapping(lvl_mapping: dict[str, dict[str, Any]]) -> dict[str, int]:
    non_neg_levels: dict[str, list[int]] = {}
    for reason, info in lvl_mapping.items():
        non_neg_code = extract_non_neg_code(reason)
        mapped_lvl = int(info.get("mapped_lvl", -1))
        if non_neg_code:
            non_neg_levels.setdefault(non_neg_code, []).append(mapped_lvl)

    non_neg_mapping: dict[str, int] = {}
    for non_neg_code, levels in non_neg_levels.items():
        positive_levels = [lvl for lvl in levels if lvl > -1]
        if positive_levels:
            level_counts = Counter(positive_levels)
            non_neg_mapping[non_neg_code] = level_counts.most_common(1)[0][0]
        else:
            non_neg_mapping[non_neg_code] = -1

    return non_neg_mapping


def handle_unseen_codes(
    df: pd.DataFrame,
    level_dfs: list[pd.DataFrame],
    lvl_mapping: dict[str, dict[str, Any]],
    output_dir: str | None = None,
    window_half_size: int = 10,
    strategy: str = "double_mapping",
) -> dict[str, dict[str, Any]]:
    """Extend first-chunk reason mapping to reason codes only seen in later chunks."""

    def extract_reason_signature(reason: str) -> str:
        return reason.strip() if reason else ""

    def has_neg_signal(reason_str: str) -> bool:
        if not reason_str or not isinstance(reason_str, str):
            return False
        neg_match = re.search(r"NEG\s*\[([^\]]*)\]", reason_str)
        if not neg_match:
            return False
        neg_content = neg_match.group(1)
        try:
            nums = [int(x.strip()) for x in neg_content.split(",") if x.strip()]
            return any(x >= 1 for x in nums)
        except Exception:
            return False

    def build_context_window(
        target_idx: int, known_codes_set: set[str], total_rows: int, half_size: int = 10
    ) -> dict[str, Any]:
        min_start = max(0, target_idx - half_size)
        min_end = min(total_rows - 1, target_idx + half_size)

        start_idx = min_start
        end_idx = min_end

        found_known_above = False
        found_known_below = False
        known_positions: list[int] = []

        for index in range(start_idx, target_idx):
            reason = df.iloc[index].get("reason", "")
            sig = extract_reason_signature(reason)
            if sig in known_codes_set:
                found_known_above = True
                known_positions.append(index)

        for index in range(target_idx + 1, end_idx + 1):
            reason = df.iloc[index].get("reason", "")
            sig = extract_reason_signature(reason)
            if sig in known_codes_set:
                found_known_below = True
                known_positions.append(index)

        if not found_known_above and min_start > 0:
            search_idx = min_start - 1
            while search_idx >= 0:
                reason = df.iloc[search_idx].get("reason", "")
                sig = extract_reason_signature(reason)
                if sig in known_codes_set:
                    found_known_above = True
                    known_positions.append(search_idx)
                    start_idx = search_idx
                    break
                search_idx -= 1

        if not found_known_below and min_end < total_rows - 1:
            search_idx = min_end + 1
            while search_idx < total_rows:
                reason = df.iloc[search_idx].get("reason", "")
                sig = extract_reason_signature(reason)
                if sig in known_codes_set:
                    found_known_below = True
                    known_positions.append(search_idx)
                    end_idx = search_idx
                    break
                search_idx += 1

        return {
            "start": start_idx,
            "end": end_idx,
            "found_known": found_known_above or found_known_below,
            "known_positions": known_positions,
        }

    non_neg_mapping = build_non_neg_mapping(lvl_mapping)
    known_codes = set(lvl_mapping.keys())

    all_codes_in_full: dict[str, dict[str, Any]] = {}
    for seg_idx, seg_df in enumerate(level_dfs):
        for _, row in seg_df.iterrows():
            reason = row.get("reason", "")
            sig = extract_reason_signature(reason)
            if not sig or sig == PLACEHOLDER_REASON:
                continue
            if sig not in all_codes_in_full:
                all_codes_in_full[sig] = {
                    "first_seg": seg_idx,
                    "first_id": row.get("id", 0),
                    "reason": reason,
                }

    unseen_codes: dict[str, dict[str, Any]] = {}
    unseen_neg_filtered: dict[str, dict[str, Any]] = {}
    for sig, info in all_codes_in_full.items():
        if sig in known_codes:
            continue
        if has_neg_signal(info["reason"]):
            unseen_neg_filtered[sig] = info
        else:
            unseen_codes[sig] = info

    logger.info(
        f"Unseen codes total: {len(unseen_codes) + len(unseen_neg_filtered)}, "
        f"NEG filtered: {len(unseen_neg_filtered)}, to process: {len(unseen_codes)}"
    )

    for sig in unseen_neg_filtered:
        lvl_mapping[sig] = {"mapped_lvl": -1, "note": "NEG_FILTERED"}

    if unseen_codes:
        if strategy == "double_mapping":
            fallback_success = 0
            fallback_failed = 0
            failed_codes = []
            for sig in unseen_codes:
                non_neg_code = extract_non_neg_code(sig)
                if non_neg_code in non_neg_mapping:
                    mapped_level = non_neg_mapping[non_neg_code]
                    lvl_mapping[sig] = {
                        "mapped_lvl": mapped_level,
                        "note": f"NON_NEG_FALLBACK from '{non_neg_code}'",
                    }
                    fallback_success += 1
                else:
                    lvl_mapping[sig] = {"mapped_lvl": -1, "note": "NO_MATCH_FALLBACK"}
                    fallback_failed += 1
                    failed_codes.append(
                        f"'{non_neg_code}' (from '{sig[:60]}...')"
                        if len(sig) > 60
                        else f"'{non_neg_code}' (from '{sig}')"
                    )

            logger.info(
                f"Double mapping result: success={fallback_success}, failed={fallback_failed}"
            )
            if failed_codes:
                logger.warning(
                    f"Failed codes (non_neg not in mapping): {failed_codes[:5]}"
                    f"{'...' if len(failed_codes) > 5 else ''}"
                )

        elif strategy == "window_llm" and output_dir:
            total_rows = len(df)
            windows: list[dict[str, Any]] = []
            for sig, info in unseen_codes.items():
                first_id = info["first_id"]
                first_seg = info["first_seg"]
                df_indices = df.index[df["id"] == first_id].tolist()
                if df_indices:
                    first_df_idx = df_indices[0]
                    window_info = build_context_window(
                        first_df_idx, known_codes, total_rows, window_half_size
                    )
                    windows.append(
                        {
                            "code": sig,
                            "first_id": first_id,
                            "first_seg": first_seg,
                            "start": window_info["start"],
                            "end": window_info["end"],
                            "found_known": window_info["found_known"],
                        }
                    )

            sorted_windows = sorted(windows, key=lambda window: window["start"])
            merged_windows: list[dict[str, Any]] = []
            current_window: dict[str, Any] | None = None

            for window in sorted_windows:
                if current_window is None:
                    current_window = {
                        "start": window["start"],
                        "end": window["end"],
                        "codes": [window["code"]],
                        "segments": [window["first_seg"]],
                    }
                elif window["start"] <= current_window["end"]:
                    current_window["end"] = max(current_window["end"], window["end"])
                    current_window["codes"].append(window["code"])
                    current_window["segments"].append(window["first_seg"])
                else:
                    merged_windows.append(current_window)
                    current_window = {
                        "start": window["start"],
                        "end": window["end"],
                        "codes": [window["code"]],
                        "segments": [window["first_seg"]],
                    }

            if current_window:
                merged_windows.append(current_window)

            windows_dir = os.path.join(output_dir, "merged_windows")
            os.makedirs(windows_dir, exist_ok=True)

            unseen_codes_set = set(unseen_codes.keys())
            unseen_neg_set = set(unseen_neg_filtered.keys())

            for index, merged_window in enumerate(merged_windows):
                window_df = df.iloc[
                    merged_window["start"] : merged_window["end"] + 1
                ].copy()

                def get_code_status(row: pd.Series) -> str:
                    reason = row.get("reason", "")
                    sig = extract_reason_signature(reason)
                    if not sig:
                        return ""
                    if sig in unseen_codes_set:
                        return "UNSEEN_TARGET"
                    if sig in unseen_neg_set:
                        return "NEG_TO_NEGATIVE_ONE"
                    if sig in known_codes:
                        return "KNOWN"
                    return ""

                window_df["code_status"] = window_df.apply(get_code_status, axis=1)
                window_path = os.path.join(
                    windows_dir,
                    f"window_{index + 1:02d}_rows_"
                    f"{merged_window['start']}-{merged_window['end']}.csv",
                )
                window_df.to_csv(window_path, index=False, encoding="utf-8-sig")

            logger.debug(
                f"Window LLM: {len(merged_windows)} windows created in {windows_dir}"
            )

    return lvl_mapping


def compact_for_llm(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse consecutive body rows into placeholder rows before LLM chunking."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["id", "heading", "level", "reason"])

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
                    "level": "-",
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
                    "level": (
                        int(lvl_int)
                        if lvl_int is not None and lvl_int != -2
                        else "Not Sure"
                    ),
                    "reason": str(row.get("reason", "") or ""),
                }
            )
            index += 1

    return pd.DataFrame(rows, columns=["id", "heading", "level", "reason"])


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
        row_filtered = row.drop(labels=["reason"], errors="ignore")
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

    basic_idx = 0
    for idx, chunk in enumerate(level_dfs):
        if (chunk["reason"].astype(str) != PLACEHOLDER_REASON).any():
            basic_idx = idx
            break
    basic_df = level_dfs[basic_idx]
    if basic_idx != 0:
        logger.info(
            f"smart parse => promoted chunk {basic_idx} as basic_df "
            f"(chunks 0..{basic_idx - 1} contain only placeholders)"
        )

    full_preds: pd.DataFrame | None = None
    try:
        with stage_timer(
            "heading.hierarchy_llm",
            chunk_count=len(level_dfs),
            base_chunk_rows=len(basic_df),
            compact_enabled=compact_enabled,
            source_row_count=len(raw_preds),
            model_name=model_name,
        ):
            logger.debug("smart parse => interpreting hierarchy patterns...")
            df4llm = basic_df.drop(columns=["reason"]).copy()
            df4llm["heading"] = df4llm["heading"].apply(clean_md_text_for_llm)
            logger.debug(f"DataFrame transformation completed, rows: {len(df4llm)}")

            layout_res = hierarchy_judge(
                df4llm, model_name, max_depth, toc_hierarchies, task="eval-headings"
            )

            layout_level_by_id: dict[Any, Any] = {}
            if isinstance(layout_res, list):
                for item in layout_res:
                    if isinstance(item, dict) and "id" in item and "level" in item:
                        layout_level_by_id[item["id"]] = item["level"]

            def level_for(row_id: Any) -> Any:
                if row_id in layout_level_by_id:
                    return layout_level_by_id[row_id]
                try:
                    return layout_level_by_id.get(int(row_id), -1)
                except (TypeError, ValueError):
                    return -1

            base_preds = (
                basic_df[["id", "heading", "reason"]].copy().reset_index(drop=True)
            )
            base_preds.insert(2, "level", base_preds["id"].map(level_for))
            save_intermediate_csv(
                base_preds, output_dir, f"preds_3_llm_base{csv_suffix}"
            )

            llm_levels: dict[int, Any] = {}
            for _, row in base_preds.iterrows():
                row_id = row["id"]
                if isinstance(row_id, bool):
                    continue
                if isinstance(row_id, int):
                    llm_levels[row_id] = row["level"]

            if len(level_dfs) > 1:
                placeholder_mask_base = base_preds["reason"].eq(PLACEHOLDER_REASON)
                figure_mask_base = base_preds["heading"].eq("Figure/Image")
                exclude_mask_base = placeholder_mask_base | figure_mask_base
                base_preds_for_mapping = base_preds[~exclude_mask_base].copy()
                base_origin_for_mapping = basic_df.loc[
                    ~exclude_mask_base.values, "level"
                ].tolist()

                base_preds_for_mapping, lvl_mapping = build_level_mapping(
                    base_preds_for_mapping, base_origin_for_mapping, mode="freq"
                )
                logger.debug(
                    f"mapping development finished: {len(lvl_mapping)} rules "
                    f"(placeholders and Figure/Image excluded)"
                )

                logger.debug(
                    f"mapping dataframe to levels across {len(level_dfs)} chunks..."
                )
                lvl_mapping = handle_unseen_codes(
                    preds_for_llm, level_dfs, lvl_mapping, output_dir
                )

                for level_df in level_dfs:
                    placeholder_mask_chunk = level_df["reason"].eq(PLACEHOLDER_REASON)
                    figure_mask_chunk = level_df["heading"].eq("Figure/Image")
                    exclude_mask_chunk = placeholder_mask_chunk | figure_mask_chunk
                    non_excluded = level_df[~exclude_mask_chunk].copy()
                    if not non_excluded.empty:
                        non_excluded = execute_level_mapping(non_excluded, lvl_mapping)
                        for _, row in non_excluded.iterrows():
                            row_id = row["id"]
                            if isinstance(row_id, bool):
                                continue
                            if isinstance(row_id, int):
                                llm_levels[row_id] = row["level"]
                logger.info(
                    f"multi-chunk mapping produced {len(llm_levels)} id->level entries"
                )
            else:
                logger.info(
                    "single chunk - skipping reason-code mapping, using LLM output directly"
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
            save_intermediate_csv(
                full_preds, output_dir, f"preds_4_llm_final{csv_suffix}"
            )

    except Exception as exc:
        logger.warning(
            f"LLM-based parsing fails due to {exc}, using non-llm pipeline..."
        )
        full_preds = fallback_hierarchy(raw_preds.copy())
    return full_preds
