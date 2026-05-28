from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

import pandas as pd
from docx.oxml.ns import qn
from loguru import logger
from pandas import Index

from app.services.document_parser.support.text_helpers import count_cn_en

HEADING_COLUMNS = Index(["id", "heading", "level", "reason"])


def get_max_lvl(code_str: str) -> int:
    """Extract the maximum hierarchy depth from a POS trigger code string.

    ``code_str`` is always ``str(pos_code)`` where *pos_code* is a list of
    integers, so the ``[…]`` bracket match is guaranteed.
    """
    match = re.search(r"\[([^]]+)]", code_str)
    nums = [int(item.strip()) for item in match.group(1).split(",")]
    max_value = int(max(nums))
    return max_value if max_value > 1 else -2


def judge_by_conditions(
    text,
    scope: int = 20,
    return_detail: bool = False,
    cn_special_index: int = 12,
    **legacy_options: Any,
):
    legacy_cn_special_index = legacy_options.pop("CN_SPECIAL_IDX", None)
    if legacy_cn_special_index is not None:
        cn_special_index = int(legacy_cn_special_index)
    if legacy_options:
        unknown_options = ", ".join(sorted(legacy_options))
        raise TypeError(f"Unknown heading condition option(s): {unknown_options}")

    text = text.replace("\u3000", " ")
    text = unicodedata.normalize("NFKC", text)[:scope]

    pos_regex_conditions = [
        r"^\d+(?:\s*\.\s*\d+)+(?![、，。！？；：])(?=\s|$|\w|[一-龥])",
        r"^\d、\s{0,4}(?=\S|$)",
        r"^\d+\.(?!\d)\s{0,4}(?=\S)",
        r"^[0-9]{1,2}\s{1,8}(?=\S)",
        r"^\d+(?:\.\d+)*、\s*(?=[A-Za-z一-龥])",
        r"^[一二三四五六七八九十百千万]+、\s{0,4}(?=\S|$)",
        r"^[一二三四五六七八九十百千万]+(?:\s*\.[一二三四五六七八九十百千万\d]+)+",
        r"^[一二三四五六七八九十百千万]+(?=\s|$)",
        r"^[\(\（]\s*\d+(?:\.\d+)*(?!\.0)\s*[\)\）]",
        r"^\d+(?:\.\d+)*(?!\.0)\s*[\)\）]",
        r"^[\(\（]\s*[一二三四五六七八九十百千万]+(?:\.[一二三四五六七八九十百千万\d]+)*\s*[\)\）]",
        r"^[一二三四五六七八九十百千万]+(?:\.[一二三四五六七八九十百千万\d]+)*\s*[\)\）]",
        r"^第[一二三四五六七八九十百千万\d]+(?:\.[一二三四五六七八九十百千万\d]+)*(章|节|条|部分|款|目|项|编|篇|卷|辑)?(?=$|\s|[A-Za-z0-9\u4e00-\u9fa5])",
        r"^[A-Za-z](?:\.\d+)*[\.、](?=\s*\S)",
        r"^[\(\（]\s*[A-Za-z](?:\.\d+)*(?!\.0)\s*[\)\）]",
        r"^[A-Za-z](?:\.\d+)*(?!\.0)\s*[\)\）]",
        r"^((附件|附录|附表|附图)|(?i:appendix))[\s_\-—]{0,4}(?:\[)?[一二三四五六七八九十A-Za-z\d]",
    ]

    pos_triggered_code = []
    reason_suffix_parts = []

    for index, regex in enumerate(pos_regex_conditions):
        match = re.match(regex, text)
        if match:
            matched_text = match.group(0)
            count = sum(matched_text.count(symbol) for symbol in ".-") + 1
            if index == cn_special_index and return_detail:
                unit_match = re.search(r"(章|节|条|部分|款|目|项|编|篇|卷|辑)", matched_text)
                if unit_match:
                    reason_suffix_parts.append(f"[CN:{unit_match.group(1)}]")
            pos_triggered_code.append(count)
        else:
            pos_triggered_code.append(0)

    if return_detail:
        reason_suffix = " ".join(reason_suffix_parts) if reason_suffix_parts else ""
        return pos_triggered_code, {
            "reason_suffix": f" {reason_suffix}" if reason_suffix else ""
        }
    return pos_triggered_code


def remove_by_conditions(text, *, include_punc: bool = False):
    """Evaluate negative (non-heading) conditions against *text*.

    ``include_punc`` controls the end-of-line punctuation rule.  It is
    intentionally **off** during initial scanning so that lines remain heading candidates.  
    The punctuation check is enabled only during the ``judge_negs`` second pass (after merges
    may have altered heading text).
    """
    neg_conditions = [
        r"^\d{3,}",
        r"(?i)(^https?://\S+|^www\.\S+|^P\.S|^\b\d{0,2}\s*(?:a\.m|p\.m)\b)",
        (
            r"(?:"
            r"\$[^$]*\\[A-Za-z]+(?:\s*\{[^{}]*\})?[^$]*\$"
            r"|"
            r"\\(?:times|div|cdot|pm|mp|leq|geq|neq|approx|equiv|sim|infty"
            r"|sum|prod|int|sqrt|frac|mathrm|mathbf|mathit|mathcal"
            r"|text(?:bf|it|rm)?|alpha|beta|gamma|delta|epsilon|theta"
            r"|lambda|mu|sigma|pi|omega|partial|nabla"
            r"|left|right|begin|end|overline|underline|hat|vec|tilde)\b"
            r")"
        ),
        r"^0\.\d+\S*",
        r"^\d*\.\d+$",
        r"[。！；].+",
        (
            r"^\d+\.?\d*\s{0,2}"
            r"(?:mm|cm|km|nm|μm|inch(?:es)?|ft|yd|mi"
            r"|kg|mg|μg|lb|oz"
            r"|kPa|MPa|GPa|Pa|psi|bar"
            r"|°[CFK]"
            r"|Hz|kHz|MHz|GHz"
            r"|mol|mL|dL|dB|Nm|kN|MN|kW|MW|GW|hp|rpm|cc|cal|kcal)\b"
        ),
    ]

    neg_triggered_code = []
    for regex in neg_conditions:
        neg_triggered_code.append(1 if re.search(regex, text) else 0)

    # End-of-line punctuation — only checked during judge_negs second pass.
    if include_punc:
        neg_triggered_code.append(1 if re.search(r"[.,;，。；]$", text) else 0)
    else:
        neg_triggered_code.append(0)

    MAX_HEADING_TOKENS = 10
    neg_triggered_code.append(1 if count_cn_en(text) > MAX_HEADING_TOKENS else 0)

    return neg_triggered_code


def md_heading_match(line, as_is: bool = True):
    match = re.match(r"^\s*(#+)\s*(.*)$", line)
    if not match:
        return line, -1

    level = len(match.group(1))
    return (line, level) if as_is else (line.lstrip("#").strip(), level)


# Pre-compute zero-filled code arrays so non-heading lines get correct-width reason strings.
_ZERO_POS_CODE = judge_by_conditions("")
_ZERO_NEG_CODE = remove_by_conditions("")


def filter_markdown_headings(
    md_lines: list[str],
    layout_json_path: str | None = None,
) -> pd.DataFrame:
    meta_ctx = None
    if layout_json_path:
        try:
            from app.services.document_parser.structure.metadata_extractor import MetadataContext

            meta_ctx = MetadataContext(md_lines, layout_json_path)
        except Exception as exc:
            logger.warning(f"Failed to create MetadataContext: {exc}")

    raw_candidates = []
    for line_index, line in enumerate(md_lines):
        line = line.strip()
        if not line:
            continue

        if _is_non_heading_markdown_line(line):
            est_level = -1
            reason = f"POS {_ZERO_POS_CODE} NEG {_ZERO_NEG_CODE}"
            if meta_ctx:
                reason += " META [0, 0, 0]"
            line = "Figure/Image"
        else:
            est_level, reason, line = _estimate_markdown_heading_level(line, meta_ctx)

        raw_candidates.append((line_index, line, est_level, reason))

    return pd.DataFrame(raw_candidates, columns=HEADING_COLUMNS, index=None)


def filter_document_headings(
    heading_infos: list[tuple[Any, Any, str]],
    *,
    enable_regex: bool = True,
) -> pd.DataFrame:
    raw_candidates = []
    logger.debug("Filtering docx heading candidates... total_items={}", len(heading_infos))

    for element_id, paragraph, text in heading_infos:
        reason = ""
        est_level = None
        style_level = _find_docx_style_level(paragraph)
        setting_level = _find_docx_outline_level(paragraph)

        if style_level is not None:
            est_level = style_level
            reason = f"style-{style_level}"
        elif setting_level is not None:
            est_level = setting_level
            reason = f"outline-{setting_level}"

        is_bold = 1 if _is_bold_docx_paragraph(paragraph) else 0

        if enable_regex:
            pos_code, detail_info = judge_by_conditions(text, return_detail=True)
            neg_code = remove_by_conditions(text)

            if any(value > 0 for value in neg_code):
                code_level = -1
                code_reason = (
                    f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"
                )
            elif any(value > 0 for value in pos_code) and all(
                value == 0 for value in neg_code
            ):
                code_level = get_max_lvl(str(pos_code))
                code_reason = (
                    f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"
                )
            else:
                code_level = -1
                code_reason = f"POS {pos_code} NEG {neg_code}"

            if is_bold:
                code_reason += f" META [0, 0, {is_bold}]"

            if est_level is None:
                est_level = code_level
                reason = code_reason
            else:
                reason = f"{reason} AND {code_reason}"

        raw_candidates.append((element_id, text, est_level, reason))

    candidates = pd.DataFrame(raw_candidates, columns=HEADING_COLUMNS, index=None)
    if candidates.empty:
        return pd.DataFrame(columns=HEADING_COLUMNS)

    candidates = postprocess_headings(candidates, task="merge_continuous")
    return postprocess_headings(candidates, task="merge_short")


def postprocess_headings(df: pd.DataFrame, task: str, max_depth: int = -1) -> pd.DataFrame:
    if task == "judge_negs":
        return _judge_negative_headings(df)

    if task == "merge_continuous":
        return _merge_continuous_non_headings(df)

    if task == "merge_short":
        return _merge_short_heading_groups(df)

    return df


def _is_non_heading_markdown_line(line: str) -> bool:
    return (
        ("<!--" in line and "-->" in line)
        or line.startswith("|")
        or line.startswith("<table>")
        or ("![" in line and "](" in line)
    )


def _estimate_markdown_heading_level(line: str, meta_ctx: Any | None):
    from app.services.document_parser.structure.metadata_extractor import detect_and_strip_md_bold

    line_clean, hash_level = md_heading_match(line, as_is=False)
    stripped_line, is_full_bold = detect_and_strip_md_bold(line_clean)
    pos_code, detail_info = judge_by_conditions(stripped_line, return_detail=True)
    neg_code = remove_by_conditions(stripped_line)

    if any(value > 0 for value in neg_code):
        code_level = -1
        code_reason = f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"
    elif any(value > 0 for value in pos_code) and all(value == 0 for value in neg_code):
        code_level = get_max_lvl(str(pos_code))
        code_reason = f"POS {pos_code}{detail_info.get('reason_suffix', '')} NEG {neg_code}"
    else:
        code_level = -1
        code_reason = f"POS {pos_code} NEG {neg_code}"

    if meta_ctx:
        size_rank, occurrence = meta_ctx.get_meta_for_line(line_clean)
        bold_value = 1 if is_full_bold else 0
        code_reason += meta_ctx.format_meta_suffix(size_rank, occurrence, bold_value)
    elif is_full_bold:
        code_reason += " META [0, 0, 1]"

    if hash_level <= 0:
        return code_level, code_reason, line_clean

    if isinstance(code_level, int):
        est_level = max(hash_level, code_level)
    else:
        est_level = code_level
    return est_level, f"{hash_level}# AND {code_reason}", line_clean


def _find_docx_style_level(paragraph: Any):
    try:
        style_name = paragraph.style.name
    except Exception:
        style_name = "normal"

    if not (style_name.startswith("Heading") or style_name.startswith("标题")):
        return None

    try:
        return int(style_name.split(" ")[1])
    except Exception:
        return -2


def _find_docx_outline_level(paragraph: Any):
    paragraph_properties = paragraph._element.find(qn("w:pPr"))
    if paragraph_properties is None:
        return None

    outline_level = paragraph_properties.find(qn("w:outlineLvl"))
    if outline_level is None:
        return None

    return int(outline_level.get(qn("w:val"))) + 1


def _is_bold_docx_paragraph(paragraph: Any):
    if paragraph.runs and all(run.bold for run in paragraph.runs if run.text.strip()):
        return True
    return None


def _judge_negative_headings(df: pd.DataFrame) -> pd.DataFrame:
    for index, row in df.iterrows():
        neg_code = remove_by_conditions(row["heading"], include_punc=True)
        if any(value > 0 for value in neg_code):
            current_code = str(df.loc[index, "reason"])

            neg_match = re.search(r"(.*NEG\s*)\[[^\]]*\](.*)", current_code)
            if neg_match:
                updated_code = f"{neg_match.group(1)}{neg_code}{neg_match.group(2)}"
            else:
                updated_code = f"{current_code} NEG {neg_code}"

            df.loc[index, "level"] = -1
            df.loc[index, "reason"] = updated_code
    return df


def _merge_continuous_non_headings(df: pd.DataFrame) -> pd.DataFrame:
    denoised_rows = []
    punc_pattern = re.compile(r'[.,!?;:，。！？；：）】〕｝〉》’”"]$')

    index = 0
    while index < len(df):
        row = df.iloc[index]
        current_content = str(row["heading"]).strip()
        current_level = row["level"]

        next_index = index + 1
        while next_index < len(df):
            next_row = df.iloc[next_index]
            next_content = str(next_row["heading"]).strip()
            next_level = next_row["level"]

            expected_id = row["id"] + (next_index - index)
            if next_row["id"] != expected_id:
                break

            current_not_punc = not punc_pattern.search(current_content[-2:])
            if (current_level == -1 and next_level == -1) and current_not_punc:
                current_content += " " + next_content
                next_index += 1
            else:
                break

        merged_row = row.copy()
        merged_row["heading"] = current_content
        denoised_rows.append(tuple(merged_row))
        index = next_index

    return pd.DataFrame(denoised_rows, columns=HEADING_COLUMNS)


def _merge_short_heading_groups(df: pd.DataFrame) -> pd.DataFrame:
    group_to_indices = defaultdict(list)
    for index, row in df.iterrows():
        level = row["level"]
        reason = row["reason"]
        if level != -1:
            group_to_indices[(level, reason)].append(index)

    checked_pairs = set()
    for _, indices in group_to_indices.items():
        _merge_short_recursive(df, indices, merge_threshold=3, checked_pairs=checked_pairs)

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


def _merge_short_recursive(
    df: pd.DataFrame,
    indices: list[int],
    merge_threshold: int = 3,
    checked_pairs: set[tuple[int, int]] | None = None,
) -> None:
    if checked_pairs is None:
        checked_pairs = set()

    if len(indices) < 2:
        return

    for group_index in range(len(indices) - 1):
        index, next_index = indices[group_index], indices[group_index + 1]
        if (index, next_index) in checked_pairs:
            continue
        checked_pairs.add((index, next_index))

        between = df.loc[index + 1 : next_index - 1]
        current_text = df.at[index, "heading"].strip()

        if len(between) > 0:
            _merge_short_between_headings(
                df, between, index, next_index, current_text, merge_threshold
            )

        sub_between = between[between["level"] != -1]
        code_to_sub = defaultdict(list)
        for row_index, row in sub_between.iterrows():
            level = row["level"]
            reason = row["reason"]
            if level != -1:
                code_to_sub[(level, reason)].append(row_index)

        for _, sub_indices in code_to_sub.items():
            _merge_short_recursive(df, sub_indices, merge_threshold, checked_pairs)


def _merge_short_between_headings(
    df: pd.DataFrame,
    between: pd.DataFrame,
    index: int,
    next_index: int,
    current_text: str,
    merge_threshold: int,
) -> None:
    between_lengths = [count_cn_en(content) for content in between["heading"].tolist()]
    between_levels = [level for level in between["level"].tolist()]
    half_current_length = int(count_cn_en(current_text) / 2)
    too_short = (
        sum(between_lengths) <= merge_threshold
        or sum(between_lengths) < half_current_length
    )

    if not too_short or not all(level == -1 for level in between_levels):
        return

    next_text = df.at[next_index, "heading"].strip()
    logger.debug(
        f"⚠️ too short between {index}=>{current_text[:15]} and {next_index}=>{next_text[:15]} => merge to {index}"
    )
    between_texts = [
        heading_text.strip()
        for _, row in between.iterrows()
        if isinstance(heading_text := row["heading"], str) and heading_text.strip()
    ]

    joined_text = ""
    if between_texts:
        joined_text = "\n".join(between_texts)
        df.at[index, "heading"] = f"{current_text} {joined_text}"

    for row_index in between.index:
        df.at[row_index, "level"] = -1
        df.at[row_index, "reason"] = f"Merged into {index}"
    logger.debug(f"\tmerged texts: {joined_text[:50]}...")
