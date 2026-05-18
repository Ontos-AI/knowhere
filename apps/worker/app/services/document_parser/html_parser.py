# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false
"""
HTML table parsing and extraction utilities.

This module provides functions for converting pandas DataFrames to HTML tables
with support for:
- HTML to DataFrame/Markdown conversion
- HTML header expansion
- Nested HTML table parsing
"""

from typing import Dict, List, Optional

import pandas as pd
from bs4 import BeautifulSoup

from shared.core.exceptions.domain_exceptions import TableParsingException
from shared.services.text_processing.tokenization import remove_duplicates_orderkept


class HTMLHeaderExpander:
    """
    Expands multi-level HTML table headers into a flat list of column names.

    Handles:
    - rowspan: cells that span multiple rows
    - colspan: cells that span multiple columns
    - Multi-row headers: combines parent -> child relationships with " > "

    Example:
        Row 0: | A(rs=2) | B(cs=2) |
        Row 1: |         | B1 | B2 |

        Output: ["A", "B > B1", "B > B2"]

    Ported from snap-fill's HeaderMatrixExpander, using BeautifulSoup instead of PyQuery.
    """

    def __init__(self, table_html: str):
        """Initialize with table HTML content."""
        soup = BeautifulSoup(table_html, "html.parser")
        table = soup.find("table")
        if table:
            self.rows = table.find_all("tr", recursive=False)
            # Also check inside thead/tbody
            if not self.rows:
                self.rows = table.find_all("tr")
        else:
            self.rows = []

    def expand_headers(
        self, header_row_count: int = 2, start_row: int = 0
    ) -> List[str]:
        """
        Expand multi-row headers into a flat column list.

        Args:
            header_row_count: Number of rows that form the header (default 2)
            start_row: Starting row index for headers (default 0)

        Returns:
            List of column header strings, with nested headers joined by " > "
        """
        grid = self._build_grid(header_row_count, start_row)
        if not grid or not grid[0]:
            return []

        max_cols = len(grid[0])

        # Build column headers by combining rows vertically
        headers = []
        for col in range(max_cols):
            parts = []
            prev_text = None
            for row in range(len(grid)):
                text = grid[row][col]
                # Only add if non-empty and different from previous
                if text and text != prev_text:
                    parts.append(text)
                    prev_text = text

            if parts:
                headers.append(" > ".join(parts))
            else:
                headers.append("")  # Empty column

        return headers

    def _build_grid(
        self, header_row_count: int, start_row: int = 0
    ) -> List[List[Optional[str]]]:
        """
        Build expanded grid from table rows.

        Algorithm:
        1. First pass: calculate max columns by expanding all colspan
        2. Create occupied grid to track cells filled by rowspan/colspan
        3. For each cell, place text in all positions it spans
        """
        if start_row + header_row_count > len(self.rows):
            return []

        header_rows = self.rows[start_row : start_row + header_row_count]

        # First pass: calculate max columns (sum of colspan for each row)
        max_cols = 0
        for tr in header_rows:
            col_count = 0
            for td in tr.find_all(["td", "th"], recursive=False):
                col_count += int(td.get("colspan", 1))
            max_cols = max(max_cols, col_count)

        if max_cols == 0:
            return []

        # Create grid and occupied tracker
        grid = [[None] * max_cols for _ in range(header_row_count)]
        occupied = [[False] * max_cols for _ in range(header_row_count)]

        # Process each row
        for row_idx, tr in enumerate(header_rows):
            col_ptr = 0  # Current column position in this row

            for td in tr.find_all(["td", "th"], recursive=False):
                text = td.get_text(strip=True)
                rowspan = int(td.get("rowspan", 1))
                colspan = int(td.get("colspan", 1))

                # Skip past any already-occupied cells (from previous row's rowspan)
                while col_ptr < max_cols and occupied[row_idx][col_ptr]:
                    col_ptr += 1

                if col_ptr >= max_cols:
                    break

                # Fill all cells covered by this rowspan/colspan
                for r in range(row_idx, min(row_idx + rowspan, header_row_count)):
                    for c in range(col_ptr, min(col_ptr + colspan, max_cols)):
                        grid[r][c] = text
                        occupied[r][c] = True

                # Move column pointer past this cell
                col_ptr += colspan

        return grid

    def get_grid_debug(
        self, header_row_count: int = 2, start_row: int = 0
    ) -> List[List[Optional[str]]]:
        """Return the expanded grid for debugging and testing purposes."""
        return self._build_grid(header_row_count, start_row)

    def get_unique_headers(
        self, header_row_count: int = 2, start_row: int = 0
    ) -> List[str]:
        """
        Get deduplicated headers for use in keywords/LLM prompts.
        Removes duplicate column names (from colspan expansion) while preserving order.
        """
        headers = self.expand_headers(header_row_count, start_row)
        seen = set()
        unique = []
        for h in headers:
            if h and h not in seen:
                seen.add(h)
                unique.append(h)
        return unique

    def detect_header_row_count(
        self, start_row: int = 0, max_scan_rows: int = 5
    ) -> int:
        """
        Automatically detect header row count using the maximum rowspan.

        Logic:
        - scan at most ``max_scan_rows`` rows starting at ``start_row``
        - find the largest rowspan value among all cells
        - ``rowspan=2`` means a two-row header, ``rowspan=3`` means three rows

        Returns:
            int: Detected header row count, at least 1.
        """
        if start_row >= len(self.rows):
            return 1

        max_rowspan = 1
        scan_end = min(start_row + max_scan_rows, len(self.rows))

        for row in self.rows[start_row:scan_end]:
            for cell in row.find_all(["td", "th"], recursive=False):
                rowspan = int(cell.get("rowspan", 1))
                if rowspan > max_rowspan:
                    max_rowspan = rowspan

        return max_rowspan

    def detect_row_indices(
        self,
        header_row_count: int,
        start_row: int = 0,
        end_row: int = None,
        max_scan_cols: int = 3,
    ) -> Dict:
        """
        Detect row-index columns and return the row-index metadata.

        Logic:
        1. start from data rows after the header
        2. scan left to right; a non-empty, non-numeric column is a row index
        3. stop once a data column is reached

        Returns:
            dict: {
                'row_index_col_count': int,
                'row_index_col_name': str or None,
                'row_indices': List[str],
            }
        """
        result = {
            "row_index_col_count": 0,
            "row_index_col_name": None,
            "row_indices": [],
        }

        data_start_row = start_row + header_row_count
        if data_start_row >= len(self.rows):
            return result

        # Build header grid for column names
        grid = self._build_grid(header_row_count, start_row)
        if not grid or not grid[0]:
            return result

        # Data rows
        if end_row is not None:
            data_end_row = min(end_row + 1, len(self.rows))
        else:
            data_end_row = len(self.rows)

        data_rows = self.rows[data_start_row:data_end_row]
        if not data_rows:
            return result

        row_index_col_count = 0
        row_index_col_names = []

        for cell_idx in range(max_scan_cols):
            col_values = []
            is_index_col = True

            for row in data_rows:
                cells = row.find_all(["td", "th"], recursive=False)

                if cell_idx >= len(cells):
                    continue

                cell_value = cells[cell_idx].get_text(strip=True)

                if not cell_value:
                    is_index_col = False
                    break

                # Check if purely numeric
                if (
                    cell_value.replace(".", "")
                    .replace("-", "")
                    .replace(" ", "")
                    .isdigit()
                ):
                    is_index_col = False
                    break

                col_values.append(cell_value)

            if is_index_col and col_values:
                row_index_col_count += 1
                row_index_col_names.append(col_values)
            else:
                break  # Stop at first data column

        if row_index_col_count > 0:
            result["row_index_col_count"] = row_index_col_count
            result["row_index_col_name"] = grid[-1][0] if grid and grid[-1] else None
            result["row_indices"] = row_index_col_names[0]

        return result


def parse_nested_htmltb(table):
    """Parse nested HTML table into a list of rows.

    Handles nested tables recursively.

    Args:
        table: BeautifulSoup table element

    Returns:
        List of rows, where each row is a list of cell values or nested tables
    """
    rows = []
    for tr in table.find_all("tr", recursive=False):
        row = []
        for td in tr.find_all(["td", "th"], recursive=False):
            inner_table = td.find("table")
            if inner_table:
                row.append(parse_nested_htmltb(inner_table))
            else:
                row.append(td.get_text(strip=True))
        if row:
            rows.append(row)
    return rows


def html_to_md_lines(html: str):
    """Convert HTML table to list of markdown-like lines.

    Args:
        html: HTML string containing a table

    Returns:
        List of strings, each representing a row with cells separated by ' | '
    """
    soup = BeautifulSoup(html, "html.parser")
    lines = []
    for row in soup.find_all("tr"):
        row_text = []
        for cell in row.find_all("td", recursive=False):
            text = cell.get_text(separator=" ", strip=True)
            if text:
                row_text.append(text)
        if row_text:
            lines.append(" | ".join(row_text))
    lines = remove_duplicates_orderkept(lines)
    return lines


def tb_htmlstr_to_df(html_str):
    """Convert first table in HTML string to DataFrame"""
    soup = BeautifulSoup(html_str, "html.parser")
    table = soup.find("table")
    if not table:
        raise TableParsingException(
            user_message="No table structure found in the document",
            reason="INVALID_FORMAT",
            internal_message="No <table> found in the HTML string",
        )
    nested_list = parse_nested_htmltb(table)
    try:
        df = pd.DataFrame(nested_list[1:], columns=nested_list[0])
    except Exception:
        df = pd.DataFrame(nested_list)
    return df


def merge_html_tables(lines: list) -> list:
    """Merge multi-line HTML tables into single lines.

    MinerU and some parsers output HTML tables with line breaks inside.
    This function joins all lines from <table> to </table> into a single line.

    Args:
        lines: List of text lines (already stripped)

    Returns:
        List of lines with HTML tables merged into single lines
    """
    merged_lines = []
    in_table = False
    table_buffer = []

    for line in lines:
        if "<table" in line and not in_table:
            in_table = True
            table_buffer = [line]
            # Handle single-line complete table
            if "</table>" in line:
                merged_lines.append(" ".join(table_buffer))
                table_buffer = []
                in_table = False
        elif in_table:
            table_buffer.append(line)
            if "</table>" in line:
                merged_lines.append(" ".join(table_buffer))
                table_buffer = []
                in_table = False
        else:
            merged_lines.append(line)

    # Handle unclosed table at end of file
    if table_buffer:
        merged_lines.append(" ".join(table_buffer))

    return merged_lines


def first_cols_rows_html(html_str, max_items=10, max_chars=20):
    """Extract first row and first column from HTML table string.

    This function mirrors the logic of _first_cols_rows in doc_parser.py
    but works with HTML strings instead of DOCX Table objects.

    Args:
        html_str: HTML table string
        max_items: Maximum number of items to extract (default 10)
        max_chars: Maximum characters per item (default 20)

    Returns:
        Tuple of (first_row_text, first_col_text) with ' | ' as separator
    """
    from app.services.document_parser.text_helpers import truncate_text

    soup = BeautifulSoup(html_str, "html.parser")
    table = soup.find("table")
    if not table:
        return "", ""

    rows = table.find_all("tr")
    if not rows:
        return "", ""

    # First row extraction (deduplicated, order preserved, max items, truncated)
    first_row_cells = rows[0].find_all(["td", "th"])
    seen_row = set()
    unique_row = []
    for cell in first_row_cells:
        if len(unique_row) >= max_items:
            break
        text = cell.get_text(strip=True)
        if text and text not in seen_row:
            seen_row.add(text)
            unique_row.append(truncate_text(text, max_chars, 0))
    first_row_text = " | ".join(unique_row) if unique_row else ""

    # First column extraction (deduplicated, order preserved, max items, truncated)
    seen_col = set()
    unique_col = []
    for row in rows:
        if len(unique_col) >= max_items:
            break
        cells = row.find_all(["td", "th"])
        if cells:
            text = cells[0].get_text(strip=True)
            if text and text not in seen_col:
                seen_col.add(text)
                unique_col.append(truncate_text(text, max_chars, 0))
    first_col_text = " | ".join(unique_col) if unique_col else ""

    return first_row_text, first_col_text
