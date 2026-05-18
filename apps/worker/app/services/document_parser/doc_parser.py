# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false
import json
import os

import pandas as pd
from app.services.document_parser.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.docx_asset_accumulator import DocxAssetAccumulator
from app.services.document_parser.docx_asset_store import DocxAssetStore
from app.services.document_parser.docx_block_stream import iter_block_items
from app.services.document_parser.docx_table_html import table2html
from app.services.document_parser.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.inline_asset import (
    build_image_asset_row,
    build_table_asset_row,
)
from app.services.document_parser.parser_rows import ParsedRow, ParsedRowsBuilder
from app.services.document_parser.path_helpers import (
    find_matches_parsing,
    process_path_texts,
    remove_spaces,
)
from app.services.document_parser.heading_hierarchy import (
    HeadingHierarchyInput,
    predict_heading_hierarchy,
)
from app.services.document_parser.image_parser import (
    _get_vision_client,
    ask_image,
    perceptual_hash,
)
from app.services.document_parser.table_text_parser import sanitize_table_name_from_header
from app.services.document_parser.toc_docx import build_docx_toc_hierarchies
from app.services.document_parser.txt_parser import postprocess_leaf_dics
from docx.text.paragraph import Paragraph
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import DocxParsingException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.utils.chunk_refs import build_chunk_ref, has_chunk_ref
from shared.utils.file_loading import load_file_bytes
from shared.utils.file_utils import path_handle
from shared.utils.text_utils import tokenize2stw_remove


def get_leaf_dics(node, path=[]):
    """
    :function find all bottom-level knowledge pieces and flat them into a list, each element contains the path from root to bottom
    """
    leaf_dic_paths = []
    if isinstance(node, dict) and "content" in node:
        current_path = path + [node["heading"]] if "heading" in node else path
        if any(isinstance(item, dict) for item in node["content"]):
            for item in node["content"]:
                leaf_dic_paths.extend(get_leaf_dics(item, current_path))
        else:
            leaf_dic_paths.append((current_path, node))
    # if there is no 'content' key, it exists between higher-level and the lower-level sections
    else:
        iso_node = {"heading": path, "content": [node]}
        leaf_dic_paths.append((path, iso_node))
    return leaf_dic_paths


def _find_img_context(headings_stack, max_chars=100):
    """Find the nearest non-table/image text context by looking backward in headings_stack.

    Args:
        headings_stack: Stack of heading dicts with 'content' lists
        max_chars: Maximum characters to return (truncated if exceeded)

    Returns:
        The nearest valid text context, or empty string if none found
    """
    from app.services.document_parser.text_helpers import truncate_text

    try:
        content_list = headings_stack[-1].get("content", [])
        # Traverse backward to find non-table/image content
        for item in reversed(content_list):
            item_stripped = str(item).strip()
            # Skip image/table reference blocks when looking for textual context.
            if has_chunk_ref(item_stripped):
                continue
            # Found valid text
            if item_stripped:
                return truncate_text(item_stripped, max_chars, 0)
        return ""
    except Exception as e:
        raise DocxParsingException(
            user_message="Failed to process document content",
            reason="CONTENT_PROCESSING_FAILED",
            internal_message=str(e),
            original_exception=e,
        )


def handle_image(
    df_list,
    img_file,
    asset_store,
    headings_stack,
    current_heading,
    img_count,
    smart_summary=False,
    seen_images=None,
):
    time_stamp = get_str_time()

    # Document-level dedup: use perceptual hash to catch visually-identical
    # images that differ only in compression/metadata
    img_hash = perceptual_hash(img_file["data"])
    if seen_images is not None and img_hash in seen_images:
        cached = seen_images[img_hash]
        headings_stack[-1]["content"].append(cached["image_ref"])
        df_list.append(
            build_image_asset_row(
                content=cached["image_ref"],
                relative_path=cached["img_path"],
                summary=cached["img_summary_field"],
                know_id=cached["temp_uid"],
                addtime=time_stamp,
            ).to_list()
        )
        logger.debug(f"Skipped duplicate image (hash={img_hash[:12]}...)")
        return headings_stack, df_list, False  # False = cache hit, don't increment

    client = _get_vision_client()
    last_context = _find_img_context(headings_stack)

    # Image index (always present)
    image_index = f"image-{img_count + 1}"

    img_ext = os.path.splitext(img_file["image_name"])[-1]
    raw_img_name = process_path_texts(
        f"image-{str(img_count + 1)} {current_heading} {last_context}", last=30
    )
    raw_image_asset = asset_store.write_image(raw_img_name, img_ext, img_file["data"])

    # LLM title + summary (optional, with fallback to last_context)
    llm_title = None
    llm_summary = None
    if smart_summary:
        from app.services.document_parser.txt_parser import split_title_summary

        # TODO: Risk of missing text content if the image is a screenshot of pure text.
        # Consider adding judge-image-type and OCR fallback as done in image_parser.parse_image.
        llm_resp = ask_image(
            client,
            asset_store.image_dir,
            [f"{raw_img_name}{img_ext}"],
            title_text=last_context,
        )
        if llm_resp:
            llm_title, llm_summary = split_title_summary(llm_resp)

    # Fallback: LLM summary -> last_context -> None
    img_summary = llm_summary or last_context or None
    # Fallback: LLM title -> last_context -> None
    img_title = llm_title or last_context or None

    # Use LLM title alone for clean naming; fallback to heading+context
    if llm_title:
        img_name = process_path_texts(
            f"image-{str(img_count + 1)} {llm_title}", last=30
        )
    else:
        img_name = process_path_texts(
            f"image-{str(img_count + 1)} {current_heading} {img_title or ''}", last=30
        )
    image_asset = asset_store.rename_image(raw_image_asset, img_name)

    temp_uid = gen_str_codes(img_hash)

    # Build img_summary_field for df_list: image-n + optional summary
    if img_summary:
        img_summary_field = f"{image_index}\n{img_summary}"
    else:
        img_summary_field = image_index

    img_ref = build_chunk_ref(image_asset.relative_path)

    # Build image_ref for heading_stack: optional summary + image path ref
    if img_summary:
        image_ref = f"\n{img_summary}\n{img_ref}\n"
    else:
        image_ref = f"\n{img_ref}\n"

    headings_stack[-1]["content"].append(image_ref)
    df_list.append(
        build_image_asset_row(
            content=image_ref,
            relative_path=image_asset.relative_path,
            summary=img_summary_field,
            know_id=temp_uid,
            addtime=time_stamp,
        ).to_list()
    )

    # Cache result for document-level dedup
    if seen_images is not None:
        seen_images[img_hash] = {
            "img_path": image_asset.relative_path,
            "image_ref": image_ref,
            "img_summary_field": img_summary_field,
            "temp_uid": temp_uid,
        }

    return headings_stack, df_list, True  # True = new image processed


def _first_cols_rows(table_block, max_items=10, max_chars=20):
    """Extract deduplicated first row and first column texts from a table block.

    Args:
        table_block: python-docx Table object
        max_items: Maximum number of items to extract (default 10)
        max_chars: Maximum characters per item (default 20)

    Returns:
        Tuple of (first_row_text, first_col_text) with ' | ' as separator
    """
    from app.services.document_parser.text_helpers import truncate_text

    first_row_text = ""
    first_col_text = ""

    if not table_block.rows:
        return first_row_text, first_col_text

    # First row extraction (deduplicated, order preserved, max items, truncated)
    seen_row = set()
    unique_row_cells = []
    for cell in table_block.rows[0].cells:
        if len(unique_row_cells) >= max_items:
            break
        cell_text = cell.text.strip()
        if cell_text and cell_text not in seen_row:
            seen_row.add(cell_text)
            unique_row_cells.append(truncate_text(cell_text, max_chars, 0))
    first_row_text = " | ".join(unique_row_cells) if unique_row_cells else ""

    # First column extraction (deduplicated, order preserved, max items, truncated)
    seen_col = set()
    unique_col_cells = []
    for row in table_block.rows:
        if len(unique_col_cells) >= max_items:
            break
        if row.cells:
            cell_text = row.cells[0].text.strip()
            if cell_text and cell_text not in seen_col:
                seen_col.add(cell_text)
                unique_col_cells.append(truncate_text(cell_text, max_chars, 0))
    first_col_text = " | ".join(unique_col_cells) if unique_col_cells else ""

    return first_row_text, first_col_text


def handle_table(
    df_list,
    block,
    asset_store,
    headings_stack,
    current_heading,
    table_count,
    summary_table=False,
    summary_image=False,
    cell_images=None,
    img_count=0,
    seen_images=None,
):
    time_stamp = get_str_time()

    # Process cell images: save to disk + optional LLM summary
    cell_image_map = {}  # {(row, col): "description text"} for table2html
    table_img_entries = []  # df_list entries for images

    if cell_images:
        for (row_idx, col_idx), images in cell_images.items():
            descriptions = []
            for img_data in images:
                # Document-level dedup: perceptual hash for visual duplicates
                cell_img_hash = perceptual_hash(img_data["data"])
                if seen_images is not None and cell_img_hash in seen_images:
                    cached = seen_images[cell_img_hash]
                    descriptions.append(f"[{cached['img_summary_field']}]")
                    table_img_entries.append(
                        [
                            cached["image_ref"],
                            cached["img_path"],
                            "image",
                            len(cached["image_ref"]),
                            "",
                            cached["img_summary_field"],
                            cached["temp_uid"],
                            "",
                            "",
                            time_stamp,
                            "",
                        ]
                    )
                    logger.debug(
                        f"Skipped duplicate table cell image (hash={cell_img_hash[:12]}...)"
                    )
                    continue

                img_count += 1
                img_ext = os.path.splitext(img_data["image_name"])[-1]
                image_index = f"image-{img_count}"

                # Save image to disk
                img_name = process_path_texts(
                    f"table-{table_count + 1}-{image_index} {current_heading}", last=30
                )
                image_asset = asset_store.write_image(
                    img_name, img_ext, img_data["data"]
                )

                # LLM summary (optional)
                img_summary = None
                if summary_image:
                    try:
                        client = _get_vision_client()
                        img_summary = ask_image(
                            client,
                            asset_store.image_dir,
                            [f"{img_name}{img_ext}"],
                            title_text=current_heading,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to summarize table image: {e}")

                effective_desc = img_summary or image_index
                descriptions.append(f"[{effective_desc}]")

                # Also add as IMAGE entry in df_list for indexing
                temp_uid = gen_str_codes(cell_img_hash)
                img_summary_field = (
                    f"{image_index}\n{img_summary}" if img_summary else image_index
                )
                img_ref = build_chunk_ref(image_asset.relative_path)
                if img_summary:
                    image_ref = f"\n{img_summary}\n{img_ref}\n"
                else:
                    image_ref = f"\n{img_ref}\n"
                table_img_entries.append(
                    build_image_asset_row(
                        content=image_ref,
                        relative_path=image_asset.relative_path,
                        summary=img_summary_field,
                        know_id=temp_uid,
                        addtime=time_stamp,
                    ).to_list()
                )

                # Cache result for document-level dedup
                if seen_images is not None:
                    seen_images[cell_img_hash] = {
                        "img_path": image_asset.relative_path,
                        "image_ref": image_ref,
                        "img_summary_field": img_summary_field,
                        "temp_uid": temp_uid,
                    }

            cell_image_map[(row_idx, col_idx)] = " ".join(descriptions)

        logger.info(
            f"Extracted {sum(len(v) for v in cell_images.values())} images from table-{table_count + 1} cells"
        )

    # Generate HTML with image descriptions embedded
    tb_html_str = table2html(
        block, cell_image_map=cell_image_map if cell_image_map else None
    )
    if not tb_html_str.strip():
        return headings_stack, df_list, img_count

    # Add table image entries to df_list
    df_list.extend(table_img_entries)

    # Extract first row and first column headers (used ONLY for fallback file naming)
    first_row_text, first_col_text = _first_cols_rows(block)
    raw_tb_name = (
        sanitize_table_name_from_header(first_row_text) if first_row_text else ""
    )

    # Table index (always present)
    table_index = f"table-{table_count + 1}"

    # LLM title + keywords + summary (only when summary_table is enabled)
    llm_title = None
    llm_summary = None
    tb_keywords = ""
    if summary_table:
        from app.services.document_parser.txt_parser import (
            extract_title_keywords_summary,
        )

        llm_title, tb_keywords, llm_summary = extract_title_keywords_summary(
            tb_html_str, max_keywords=3
        )

    # Build tb_summary for df_list: table-n + optional LLM summary
    if llm_summary:
        tb_summary = f"{table_index}\n{llm_summary}"
    else:
        tb_summary = table_index

    temp_uid = gen_str_codes((tb_html_str + str(table_count)))

    # Use LLM title for filename when available, fallback to raw_tb_name
    effective_name = llm_title if llm_title else raw_tb_name
    tb_name = path_handle(
        f"table-{str(table_count + 1)} {effective_name}", mode="clean_single"
    )
    table_asset = asset_store.write_table(tb_name, tb_html_str)
    tb_ref = build_chunk_ref(table_asset.relative_path)
    # Build table_ref for heading_stack: optional LLM summary + table path ref
    if llm_summary:
        table_ref = f"\n{llm_summary}\n{tb_ref}\n"
    else:
        table_ref = f"\n{tb_ref}\n"
    headings_stack[-1]["content"].append(table_ref)
    df_list.append(
        build_table_asset_row(
            content=tb_html_str,
            relative_path=table_asset.relative_path,
            summary=tb_summary,
            keywords=tb_keywords,
            know_id=temp_uid,
            addtime=time_stamp,
        ).to_list()
    )
    return headings_stack, df_list, img_count


def parse_docx(
    docx_path,
    llm_paras,
    output_dir=None,
    filename="",
    file_url="",
    relative_root=None,
):
    doc_data = load_file_bytes(docx_path, file_url=file_url)

    doc_structure = []
    heading_data = pd.DataFrame(columns=["text", "level"])
    headings_stack = [{"level": -1, "content": doc_structure}]
    current_heading = ""

    asset_store = DocxAssetStore(output_dir)
    asset_store.reset()

    block_tuples = list(iter_block_items(doc_data))
    # Record first TOC block position before filtering, for pre-TOC exclusion in pred_titles
    toc_blocks = [b for b in block_tuples if "TOC" in b[2]]
    first_toc_ele_num = toc_blocks[0][0] if toc_blocks else None
    toc_hierarchies = build_docx_toc_hierarchies(block_tuples)
    if toc_hierarchies and output_dir:
        toc_json_path = os.path.join(output_dir, "toc_hierarchies.json")
        with open(toc_json_path, "w", encoding="utf-8") as f:
            json.dump(toc_hierarchies, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved DOCX TOC hierarchies to {toc_json_path}")
    block_tuples = [b for b in block_tuples if "TOC" not in b[2]]  # remove toc area

    heading_infos = []
    for block_tuple in block_tuples:
        block = block_tuple[1]
        if isinstance(block, Paragraph):
            title_text = block.text.strip()
            if len(title_text) > 0:
                heading_infos.append((block_tuple[0], block, title_text))

    heading_candidates = []
    outline_dic = {-1: -1}
    smart_title_parse = llm_paras["smart_title_parse"]
    if llm_paras["doc_type"] not in "templates":
        model_name = (
            (
                llm_paras.get("hierarchy_model_name")
                or llm_paras.get("model_name", settings.NORMOL_MODEL)
            )
            if llm_paras
            else (settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL)
        )
        heading_candidates = predict_heading_hierarchy(
            HeadingHierarchyInput(
                infos=heading_infos,
                doc_type="docx",
                toc_hierarchies=toc_hierarchies or None,
                enable_regex=True,
                smart_parse=smart_title_parse,
                model_name=model_name,
                output_dir=output_dir,
                first_toc_ele_num=first_toc_ele_num,
            )
        )

    if len(heading_candidates) > 0 and not (heading_candidates["level"] == -1).all():
        assert heading_candidates["id"].is_unique
        outline_dic = dict(zip(heading_candidates["id"], heading_candidates["level"]))
    else:
        text = filename.split(".")[0]
        outline_level = 1
        heading_data.loc[len(heading_data)] = [text, outline_level]
        current_heading = text
        new_content = {"heading": text, "content": [], "level": outline_level}
        headings_stack[-1]["content"].append(new_content)
        headings_stack.append(new_content)
        logger.debug("⚠️no headings detected, using file name or mine a heading=>", text)

    asset_accumulator = DocxAssetAccumulator(
        asset_store=asset_store,
        should_summary_image=llm_paras["summary_image"],
        should_summary_table=llm_paras["summary_table"],
        image_handler=handle_image,
        table_handler=handle_table,
    )

    logger.debug("Parsing docx file... total_blocks={}", len(block_tuples))
    for block_tuple in block_tuples:
        ele_num, block, label, meta = block_tuple
        last_heading_before_block = current_heading

        if label == "PTXT":  # block could be doc para or plain string
            text = getattr(block, "text", str(block)).strip()
            if not text:
                continue

            outline_level = outline_dic.get(ele_num, -1)
            if outline_level > 0:
                # logger.debug('Found a title: ', text, ' current level: ', outline_level)
                try:
                    last_heading = headings_stack[-1]["heading"]
                    if last_heading == text:
                        continue
                except Exception:
                    pass

                while headings_stack and headings_stack[-1]["level"] >= outline_level:
                    headings_stack.pop()

                current_heading = text
                new_content = {"heading": text, "content": [], "level": outline_level}
                headings_stack[-1]["content"].append(new_content)
                headings_stack.append(new_content)
            # plain texts
            else:
                text = remove_spaces(text)
                headings_stack[-1]["content"].append(text)

        elif label == "IMAGE":
            if meta and meta.get("size", 0) < 10 * 1024:
                continue

            headings_stack = asset_accumulator.append_image(
                meta,
                headings_stack,
                current_heading,
            )
            current_heading = last_heading_before_block

        elif label == "TABLE":
            # TODO: handle cross-page tables
            headings_stack = asset_accumulator.append_table(
                block,
                headings_stack,
                current_heading,
                cell_images=meta,
            )
            current_heading = last_heading_before_block

        else:  # TODO: handle latex, etc.
            pass

    return {"content": doc_structure}, asset_accumulator.rows()


def convert_doc2dics(
    parsed_structure, df_list, output_dir, base_llm_paras, relative_root=None
):
    split_char = settings.SPLIT_CHAR or "/"
    leaf_dics = get_leaf_dics(parsed_structure)
    leaf_dics = postprocess_leaf_dics(leaf_dics, base_llm_paras)

    # Use relative_root for path construction instead of absolute output_dir
    doc_name = relative_root if relative_root else output_dir.split(os.sep)[-1]
    if len(leaf_dics) == 0:
        raise DocxParsingException(
            user_message="Document content could not be extracted",
            reason="EMPTY_CONTENT",
            internal_message="Parsed leaf_dics is empty after processing",
        )

    path_keys = []
    time_stamp = get_str_time()
    path_counter = {}  # Track path occurrences for deduplication

    for _, row in leaf_dics.iterrows():
        key = row["path_identifier"]

        # Skip leaf nodes with no actual content (empty heading-only sections)
        content_lst = row["content_lst"]
        joined = "\n".join(content_lst).strip()
        if not joined:
            logger.debug(f"Skipping empty leaf node: {key}")
            continue

        # Build tentative path to check for duplicates
        tentative_path = doc_name + split_char + key

        # Deduplicate: if path already exists, add suffix
        if tentative_path in path_counter:
            path_counter[tentative_path] += 1
            suffix = path_counter[tentative_path]
            key = f"{key}_{suffix}"  # Modify key with suffix
        else:
            path_counter[tentative_path] = 1

        path_keys.append((doc_name + split_char + key))
        bottom_content = joined
        bottom_tokens = tokenize2stw_remove(
            [bottom_content], base_llm_paras["stopwords"]
        )
        match_type = find_matches_parsing(bottom_content, key)

        try:
            keywords = row["keywords"]
            summary = row["local_summary"]
            # Deterministic know_id: filter out IMAGE/TABLE ref items, hash pure text only
            text_items = [
                item for item in row["content_lst"] if not has_chunk_ref(str(item))
            ]
            pure_text = "\n".join(text_items).strip()
            know_id = gen_str_codes(pure_text)
            # Use relative_root for path instead of absolute kb_dir
            path_suffix = key if key.strip() else ""
            know_path = (
                split_char.join([relative_root, path_suffix])
                if relative_root and path_suffix
                else (relative_root or path_suffix)
            )
            df_list.append(
                ParsedRow(
                    content=bottom_content,
                    path=know_path,
                    type=match_type,
                    keywords=keywords,
                    summary=summary,
                    know_id=know_id,
                    tokens=bottom_tokens,
                    addtime=time_stamp,
                ).to_list()
            )
        except KnowhereException:
            raise
        except Exception as e:
            logger.debug(f"❌Failed to parse docx document: {e}")
            raise DocxParsingException(
                user_message="Failed to process document content",
                reason="CONTENT_PROCESSING_FAILED",
                internal_message=str(e),
                original_exception=e,
            )

    rows_builder = ParsedRowsBuilder()
    for row_values in df_list:
        rows_builder.append(
            ParsedRow(
                content=str(row_values[0]),
                path=str(row_values[1]),
                type=str(row_values[2]),
                length=int(row_values[3]),
                keywords=str(row_values[4]),
                summary=str(row_values[5]),
                know_id=str(row_values[6]),
                tokens=str(row_values[7]),
                connectto=str(row_values[8]),
                addtime=str(row_values[9]),
                page_nums=str(row_values[10]),
            )
        )
    doc_df = rows_builder.to_dataframe()
    doc_df = process_dup_paths_df(doc_df)
    return doc_df
