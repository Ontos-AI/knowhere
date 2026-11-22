from __future__ import annotations

import html
import re
import unicodedata

import pandas as pd

from app.services.common.file_utils import MAX_ASSET_FILE_NAME_CHARS


def sanitize_table_name_from_header(raw_header_text: str) -> str:
    """Build a concise, filesystem-safe table name from raw first-row header text."""
    from shared.utils.text_utils import _is_meaningful_token

    if not raw_header_text:
        return ""

    parts = re.split(r"\s*\|\s*|_+br_|\n", raw_header_text)

    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        part = part.strip()
        if not part or part in seen:
            continue
        seen.add(part)
        unique.append(part)

    meaningful = [field for field in unique if _is_meaningful_token(field)]
    result = " ".join(meaningful)
    if len(result) > MAX_ASSET_FILE_NAME_CHARS:
        result = result[:MAX_ASSET_FILE_NAME_CHARS].rstrip()
    return result


def identify_tables(line: str) -> tuple[bool, str | None, list[str] | None]:
    """Identify whether one logical Markdown line contains a table."""
    html_table_pattern = r"<table.*?>.*?</table>"
    tables = re.findall(html_table_pattern, line, re.DOTALL)
    if bool(tables):
        return True, "html", tables

    if line.startswith("|") and line.endswith("|"):
        return True, "md", []

    return False, None, None


def df2md(table_frame: pd.DataFrame, *, index: bool = False, na_rep: str = "—") -> str:
    """Convert a DataFrame to a Markdown table while preserving display width."""

    def get_display_width(text: str) -> int:
        width = 0
        for character in text:
            if unicodedata.east_asian_width(character) in ("F", "W"):
                width += 2
            else:
                width += 1
        return width

    def pad_to_width(text: str, target_width: int) -> str:
        current_width = get_display_width(text)
        padding = target_width - current_width
        return text + " " * max(0, padding)

    table_frame = table_frame.copy()

    if index:
        table_frame = table_frame.reset_index()

    table_frame = table_frame.fillna(na_rep).astype(str)

    column_widths: dict[object, int] = {}
    for column in table_frame.columns:
        header_width = get_display_width(str(column))
        max_content_width = (
            max(table_frame[column].apply(get_display_width))
            if len(table_frame) > 0
            else 0
        )
        column_widths[column] = max(header_width, max_content_width)

    header_cells = [
        pad_to_width(str(column), column_widths[column])
        for column in table_frame.columns
    ]
    header_line = "| " + " | ".join(header_cells) + " |"

    separator_cells = ["-" * column_widths[column] for column in table_frame.columns]
    separator_line = "|-" + "-|-".join(separator_cells) + "-|"

    data_lines: list[str] = []
    for _, row in table_frame.iterrows():
        cells = [
            pad_to_width(str(row[column]), column_widths[column])
            for column in table_frame.columns
        ]
        data_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join([header_line, separator_line, *data_lines])


def split_md_table_row(line: str) -> list[str]:
    """Cells of one Markdown pipe-table row.

    Follows the GFM table rule: the outer pipes delimit the row, an escaped
    ``\\|`` is a literal pipe inside a cell, and each cell is trimmed.
    """
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and text[index + 1 : index + 2] == "|":
            current.append("|")
            index += 2
            continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    cells.append("".join(current).strip())
    return cells


def join_md_table_row(cells: list[str]) -> str:
    """Inverse of ``split_md_table_row``: escape literal pipes and add the outer pipes."""
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _is_md_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r"[\s\-:]+", cell) for cell in cells)


def extract_tables_by_forms(table_text: str, form: str) -> str | None:
    if form == "html":
        return table_text

    if form != "md":
        return None

    rows = [split_md_table_row(line) for line in table_text.splitlines() if line.strip()]
    header, body = rows[0], [row for row in rows[1:] if not _is_md_separator_row(row)]
    return _md_rows_to_html(header, body)


def _md_rows_to_html(header: list[str], body: list[list[str]]) -> str:
    parts = ["<table><thead><tr>"]
    parts.extend(f"<th>{html.escape(cell)}</th>" for cell in header)
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>")
        parts.extend(f"<td>{html.escape(cell)}</td>" for cell in row)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)
