# pyright: reportArgumentType=false, reportAssignmentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false, reportOperatorIssue=false, reportOptionalMemberAccess=false, reportOptionalSubscript=false
import os

import gevent
import pandas as pd
from app.services.document_parser.heading_candidates import (
    filter_document_headings,
    filter_markdown_headings,
    postprocess_headings,
)
from app.services.document_parser.heading_llm_executor import (
    build_level_mapping,
    execute_level_mapping,
    execute_llm_heading_hierarchy,
)
from app.services.document_parser.heading_tree import (
    build_tree_from_dataframe as build_heading_tree_from_dataframe,
)
from app.services.document_parser.heading_tree import (
    remove_isolated_nodes as remove_isolated_heading_nodes,
)
from app.services.document_parser.heading_tree import (
    tree_to_dataframe as heading_tree_to_dataframe,
)
from app.services.document_parser.stage_profiler import stage_timer
from app.services.document_parser.table_text_parser import df2md
from gevent.pool import Pool as GeventPool

from loguru import logger

from shared.core.config import settings

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
    return build_heading_tree_from_dataframe(df)


def tree_to_dataframe(tree, node_to_id, original_df):
    return heading_tree_to_dataframe(tree, node_to_id, original_df)


def remove_isolated_nodes(tree):
    return remove_isolated_heading_nodes(tree)


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
        raw_preds = filter_markdown_headings(infos)
    elif doc_type == "md":
        raw_preds = filter_markdown_headings(infos, layout_json_path=layout_json_path)
    elif doc_type == "docx":
        raw_preds = filter_document_headings(infos, enable_regex=enable_regx)
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
        max_len: maximum heading length for executor chunk preparation
        max_depth: maximum hierarchy depth
        model_name: LLM model name
        output_dir: output directory, used to save intermediate results CSV
        csv_suffix: suffix for intermediate CSV filenames
    """
    model_name = _resolve_hierarchy_model_name(model_name)
    return execute_llm_heading_hierarchy(
        raw_preds=raw_preds,
        prompt_limt=prompt_limt,
        hierarchy_judge=hiearchy_llm,
        fallback_hierarchy=est_hierarchies_naive,
        save_intermediate_csv=save_intermediate_csv,
        toc_hierarchies=toc_hierarchies,
        max_len=max_len,
        max_depth=max_depth,
        model_name=model_name,
        output_dir=output_dir,
        csv_suffix=csv_suffix,
    )
