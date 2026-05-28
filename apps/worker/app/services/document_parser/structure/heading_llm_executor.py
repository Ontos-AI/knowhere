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

    The output DataFrame has columns [id, heading, note, reason].

    ``note`` is used internally by ``run_merge_pre_pass`` only — it is
    NOT forwarded to the main hierarchy LLM:
    - ``"?"`` marks a heading candidate that is **directly adjacent** to the
      previous candidate with **no placeholder between them**.  This signals
      to the pre-pass that the pair should be evaluated for possible merging.
    - ``""`` (empty) for all other rows (normal candidates and placeholders).

    The ``level`` column is intentionally NOT forwarded to the LLM.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["id", "heading", "note", "reason"])

    rows: list[dict[str, Any]] = []
    index = 0
    row_count = len(df)
    prev_was_candidate = False  # True when the immediately preceding output row is a candidate

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
                    "note": "",
                    "reason": PLACEHOLDER_REASON,
                }
            )
            prev_was_candidate = False
            index = end_index
        else:
            row = df.iloc[index]
            # Mark with '?' when directly following another candidate (no placeholder gap)
            note = "?" if prev_was_candidate else ""
            rows.append(
                {
                    "id": int(row["id"]),
                    "heading": str(row["heading"]),
                    "note": note,
                    "reason": str(row.get("reason", "") or ""),
                }
            )
            prev_was_candidate = True
            index += 1

    return pd.DataFrame(rows, columns=["id", "heading", "note", "reason"])


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


def run_merge_pre_pass(
    compact_df: pd.DataFrame,
    model_name: str | None = None,
) -> dict[int, str]:
    """Focused pre-pass: decide merge/keep for consecutive heading candidate groups.

    Runs BEFORE the main hierarchy LLM call. Scans ``compact_df`` (output of
    ``compact_for_llm``) for rows whose ``note == "?"``, groups them together
    with their preceding candidate, and sends all groups in a single focused
    ``eval-merge-groups`` LLM call.

    Returns a dict ``{row_id: "<"}`` for every row the LLM decides to merge
    into the previous heading.  The caller seeds ``llm_levels`` with these
    decisions before running the main hierarchy LLM so that merge decisions
    are final and the main LLM only handles level assignment.
    """
    # ── 1. Collect consecutive groups ──
    groups: list[list[dict[str, Any]]] = []  # each element: list of {id, heading}
    current_group: list[dict[str, Any]] = []

    for _, row in compact_df.iterrows():
        if row.get("reason") == PLACEHOLDER_REASON:
            # Body-text placeholder breaks any running group
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = []
            continue

        note = str(row.get("note", ""))
        if note == "?":
            # Continuation of a consecutive run
            current_group.append({"id": int(row["id"]), "heading": str(row["heading"])})
        else:
            # Start of a new candidate — flush previous group if large enough
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = [{"id": int(row["id"]), "heading": str(row["heading"])}]

    if len(current_group) >= 2:
        groups.append(current_group)

    if not groups:
        logger.info("merge pre-pass: no consecutive groups found, skipping")
        return {}

    logger.info(f"merge pre-pass: {len(groups)} consecutive group(s) to evaluate")

    # ── 2. Format groups for the prompt ──
    lines: list[str] = []
    for g_idx, group in enumerate(groups, start=1):
        headings_str = " | ".join(f'"{item["heading"]}"' for item in group)
        lines.append(f"Group {g_idx}: [{headings_str}]")
    texts = "\n".join(lines)

    # ── 3. Call LLM directly (bypass df2md — texts is already formatted) ──
    from shared.services.ai.prompt_service import build_prompt
    from shared.services.ai.openai_compatible_client_sync import get_openai_client
    from shared.services.ai.response_process_service import eval_response

    try:
        prompt, temperature, top_p, max_tokens = build_prompt(
            task="eval-merge-groups",
            texts=texts,
            query="",
            paras={"max_tokens": min(800, len(groups) * 50 + 200)},
        )
        messages = [
            {"role": "system", "content": "you are a document structure expert"},
            {"role": "user", "content": prompt},
        ]
        with stage_timer("heading.merge_pre_pass_llm", group_count=len(groups), model_name=model_name):
            answer = get_openai_client(model=model_name).chat_completion(
                messages=messages,
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        result = eval_response(answer)
    except Exception as exc:
        logger.warning(f"merge pre-pass LLM call failed: {exc}, skipping pre-pass")
        return {}


    # ── 4. Parse result → {id: "<"} ──
    merge_ids: dict[int, str] = {}
    if not isinstance(result, list):
        logger.warning(f"merge pre-pass: unexpected result type {type(result)}, skipping")
        return {}

    for item in result:
        if not isinstance(item, dict):
            continue
        g_idx = item.get("group")
        should_merge = item.get("merge", False)
        if not should_merge:
            continue
        try:
            g_idx = int(g_idx)
        except (TypeError, ValueError):
            continue
        if g_idx < 1 or g_idx > len(groups):
            continue
        group = groups[g_idx - 1]
        # Mark all rows except the first as "<"
        for member in group[1:]:
            merge_ids[member["id"]] = "<"
            logger.debug(
                f"merge pre-pass: id={member['id']} '{member['heading'][:50]}' → '<'"
            )

    logger.info(f"merge pre-pass: {len(merge_ids)} row(s) flagged for merge")
    return merge_ids


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

    # ── Merge pre-pass (always on when compact is enabled) ──
    # Runs a focused LLM call BEFORE the main hierarchy call to resolve all
    # consecutive-candidate groups. Main LLM receives clean [id, heading] only.
    pre_pass_levels: dict[int, str] = {}
    if compact_enabled:
        pre_pass_levels = run_merge_pre_pass(
            preds_for_llm, model_name=model_name
        )

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
            # Seed with pre-pass merge decisions; main LLM cannot override them
            llm_levels: dict[int, Any] = dict(pre_pass_levels)

            for chunk_idx, chunk_df in enumerate(level_dfs):
                # Skip chunks that contain only placeholders
                non_placeholder_mask = chunk_df["reason"].astype(str) != PLACEHOLDER_REASON
                if not non_placeholder_mask.any():
                    logger.debug(
                        f"smart parse => chunk {chunk_idx}: all placeholders, skipping"
                    )
                    continue

                # Send only [id, heading] to the main LLM — no note, no merge hints
                df4llm = chunk_df.drop(columns=["reason", "level", "note"], errors="ignore").copy()
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
                                row_id = int(item["id"])
                                # Pre-pass merge decisions take priority — never override
                                if row_id in pre_pass_levels:
                                    continue
                                llm_levels[row_id] = item["level"]
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

            def resolve_level(row_id: Any) -> Any:
                try:
                    int_id = int(row_id)
                except (TypeError, ValueError):
                    return -1
                level = llm_levels.get(int_id, -1)
                # Pre-pass merge decisions arrive as "<"; pass through for _apply_merge_signals
                if level == "<":
                    return "<"
                try:
                    return int(level)
                except (TypeError, ValueError):
                    return -1

            full_preds["level"] = full_preds["id"].map(resolve_level)

    except Exception as exc:
        logger.warning(
            f"LLM-based parsing fails due to {exc}, using non-llm pipeline..."
        )
        full_preds = fallback_hierarchy(raw_preds.copy())
    return full_preds

