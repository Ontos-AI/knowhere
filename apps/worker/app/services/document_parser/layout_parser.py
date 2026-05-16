# pyright: reportArgumentType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false, reportOperatorIssue=false, reportOptionalMemberAccess=false, reportOptionalSubscript=false
import os
import re
import unicodedata
from collections import Counter, defaultdict

import gevent
import pandas as pd
from app.services.document_parser.text_helpers import count_cn_en, truncate_text_by_tokens
from app.services.document_parser.stage_profiler import stage_timer
from app.services.document_parser.table_parser import df2md
from docx.oxml.ns import qn
from gevent.pool import Pool as GeventPool

try:
    from markitdown import MarkItDown
except ImportError:
    # Fall back to a pass-through shim when markitdown is unavailable.
    class MarkItDown:
        def convert(self, content):
            return content


from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import WorkerHandlingException

# TaskRedis dependency is removed, use Redis directly to track
from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.response_process_service import eval_response

# ARQ dependency is removed, use Celery instead
from shared.utils.OpenAICompatibleClientSync import get_openai_client

# ==================== Helper Functions ====================


def _resolve_hierarchy_model_name(model_name=None):
    """Resolve the dedicated hierarchy LLM model with backward-compatible fallback."""
    return model_name or settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL


def save_intermediate_csv(df: pd.DataFrame, output_dir: str, filename: str):
    """
    save intermediate result to csv file, use utf-8-sig encoding to support Chinese and English
    Only saves when LOCAL_DEBUG environment variable is set to 'true'.

    Args:
        df: DataFrame to save
        output_dir: output directory path
        filename: filename (without extension)
    """
    if os.environ.get("LOCAL_DEBUG", "").lower() not in ("true", "1"):
        return
    if output_dir is None or df is None or df.empty:
        return

    try:
        csv_path = os.path.join(output_dir, f"{filename}.csv")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.debug(f"📊 Saved intermediate result to {csv_path}, rows={len(df)}")
    except Exception as e:
        logger.warning(f"Failed to save intermediate CSV {filename}: {e}")


# ==================== Tree Structure Functions (from sxjg) ====================


def build_tree_from_dataframe(df):
    """
    develop json tree from dataframe

    Args:
        df: DataFrame, including id, heading, level columns

    Returns:
        tree: pure nested dict structure
        node_to_id: map from tree node to id (use unique node key)
        id_to_row: map from id to original row data
    """
    headings = df[df["level"] > -1].copy()

    node_to_id = {}  # {(tree_node_key, parent_path): id}
    id_to_node_info = {}  # {id: (tree_node_key, parent_path)}
    id_to_row = {}
    root = {}
    stack = [(0, root, "ROOT", "")]

    for _, row in headings.iterrows():
        heading_txt = row["heading"]
        row_id = int(row["id"])
        level = int(row["level"])

        # record id to row mapping
        id_to_row[row_id] = row.to_dict()

        # find suitable parent node
        while len(stack) > 1 and stack[-1][0] >= level:
            stack.pop()

        # get parent node info
        parent_level, parent_dict, parent_heading, parent_path = stack[-1]

        # create unique key for tree node: if there are duplicate headings under the same parent, add ID suffix
        tree_node_key = heading_txt

        if tree_node_key in parent_dict:
            tree_node_key = f"{heading_txt}#{row_id}"

        # build mapping: use (tree_node_key, parent_path) as key
        node_key = (tree_node_key, parent_path)
        node_to_id[node_key] = row_id
        id_to_node_info[row_id] = node_key

        parent_dict[tree_node_key] = {}
        current_path = (
            f"{parent_path}/{tree_node_key}" if parent_path else tree_node_key
        )
        stack.append((level, parent_dict[tree_node_key], tree_node_key, current_path))
    return root, node_to_id, id_to_row


def tree_to_dataframe(tree, node_to_id, original_df):
    """
    convert processed tree structure back to dataframe

    Args:
        tree: processed pure nested dict structure
        node_to_id: map from node to id {(tree_node_key, parent_path): id}
        original_df: original dataframe

    Returns:
        updated_df: updated dataframe
    """

    # extract all retained headings from tree
    def extract_headings(node_dict, current_level=1, parent_path=""):
        """recursively extract all headings and their new levels"""
        results = []
        for tree_node_key, children in node_dict.items():
            # use (tree_node_key, parent_path) as key to find ID
            node_key = (tree_node_key, parent_path)
            row_id = node_to_id.get(node_key, -1)

            if row_id >= 0:
                # extract original heading from tree_node_key (remove possible ID suffix)
                original_heading = (
                    tree_node_key.split("#")[0]
                    if "#" in tree_node_key
                    else tree_node_key
                )

                results.append(
                    {
                        "id": row_id,
                        "heading": original_heading,
                        "level": current_level,
                        "tree_key": tree_node_key,
                        "parent_path": parent_path,
                    }
                )
                # recursively process child nodes
                if isinstance(children, dict) and children:
                    current_path = (
                        f"{parent_path}/{tree_node_key}"
                        if parent_path
                        else tree_node_key
                    )
                    results.extend(
                        extract_headings(children, current_level + 1, current_path)
                    )
        return results

    preserved_headings = extract_headings(tree)
    preserved_ids = set([h["id"] for h in preserved_headings])

    updated_df = original_df.copy()
    removed_count = 0
    level_changed_count = 0

    for idx, row in original_df.iterrows():
        row_id = int(row["id"])
        old_level = int(row["level"]) if row["level"] not in [-2, "nan", -1] else -1

        if old_level > -1:
            if row_id in preserved_ids:
                new_level = next(
                    (h["level"] for h in preserved_headings if h["id"] == row_id),
                    old_level,
                )
                updated_df.at[idx, "level"] = new_level
                if new_level != old_level:
                    level_changed_count += 1
            else:
                updated_df.at[idx, "level"] = -1
                removed_count += 1

    logger.debug(
        f"Tree changed: removed headings={removed_count}, level changed={level_changed_count}, preserved headings={len(preserved_ids)}"
    )
    return updated_df


def remove_isolated_nodes(tree):
    """
    rules: if a heading has only one child heading, and the child heading has no further child headings,
         then delete this isolated child heading

    Args:
        tree: pure nested dict structure, format as {heading: {child_heading: {...}}}

    Returns:
        processed_tree: processed tree structure
    """

    def recursive_check_and_remove(node_dict, parent_path=""):
        if not isinstance(node_dict, dict):
            return node_dict

        result_dict = {}

        for heading, children in node_dict.items():
            if isinstance(children, dict) and len(children) == 1:
                child_heading = list(children.keys())[0]
                grandchildren = children[child_heading]

                if not grandchildren or (
                    isinstance(grandchildren, dict) and len(grandchildren) == 0
                ):
                    result_dict[heading] = {}
                    logger.debug(
                        f"remove isolated heading: {parent_path}/{heading}/{child_heading}"
                    )
                else:
                    processed_children = recursive_check_and_remove(
                        children, f"{parent_path}/{heading}" if parent_path else heading
                    )
                    result_dict[heading] = processed_children
            elif isinstance(children, dict) and children:
                processed_children = recursive_check_and_remove(
                    children, f"{parent_path}/{heading}" if parent_path else heading
                )
                result_dict[heading] = processed_children
            else:
                result_dict[heading] = children

        return result_dict

    processed_tree = recursive_check_and_remove(tree)
    return processed_tree


# def if_no_pos_code(reason_str: str) -> bool:
#     """
#     Check whether all pos_code values are zero.
#     reason format: "POS [0, 0, ...] NEG [...]"
#     """
#     if not reason_str or not isinstance(reason_str, str):
#         return True

#     pos_match = re.search(r'POS\s*\[([^\]]*)\]', reason_str)
#     if not pos_match:
#         return True
#     pos_content = pos_match.group(1)
#     try:
#         nums = [int(x.strip()) for x in pos_content.split(',') if x.strip()]
#         return all(x == 0 for x in nums)
#     except:
#         return True


# ==================== Level Mapping Functions ====================


def build_level_mapping(df, origin_lvls, mode="max"):
    df = df.copy()
    df["origin_level"] = origin_lvls

    mapping = df.groupby("reason")["level"].apply(list).to_dict()

    processed_mapping = {}
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
    return df, processed_mapping


def execute_level_mapping(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    def map_row(row):
        reason = row["reason"]
        if reason in mapping:
            return mapping[reason]["mapped_lvl"]
        return row["level"]

    df = df.copy()
    origin_est_lvls = df["level"].tolist()
    df["level"] = df.apply(map_row, axis=1)
    df["origin_level"] = origin_est_lvls
    return df


def extract_non_neg_code(reason_str: str) -> str:
    """
    Extract the non-NEG code from reason_str (strip only NEG part, preserve META)

    Example: "POS [1, 0, 0] NEG [0, 0, 0] META [1, 2, 1]" -> "POS [1, 0, 0] META [1, 2, 1]"
    Example: "3# AND POS [1, 0] NEG [0, 0]" -> "3# AND POS [1, 0]"
    Example: "3# AND POS [1, 0] NEG [0, 0] META [1, 1, 0]" -> "3# AND POS [1, 0] META [1, 1, 0]"
    """
    if not reason_str or not isinstance(reason_str, str):
        return ""
    neg_match = re.search(r"\s*NEG\s*\[[^\]]*\]", reason_str)
    if neg_match:
        # Remove only the NEG [...] part, keep everything before and after
        before_neg = reason_str[: neg_match.start()]
        after_neg = reason_str[neg_match.end() :]
        return (before_neg + after_neg).strip()
    return reason_str.strip()


def build_non_neg_mapping(lvl_mapping: dict) -> dict:
    """
    Build non-NEG code mapping from complete lvl_mapping, select by highest frequency

    Args:
        lvl_mapping: reason -> level mapping

    Returns:
        non_neg_mapping: {non_neg_code: mapped_lvl}
    """
    # collect all levels for each non_neg_code
    non_neg_levels = {}
    for reason, info in lvl_mapping.items():
        non_neg_code = extract_non_neg_code(reason)
        mapped_lvl = info.get("mapped_lvl", -1)
        if non_neg_code:
            if non_neg_code not in non_neg_levels:
                non_neg_levels[non_neg_code] = []
            non_neg_levels[non_neg_code].append(mapped_lvl)

    # select by highest frequency
    non_neg_mapping = {}
    for non_neg_code, levels in non_neg_levels.items():
        positive_levels = [lvl for lvl in levels if lvl > -1]
        if positive_levels:
            level_counts = Counter(positive_levels)
            most_common_level = level_counts.most_common(1)[0][0]
            non_neg_mapping[non_neg_code] = most_common_level
        else:
            non_neg_mapping[non_neg_code] = -1

    return non_neg_mapping


def handle_unseen_codes(
    df: pd.DataFrame,
    level_dfs: list,
    lvl_mapping: dict,
    output_dir: str = None,
    window_half_size: int = 10,
    strategy: str = "double_mapping",
) -> dict:
    """
    Handle unseen codes with configurable strategy

    Args:
        df: original complete DataFrame
        level_dfs: segment DataFrames
        lvl_mapping: existing level mapping
        output_dir: output directory (optional, only used for window_llm strategy)
        window_half_size: window half size (how many rows above and below)
        strategy: "double_mapping" or "window_llm"
            - double_mapping: use non-neg code fallback (fast, no LLM call)
            - window_llm: create windows for LLM to judge (slower, more accurate)

    Returns:
        updated lvl_mapping
    """

    def extract_reason_signature(reason: str) -> str:
        """Extract reason signature"""
        return reason.strip() if reason else ""

    def has_neg_signal(reason_str: str) -> bool:
        """Check if NEG signal exists (any value >= 1)"""
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
        target_idx: int, known_codes_set: set, total_rows: int, half_size: int = 10
    ) -> dict:
        """
        Build context window for unseen codes
        1. window size: half_size
        2. window should contain at least one known code
        """
        min_start = max(0, target_idx - half_size)
        min_end = min(total_rows - 1, target_idx + half_size)

        start_idx = min_start
        end_idx = min_end

        found_known_above = False
        found_known_below = False
        known_positions = []

        # check above
        for i in range(start_idx, target_idx):
            reason = df.iloc[i].get("reason", "")
            sig = extract_reason_signature(reason)
            if sig in known_codes_set:
                found_known_above = True
                known_positions.append(i)

        # check below
        for i in range(target_idx + 1, end_idx + 1):
            reason = df.iloc[i].get("reason", "")
            sig = extract_reason_signature(reason)
            if sig in known_codes_set:
                found_known_below = True
                known_positions.append(i)

        # expand above if needed
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

        # expand below if needed
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

    # build non-neg mapping
    non_neg_mapping = build_non_neg_mapping(lvl_mapping)

    # get known codes
    known_codes = set(lvl_mapping.keys())

    # record all codes from all segments.  Placeholder rows (reason ==
    # PLACEHOLDER_REASON) are injected by _compact_for_llm and are never real
    # heading candidates, so they must be skipped here — otherwise they would
    # show up as an "unseen code" and fall through to NO_MATCH_FALLBACK, adding
    # harmless but noisy warnings to the log.
    all_codes_in_full = {}
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

    # find unseen codes
    unseen_codes = {}
    unseen_neg_filtered = {}
    for sig, info in all_codes_in_full.items():
        if sig in known_codes:
            continue
        if has_neg_signal(info["reason"]):
            unseen_neg_filtered[sig] = info
        else:
            unseen_codes[sig] = info

    logger.info(
        f"Unseen codes total: {len(unseen_codes) + len(unseen_neg_filtered)}, NEG filtered: {len(unseen_neg_filtered)}, to process: {len(unseen_codes)}"
    )

    # if neg signal, map to -1
    for sig in unseen_neg_filtered:
        lvl_mapping[sig] = {"mapped_lvl": -1, "note": "NEG_FILTERED"}

    # handle remaining unseen_codes based on strategy
    if unseen_codes:
        if strategy == "double_mapping":
            # Strategy 1: use non-neg code fallback
            fallback_success = 0
            fallback_failed = 0
            failed_codes = []
            for sig, info in unseen_codes.items():
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
                    f"Failed codes (non_neg not in mapping): {failed_codes[:5]}{'...' if len(failed_codes) > 5 else ''}"
                )

        elif strategy == "window_llm" and output_dir:
            # Strategy 2: create windows for LLM to judge
            total_rows = len(df)
            windows = []
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

            # merge windows
            sorted_windows = sorted(windows, key=lambda x: x["start"])
            merged_windows = []
            current_window = None

            for w in sorted_windows:
                if current_window is None:
                    current_window = {
                        "start": w["start"],
                        "end": w["end"],
                        "codes": [w["code"]],
                        "segments": [w["first_seg"]],
                    }
                elif w["start"] <= current_window["end"]:
                    current_window["end"] = max(current_window["end"], w["end"])
                    current_window["codes"].append(w["code"])
                    current_window["segments"].append(w["first_seg"])
                else:
                    merged_windows.append(current_window)
                    current_window = {
                        "start": w["start"],
                        "end": w["end"],
                        "codes": [w["code"]],
                        "segments": [w["first_seg"]],
                    }

            if current_window:
                merged_windows.append(current_window)

            # save windows
            windows_dir = os.path.join(output_dir, "merged_windows")
            os.makedirs(windows_dir, exist_ok=True)

            unseen_codes_set = set(unseen_codes.keys())
            unseen_neg_set = set(unseen_neg_filtered.keys())

            for i, mw in enumerate(merged_windows):
                window_df = df.iloc[mw["start"] : mw["end"] + 1].copy()

                def get_code_status(row):
                    reason = row.get("reason", "")
                    sig = extract_reason_signature(reason)
                    if not sig:
                        return ""
                    if sig in unseen_codes_set:
                        return "★ UNSEEN_TARGET"
                    elif sig in unseen_neg_set:
                        return "NEG→-1"
                    elif sig in known_codes:
                        return "KNOWN"
                    else:
                        return ""

                window_df["code_status"] = window_df.apply(get_code_status, axis=1)
                window_path = os.path.join(
                    windows_dir,
                    f"window_{i + 1:02d}_rows_{mw['start']}-{mw['end']}.csv",
                )
                window_df.to_csv(window_path, index=False, encoding="utf-8-sig")

            logger.debug(
                f"Window LLM: {len(merged_windows)} windows created in {windows_dir}"
            )
            # TODO: use llm to assign level based on window data

    return lvl_mapping


def detect_outlines_md(line):
    pos_code = judge_by_conditions(line)
    any(x > 0 for x in pos_code)


def get_max_lvl(code_str: str):
    match = re.search(r"\[([^]]+)]", code_str)
    if not match:
        return "Sure"

    nums = [int(x.strip()) for x in match.group(1).split(",")]
    max_val = int(max(nums))
    return max_val if max_val > 1 else -2  # -2 = "Not Sure" sentinel (int-safe)


PLACEHOLDER_REASON = "__PLACEHOLDER__"


def _compact_for_llm(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse consecutive ``level == -1`` rows into a single placeholder row.

    Rows with ``level >= 1`` (heading candidates) and ``level == -2`` ("Not Sure")
    are preserved verbatim so the LLM can still judge them.  Each run of
    consecutive ``-1`` rows becomes one placeholder row whose:

        id      = "start-end" (always a range; "N-N" when the run is one row)
        heading = "[N BODY LINES]" where N is the run length
        level   = "-"
        reason  = ``PLACEHOLDER_REASON``

    The id is ALWAYS a hyphenated string, even for single-row runs, so that
    ``int(id)`` fails for every placeholder.  This lets downstream code identify
    placeholders structurally (non-integer id) without depending on ``reason``
    or length heuristics.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["id", "heading", "level", "reason"])

    rows = []
    i = 0
    n = len(df)
    while i < n:
        lvl_raw = df.iloc[i]["level"]
        try:
            lvl_int = int(lvl_raw)
        except (TypeError, ValueError):
            lvl_int = None

        if lvl_int == -1:
            j = i
            while j < n:
                try:
                    nxt_lvl = int(df.iloc[j]["level"])
                except (TypeError, ValueError):
                    break
                if nxt_lvl != -1:
                    break
                j += 1
            start_id = int(df.iloc[i]["id"])
            end_id = int(df.iloc[j - 1]["id"])
            run = j - i
            rows.append(
                {
                    "id": f"{start_id}-{end_id}",
                    "heading": f"[{run} BODY LINES]",
                    "level": "-",
                    "reason": PLACEHOLDER_REASON,
                }
            )
            i = j
        else:
            r = df.iloc[i]
            rows.append(
                {
                    "id": int(r["id"]),
                    "heading": str(r["heading"]),
                    "level": (
                        int(lvl_int)
                        if lvl_int is not None and lvl_int != -2
                        else "Not Sure"
                    ),
                    "reason": str(r.get("reason", "") or ""),
                }
            )
            i += 1

    return pd.DataFrame(rows, columns=["id", "heading", "level", "reason"])


def heading_tb_transfer(df, threshold=3000, max_start=50, max_end=10):
    raw_headings = df["heading"].tolist()
    df["heading"] = df["heading"].apply(
        lambda x: truncate_text_by_tokens(x, max_start, max_end)
    )

    sub_dfs = []
    current_rows = []
    current_len = 0
    for _, row in df.iterrows():
        row_filtered = row.drop(labels=["reason"], errors="ignore")
        row_len = sum(count_cn_en(str(v)) for v in row_filtered.values)

        if current_len + row_len > threshold and current_rows:
            sub_dfs.append(pd.DataFrame(current_rows, columns=df.columns))
            current_rows = [row.tolist()]
            current_len = row_len
        else:
            current_rows.append(row.tolist())
            current_len += row_len

    if current_rows:
        sub_dfs.append(pd.DataFrame(current_rows, columns=df.columns))
    return sub_dfs, raw_headings


def judge_by_conditions(text, scope=20, return_detail=False, CN_SPECIAL_IDX=12):
    """
    judge level features as one-hot embeddings for texts

    Args:
        text: input text
        scope: text scope for judging
        return_detail: whether to return detailed information (including unit type)
        CN_SPECIAL_IDX: index of special Chinese number

    Returns:
        if return_detail=False: return pos_triggered_code list
        if return_detail=True: return (pos_triggered_code, detail_info) tuple
            where detail_info is a dictionary containing additional information, such as Chinese unit type
    """
    text = text.replace("\u3000", " ")
    text = unicodedata.normalize("NFKC", text)[:scope]

    # ========== English Numbering ==========
    regex_en_num_dots = r"^\d+(?:\s*\.\s*\d+)+(?![、，。！？；：])(?=\s|$|\w|[一-龥])"
    regex_en_num_dun = r"^\d、\s{0,4}(?=\S|$)"  # 1、xxx
    regex_en_num_dots_dun = r"^\d+(?:\.\d+)*、\s*(?=[A-Za-z一-龥])"
    regex_en_num_single_dot = r"^\d+\.(?!\d)\s{0,4}(?=\S)"  # 1.xxx
    regex_en_num_space = r"^[0-9]{1,2}\s{1,8}(?=\S)"  # 1 xxx
    # ========== Chinese Numbering ==========
    regex_cn_num_dun = r"^[一二三四五六七八九十百千万]+、\s{0,4}(?=\S|$)"
    regex_cn_num_mix = (
        r"^[一二三四五六七八九十百千万]+(?:\s*\.[一二三四五六七八九十百千万\d]+)+"
    )
    regex_cn_num_plain = r"^[一二三四五六七八九十百千万]+(?=\s|$)"
    # ========== English Bracketing ==========
    regex_en_brac_paren = r"^[\(\（]\s*\d+(?:\.\d+)*(?!\.0)\s*[\)\）]"
    regex_en_brac_right = r"^\d+(?:\.\d+)*(?!\.0)\s*[\)\）]"
    # ========== Chinese Bracketing ==========
    regex_cn_brac_paren = r"^[\(\（]\s*[一二三四五六七八九十百千万]+(?:\.[一二三四五六七八九十百千万\d]+)*\s*[\)\）]"
    regex_cn_brac_right = r"^[一二三四五六七八九十百千万]+(?:\.[一二三四五六七八九十百千万\d]+)*\s*[\)\）]"
    # ========== Chinese Special ==========
    regex_cn_special = r"^第[一二三四五六七八九十百千万\d]+(?:\.[一二三四五六七八九十百千万\d]+)*(章|节|条|部分|款|目|项|编|篇|卷|辑)?(?=$|\s|[A-Za-z0-9\u4e00-\u9fa5])"
    # ========== English Letter Numbering ==========
    regex_letter_dot = r"^[A-Za-z](?:\.\d+)*[\.、](?=\s*\S)"
    regex_letter_brac_paren = r"^[\(\（]\s*[A-Za-z](?:\.\d+)*(?!\.0)\s*[\)\）]"
    regex_letter_brac_right = r"^[A-Za-z](?:\.\d+)*(?!\.0)\s*[\)\）]"
    # ========== Appendix ==========
    regex_appendix = r"^((附件|附录|附表|附图)|(?i:appendix))[\s_\-—]{0,4}(?:\[)?[一二三四五六七八九十A-Za-z\d]"

    pos_regex_conditions = [
        # English Numbering
        regex_en_num_dots,
        regex_en_num_dun,
        regex_en_num_single_dot,
        regex_en_num_space,
        regex_en_num_dots_dun,
        # Chinese Numbering
        regex_cn_num_dun,
        regex_cn_num_mix,
        regex_cn_num_plain,
        # English Bracketing
        regex_en_brac_paren,
        regex_en_brac_right,
        # Chinese Bracketing
        regex_cn_brac_paren,
        regex_cn_brac_right,
        # Chinese Special
        regex_cn_special,
        # English Letter Numbering
        regex_letter_dot,
        regex_letter_brac_paren,
        regex_letter_brac_right,
        # Appendix
        regex_appendix,
    ]

    pos_triggered_code = []
    reason_suffix_parts = []

    for idx, regex in enumerate(pos_regex_conditions):
        match = re.match(regex, text)
        if match:
            matched_text = match.group(0)
            symbols = ".-"
            count_ = sum(matched_text.count(s) for s in symbols) + 1

            # Special handling for Chinese chapter/section/item markers.
            if idx == CN_SPECIAL_IDX and return_detail:
                unit_match = re.search(
                    r"(章|节|条|部分|款|目|项|编|篇|卷|辑)", matched_text
                )
                if unit_match:
                    unit = unit_match.group(1)
                    reason_suffix_parts.append(f"[CN:{unit}]")
            pos_triggered_code.append(count_)
        else:
            pos_triggered_code.append(0)

    if return_detail:
        detail_info = {
            "reason_suffix": (
                " ".join(reason_suffix_parts) if reason_suffix_parts else ""
            )
        }
        if detail_info["reason_suffix"]:
            detail_info["reason_suffix"] = " " + detail_info["reason_suffix"]
        return pos_triggered_code, detail_info
    return pos_triggered_code


def remove_by_conditions(text, include_punc=False):
    neg_condition_num = r"^\d{3,}"
    neg_condition_zero = r"^0\.\d+[\u4e00-\u9fa5A-Za-z\S]*"  # 0.2xxx
    neg_decimal_only = r"^\d*\.\d+$"  # 0.2 .23
    neg_condition_http = (
        r"(?i)(^https?://\S+|^www\.\S+|^P\.S|^\b\d{0,2}\s*(?:a\.m|p\.m)\b)"
    )
    # LaTeX: wrapped ($..\cmd..$) OR bare commands (\times, \mathrm, etc.)
    neg_condition_latex = (
        r"(?:"
        r"\$[^$]*\\[A-Za-z]+(?:\s*\{[^{}]*\})?[^$]*\$"  # wrapped: $...\cmd...$
        r"|"
        r"\\(?:times|div|cdot|pm|mp|leq|geq|neq|approx|equiv|sim|infty"
        r"|sum|prod|int|sqrt|frac|mathrm|mathbf|mathit|mathcal"
        r"|text(?:bf|it|rm)?|alpha|beta|gamma|delta|epsilon|theta"
        r"|lambda|mu|sigma|pi|omega|partial|nabla"
        r"|left|right|begin|end|overline|underline|hat|vec|tilde)\b"
        r")"
    )
    # Number immediately followed by measurement unit (e.g. 25.40mm, 100kPa)
    neg_condition_unit = (
        r"^\d+\.?\d*\s{0,2}"
        r"(?:mm|cm|km|nm|μm|inch(?:es)?|ft|yd|mi"
        r"|kg|mg|μg|lb|oz"
        r"|kPa|MPa|GPa|Pa|psi|bar"
        r"|°[CFK]"
        r"|Hz|kHz|MHz|GHz"
        r"|mol|mL|dL|dB|Nm|kN|MN|kW|MW|GW|hp|rpm|cc|cal|kcal)\b"
    )
    neg_condition_punc_mid = r"[。！；].+"
    neg_condition_punc_end = r"[.,;，。；]$"

    neg_conditions = [
        neg_condition_num,
        neg_condition_http,
        neg_condition_latex,
        neg_condition_zero,
        neg_decimal_only,
        neg_condition_punc_mid,
        neg_condition_unit,
    ]

    neg_triggered_code = []
    for regex in neg_conditions:
        match = re.search(regex, text)
        neg_triggered_code.append(1 if match else 0)

    if include_punc:
        match = re.search(neg_condition_punc_end, text)
        neg_triggered_code.append(1 if match else 0)
    else:
        neg_triggered_code.append(0)

    return neg_triggered_code


def md_heading_match(line, as_is=True):
    """handle markdown headings, considering # < ! [...."""
    match = re.match(r"^\s*(#+)\s*(.*)$", line)
    if match:
        level = len(match.group(1))  # count the number of '#'
        if as_is:  # determine if remove the '#'
            return line, level
        else:
            return line.lstrip("#").strip(), level
    else:
        return line, -1


def filter_md_headings(md_lines, num_pos=17, num_neg=7, layout_json_path=None):
    """filter candidate headings for .md

    Args:
        md_lines: list of markdown lines
        num_pos: number of positive conditions
        num_neg: number of negative conditions
        layout_json_path: optional path to layout.json for META features (size ranking)
    """
    # Create MetadataContext if layout_json_path is provided
    meta_ctx = None
    if layout_json_path:
        try:
            from .metadata_extractor import MetadataContext

            meta_ctx = MetadataContext(md_lines, layout_json_path)
        except Exception as e:
            logger.warning(f"Failed to create MetadataContext: {e}")

    raw_candidates = []
    for i, line in enumerate(md_lines):
        line = line.strip()
        if not line:
            continue

        if (
            ("<!--" in line and "-->" in line)  # annotation line
            or line.startswith("|")  # table line
            or line.startswith("<table>")
            or "![" in line
            and "](" in line  # image line
        ):
            est_lvl = -1
            zero_pos_code = [0] * num_pos
            zero_neg_code = [0] * num_neg
            str_lvl = f"POS {zero_pos_code} NEG {zero_neg_code}"
            if meta_ctx:
                str_lvl += " META [0, 0, 0]"
            line = "Figure/Image"
        else:
            line_clean, hash_lvl = md_heading_match(
                line, as_is=False
            )  # detect "#" in .md lines

            # NEW: detect and strip full-line bold markers (e.g. **3.4 Title** -> 3.4 Title)
            from .metadata_extractor import detect_and_strip_md_bold

            line_clean_stripped, is_full_bold = detect_and_strip_md_bold(line_clean)

            # Use stripped text for POS/NEG analysis (fixes '**3.4' -> '3.4' issue)
            pos_code, detail_info = judge_by_conditions(
                line_clean_stripped, return_detail=True
            )
            neg_code = remove_by_conditions(line_clean_stripped)

            if any(x > 0 for x in neg_code):
                code_lvl = -1
                code_str = f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"

            elif any(x > 0 for x in pos_code) and all(x == 0 for x in neg_code):
                code_lvl = get_max_lvl(str(pos_code))
                code_str = f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"

            else:
                code_lvl = -1
                code_str = f"POS {pos_code} NEG {neg_code}"

            # Add META suffix with bold dimension
            if meta_ctx:
                size_rank, occurrence = meta_ctx.get_meta_for_line(line_clean)
                is_bold_int = 1 if is_full_bold else 0
                code_str += meta_ctx.format_meta_suffix(
                    size_rank, occurrence, is_bold_int
                )
            else:
                # Even without layout.json, output bold info in META
                if is_full_bold:
                    code_str += " META [0, 0, 1]"

            if hash_lvl <= 0:
                est_lvl = code_lvl
                str_lvl = code_str
            else:
                if isinstance(code_lvl, int):
                    est_lvl = max(
                        hash_lvl, code_lvl
                    )  # current miner tend to produce fewer #s
                else:
                    est_lvl = code_lvl  # code_lvl could be not sure
                str_lvl = f"{hash_lvl}# AND {code_str}"
        raw_candidates.append((i, line, est_lvl, str_lvl))

    preds_df = pd.DataFrame(
        raw_candidates, columns=["id", "heading", "level", "reason"], index=None
    )
    return preds_df


def filter_doc_headings(titles_material, enable_regx=True, enable_style_check=False):
    """filter candidate headings for docx"""

    def find_docstyle(para_):
        try:
            style_name = para_.style.name
        except Exception:
            style_name = "normal"
        if style_name.startswith("Heading") or style_name.startswith("标题"):
            try:
                outline_level = int(style_name.split(" ")[1])
            except Exception:
                outline_level = -2  # "Not Sure" sentinel
            return outline_level
        else:
            return None

    def find_otsetting(para_):
        ppr = para_._element.find(qn("w:pPr"))
        if ppr is not None:
            plvl = ppr.find(qn("w:outlineLvl"))
        else:
            return None

        if plvl is not None:
            outline_level = int(plvl.get(qn("w:val"))) + 1
            return outline_level
        else:
            return None

    def find_bold(para_):
        if para_.runs and all(run.bold for run in para_.runs if run.text.strip()):
            return True
        else:
            return None

    raw_candidates = []
    logger.debug(
        "Filtering docx heading candidates... total_items={}", len(titles_material)
    )
    for ele_id, para, text in titles_material:
        str_lvl = ""
        est_lvl = None
        style_lvl = find_docstyle(para)
        setting_lvl = find_otsetting(para)

        # 1. check .docx style settings
        if style_lvl is not None:
            est_lvl = style_lvl
            str_lvl = f"style-{style_lvl}"

        # 2. check .docx paragraph numbering settings
        elif setting_lvl is not None:
            est_lvl = setting_lvl
            str_lvl = f"outline-{setting_lvl}"

        # 3. detect bold (unconditionally, encode as META dimension)
        is_bold = 1 if find_bold(para) else 0

        # 4. proceed condition judge
        if enable_regx:
            pos_code, detail_info = judge_by_conditions(text, return_detail=True)
            neg_code = remove_by_conditions(text)

            if any(x > 0 for x in neg_code):
                code_lvl = -1
                code_str = f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"
            elif any(x > 0 for x in pos_code) and all(x == 0 for x in neg_code):
                code_lvl = get_max_lvl(str(pos_code))
                code_str = f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"
            else:
                code_lvl = -1
                code_str = f"POS {pos_code} NEG {neg_code}"

            # Append bold as META dimension (DOCX has no layout.json, so only bold)
            if is_bold:
                code_str += f" META [0, 0, {is_bold}]"

            if est_lvl is None:
                est_lvl = code_lvl
                str_lvl = code_str
            else:
                str_lvl = f"{str_lvl} AND {code_str}"
        raw_candidates.append((ele_id, text, est_lvl, str_lvl))

    preds_df = pd.DataFrame(
        raw_candidates, columns=["id", "heading", "level", "reason"], index=None
    )

    # initial merge isolated and short texts
    preds_df = postprocess_headings(preds_df, task="merge_continuous")
    preds_df = postprocess_headings(preds_df, task="merge_short")
    return preds_df


def format_toc_context_for_llm(toc_context) -> str:
    """Convert TOC hierarchy or structured payloads into compact LLM-friendly plain text."""
    if not toc_context:
        return ""

    if isinstance(toc_context, str):
        return toc_context

    toc_items = toc_context if isinstance(toc_context, list) else [toc_context]
    formatted_blocks = []

    for toc_idx, toc_item in enumerate(toc_items, start=1):
        if not isinstance(toc_item, dict):
            formatted_blocks.append(str(toc_item))
            continue

        toc_range = toc_item.get("toc_range")
        toc_entries = toc_item.get("toc_with_level") or []

        if toc_range and len(toc_range) == 2:
            formatted_blocks.append(
                f"TOC {toc_idx} (source rows {toc_range[0]}-{toc_range[1]}):"
            )
        else:
            formatted_blocks.append(f"TOC {toc_idx}:")

        if not toc_entries:
            formatted_blocks.append("- No TOC entries available")
            continue

        if isinstance(toc_entries, str):
            toc_entries = toc_entries.strip()
            if toc_entries:
                formatted_blocks.append(toc_entries)
            else:
                formatted_blocks.append("- No TOC entries available")
            continue

        for entry in toc_entries:
            if not isinstance(entry, dict):
                continue

            heading = str(entry.get("heading", "")).strip().replace("\n", " ")
            if not heading:
                continue

            level = entry.get("level")
            line_id = entry.get("id")
            if isinstance(level, int):
                formatted_blocks.append(f"- level {level} | id {line_id} | {heading}")
            else:
                formatted_blocks.append(f"- id {line_id} | {heading}")

    return "\n".join(formatted_blocks)


def hiearchy_llm(
    df,
    model_name=None,
    max_depth=6,
    toc_context=None,
    max_len=8192,
    task="eval-headings",
):
    """Apply LLM to analyze the hierarchy of headings

    Args:
        df: DataFrame with id, heading columns
        model_name: LLM model name (optional, uses default if None)
        max_depth: Maximum hierarchy depth
        max_len: Hard cap for LLM completion max_tokens (default 2048).
                 Actual value is derived from the number of heading candidates.
        task: Prompt task type - "eval-headings" for general document, "eval-toc-headings" for TOC
        toc_context: Optional formatted TOC context string for guiding level assignment

    Returns:
        List of dicts with id and level, one per row in ``df`` (missing IDs -> level=-1).
    """

    model_name = _resolve_hierarchy_model_name(model_name)
    level_md = df2md(df)

    # Completion budget is driven by the number of heading candidates, not the
    # markdown input length.  Each JSON entry is `{"id":X,"level":Y}` ≈ 25 tokens;
    # add 200 tokens overhead for brackets/whitespace and leave a 512 floor for
    # tiny inputs.  Non-int ids (placeholders like "10-12" or "-") are excluded.
    def _is_candidate_id(val):
        if isinstance(val, bool):
            return False
        if isinstance(val, int):
            return True
        try:
            int(val)
            return True
        except (TypeError, ValueError):
            return False

    n_candidates = int(df["id"].apply(_is_candidate_id).sum()) if len(df) > 0 else 0
    ot_limit = max(512, n_candidates * 25 + 200)
    ot_limit = min(ot_limit, max_len)
    formatted_toc_context = format_toc_context_for_llm(toc_context)

    paras = {
        "max_tokens": ot_limit,
        "max_depth": max_depth,
        "toc_context": formatted_toc_context,
    }
    prompt, temperature, top_p, max_tokens = build_prompt(
        task=task, texts=level_md, query="", paras=paras
    )
    messages = [
        {"role": "system", "content": "you are a document auditing expert"},
        {"role": "user", "content": prompt},
    ]

    try:
        with stage_timer(
            "heading.hierarchy_llm_call",
            model_name=model_name,
            row_count=len(df),
            task=task,
            candidate_count=n_candidates,
            max_tokens=max_tokens,
        ):
            answer = get_openai_client(model=model_name).chat_completion(
                messages=messages,
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            layout_res = eval_response(answer)

        # Validate eval_response result — it can return a raw string when JSON parsing fails
        if not isinstance(layout_res, list):
            raise ValueError(
                f"LLM returned non-list response (type={type(layout_res).__name__}), "
                f"raw content: {str(layout_res)[:200]}"
            )

        # Validate each item is a dict with required keys
        for i, item in enumerate(layout_res):
            if not isinstance(item, dict) or "id" not in item or "level" not in item:
                raise ValueError(f"LLM response item[{i}] is malformed: {item!r}")

        # Drop items whose id is not a clean integer.  This includes placeholder
        # rows ("10-12", "-") that the LLM may echo back despite the prompt telling
        # it not to.
        clean_res = []
        dropped = 0
        for item in layout_res:
            raw_id = item["id"]
            if isinstance(raw_id, bool):
                dropped += 1
                continue
            if isinstance(raw_id, int):
                clean_res.append({"id": raw_id, "level": item["level"]})
                continue
            try:
                clean_res.append({"id": int(raw_id), "level": item["level"]})
            except (TypeError, ValueError):
                dropped += 1
        if dropped:
            logger.debug(f"filtered {dropped} non-integer-id entries from LLM response")

        # LLM only returns heading rows (level >= 1). Reconstruct full result so the
        # returned list has one entry per row in ``df``, with missing ids defaulting
        # to level=-1.  Rows whose ``id`` is itself non-integer (placeholders) keep
        # their id as-is and level=-1 so the caller can filter them out.
        llm_levels = {item["id"]: item["level"] for item in clean_res}
        full_result = []
        for row_id in df["id"].tolist():
            if _is_candidate_id(row_id):
                try:
                    int_id = int(row_id)
                except (TypeError, ValueError):
                    int_id = row_id
                full_result.append({"id": int_id, "level": llm_levels.get(int_id, -1)})
            else:
                full_result.append({"id": row_id, "level": -1})
        logger.debug(
            f"LLM returned {len(clean_res)} heading levels out of {n_candidates} candidates "
            f"({len(df)} total rows)"
        )
        return full_result
    except Exception as e:
        logger.error(f"detect hierarchy by LLM failed: {e}")
        raise


def _compute_zone_boundaries(toc_hierarchies, coordinate_mode="post_removal"):
    """Compute content zone boundaries for documents with multiple TOC areas.

    When multiple TOCs exist, they divide the document into zones. Each zone
    starts right after a TOC area and extends to just before the next TOC area
    (or end of document).

    coordinate_mode:
        - "post_removal": TOC ranges are in original coordinates, but heading IDs
          are measured after TOC rows were removed (MD/PDF path).
        - "original": heading IDs stay in original document coordinates, so zones
          can be computed directly from TOC boundaries (DOCX path).

    Args:
        toc_hierarchies: List of toc hierarchy dicts (sorted by toc_range start)

    Returns:
        List of (zone_start_post, zone_end_post_or_None, toc_hierarchy_dict)
        zone_end_post is None for the last zone (extends to end of document)
    """
    if coordinate_mode not in {"post_removal", "original"}:
        raise ValueError(f"Unsupported coordinate_mode: {coordinate_mode}")

    sorted_tocs = sorted(toc_hierarchies, key=lambda t: t["toc_range"][0])

    zones = []
    cumulative_removed = 0

    for i, toc in enumerate(sorted_tocs):
        toc_start, toc_end = toc["toc_range"]
        zone_start = toc_end + 1

        if coordinate_mode == "post_removal":
            toc_size = toc_end - toc_start + 1
            cumulative_removed += toc_size
            zone_start -= cumulative_removed

        if i + 1 < len(sorted_tocs):
            next_toc_start = sorted_tocs[i + 1]["toc_range"][0]
            zone_end = next_toc_start - 1
            if coordinate_mode == "post_removal":
                zone_end -= cumulative_removed
        else:
            zone_end = None  # to end of document

        if zone_end is not None and zone_end < zone_start:
            continue
        zones.append((zone_start, zone_end, toc))

    return zones


def _resolve_first_toc_boundary(toc_hierarchies=None, first_toc_ele_num=None):
    """Resolve the earliest available first-TOC boundary across coordinate sources."""
    toc_range_start = None
    if toc_hierarchies:
        first_range = toc_hierarchies[0].get("toc_range")
        if first_range and len(first_range) == 2:
            toc_range_start = first_range[0]

    candidates = [
        value for value in (toc_range_start, first_toc_ele_num) if value is not None
    ]
    if not candidates:
        return None

    resolved_start = min(candidates)
    if (
        toc_range_start is not None
        and first_toc_ele_num is not None
        and toc_range_start != first_toc_ele_num
    ):
        logger.info(
            f"📌 TOC boundary mismatch detected: toc_range start={toc_range_start}, "
            f"first_toc_ele_num={first_toc_ele_num}, using earliest={resolved_start}"
        )
    return resolved_start


def pred_titles(
    infos,
    doc_type,
    toc_hierarchies=None,
    prompt_limt=4000,
    enable_regx=True,
    smart_parse=False,
    model_name=None,
    output_dir=None,
    layout_json_path=None,
    first_toc_ele_num=None,
):
    """
    predict title hierarchy

    Args:
        infos: document information
        doc_type: document type (pptx, md, docx)
        toc_hierarchies: TOC hierarchy information (if any)
        prompt_limt: prompt character limit
        enable_regx: whether to enable regex matching
        smart_parse: whether to use LLM intelligent parsing
        model_name: LLM model name
        output_dir: output directory for saving intermediate CSV results
        layout_json_path: path to layout.json for META features (optional)
        first_toc_ele_num: ele_num of the first TOC block in DOCX (for pre-TOC exclusion)
    """
    model_name = _resolve_hierarchy_model_name(model_name)
    logger.info(
        f"Start to predict title hierarchy: doc_type={doc_type}, smart_parse={smart_parse}, candidate titles={len(infos)}"
    )

    if doc_type == "pptx":
        raw_preds = filter_md_headings(infos)
    elif doc_type == "md":
        raw_preds = filter_md_headings(infos, layout_json_path=layout_json_path)
    elif doc_type == "docx":
        raw_preds = filter_doc_headings(infos, enable_regx)
    else:
        raw_preds = pd.DataFrame(columns=["id", "heading", "level", "reason"])

    # ── Exclude pre-TOC lines from heading prediction ──
    # When TOC is detected, lines/blocks before the first TOC area are typically
    # cover/metadata (company name, version, classification marks), not real
    # headings.  Remove them before LLM judging to avoid misjudgment, then
    # splice back with level=-1 after all processing is done.
    pre_toc_rows = None
    first_toc_start = None
    if doc_type == "md":
        first_toc_start = _resolve_first_toc_boundary(toc_hierarchies=toc_hierarchies)
    elif doc_type == "docx":
        first_toc_start = _resolve_first_toc_boundary(
            toc_hierarchies=toc_hierarchies,
            first_toc_ele_num=first_toc_ele_num,
        )

    if first_toc_start is not None and first_toc_start > 0:
        pre_toc_mask = raw_preds["id"] < first_toc_start
        if pre_toc_mask.any():
            pre_toc_rows = raw_preds[pre_toc_mask].copy()
            pre_toc_rows["level"] = -1
            raw_preds = raw_preds[~pre_toc_mask].reset_index(drop=True)
            if doc_type == "docx":
                logger.info(
                    f"📌 Excluded {len(pre_toc_rows)} pre-TOC blocks "
                    f"(id < {first_toc_start}) from heading prediction"
                )
            else:
                logger.info(
                    f"📌 Excluded {len(pre_toc_rows)} pre-TOC lines "
                    f"(id < {first_toc_start}) from heading prediction"
                )

    # 2. Zone-based prediction when multiple TOCs exist
    if (
        toc_hierarchies
        and len(toc_hierarchies) > 1
        and doc_type in {"md", "docx"}
        and smart_parse
    ):
        # Multiple TOCs divide the document into independent zones.
        # Each zone gets its own naive + LLM pipeline with zone-specific TOC context.
        coordinate_mode = "post_removal" if doc_type == "md" else "original"
        zones = _compute_zone_boundaries(
            toc_hierarchies, coordinate_mode=coordinate_mode
        )
        logger.info(
            f"🗂️ Zone-based prediction: {len(zones)} zones from {len(toc_hierarchies)} TOCs"
        )

        def _process_single_zone(zone_idx, zone_start, zone_end, zone_toc):
            """Process a single zone independently. Returns (zone_idx, zone_heading_df)."""
            # Extract rows belonging to this zone
            if zone_end is not None:
                zone_mask = (raw_preds["id"] >= zone_start) & (
                    raw_preds["id"] <= zone_end
                )
            else:
                zone_mask = raw_preds["id"] >= zone_start
            zone_preds = raw_preds[zone_mask].copy().reset_index(drop=True)

            if zone_preds.empty:
                logger.warning(f"  Zone {zone_idx}: empty, skipping")
                return zone_idx, None

            zone_range_str = f"[{zone_start}, {zone_end or 'end'}]"
            logger.info(
                f"  Zone {zone_idx}: {len(zone_preds)} rows, post-removal range {zone_range_str}"
            )

            # Independent naive + LLM prediction for this zone
            zone_heading = est_hierarchies_naive(
                zone_preds, smart_parse, output_dir=output_dir
            )
            zone_heading = est_hierarchies_llm(
                zone_heading,
                prompt_limt,
                toc_hierarchies=[zone_toc],  # Single TOC for this zone
                model_name=model_name,
                output_dir=output_dir,
                csv_suffix=f"_zone_{zone_idx}",
            )
            valid_count = (
                len(zone_heading[zone_heading["level"] > 0])
                if not zone_heading.empty
                else 0
            )
            logger.info(f"  Zone {zone_idx}: ✅ {valid_count} valid headings")
            return zone_idx, zone_heading

        if len(zones) == 1:
            # Single zone: no parallel overhead
            zone_start, zone_end, zone_toc = zones[0]
            _, zone_heading = _process_single_zone(0, zone_start, zone_end, zone_toc)
            zone_results = [zone_heading] if zone_heading is not None else []
        else:
            # Multiple zones: parallel hierarchy prediction via gevent
            logger.info(
                f"Parallelizing zone hierarchy prediction for {len(zones)} zones"
            )
            pool = GeventPool(size=len(zones))
            greenlets = [
                pool.spawn(
                    _process_single_zone, zone_idx, zone_start, zone_end, zone_toc
                )
                for zone_idx, (zone_start, zone_end, zone_toc) in enumerate(zones)
            ]
            gevent.joinall(greenlets)

            # Collect results sorted by zone index to maintain document order
            results = sorted(
                [g.value for g in greenlets if g.value is not None], key=lambda r: r[0]
            )
            zone_results = [
                heading_df for _, heading_df in results if heading_df is not None
            ]

        if zone_results:
            heading_preds = (
                pd.concat(zone_results, ignore_index=True)
                .sort_values("id")
                .reset_index(drop=True)
            )
        else:
            heading_preds = pd.DataFrame(columns=["id", "heading", "level", "reason"])
        logger.info("✅ Zone-based LLM hierarchy parsing completed")
    else:
        # Single-zone: current behavior
        heading_preds = est_hierarchies_naive(
            raw_preds, smart_parse, output_dir=output_dir
        )
        if smart_parse:
            heading_preds = est_hierarchies_llm(
                heading_preds,
                prompt_limt,
                toc_hierarchies,
                model_name=model_name,
                output_dir=output_dir,
            )
            logger.info("✅ LLM hierarchy parsing completed")

    # 3. final polishing for certain types
    if doc_type in ["docx"]:
        heading_preds = postprocess_headings(heading_preds, task="merge_continuous")
        heading_preds = postprocess_headings(heading_preds, task="merge_short")
        heading_preds = postprocess_headings(heading_preds, task="judge_negs")
        logger.debug("Docx hiearchy detection postprocessing completed")

    if heading_preds["level"].eq(-1).all():  # if non are estimated as headings
        logger.warning("⚠️ No valid headings estimated")
        heading_preds = pd.DataFrame()
    else:
        heading_preds["level"] = (
            pd.to_numeric(heading_preds["level"], errors="coerce")
            .fillna(-1)
            .astype(int)
        )

        # process isolated nodes
        try:
            tree, node_to_id, _ = build_tree_from_dataframe(heading_preds)
            processed_tree = remove_isolated_nodes(tree)
            heading_preds = tree_to_dataframe(processed_tree, node_to_id, heading_preds)
        except Exception as e:
            logger.warning(f"Tree structure optimization failed, skipping: {e}")

        logger.info(
            f"✅ Heading parsing completed, final {len(heading_preds[heading_preds['level'] > 0])} valid headings"
        )

    # ── Splice pre-TOC rows back ──
    if pre_toc_rows is not None and not heading_preds.empty:
        heading_preds = (
            pd.concat(
                [pre_toc_rows[["id", "heading", "level", "reason"]], heading_preds],
                ignore_index=True,
            )
            .sort_values("id")
            .reset_index(drop=True)
        )
        logger.debug(
            f"📌 Spliced {len(pre_toc_rows)} pre-TOC lines back into predictions"
        )

    # Save heading_preds as preds_5
    save_intermediate_csv(heading_preds, output_dir, "preds_5_final_output")
    return heading_preds


def est_hierarchies_naive(raw_preds, proceed_smart=True, output_dir=None):
    """Detect hierarchies by non-LLM

    Args:
        raw_preds: raw data
        proceed_smart: whether to proceed with smart parsing
        output_dir: output directory, used to save intermediate results CSV
    """
    logger.debug("🚀 non-llm parsing => recursive processing")
    save_preds = raw_preds.copy()

    heading_preds = postprocess_headings(raw_preds, task="collapse")
    save_preds.insert(
        save_preds.columns.get_loc("level") + 1,
        "lvl_cola",
        heading_preds["level"].tolist(),
    )

    heading_preds = postprocess_headings(heading_preds, task="judge_negs")
    save_preds.insert(
        save_preds.columns.get_loc("lvl_cola") + 1,
        "lvl_neg",
        heading_preds["level"].tolist(),
    )
    save_preds["reason"] = heading_preds["reason"]

    # mapping based on freq
    if not proceed_smart:
        heading_preds["level"] = heading_preds["level"].map(
            lambda x: -1 if x == -2 else x
        )
        heading_preds, lvl_mapping = build_level_mapping(
            heading_preds, heading_preds["level"].tolist(), mode="freq"
        )
        heading_preds = execute_level_mapping(heading_preds, lvl_mapping)
        heading_preds.drop("origin_level", axis=1, inplace=True)
        save_preds.insert(
            save_preds.columns.get_loc("lvl_neg") + 1,
            "lvl_map",
            heading_preds["level"].tolist(),
        )

    return heading_preds


def est_hierarchies_llm(
    raw_preds,
    prompt_limt,
    toc_hierarchies=None,
    max_len=30,
    max_depth=6,
    model_name=None,
    output_dir=None,
    csv_suffix="",
):
    """LLM-based hierarchy detection — first chunk via LLM, remaining chunks via reason-code mapping.

    When ``KB_LAYOUT_LLM_COMPACT_INPUT`` is enabled (default), consecutive
    ``level == -1`` rows in ``raw_preds`` are folded into a single placeholder
    row (``[N BODY LINES]``) before chunking.  This shrinks the prompt, makes
    most documents fit into a single chunk (skipping the lossy reason-code
    mapping), and preserves the positional signal for the LLM.

    Strategy:
        1. (Optional) Compact raw_preds so consecutive body rows become placeholders.
        2. Send only the first chunk to LLM for hierarchy prediction.
        3. Collect ``{id -> level}`` from the LLM response (int ids only).
        4. For multi-chunk docs, extend that mapping via reason-code mapping on
           chunks 1..N (placeholders excluded).
        5. Expand the id->level mapping back onto the ORIGINAL ``raw_preds``;
           any row not present in the mapping defaults to ``level = -1``.

    Args:
        raw_preds: raw data
        prompt_limt: prompt character limit
        toc_hierarchies: TOC hierarchies
        max_len: maximum heading length (passed through to heading_tb_transfer)
        max_depth: maximum hierarchy depth
        model_name: LLM model name
        output_dir: output directory, used to save intermediate results CSV
        csv_suffix: suffix for intermediate CSV filenames
    """
    model_name = _resolve_hierarchy_model_name(model_name)
    if len(raw_preds) == 0:
        return pd.DataFrame(columns=["id", "heading", "level", "reason"])

    compact_enabled = os.environ.get(
        "KB_LAYOUT_LLM_COMPACT_INPUT", "true"
    ).strip().lower() in ("true", "1", "yes", "on")
    preds_for_llm = _compact_for_llm(raw_preds) if compact_enabled else raw_preds.copy()
    if compact_enabled:
        placeholder_count = int(preds_for_llm["reason"].eq(PLACEHOLDER_REASON).sum())
        logger.info(
            f"smart parse => compact input: {len(raw_preds)} → {len(preds_for_llm)} rows "
            f"({placeholder_count} placeholder groups)"
        )

    # Short-circuit: if there are no heading candidates to judge (all rows were
    # collapsed into placeholders, or raw_preds contains only level==-1 rows
    # with compaction disabled), skip the LLM entirely and return raw_preds
    # with all levels set to -1.
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

    level_dfs, _raw_headings = heading_tb_transfer(
        preds_for_llm, threshold=prompt_limt, max_start=max_len, max_end=5
    )
    chunk_sizes = [len(d) for d in level_dfs]
    logger.info(
        f"smart parse => {len(level_dfs)} chunk(s) | rows per chunk: {chunk_sizes} | "
        f"threshold={prompt_limt} | max_start={max_len}"
    )

    # Pick the first chunk that actually contains heading candidates.  When
    # compaction is enabled a small prompt_limt may push a placeholder-only
    # chunk to index 0 — using it would waste an LLM call and produce an empty
    # mapping.  Placeholder chunks that precede the chosen one contribute no
    # reason-code signal (their ids map to -1 anyway).
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
    full_preds = None
    try:
        with stage_timer(
            "heading.hierarchy_llm",
            chunk_count=len(level_dfs),
            base_chunk_rows=len(basic_df),
            compact_enabled=compact_enabled,
            source_row_count=len(raw_preds),
            model_name=model_name,
        ):
            logger.debug("🚀 smart parse => interpreting hierarchy patterns...")
            df4llm = basic_df.drop(columns=["reason"]).copy()
            from .metadata_extractor import clean_md_text_for_llm

            # Keep formatting signals in `reason` / preliminary `level`, but let the LLM
            # judge hierarchy from the semantic heading text instead of raw markdown markers.
            df4llm["heading"] = df4llm["heading"].apply(clean_md_text_for_llm)
            logger.debug(f"DataFrame transformation completed, rows: {len(df4llm)}")

            layout_res = hiearchy_llm(
                df4llm, model_name, max_depth, toc_hierarchies, task="eval-headings"
            )

            # Build base_preds by aligning on basic_df["id"]: we always have one
            # row per chunk-0 row in the rendered output, regardless of how many
            # entries the LLM actually returned.  Missing ids -> level=-1.
            layout_level_by_id = {}
            if isinstance(layout_res, list):
                for item in layout_res:
                    if isinstance(item, dict) and "id" in item and "level" in item:
                        layout_level_by_id[item["id"]] = item["level"]

            def _level_for(rid):
                if rid in layout_level_by_id:
                    return layout_level_by_id[rid]
                try:
                    return layout_level_by_id.get(int(rid), -1)
                except (TypeError, ValueError):
                    return -1

            base_preds = (
                basic_df[["id", "heading", "reason"]].copy().reset_index(drop=True)
            )
            base_preds.insert(2, "level", base_preds["id"].map(_level_for))

            # Save base_preds as preds_3 (reflects what the LLM saw, compact or not)
            save_intermediate_csv(
                base_preds, output_dir, f"preds_3_llm_base{csv_suffix}"
            )

            # Collect {int_id -> level} from chunk-0 LLM output.  Placeholder rows
            # have non-integer ids and are skipped.
            llm_levels = {}
            for _, row in base_preds.iterrows():
                rid = row["id"]
                if isinstance(rid, bool):
                    continue
                if isinstance(rid, int):
                    llm_levels[rid] = row["level"]

            if len(level_dfs) > 1:
                # Multi-chunk: build reason-code mapping from chunk-0 candidates and
                # apply it to chunks 1..N to infer levels for headings beyond chunk 0.
                # Placeholder rows are excluded from both the mapping source and the
                # per-chunk application — they always map to level=-1 in the final df.
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
                            rid = row["id"]
                            if isinstance(rid, bool):
                                continue
                            if isinstance(rid, int):
                                # Mapping may override chunk-0 LLM decisions when
                                # two rows share the same reason; accept that (the
                                # mapping is by construction the "representative"
                                # level for each reason-code).
                                llm_levels[rid] = row["level"]
                logger.info(
                    f"multi-chunk mapping produced {len(llm_levels)} id→level entries"
                )
            else:
                logger.info(
                    "single chunk — skipping reason-code mapping, using LLM output directly"
                )

            # Expand back onto the original raw_preds: heading candidates take
            # the LLM/mapping-assigned level; everything else is body text (-1).
            full_preds = raw_preds.copy()
            full_preds = full_preds[["id", "heading", "level", "reason"]]

            def _resolve_level(rid):
                try:
                    int_id = int(rid)
                except (TypeError, ValueError):
                    return -1
                lvl = llm_levels.get(int_id, -1)
                try:
                    return int(lvl)
                except (TypeError, ValueError):
                    return -1

            full_preds["level"] = full_preds["id"].map(_resolve_level).astype(int)

            save_intermediate_csv(
                full_preds, output_dir, f"preds_4_llm_final{csv_suffix}"
            )

    except Exception as e:
        logger.warning(f"LLM-based parsing fails due to {e}, using non-llm pipeline...")
        full_preds = est_hierarchies_naive(raw_preds.copy())
    return full_preds


def collapse_recursive(df, task, indices, merge_th=3, checked_pairs=None, depth=0):
    """recursive collapse"""
    if checked_pairs is None:
        checked_pairs = set()

    if len(indices) < 2:
        return

    for k in range(len(indices) - 1):
        i, j = indices[k], indices[k + 1]
        if (i, j) in checked_pairs:
            continue
        checked_pairs.add((i, j))

        between = df.loc[i + 1 : j - 1]
        i_txt = df.at[i, "heading"].strip()
        j_txt = df.at[j, "heading"].strip()

        if task == "merge_short" and len(between) > 0:
            between_lens = [count_cn_en(c) for c in between["heading"].tolist()]
            between_lvls = [bl for bl in between["level"].tolist()]
            i_half_len = int(count_cn_en(i_txt) / 2)
            too_short = sum(between_lens) <= merge_th or sum(between_lens) < i_half_len

            if too_short and all(
                bl == -1 for bl in between_lvls
            ):  # only non-headings can be merged
                logger.debug(
                    f"⚠️ too short between {i}=>{i_txt[:15]} and {j}=>{j_txt[:15]} => merge to {i}"
                )
                between_txts = [
                    str(r["heading"]).strip()
                    for _, r in between.iterrows()
                    if isinstance(r.get("heading"), str) and r["heading"].strip()
                ]

                if between_txts:
                    joined_txt = "\n".join(between_txts)
                    df.at[i, "heading"] = f"{i_txt} {joined_txt}"

                for idx in between.index:
                    df.at[idx, "level"] = -1
                    df.at[idx, "reason"] = f"Merged into {i}"
                logger.debug(f"\tmerged texts: {joined_txt[:50]}...")

        elif task == "collapse" and len(between) == 0:
            logger.debug(
                f"⚠️ Empty between i={i_txt[:15]}, j={j_txt[:15]} => set i.level=-1, j.level=Not Sure"
            )
            df.at[i, "level"] = -2  # "Not Sure" sentinel (int-safe)
            df.at[j, "level"] = -2  # "Not Sure" sentinel (int-safe)

        # ========== get subgroups for recursive tasks ==========
        sub_between = between[between["level"] != -1]
        code2sub = defaultdict(list)
        for idx, row in sub_between.iterrows():
            level = row["level"]
            reason = row["reason"]
            if level != -1:
                code2sub[(level, reason)].append(idx)

        for _, sub_indices in code2sub.items():
            collapse_recursive(
                df, task, sub_indices, merge_th, checked_pairs, depth + 1
            )


def postprocess_headings(df, task, max_depth=-1):
    """postprocess headings"""
    if task == "judge_negs":
        for i, row in df.iterrows():
            neg_code = remove_by_conditions(row["heading"], include_punc=True)
            if any(x > 0 for x in neg_code):
                current_code = str(df.loc[i, "reason"])

                neg_match = re.search(r"(.*NEG\s*)\[[^\]]*\](.*)", current_code)
                if neg_match:
                    update_code = f"{neg_match.group(1)}{neg_code}{neg_match.group(2)}"
                else:
                    update_code = f"{current_code} NEG {neg_code}"

                df.loc[i, "level"] = -1
                df.loc[i, "reason"] = update_code
        return df

    elif task == "merge_continuous":
        denoised_rows = []
        punc_pattern = re.compile(r'[.,!?;:，。！？；：）】〕｝〉》’”"]$')

        i = 0
        while i < len(df):
            row = df.iloc[i]
            current_content = str(row["heading"]).strip()
            current_level = row["level"]

            j = i + 1
            while j < len(df):
                next_row = df.iloc[j]
                next_content = str(next_row["heading"]).strip()
                next_level = next_row["level"]

                # Skip merge if ID is not continuous (indicates table/image was skipped in between)
                expected_id = row["id"] + (j - i)
                if next_row["id"] != expected_id:
                    break

                # both current and next rows are not heading & current row has no punctuation -> merge
                current_not_punc = not punc_pattern.search(current_content[-2:])
                if (current_level == -1 and next_level == -1) and current_not_punc:
                    current_content += " " + next_content
                    j += 1
                else:
                    break

            merge_row = row.copy()
            merge_row["heading"] = current_content
            denoised_rows.append(tuple(merge_row))
            i = j
        return pd.DataFrame(denoised_rows, columns=["id", "heading", "level", "reason"])

    elif task == "merge_short" or task == "collapse":
        group2indices = defaultdict(list)
        for idx, row in df.iterrows():
            level = row["level"]
            reason = row["reason"]
            if level != -1:
                group2indices[(level, reason)].append(idx)

        checked_pairs = set()
        for _, indices in group2indices.items():
            collapse_recursive(
                df, task, indices, merge_th=3, checked_pairs=checked_pairs, depth=0
            )

        if task == "merge_short":
            drop_between = df.index[
                df["reason"].astype(str).str.startswith("Merged into", na=False)
            ].tolist()
            if drop_between:
                logger.debug(
                    f"🛠️ Delete rows labeled as merged into, total {len(drop_between)} rows"
                )
                df.drop(drop_between, inplace=True)
                df.reset_index(drop=True, inplace=True)
        return df

    else:
        return None


# def parse_outline_hier(markdown_text):
#     lines = markdown_text.strip().splitlines()
#     stack = []
#     root = []
#     for line in lines:
#         line = line.replace('markdown', '')  # handle possible unexpected outputs
#         if not line.strip():
#             continue

#         stripped = line.lstrip()
#         indent = len(line) - len(stripped)
#         match = re.match(r"[-*+] (.+)", stripped)
#         if not match:
#             continue

#         title = match.group(1).strip()
#         node = {"chapter": title, "children": [], 'serial': 1}
#         level = indent // 2  # Two spaces per level, adjustable if needed.
#         if level == 0:
#             node['serial'] = len(root)+1
#             root.append(node)
#             stack = [(level, node)]
#         else:
#             while stack and stack[-1][0] >= level:
#                 stack.pop()
#             if stack:
#                 parent = stack[-1][1]
#                 node['serial'] = len(parent['children']) + 1
#                 parent["children"].append(node)
#             stack.append((level, node))
#     return root


# def outline_to_markdown(nodes, level=0, path=""):
#     rows = []
#     def traverse(node_list, level, path_prefix):
#         for node in node_list:
#             split_char = settings.SPLIT_CHAR or "/"
#             current_path = f"{path_prefix} {split_char} {node['chapter']}" if path_prefix else node['chapter']
#             rows.append({
#                 "path": current_path,
#                 "title": node["chapter"],
#                 "thoughts": node.get("thoughts", "").strip(),
#                 "level": level
#             })
#             if node.get("children"):
#                 traverse(node["children"], level + 1, current_path)
#     traverse(nodes, level, path)
#     return pd.DataFrame(rows)
