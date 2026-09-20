"""Load table HTML from storage and expand rowspan/colspan into a grid.

Used by explore-phase ``corpus.read``, grep/recall hit mounting, and
``corpus.query_table``. Final retrieval assembly does not call this module.
"""

from __future__ import annotations

import html
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from shared.services.retrieval.settings import (
    EVIDENCE_TEXT_CHAR_BUDGET,
    LARGE_TABLE_AXIS,
    QUERY_TABLE_NAME,
    TABLE_FOCUS_RADIUS,
)
from shared.services.storage.result_storage import get_result_storage

_TABLE_HTML_RE = re.compile(r"<table(?:\s|>)", re.IGNORECASE)
_CELL_RE = re.compile(r"^(?:cell=)?r(\d+)c(\d+)$", re.IGNORECASE)
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|"
    r"pragma|replace|vacuum|reindex|into)\b",
    re.IGNORECASE,
)


def looks_like_table_html(text: str) -> bool:
    return bool(_TABLE_HTML_RE.search(str(text or "")))


def load_table_html(row: Mapping[str, Any]) -> str:
    """Return table HTML from inline content or the stored artifact file."""
    content = str(row.get("content") or "").strip()
    if looks_like_table_html(content):
        return content
    job_id = str(row.get("job_id") or "").strip()
    artifact = str(row.get("file_path") or content or "").strip()
    storage = get_result_storage()
    normalized = storage.normalize_artifact_ref(artifact)
    if not job_id or not normalized:
        return ""
    temp_path = storage.download_raw_to_temp(
        job_id=job_id,
        relative_path=normalized,
        suffix=".html",
        temp_dir=tempfile.gettempdir(),
    )
    return Path(temp_path).read_text(encoding="utf-8")


def html_to_grid(table_html: str) -> list[list[str]]:
    soup = BeautifulSoup(str(table_html or ""), "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    source_rows = table.find_all("tr")
    parsed: list[list[tuple[str, int, int]]] = []
    max_cols = 0
    for tr in source_rows:
        cells: list[tuple[str, int, int]] = []
        for td in tr.find_all(["td", "th"], recursive=False):
            rowspan = _span(td.get("rowspan"))
            colspan = _span(td.get("colspan"))
            cells.append((td.get_text(strip=True), rowspan, colspan))
        max_cols = max(max_cols, sum(cell[2] for cell in cells))
        parsed.append(cells)
    extra = 0
    for row_idx, cells in enumerate(parsed):
        for _text, rowspan, _colspan in cells:
            extra = max(extra, row_idx + rowspan - len(parsed))
    row_count = len(parsed) + extra
    if row_count == 0 or max_cols == 0:
        return []
    grid = [[""] * max_cols for _ in range(row_count)]
    occupied = [[False] * max_cols for _ in range(row_count)]
    for row_idx, cells in enumerate(parsed):
        col_ptr = 0
        for text, rowspan, colspan in cells:
            while col_ptr < max_cols and occupied[row_idx][col_ptr]:
                col_ptr += 1
            if col_ptr >= max_cols:
                break
            for r in range(row_idx, min(row_idx + rowspan, row_count)):
                for c in range(col_ptr, min(col_ptr + colspan, max_cols)):
                    grid[r][c] = text
                    occupied[r][c] = True
            col_ptr += colspan
    while grid and not any(cell for cell in grid[-1]):
        grid.pop()
    return grid


def column_headers(grid: list[list[str]]) -> list[str]:
    return list(grid[0]) if grid else []


def row_headers(grid: list[list[str]]) -> list[str]:
    return [row[0] if row else "" for row in grid]


def is_large_grid(grid: list[list[str]]) -> bool:
    if not grid:
        return False
    return len(grid) >= LARGE_TABLE_AXIS or len(grid[0]) >= LARGE_TABLE_AXIS


def cell_label(row: int, col: int) -> str:
    return f"r{row + 1}c{col + 1}"


def parse_focus(focus: object) -> tuple[int, int] | str | None:
    """Return 0-based (row, col), a locate string, or None."""
    if focus is None:
        return None
    if isinstance(focus, Mapping):
        cell = str(focus.get("cell") or "").strip()
        parsed_cell = _parse_cell_label(cell)
        if parsed_cell is not None:
            return parsed_cell
        if focus.get("row") is not None and focus.get("col") is not None:
            return int(focus["row"]) - 1, int(focus["col"]) - 1
        locate = str(focus.get("locate") or "").strip()
        return locate or None
    text = str(focus).strip()
    parsed_cell = _parse_cell_label(text)
    if parsed_cell is not None:
        return parsed_cell
    return text or None


def locate_cell(
    grid: list[list[str]],
    matcher: re.Pattern[str] | str,
) -> tuple[int, int] | None:
    if isinstance(matcher, str):
        if not matcher:
            return None
        compiled = re.compile(re.escape(matcher), flags=re.IGNORECASE)
    else:
        compiled = matcher
    for row_idx, row in enumerate(grid):
        for col_idx, cell in enumerate(row):
            if compiled.search(cell):
                return row_idx, col_idx
    return None


def window_grid(
    grid: list[list[str]],
    *,
    row: int,
    col: int,
    radius: int = TABLE_FOCUS_RADIUS,
) -> list[list[str]]:
    if not grid:
        return []
    col_count = len(grid[0])
    row_start = max(0, row - radius)
    row_end = min(len(grid), row + radius + 1)
    col_start = max(0, col - radius)
    col_end = min(col_count, col + radius + 1)
    include_header_row = row_start > 0
    include_header_col = col_start > 0
    window: list[list[str]] = []
    if include_header_row:
        header = _window_row(
            grid[0],
            col_start=col_start,
            col_end=col_end,
            include_header_col=include_header_col,
            header_cell=grid[0][0] if grid[0] else "",
        )
        window.append(header)
    for row_idx in range(row_start, row_end):
        window.append(
            _window_row(
                grid[row_idx],
                col_start=col_start,
                col_end=col_end,
                include_header_col=include_header_col,
                header_cell=grid[row_idx][0] if grid[row_idx] else "",
            )
        )
    return window


def grid_to_html(grid: list[list[str]]) -> str:
    if not grid:
        return ""
    parts = ["<table>"]
    for row_idx, row in enumerate(grid):
        parts.append("<tr>")
        tag = "th" if row_idx == 0 else "td"
        for cell in row:
            parts.append(f"<{tag}>{html.escape(cell)}</{tag}>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def render_explore_table(
    row: Mapping[str, Any],
    *,
    focus: object = None,
    char_budget: int = EVIDENCE_TEXT_CHAR_BUDGET,
) -> str:
    table_html = load_table_html(row)
    if not table_html:
        return ""
    grid = html_to_grid(table_html)
    if not grid:
        return table_html if looks_like_table_html(table_html) else ""
    too_large = is_large_grid(grid) or len(table_html) > char_budget
    if not too_large:
        return table_html
    chunk_id = str(row.get("chunk_id") or "")
    document_id = str(row.get("document_id") or "")
    lines = [
        (
            f"This table is too large ({len(grid)} rows × {len(grid[0])} cols). "
            "Read does not return the full table. Use corpus.query_table with "
            f"document_id={document_id} chunk_id={chunk_id} and a SELECT."
        ),
        f"Column headers: {_join_headers(column_headers(grid))}",
        f"Row headers: {_join_headers(row_headers(grid))}",
        f"SQL table name: {QUERY_TABLE_NAME}",
    ]
    resolved = _resolve_focus_cell(grid, focus)
    if resolved is not None:
        focus_row, focus_col = resolved
        window = window_grid(grid, row=focus_row, col=focus_col)
        lines.append(f"Window around {cell_label(focus_row, focus_col)}:")
        lines.append(grid_to_html(window))
    return "\n".join(line for line in lines if line)


def normalize_select_sql(sql: str) -> str | None:
    text = str(sql or "").strip().rstrip(";")
    if not text or ";" in text:
        return None
    if not re.match(r"^select\b", text, flags=re.IGNORECASE):
        return None
    if _FORBIDDEN_SQL.search(text):
        return None
    if not re.search(r"\blimit\b", text, flags=re.IGNORECASE):
        text = f"{text} LIMIT {LARGE_TABLE_AXIS}"
    return text


def sql_columns(grid: list[list[str]]) -> list[str]:
    if not grid:
        return ["row_header"]
    headers = column_headers(grid)
    used = {"row_header"}
    columns = ["row_header"]
    rest = headers[1:] if len(headers) > 1 else []
    if not rest and len(grid[0]) > 1:
        rest = [f"col_{index}" for index in range(1, len(grid[0]))]
    for index, header in enumerate(rest, start=1):
        columns.append(_sql_ident(header, used, fallback=f"col_{index}"))
    return columns


def grid_sql_rows(grid: list[list[str]]) -> list[list[str]]:
    if len(grid) <= 1:
        return []
    col_count = len(grid[0])
    rows: list[list[str]] = []
    for row in grid[1:]:
        padded = list(row) + [""] * (col_count - len(row))
        values = [padded[0]]
        values.extend(padded[1:] if col_count > 1 else [])
        rows.append(values)
    return rows


def _resolve_focus_cell(
    grid: list[list[str]],
    focus: object,
) -> tuple[int, int] | None:
    parsed = parse_focus(focus)
    if parsed is None:
        return None
    if isinstance(parsed, tuple):
        row, col = parsed
        if 0 <= row < len(grid) and 0 <= col < len(grid[0]):
            return row, col
        return None
    return locate_cell(grid, parsed)


def _window_row(
    row: list[str],
    *,
    col_start: int,
    col_end: int,
    include_header_col: bool,
    header_cell: str,
) -> list[str]:
    cells: list[str] = []
    if include_header_col:
        cells.append(header_cell)
    cells.extend(row[col_start:col_end])
    return cells


def _join_headers(values: list[str]) -> str:
    return " | ".join(value for value in values if value) or "(none)"


def _parse_cell_label(text: str) -> tuple[int, int] | None:
    match = _CELL_RE.match(str(text or "").strip())
    if match is None:
        return None
    return int(match.group(1)) - 1, int(match.group(2)) - 1


def _span(value: object) -> int:
    try:
        parsed = int(value or 1)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _sql_ident(name: str, used: set[str], *, fallback: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", str(name or "").strip()) or fallback
    if base[0].isdigit():
        base = f"c_{base}"
    candidate = base
    suffix = 2
    while candidate.lower() in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate
