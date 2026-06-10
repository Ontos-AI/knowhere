# pyright: reportArgumentType=false, reportAssignmentType=false, reportOptionalIterable=false, reportOptionalMemberAccess=false, reportOptionalOperand=false, reportOptionalSubscript=false
import json
import os
import re
import shutil

from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.formats.markdown.deferred_summary import (
    MarkdownDeferredSummaryInput,
    apply_markdown_deferred_summaries,
)
from app.services.document_parser.formats.markdown.image_asset import (
    MarkdownImageAssetRequest,
    build_markdown_image_name,
    build_markdown_image_asset,
)
from app.services.document_parser.formats.markdown.parse_state import MarkdownParseState
from app.services.document_parser.formats.markdown.table_asset import (
    MarkdownTableAssetRequest,
    build_markdown_table_asset,
)
from app.services.document_parser.support.parser_rows import ParsedRow
from app.services.document_parser.support.path_helpers import find_matches_parsing
from app.services.document_parser.formats.html.parser import (
    merge_html_tables,
)
from app.services.document_parser.formats.image.parser import (
    MD_IMAGE_PATTERN,
    detect_summary_img_md,
)
from app.services.document_parser.structure.heading_hierarchy import (
    HeadingHierarchyInput,
    predict_heading_hierarchy,
)
from app.services.document_parser.structure.heading_candidates import md_heading_match
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.document_parser.tables.table_text_parser import (
    extract_tables_by_forms,
    identify_tables,
)
from app.services.document_parser.structure.toc_parser import detect_tocs_in_texts
from app.services.document_parser.formats.text.parser import extract_title_keywords_summary
from loguru import logger

from shared.core.config import settings
from shared.utils.chunk_refs import has_chunk_ref
from shared.utils.text_utils import tokenize2stw_remove


def find_surround_context(md_lines, lid):
    def is_skip(line):
        s = line.strip()
        is_image = re.findall(MD_IMAGE_PATTERN, line, flags=re.IGNORECASE)
        is_table, _, _ = identify_tables(line)
        return not s or is_image or is_table

    n = len(md_lines)
    prev_text = ""
    for i in range(max(lid - 5, 0), lid):
        if not is_skip(md_lines[i]):
            prev_text = md_lines[i].strip()
            break

    next_text = ""
    for i in range(lid + 1, min(lid + 6, n)):
        if not is_skip(md_lines[i]):
            next_text = md_lines[i].strip()
            break
    return f"{prev_text} {next_text}".strip()


def heading_md_relocate(md_lines, heading_preds):
    """Relocate markdown headings based on predicted levels (sxjg simplified logic)

    When ``_apply_merge_signals`` has merged continuation rows (level='<')
    into a preceding heading, the DataFrame's ``heading`` column contains the
    merged text while ``md_lines`` still has the original truncated text.
    For positive-level headings, we use the DataFrame's ``heading`` to ensure the merged text is emitted.
    """

    def remove_hash(txt):
        return re.sub(r"^\s*(#+)\s*", "", txt)

    for lid, line_txt in enumerate(md_lines):
        pred_level_df = heading_preds[heading_preds["id"] == lid]

        if pred_level_df.empty:  # if the line does not enter predicting
            line_txt = remove_hash(line_txt)
        else:
            pred_level = pred_level_df["level"].iloc[0]
            if pred_level < 0:
                line_txt = remove_hash(line_txt)
            else:
                # Use the DataFrame heading text which may have been updated
                # by _apply_merge_signals (continuation rows appended).
                heading_text = str(pred_level_df["heading"].iloc[0]).strip()
                if not heading_text:
                    # Fallback: strip original line's '#' prefix
                    heading_text = line_txt.lstrip("#").lstrip()
                line_txt = f"{'#' * int(pred_level)} {heading_text}"
        # update lines
        md_lines[lid] = line_txt.strip()

    md_lines = [line for line in md_lines if line.strip() != ""]
    return md_lines  # note the length=original md_lines but contents/level are updated


def eval_md_headings(
    md_lines,
    source_type,
    toc_hierarchies=None,
    smart_parse=False,
    model_name=None,
    output_dir=None,
    layout_json_path=None,
    is_first_shard=True,
):
    """Evaluate markdown headings with optional TOC hierarchies context"""
    heading_preds = predict_heading_hierarchy(
        HeadingHierarchyInput(
            infos=md_lines,
            doc_type=source_type,
            toc_hierarchies=toc_hierarchies,
            enable_regex=True,
            smart_parse=smart_parse,
            model_name=model_name,
            output_dir=output_dir,
            layout_json_path=layout_json_path,
            is_first_shard=is_first_shard,
        )
    )

    if len(heading_preds) == 0:
        lines_with_heading = md_lines
    else:
        lines_with_heading = heading_md_relocate(md_lines, heading_preds)
    return lines_with_heading


def clean_md_table_lines(table_lines, start_line_num):
    expected_columns = table_lines[0].count("|") - 1
    cleaned_lines = []
    error_lines = []  # To record line numbers that need cleaning

    for i, line in enumerate(table_lines):
        line_columns = line.count("|") - 1
        current_line_num = (
            start_line_num + i
        )  # Calculate the current line number in the original file
        if line_columns == expected_columns:
            cleaned_lines.append(line)
        else:
            error_lines.append(current_line_num)
            if line_columns > expected_columns:
                parts = line.split("|")
                cleaned_line = "|".join(
                    parts[: expected_columns + 1]
                )  # If there are more columns, combine them (or drop extra columns)
                cleaned_lines.append(cleaned_line)
            elif line_columns < expected_columns:
                # If there are fewer columns, pad the line (or you could skip it)
                cleaned_line = line + "|" * (expected_columns - line_columns)
                cleaned_lines.append(cleaned_line)
    return cleaned_lines, error_lines


def update_df_list(
    df_list,
    content_items,
    path,
    llm_paras,
    time_stamp,
    page_nums="",
    summary_len=1500,
    skip_llm=False,
):
    """Flush accumulated content_items into a chunk row in df_list.

    Args:
        content_items: list of content strings. Each item is either pure text
            or an IMAGE/TABLE ref block. know_id is generated from pure text
            items only (deterministic), while full content includes all items.
        skip_llm: if True, skip inline LLM calls (deferred to parallel batch).
    """
    # Separate pure text from IMAGE/TABLE ref blocks for deterministic know_id
    text_items = [item for item in content_items if not has_chunk_ref(str(item))]
    pure_text = "\n".join(text_items).strip()
    bottom_content = "\n".join(content_items).strip()

    match_type = find_matches_parsing(bottom_content, path)
    know_id_source = pure_text if pure_text else f"{path or ''}::{page_nums or ''}"
    know_id = gen_str_codes(know_id_source)
    bottom_tokens = tokenize2stw_remove([bottom_content], llm_paras["stopwords"])

    keywords = ""
    summary = ""
    needs_llm = (
        not skip_llm and len(bottom_content) > summary_len and llm_paras["summary_txt"]
    )
    if needs_llm:
        _title, keywords, summary = extract_title_keywords_summary(
            bottom_content, max_keywords=3, summary_len=summary_len
        )

    df_list.append(
        ParsedRow(
            content=bottom_content,
            path=path,
            type=match_type,
            keywords=keywords,
            summary=summary,
            know_id=know_id,
            tokens=bottom_tokens,
            addtime=time_stamp,
            page_nums=page_nums,
        ).to_list()
    )
    return df_list


def parse_md(
    output_dir,
    source_type,
    file_path=None,
    md_lines=None,
    base_llm_paras=None,
    relative_root=None,
    toc_hierarchies=None,
    lines_with_heading=None,
    is_first_shard=True,
):
    if lines_with_heading is not None:
        # ── Phase A bypass ──
        # Caller (e.g. oversized PDF shard-first path) already ran per-shard
        # heading prediction and passed in the merged lines_with_heading.
        # Skip TOC detection and heading prediction entirely.
        logger.info(
            f"📌 Using pre-identified headings ({len(lines_with_heading)} lines), "
            f"skipping TOC detection and heading prediction"
        )
    else:
        # ── Phase A: TOC detection + heading prediction ──
        if md_lines is None and file_path is not None:
            from app.services.common.file_loading import is_remote, load_file_bytes

            if is_remote(file_path):
                file_bytes = load_file_bytes(file_path)
                md_content = file_bytes.decode("utf-8")
                md_lines = md_content.splitlines()
            else:
                with open(file_path, "r", encoding="utf-8") as file:
                    md_lines = file.readlines()

        md_lines = [line.strip() for line in md_lines if line.strip() != ""]

        # Preprocess: merge multi-line HTML tables into single lines
        md_lines = merge_html_tables(md_lines)

        # Detect TOC using async LLM-based detection
        toc_model_name = (
            base_llm_paras.get("model_name", settings.NORMOL_MODEL)
            if base_llm_paras
            else settings.NORMOL_MODEL
        )
        hierarchy_model_name = (
            (base_llm_paras.get("hierarchy_model_name") or toc_model_name)
            if base_llm_paras
            else (settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL)
        )

        if toc_hierarchies is not None:
            # Pre-detected TOC from upstream (e.g. DOC_AGENT VLM-based extraction).
            # Skip row-based detection entirely — TOC pages have already been
            # physically stripped from the PDF, so no TOC rows exist in md_lines.
            logger.info(
                f"📌 Using pre-detected TOC hierarchies "
                f"({len(toc_hierarchies)} regions), "
                f"skipping detect_tocs_in_texts"
            )
        else:
            with stage_timer(
                "md.detect_toc", line_count=len(md_lines), model_name=toc_model_name
            ):
                toc_hierarchies, md_lines = detect_tocs_in_texts(
                    md_lines,
                    model_name=toc_model_name,
                    hierarchy_model_name=hierarchy_model_name,
                )

        # Save toc_hierarchies.json to output_dir (will be included in final zip package)
        if toc_hierarchies:
            toc_json_path = os.path.join(output_dir, "toc_hierarchies.json")
            with open(toc_json_path, "w", encoding="utf-8") as f:
                json.dump(toc_hierarchies, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved TOC hierarchies to {toc_json_path}")

        # Find layout.json path
        layout_json_path = os.path.join(output_dir, "layout.json")
        if not os.path.exists(layout_json_path):
            layout_json_path = None
            logger.debug("layout.json not found, META features will not be added")

        # estimate hierarchies with toc_hierarchies context
        with stage_timer(
            "md.predict_headings",
            line_count=len(md_lines),
            smart_parse=base_llm_paras["smart_title_parse"],
            model_name=hierarchy_model_name,
        ):
            lines_with_heading = eval_md_headings(
                md_lines,
                source_type,
                toc_hierarchies=toc_hierarchies,
                smart_parse=base_llm_paras["smart_title_parse"],
                model_name=hierarchy_model_name,
                output_dir=output_dir,
                layout_json_path=layout_json_path,
                is_first_shard=is_first_shard,
            )

    # ── Phase B: MarkdownParseState traversal ──
    # Clean old artifacts to prevent accumulation across debug runs.
    # In production each job uses a fresh workspace so rmtree never triggers.
    tb_dir = os.path.join(output_dir, "tables")
    if os.path.isdir(tb_dir):
        shutil.rmtree(tb_dir)
    os.makedirs(tb_dir, exist_ok=True)
    img_dir = os.path.join(output_dir, "images")
    if os.path.isdir(img_dir):
        # Only remove parse_md's own output (image-N-*) from previous runs
        for fname in os.listdir(img_dir):
            if re.match(r"^image-\d+", fname):
                os.remove(os.path.join(img_dir, fname))
    os.makedirs(img_dir, exist_ok=True)

    # initialize vars
    split_char = settings.SPLIT_CHAR or "/"
    parser_state = MarkdownParseState(
        relative_root=relative_root or "",
        split_char=split_char,
        llm_parameters=base_llm_paras,
        timestamp=get_str_time(),
        row_updater=update_df_list,
    )

    logger.debug("Parsing md data... total_lines={}", len(lines_with_heading))
    for i, line in enumerate(lines_with_heading):
        if parser_state.record_page_marker(line):
            continue

        last_context = find_surround_context(
            lines_with_heading, i
        )  # record the previous and next line which is not table/image
        current_heading, current_heading_level = md_heading_match(line, as_is=False)

        if (
            not current_heading_level == -1
        ):  # indicate a new path should be evaluated or added
            if parser_state.content_items:
                parser_state.flush_current_content()
            elif parser_state.path and parser_state.path != (relative_root or ""):
                # Consecutive headings with no body text between them:
                # Create a placeholder chunk so the previous heading's path
                parser_state.flush_placeholder_chunk()

            parser_state.enter_heading(current_heading, current_heading_level)

        else:  # no path change, remain in the same hierarchy
            # a. handle lines containing images (LLM deferred to post-loop parallel batch)
            # Always skip inline LLM — vision calls are deferred to parallel batch
            imgs = detect_summary_img_md(line, last_context, output_dir, mode=False)
            image_name = build_markdown_image_name(
                image_count=parser_state.image_count,
                last_context=last_context,
            )

            for img_path, _img_title, img_summary in imgs:
                image_asset = build_markdown_image_asset(
                    MarkdownImageAssetRequest(
                        output_dir=output_dir,
                        image_dir=img_dir,
                        image_path=img_path,
                        image_name=image_name,
                        image_count=parser_state.image_count,
                        last_context=last_context,
                        image_summary=img_summary,
                        timestamp=parser_state.timestamp,
                        seen_images=parser_state.seen_images,
                        summary_image=bool(base_llm_paras["summary_image"]),
                        row_index=len(parser_state.rows),
                    )
                )
                if (
                    image_asset.content_item is not None
                    and image_asset.row_values is not None
                ):
                    parser_state.append_content_item(image_asset.content_item)
                    parser_state.append_row(image_asset.row_values)
                if (
                    image_asset.cache_key is not None
                    and image_asset.cache_entry is not None
                ):
                    parser_state.seen_images[image_asset.cache_key] = (
                        image_asset.cache_entry
                    )
                if image_asset.deferred_task is not None:
                    parser_state.schedule_deferred_task(image_asset.deferred_task)
                if image_asset.should_advance_image_count:
                    parser_state.image_count += 1

            # TODO for large and dense tables, such as "Epstein flight logs",
            # integrate tabula-py as an independent extraction path to solve VLM hallucinations and misplacement
            # b. handle lines containing tables
            tb_bool, form, _ = identify_tables(line)
            if tb_bool:
                if form == "html":
                    # each line is a complete table - process immediately
                    tb_str = line
                elif form == "md":
                    # For MD tables, accumulate lines until table ends
                    parser_state.table_lines.append(line)
                    if i + 1 >= len(lines_with_heading):
                        tb_bool_next = False
                    else:
                        tb_bool_next, _, _ = identify_tables(
                            lines_with_heading[i + 1].strip()
                        )

                    if not tb_bool_next or i == len(lines_with_heading) - 1:
                        cleaned_table_lines, error_lines = clean_md_table_lines(
                            parser_state.table_lines, start_line_num=i
                        )
                        tb_str = "\n".join(cleaned_table_lines)
                        parser_state.error_line_numbers.extend(error_lines)
                        tb_str = extract_tables_by_forms(tb_str, form="md")
                    else:
                        continue  # Keep accumulating MD table lines
                else:
                    continue  # Unknown form, skip

                table_asset = build_markdown_table_asset(
                    MarkdownTableAssetRequest(
                        table_html=tb_str,
                        table_dir=tb_dir,
                        table_count=parser_state.table_count,
                        timestamp=parser_state.timestamp,
                        summary_table=bool(base_llm_paras["summary_table"]),
                        row_index=len(parser_state.rows),
                    )
                )
                parser_state.append_content_item(table_asset.content_item)
                parser_state.append_row(table_asset.row_values)
                if table_asset.deferred_task is not None:
                    parser_state.schedule_deferred_task(table_asset.deferred_task)
                parser_state.table_lines = []
                parser_state.table_count += 1

            # c. handle plain texts
            if len(imgs) == 0 and not tb_bool:
                parser_state.append_plain_text(line)

    if parser_state.content_items:
        parser_state.flush_current_content()

    # Collect text chunk deferred tasks (entries needing summary/keywords)
    summary_len = 1500
    parser_state.collect_text_summary_tasks(summary_len)
    apply_markdown_deferred_summaries(
        MarkdownDeferredSummaryInput(
            rows=parser_state.rows,
            tasks=parser_state.deferred_llm_tasks,
            output_dir=output_dir,
            summary_len=summary_len,
        )
    )

    with stage_timer("md.build_dataframe", row_count=len(parser_state.rows)):
        doc_df = parser_state.to_dataframe()

    return doc_df
