# pyright: reportArgumentType=false
from __future__ import annotations

import io
import re
import unicodedata

import pandas as pd
from bs4 import BeautifulSoup, Tag

_MAX_TABLE_NAME_CHARS = 80


def sanitize_table_name_from_header(raw_header_text: str) -> str:
    """Build a concise, filesystem-safe table name from raw first-row header text."""
    from shared.services.text_processing.tokenization import _is_meaningful_token

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
    if len(result) > _MAX_TABLE_NAME_CHARS:
        result = result[:_MAX_TABLE_NAME_CHARS].rstrip()
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


def clean_html_tb(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        seen: set[bytes] = set()
        unique_cells: list[Tag] = []
        for cell in row.find_all("td", recursive=False):
            if not isinstance(cell, Tag):
                continue
            content = cell.encode_contents()
            if content not in seen:
                seen.add(content)
                unique_cells.append(cell)
        row.clear()
        for cell in unique_cells:
            row.append(cell)
    return str(soup.prettify())


def extract_tables_by_forms(table_text: str, form: str) -> str | None:
    if form == "html":
        return table_text

    if form != "md":
        return None

    table_frame = pd.read_table(
        io.StringIO(table_text),
        sep="|",
        engine="python",
        on_bad_lines="skip",
    )
    table_frame = table_frame.iloc[:, 1:-1]
    table_frame.columns = table_frame.columns.astype(str).str.strip()

    separator_pattern = r"^[\s\-:]+$"
    table_frame = table_frame[
        ~table_frame.apply(
            lambda row: row.astype(str).str.match(separator_pattern).all(),
            axis=1,
        )
    ]
    return table_frame.to_html(index=False)
