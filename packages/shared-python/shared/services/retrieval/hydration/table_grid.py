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
from loguru import logger

from shared.services.retrieval.settings import (
    EVIDENCE_TEXT_CHAR_BUDGET,
    LARGE_TABLE_AXIS,
    QUERY_TABLE_NAME,
)
from shared.services.storage.result_storage import get_result_storage

_TABLE_HTML_RE = re.compile(r"<table(?:\s|>)", re.IGNORECASE)
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|"
    r"pragma|replace|vacuum|reindex|into)\b",
    re.IGNORECASE,
)


class TableDownloadError(RuntimeError):
    """Raised when stored table HTML exists but fails to download."""


def looks_like_table_html(text: str) -> bool:
    return bool(_TABLE_HTML_RE.search(str(text or "")))


def load_table_html(row: Mapping[str, Any]) -> str:
    """Return table HTML from inline content or the stored artifact file.

    Returns ``""`` when there is no artifact to load (nothing to fetch).
    Raises ``TableDownloadError`` when an artifact reference exists but the
    download itself fails (network/storage error) — callers decide how to
    surface that (a warning note, not a crash of the whole tool call).
    """
    content = str(row.get("content") or "").strip()
    if looks_like_table_html(content):
        return content
    job_id = str(row.get("job_id") or "").strip()
    artifact = str(row.get("file_path") or content or "").strip()
    storage = get_result_storage()
    normalized = storage.normalize_artifact_ref(artifact)
    if not job_id or not normalized:
        return ""
    try:
        temp_path = storage.download_raw_to_temp(
            job_id=job_id,
            relative_path=normalized,
            suffix=".html",
            temp_dir=tempfile.gettempdir(),
        )
        return Path(temp_path).read_text(encoding="utf-8")
    except Exception as exc:
        raise TableDownloadError(
            f"job_id={job_id} path={normalized}: {exc}"
        ) from exc


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
    char_budget: int = EVIDENCE_TEXT_CHAR_BUDGET,
) -> str:
    chunk_id = str(row.get("chunk_id") or "")
    try:
        table_html = load_table_html(row)
    except TableDownloadError as exc:
        logger.warning(f"table download failed for chunk_id={chunk_id}: {exc}")
        return f"[table unavailable: chunk_id={chunk_id} — download failed]"
    if not table_html:
        return ""
    grid = html_to_grid(table_html)
    if not grid:
        return table_html if looks_like_table_html(table_html) else ""
    too_large = is_large_grid(grid) or len(table_html) > char_budget
    if not too_large:
        return table_html
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


def _join_headers(values: list[str]) -> str:
    return " | ".join(value for value in values if value) or "(none)"


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
