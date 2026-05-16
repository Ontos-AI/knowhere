# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false
import io
import json
import os
import shutil
import zipfile

import pandas as pd
from app.services.document_parser.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.path_helpers import (
    find_matches_parsing,
    process_path_texts,
    remove_spaces,
)
from app.services.document_parser.html_parser import table2html
from app.services.document_parser.image_parser import (
    _get_vision_client,
    ask_image,
    perceptual_hash,
)
from app.services.document_parser.layout_parser import pred_titles
from app.services.document_parser.table_parser import sanitize_table_name_from_header
from app.services.document_parser.toc_parser import (
    build_docx_toc_hierarchies,
    detect_doc_tocs,
    detect_sdt_toc,
)
from app.services.document_parser.txt_parser import postprocess_leaf_dics
from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from loguru import logger
from lxml import etree

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import DocxParsingException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.utils.chunk_refs import build_chunk_ref, has_chunk_ref
from shared.utils.CommonHelperSync import load_file_bytes
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
    img_dir,
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
    img_raw_path = os.path.join(img_dir, f"{raw_img_name}{img_ext}")

    with open(img_raw_path, "wb") as image_file:
        image_file.write(img_file["data"])

    # LLM title + summary (optional, with fallback to last_context)
    llm_title = None
    llm_summary = None
    if smart_summary:
        from app.services.document_parser.txt_parser import split_title_summary

        # TODO: Risk of missing text content if the image is a screenshot of pure text.
        # Consider adding judge-image-type and OCR fallback as done in image_parser.parse_image.
        llm_resp = ask_image(
            client, img_dir, [f"{raw_img_name}{img_ext}"], title_text=last_context
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
    img_path = os.path.join(img_dir, f"{img_name}{img_ext}")
    os.rename(img_raw_path, img_path)  # if summary fails, renaming is not applied

    temp_uid = gen_str_codes(img_hash)

    # Build img_summary_field for df_list: image-n + optional summary
    if img_summary:
        img_summary_field = f"{image_index}\n{img_summary}"
    else:
        img_summary_field = image_index

    img_path = f"images/{img_name}{img_ext}"
    img_ref = build_chunk_ref(img_path)

    # Build image_ref for heading_stack: optional summary + image path ref
    if img_summary:
        image_ref = f"\n{img_summary}\n{img_ref}\n"
    else:
        image_ref = f"\n{img_ref}\n"

    headings_stack[-1]["content"].append(image_ref)
    df_list.append(
        [
            image_ref,
            img_path,
            "image",
            len(image_ref),
            "",
            img_summary_field,
            temp_uid,
            "",
            "",
            time_stamp,
            "",
        ]
    )

    # Cache result for document-level dedup
    if seen_images is not None:
        seen_images[img_hash] = {
            "img_path": img_path,
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
    tb_dir,
    headings_stack,
    current_heading,
    table_count,
    summary_table=False,
    summary_image=False,
    cell_images=None,
    img_dir=None,
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
                img_save_path = os.path.join(img_dir, f"{img_name}{img_ext}")
                with open(img_save_path, "wb") as f:
                    f.write(img_data["data"])

                # LLM summary (optional)
                img_summary = None
                if summary_image:
                    try:
                        client = _get_vision_client()
                        img_summary = ask_image(
                            client,
                            img_dir,
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
                relative_img_path = f"images/{img_name}{img_ext}"
                img_ref = build_chunk_ref(relative_img_path)
                if img_summary:
                    image_ref = f"\n{img_summary}\n{img_ref}\n"
                else:
                    image_ref = f"\n{img_ref}\n"
                table_img_entries.append(
                    [
                        image_ref,
                        relative_img_path,
                        "image",
                        len(image_ref),
                        "",
                        img_summary_field,
                        temp_uid,
                        "",
                        "",
                        time_stamp,
                        "",
                    ]
                )

                # Cache result for document-level dedup
                if seen_images is not None:
                    seen_images[cell_img_hash] = {
                        "img_path": relative_img_path,
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
    tb_path = os.path.join(tb_dir, f"{tb_name}.html")

    with open(tb_path, "w", encoding="utf-8") as f:
        f.write(tb_html_str)

    # Use relative path for tables (avoid absolute path in path column)
    tb_path = f"tables/{tb_name}.html"
    tb_ref = build_chunk_ref(tb_path)
    # Build table_ref for heading_stack: optional LLM summary + table path ref
    if llm_summary:
        table_ref = f"\n{llm_summary}\n{tb_ref}\n"
    else:
        table_ref = f"\n{tb_ref}\n"
    headings_stack[-1]["content"].append(table_ref)
    df_list.append(
        [
            tb_html_str,
            tb_path,
            "table",
            len(tb_html_str),
            tb_keywords,
            tb_summary,
            temp_uid,
            "",
            "",
            time_stamp,
            "",
        ]
    )
    return headings_stack, df_list, img_count


def iter_block_items(doc_data):
    doc_stream = io.BytesIO(doc_data)
    doc = Document(doc_stream)

    # python-docx mapping
    p_tbl_map = []
    for child in doc.element.body:
        if isinstance(child, CT_P):
            p_tbl_map.append(("p", child))
        elif isinstance(child, CT_Tbl):
            p_tbl_map.append(("tbl", child))

    with zipfile.ZipFile(io.BytesIO(doc_data), "r") as docx:
        xml = docx.read("word/document.xml")
        rels = etree.fromstring(docx.read("word/_rels/document.xml.rels"))
        rel_map = {
            r.get("Id"): r.get("Target") for r in rels.findall(".//{*}Relationship")
        }
        ns = {
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "v": "urn:schemas-microsoft-com:vml",
            "o": "urn:schemas-microsoft-com:office:office",
        }
        r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

        root = etree.fromstring(xml)
        body = root.find(".//w:body", namespaces=ns)

        ele_num = 1
        map_index = 0  # point to p_tbl_map
        toc_field_active = False

        for elem in body.iterchildren():
            if not isinstance(elem.tag, str):
                continue

            tag = etree.QName(elem.tag).localname

            # --- SDT (Structured Document Tag) container ---
            # TOC generated by MS Word is usually in sdt
            if tag == "sdt":
                sdt_toc_info = detect_sdt_toc(elem, ns)
                is_toc_sdt = sdt_toc_info["is_toc_sdt"]

                sdt_content = elem.find(".//w:sdtContent", namespaces=ns)
                if sdt_content is not None:
                    for p_elem in sdt_content.findall(".//w:p", namespaces=ns):
                        texts = p_elem.xpath(".//w:t/text()", namespaces=ns)
                        text = "".join(texts).strip()

                        if is_toc_sdt:
                            label = "TOC-AREA"
                            toc_info = detect_doc_tocs(p_elem, ns)
                        else:
                            toc_info = detect_doc_tocs(p_elem, ns)
                            if toc_info["is_style"] or toc_info["is_field_start"]:
                                label = "TOC-AREA"
                            else:
                                label = "PTXT"

                        if text:
                            meta = None
                            if "TOC" in label:
                                meta = {
                                    "toc_level": toc_info.get("toc_level"),
                                    "toc_outline_level": toc_info.get("outline_level"),
                                    "toc_left_indent": toc_info.get("left_indent"),
                                    "toc_style_name": toc_info.get("style_name"),
                                    "toc_source": "sdt",
                                }
                            yield ele_num, text, label, meta
                            ele_num += 1
                continue

            # --- text paras ---
            if tag == "p":
                texts = elem.xpath(".//w:t/text()", namespaces=ns)
                text = "".join(texts).strip()

                if map_index < len(p_tbl_map) and p_tbl_map[map_index][0] == "p":
                    p_obj = Paragraph(p_tbl_map[map_index][1], doc)
                else:
                    p_obj = None

                toc_info = detect_doc_tocs(elem, ns)
                if toc_info["is_field_start"]:
                    toc_field_active = True

                if toc_info["is_style"] or toc_field_active:
                    label = "TOC-AREA"
                else:
                    label = "PTXT"

                if text or p_obj is not None:
                    meta = None
                    if "TOC" in label:
                        meta = {
                            "toc_level": toc_info.get("toc_level"),
                            "toc_outline_level": toc_info.get("outline_level"),
                            "toc_left_indent": toc_info.get("left_indent"),
                            "toc_style_name": toc_info.get("style_name"),
                            "toc_source": "paragraph",
                        }
                    yield ele_num, p_obj or text, label, meta
                    ele_num += 1

                # images (DrawingML: <a:blip>)
                seen_rids = set()
                blips = elem.xpath(".//a:blip", namespaces=ns)
                for b in blips:
                    rid = b.get(f"{r_ns}embed")
                    if not rid or rid in seen_rids:
                        continue
                    target = rel_map.get(rid)
                    if not target or not target.startswith("media/"):
                        continue
                    seen_rids.add(rid)
                    data = docx.read("word/" + target)
                    yield (
                        ele_num,
                        None,
                        "IMAGE",
                        {
                            "image_name": target.split("/")[-1],
                            "from": "paragraph",
                            "size": len(data),
                            "data": data,
                        },
                    )
                    ele_num += 1

                # TODO: Re-evaluate VML group extraction strategy. 
                # Complex VML composite images (<v:group>) are currently skipped because extracting 
                # <v:imagedata> piece-by-piece loses textual overlay and positioning. 
                # Future plan: Use LibreOffice headless conversion to render the entire document 
                # and map the perfectly rendered images back to the layout via text anchors.
                #
                # Temporary: detect VML-only paragraphs and inject a placeholder so the
                # paragraph isn't silently swallowed, leaving its parent section empty.
                if not text and not seen_rids:
                    # No text and no DrawingML images — check for VML content
                    vml_groups = elem.xpath(".//v:group", namespaces=ns)
                    vml_images_check = elem.xpath(".//v:imagedata", namespaces=ns)
                    if vml_groups or vml_images_check:
                        vml_placeholder = "[VML graphic \u2014 extraction not yet supported]"
                        yield ele_num, vml_placeholder, "PTXT", None
                        ele_num += 1
                        logger.debug(
                            f"Injected VML placeholder for paragraph with "
                            f"{len(vml_groups)} v:group, {len(vml_images_check)} v:imagedata"
                        )
                """
                # images (VML: <v:imagedata>) — convert to PNG
                from PIL import Image as PILImage

                vml_images = elem.xpath(".//v:imagedata", namespaces=ns)
                for v in vml_images:
                    rid = v.get(f"{r_ns}id")
                    if not rid or rid in seen_rids:
                        continue
                    target = rel_map.get(rid)
                    if not target or not target.startswith("media/"):
                        continue
                    seen_rids.add(rid)
                    raw_data = docx.read("word/" + target)
                    # Convert to PNG for uniform downstream handling
                    try:
                        pil_img = PILImage.open(io.BytesIO(raw_data))
                        png_buf = io.BytesIO()
                        pil_img.save(png_buf, format="PNG")
                        png_data = png_buf.getvalue()
                    except Exception as e:
                        logger.warning(f"Failed to convert VML image to PNG: {e}")
                        continue
                    orig_name = target.split("/")[-1]
                    png_name = os.path.splitext(orig_name)[0] + ".png"
                    yield (
                        ele_num,
                        None,
                        "IMAGE",
                        {
                            "image_name": png_name,
                            "from": "paragraph_vml",
                            "size": len(png_data),
                            "data": png_data,
                        },
                    )
                    ele_num += 1
                """
                map_index += 1

                if toc_info["is_field_end"]:
                    toc_field_active = False

            # --- tables ---
            elif tag == "tbl":
                if map_index < len(p_tbl_map) and p_tbl_map[map_index][0] == "tbl":
                    tbl = Table(p_tbl_map[map_index][1], doc)
                else:
                    tbl = Table(elem, doc)

                # Extract images from each cell, keyed by (row_idx, col_idx)
                cell_images = {}  # {(row_idx, col_idx): [{'image_name', 'data', 'size'}]}
                for row_idx, tr in enumerate(elem.findall(".//w:tr", namespaces=ns)):
                    for col_idx, tc in enumerate(tr.findall(".//w:tc", namespaces=ns)):
                        cell_seen_rids = set()
                        imgs_in_cell = []
                        # DrawingML images in cell
                        blips = tc.xpath(".//a:blip", namespaces=ns)
                        for b in blips:
                            rid = b.get(f"{r_ns}embed")
                            if not rid or rid in cell_seen_rids:
                                continue
                            target = rel_map.get(rid)
                            if not target or not target.startswith("media/"):
                                continue
                            cell_seen_rids.add(rid)
                            data = docx.read("word/" + target)
                            if (
                                len(data) < 10 * 1024
                            ):  # Skip small images (<10KB, likely icons)
                                continue
                            imgs_in_cell.append(
                                {
                                    "image_name": target.split("/")[-1],
                                    "data": data,
                                    "size": len(data),
                                }
                            )
                        # TODO: VML in tables is temporarily skipped to avoid extracting 
                        # fragmented textless background images. (Same as paragraph VML logic)
                        """
                        # VML images in cell — convert to PNG
                        from PIL import Image as PILImage

                        vml_in_cell = tc.xpath(".//v:imagedata", namespaces=ns)
                        for v in vml_in_cell:
                            rid = v.get(f"{r_ns}id")
                            if not rid or rid in cell_seen_rids:
                                continue
                            target = rel_map.get(rid)
                            if not target or not target.startswith("media/"):
                                continue
                            cell_seen_rids.add(rid)
                            raw_data = docx.read("word/" + target)
                            try:
                                pil_img = PILImage.open(io.BytesIO(raw_data))
                                png_buf = io.BytesIO()
                                pil_img.save(png_buf, format="PNG")
                                png_data = png_buf.getvalue()
                            except Exception as e:
                                logger.warning(
                                    f"Failed to convert VML cell image to PNG: {e}"
                                )
                                continue
                            if len(png_data) < 10 * 1024:
                                continue
                            orig_name = target.split("/")[-1]
                            png_name = os.path.splitext(orig_name)[0] + ".png"
                            imgs_in_cell.append(
                                {
                                    "image_name": png_name,
                                    "data": png_data,
                                    "size": len(png_data),
                                }
                            )
                        """
                        if imgs_in_cell:
                            cell_images[(row_idx, col_idx)] = imgs_in_cell

                yield ele_num, tbl, "TABLE", cell_images if cell_images else None
                ele_num += 1
                map_index += 1
            else:
                continue

        # --- handle p_tbl_map at the end ---
        while map_index < len(p_tbl_map):
            tag, node = p_tbl_map[map_index]
            if tag == "p":
                toc_info = detect_doc_tocs(node, ns)
                label = "TOC-AREA" if toc_info["is_style"] else "PTXT"
                meta = None
                if "TOC" in label:
                    meta = {
                        "toc_level": toc_info.get("toc_level"),
                        "toc_outline_level": toc_info.get("outline_level"),
                        "toc_left_indent": toc_info.get("left_indent"),
                        "toc_style_name": toc_info.get("style_name"),
                        "toc_source": "tail-map",
                    }
                yield ele_num, Paragraph(node, doc), label, meta
            elif tag == "tbl":
                yield ele_num, Table(node, doc), "TABLE", None
            ele_num += 1
            map_index += 1


def parse_docx(
    docx_path,
    llm_paras,
    output_dir=None,
    filename="",
    file_url="",
    start_text="",
    end_text="",
    relative_root=None,
):
    doc_data = load_file_bytes(docx_path, file_url=file_url)

    doc_structure = []
    heading_data = pd.DataFrame(columns=["text", "level"])
    headings_stack = [{"level": -1, "content": doc_structure}]
    current_heading = ""

    # Clean old artifacts to prevent accumulation across debug runs.
    # In production each job uses a fresh workspace so rmtree never triggers.
    tb_dir = os.path.join(output_dir, "tables")
    if os.path.isdir(tb_dir):
        shutil.rmtree(tb_dir)
    os.makedirs(tb_dir, exist_ok=True)
    img_dir = os.path.join(output_dir, "images")
    if os.path.isdir(img_dir):
        shutil.rmtree(img_dir)
    os.makedirs(img_dir, exist_ok=True)

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
        heading_candidates = pred_titles(
            heading_infos,
            doc_type="docx",
            toc_hierarchies=toc_hierarchies or None,
            enable_regx=True,
            smart_parse=smart_title_parse,
            model_name=model_name,
            output_dir=output_dir,
            first_toc_ele_num=first_toc_ele_num,
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

    df_list = []
    table_count = 0
    image_count = 0
    _seen_images: dict[str, dict] = {}  # sha256_hex -> cached image info for dedup

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

            headings_stack, df_list, is_new = handle_image(
                df_list,
                meta,
                img_dir,
                headings_stack,
                current_heading,
                image_count,
                llm_paras["summary_image"],
                seen_images=_seen_images,
            )
            if is_new:
                image_count += 1
            current_heading = last_heading_before_block

        elif label == "TABLE":
            # TODO: handle cross-page tables
            headings_stack, df_list, image_count = handle_table(
                df_list,
                block,
                tb_dir,
                headings_stack,
                current_heading,
                table_count,
                summary_table=llm_paras["summary_table"],
                summary_image=llm_paras["summary_image"],
                cell_images=meta,
                img_dir=img_dir,
                img_count=image_count,
                seen_images=_seen_images,
            )
            table_count += 1
            current_heading = last_heading_before_block

        else:  # TODO: handle latex, etc.
            pass

    return {"content": doc_structure}, df_list


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
                [
                    bottom_content,
                    know_path,
                    match_type,
                    len(bottom_content),
                    keywords,
                    summary,
                    know_id,
                    bottom_tokens,
                    "",
                    time_stamp,
                    "",
                ]
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

    doc_df = pd.DataFrame(df_list, columns=settings.ALL_DF_COLS.split(","))
    doc_df = process_dup_paths_df(doc_df)
    return doc_df
