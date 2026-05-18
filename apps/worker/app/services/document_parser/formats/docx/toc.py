from __future__ import annotations

import re

from app.services.document_parser.structure.heading_candidates import (
    judge_by_conditions,
    remove_by_conditions,
)
from app.services.document_parser.structure.toc_hierarchy import build_toc_hierarchy_payload
import lxml.etree as etree

TOC_TITLE_KEYWORDS = {"目录", "目次", "contents", "table of contents"}


def parse_w_int_attr(elem, ns, attr_names):
    if elem is None:
        return None

    for attr_name in attr_names:
        raw_val = elem.get("{%s}%s" % (ns["w"], attr_name))
        if raw_val is None:
            continue
        try:
            return int(raw_val)
        except (TypeError, ValueError):
            continue
    return None


def get_docx_toc_layout_hints(elem, ns):
    ppr = elem.find("./w:pPr", namespaces=ns)
    if ppr is None:
        ppr = elem.find(".//w:pPr", namespaces=ns)

    if ppr is None:
        return {
            "outline_level": None,
            "left_indent": None,
        }

    outline_elem = ppr.find("./w:outlineLvl", namespaces=ns)
    outline_level = None
    if outline_elem is not None:
        outline_val = parse_w_int_attr(outline_elem, ns, ["val"])
        if outline_val is not None:
            outline_level = outline_val + 1

    indent_elem = ppr.find("./w:ind", namespaces=ns)
    left_indent = parse_w_int_attr(
        indent_elem, ns, ["left", "start", "leftChars", "startChars"]
    )

    return {
        "outline_level": outline_level,
        "left_indent": left_indent,
    }


def infer_toc_level_from_text(text: str):
    text_clean = str(text).strip()
    if not text_clean:
        return None

    normalized = re.sub(r"\s+", " ", text_clean).lower()
    if normalized in TOC_TITLE_KEYWORDS:
        return None

    raw_pos_code = judge_by_conditions(text_clean)
    if not isinstance(raw_pos_code, list):
        return None
    positive_codes = [
        int(value)
        for value in raw_pos_code
        if isinstance(value, int) and value > 0
    ]
    neg_code = remove_by_conditions(text_clean)
    if any(value > 0 for value in neg_code) or not positive_codes:
        return None

    return max(positive_codes)


def is_toc_title_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text).strip()).lower()
    return normalized in TOC_TITLE_KEYWORDS


def infer_toc_levels_from_indentation(entries: list) -> None:
    indent_values = sorted(
        {
            entry["left_indent"]
            for entry in entries
            if entry.get("level") is None
            and entry.get("left_indent") is not None
            and not is_toc_title_text(entry.get("heading", ""))
        }
    )

    if not indent_values:
        return

    indent_to_level = {indent: idx + 1 for idx, indent in enumerate(indent_values)}
    for entry in entries:
        if entry.get("level") is not None:
            continue
        if is_toc_title_text(entry.get("heading", "")):
            continue
        left_indent = entry.get("left_indent")
        if left_indent is None:
            continue
        entry["level"] = indent_to_level.get(left_indent)


def get_docx_toc_style_info(elem, ns):
    style = elem.find(".//w:pPr/w:pStyle", namespaces=ns)
    if style is None:
        return {
            "is_toc_style": False,
            "toc_level": None,
            "style_name": None,
        }

    val = style.get("{%s}val" % ns["w"])
    if not val:
        return {
            "is_toc_style": False,
            "toc_level": None,
            "style_name": None,
        }

    val_lower = val.lower().strip()
    if "toc" not in val_lower and "目录" not in val:
        return {
            "is_toc_style": False,
            "toc_level": None,
            "style_name": val,
        }

    level = None
    match = re.search(r"(?:toc|目录)\s*[_-]?(\d+)$", val_lower)
    if match:
        level = int(match.group(1))

    layout_hints = get_docx_toc_layout_hints(elem, ns)
    if level is None:
        level = layout_hints["outline_level"]

    return {
        "is_toc_style": True,
        "toc_level": level,
        "style_name": val,
        "outline_level": layout_hints["outline_level"],
        "left_indent": layout_hints["left_indent"],
    }


def get_toc_level(elem, ns):
    style_info = get_docx_toc_style_info(elem, ns)
    if not style_info["is_toc_style"]:
        return False

    if style_info["toc_level"] is not None:
        return style_info["toc_level"]
    return True


def detect_sdt_toc(elem, ns):
    tag = etree.QName(elem.tag).localname if isinstance(elem.tag, str) else None

    if tag != "sdt":
        return {"is_toc_sdt": False, "gallery_type": None}

    is_toc_sdt = False
    gallery_type = None

    sdt_pr = elem.find(".//w:sdtPr", namespaces=ns)
    if sdt_pr is not None:
        doc_part_obj = sdt_pr.find(".//w:docPartObj", namespaces=ns)
        if doc_part_obj is not None:
            doc_part_gallery = doc_part_obj.find(".//w:docPartGallery", namespaces=ns)
            if doc_part_gallery is not None:
                gallery_type = doc_part_gallery.get("{%s}val" % ns["w"])
                if gallery_type and "table of contents" in gallery_type.lower():
                    is_toc_sdt = True

    return {"is_toc_sdt": is_toc_sdt, "gallery_type": gallery_type}


def detect_doc_tocs(elem, ns):
    style_info = get_docx_toc_style_info(elem, ns)
    is_style = style_info["is_toc_style"]
    is_field_start = False

    instrs = elem.findall(".//w:instrText", namespaces=ns)
    for instr in instrs:
        if instr.text:
            instr_text_stripped = instr.text.strip()
            instr_text_lower = instr_text_stripped.lower()
            if (
                instr_text_lower.startswith("toc")
                or "table of contents" in instr_text_lower
                or "目录" in instr_text_stripped
            ):
                is_field_start = True
                break

    is_field_end = False
    fldchars = elem.findall(".//w:fldChar", namespaces=ns)
    for fld in fldchars:
        if fld.get("{%s}fldCharType" % ns["w"]) == "end":
            is_field_end = True
            break

    return {
        "is_style": is_style,
        "toc_level": style_info["toc_level"],
        "style_name": style_info["style_name"],
        "outline_level": style_info.get("outline_level"),
        "left_indent": style_info.get("left_indent"),
        "is_field_start": is_field_start,
        "is_field_end": is_field_end,
    }


def build_docx_toc_hierarchies(block_tuples: list) -> list:
    toc_areas = []
    current_area = []

    for ele_num, block, label, meta in block_tuples:
        if "TOC" in label:
            current_area.append((ele_num, block, meta or {}))
            continue

        if current_area:
            toc_areas.append(current_area)
            current_area = []

    if current_area:
        toc_areas.append(current_area)

    toc_hierarchies = []
    for area in toc_areas:
        toc_entries = []
        for ele_num, block, meta in area:
            toc_level = meta.get("toc_level")
            try:
                toc_level = int(toc_level) if toc_level is not None else None
            except (TypeError, ValueError):
                toc_level = None

            text = getattr(block, "text", str(block)).strip()
            if not text:
                continue

            if toc_level is None:
                outline_level = meta.get("toc_outline_level")
                try:
                    toc_level = (
                        int(outline_level) if outline_level is not None else None
                    )
                except (TypeError, ValueError):
                    toc_level = None

            if toc_level is None:
                toc_level = infer_toc_level_from_text(text)

            left_indent = meta.get("toc_left_indent")
            try:
                left_indent = int(left_indent) if left_indent is not None else None
            except (TypeError, ValueError):
                left_indent = None

            toc_entries.append(
                {
                    "id": ele_num,
                    "heading": text,
                    "level": toc_level if toc_level and toc_level > 0 else None,
                    "left_indent": left_indent,
                }
            )

        infer_toc_levels_from_indentation(toc_entries)
        payload = build_toc_hierarchy_payload(
            toc_entries,
            toc_range=(area[0][0], area[-1][0]),
            scan_range=(area[0][0], area[-1][0]),
        )
        if payload:
            toc_hierarchies.append(payload)

    return toc_hierarchies
