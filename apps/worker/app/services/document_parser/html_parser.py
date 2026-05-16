# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false
"""
HTML rendering utilities for DataFrame to HTML conversion.

This module provides functions for converting pandas DataFrames to HTML tables
with support for:
- MultiIndex columns with colspan/rowspan merging
- Row headers (semantic <th scope="row"> elements)
- Proper HTML escaping and formatting
- DOCX table to HTML conversion
- HTML to DataFrame/Markdown conversion
"""

from typing import Dict, List, Optional, Union

import pandas as pd
from bs4 import BeautifulSoup
from docx.table import Table as DocxTable

from shared.core.exceptions.domain_exceptions import TableParsingException
from shared.utils.text_utils import remove_duplicates_orderkept


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


def table2html(table: DocxTable, cell_image_map: dict = None) -> str:
    """Convert a DOCX table to HTML string with proper colspan/rowspan handling.

    Handles merged cells by:
    - Detecting horizontal merges via comparing cell._tc objects
    - Detecting vertical merges via vMerge XML attribute
    - Generating proper colspan and rowspan attributes

    Args:
        table: python-docx Table object
        cell_image_map: Optional dict mapping (row_idx, col_idx) to image description
                        strings. col_idx corresponds to the unique tc index in each row
                        (matching XML <w:tc> ordering, not expanded python-docx cells).

    Returns:
        HTML string representation of the table with merged cells
    """

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def get_cell_vmerge(cell):
        """Get vMerge status: 'restart', 'continue', or None"""
        tc = cell._tc
        tcPr = tc.find(".//w:tcPr", namespaces=NS)
        if tcPr is not None:
            vMerge = tcPr.find(".//w:vMerge", namespaces=NS)
            if vMerge is not None:
                val = vMerge.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
                )
                return (
                    val if val else "continue"
                )  # If no val attribute, it's a continuation
        return None

    n_rows = len(table.rows)
    if n_rows == 0:
        return "<table border='1'></table>"

    # Build grid: track unique cells and their positions
    # grid[row][col] = (cell_tc_id, cell, is_new_cell)
    # We use id(cell._tc) as unique identifier for cells

    grid = []
    for row_idx, row in enumerate(table.rows):
        row_data = []
        prev_tc_id = None
        for cell in row.cells:
            tc_id = id(cell._tc)
            is_new = tc_id != prev_tc_id
            row_data.append((tc_id, cell, is_new))
            prev_tc_id = tc_id
        grid.append(row_data)

    # Rows may have different cell counts due to complex merges;
    # use the maximum for grid allocation, per-row length for access.
    n_cols = max(len(r) for r in grid) if grid else 0

    # Calculate colspan for each cell (count consecutive cells with same _tc)
    colspan_grid = [[0] * n_cols for _ in range(n_rows)]

    for row_idx in range(n_rows):
        row_len = len(grid[row_idx])
        col_idx = 0
        while col_idx < row_len:
            tc_id = grid[row_idx][col_idx][0]
            span = 1
            while (
                col_idx + span < row_len and grid[row_idx][col_idx + span][0] == tc_id
            ):
                span += 1
            colspan_grid[row_idx][col_idx] = span
            col_idx += span

    # Calculate rowspan for cells with vMerge='restart'
    rowspan_grid = [[1] * n_cols for _ in range(n_rows)]

    for col_idx in range(n_cols):
        row_idx = 0
        while row_idx < n_rows:
            if col_idx >= len(grid[row_idx]):
                row_idx += 1
                continue
            cell = grid[row_idx][col_idx][1]
            vmerge = get_cell_vmerge(cell)

            if vmerge == "restart":
                # Count how many 'continue' cells follow
                span = 1
                while row_idx + span < n_rows:
                    if col_idx >= len(grid[row_idx + span]):
                        break
                    next_cell = grid[row_idx + span][col_idx][1]
                    next_vmerge = get_cell_vmerge(next_cell)
                    if next_vmerge == "continue":
                        span += 1
                    else:
                        break
                rowspan_grid[row_idx][col_idx] = span
                row_idx += span
            elif vmerge == "continue":
                # This cell is part of a vertical merge, mark as 0 (skip)
                rowspan_grid[row_idx][col_idx] = 0
                row_idx += 1
            else:
                row_idx += 1

    # Build HTML
    html_parts = ["<table border='1'>"]

    for row_idx in range(n_rows):
        html_parts.append("<tr>")
        col_idx = 0
        unique_col_idx = 0  # Tracks unique tc index per row (matches XML <w:tc> order)

        while col_idx < len(grid[row_idx]):
            tc_id, cell, is_new = grid[row_idx][col_idx]

            # Skip if this cell is a horizontal continuation
            if not is_new:
                col_idx += 1
                continue

            # Skip if this cell is a vertical continuation
            rowspan = rowspan_grid[row_idx][col_idx]
            if rowspan == 0:
                unique_col_idx += 1
                col_idx += 1
                continue

            colspan = colspan_grid[row_idx][col_idx]

            # Build cell content
            if cell.tables:
                # Nested table
                content = "".join(
                    table2html(nested_table) for nested_table in cell.tables
                )
            else:
                content = cell.text.strip().replace("\n", "<br/>")

            # Append image descriptions if available
            if cell_image_map:
                img_desc = cell_image_map.get((row_idx, unique_col_idx))
                if img_desc:
                    content += f"<br/><em>{img_desc}</em>"

            # Build attributes
            attrs = []
            if colspan > 1:
                attrs.append(f'colspan="{colspan}"')
            if rowspan > 1:
                attrs.append(f'rowspan="{rowspan}"')

            attr_str = " " + " ".join(attrs) if attrs else ""
            html_parts.append(f"<td{attr_str}>{content}</td>")

            unique_col_idx += 1
            col_idx += colspan

        html_parts.append("</tr>")

    html_parts.append("</table>")
    return "".join(html_parts)


def render_multiindex_thead(columns: pd.MultiIndex, escape: bool = False) -> str:
    """
    Convert MultiIndex columns to HTML thead with colspan/rowspan.

    This function generates a proper multi-row <thead> structure where:
    - Horizontally adjacent identical values are merged with colspan
    - Vertically repeated values are merged with rowspan

    Args:
        columns: pandas MultiIndex representing the column headers
        escape: Whether to HTML-escape the cell values

    Returns:
        HTML string for the <thead> element
    """
    import html as html_lib

    n_levels = columns.nlevels
    n_cols = len(columns)

    # Build a 2D grid of values [level][col]
    grid = []
    for level in range(n_levels):
        row = [columns.get_level_values(level)[col] for col in range(n_cols)]
        grid.append(row)

    # Calculate colspan for each cell (horizontal merging)
    # colspan[level][col] = number of columns this cell spans
    colspan = [[1] * n_cols for _ in range(n_levels)]

    for level in range(n_levels):
        col = 0
        while col < n_cols:
            span = 1
            while col + span < n_cols and grid[level][col] == grid[level][col + span]:
                # Check if the parent cells also match (for correct hierarchical merging)
                parent_match = True
                for parent_level in range(level):
                    if grid[parent_level][col] != grid[parent_level][col + span]:
                        parent_match = False
                        break
                if parent_match:
                    span += 1
                else:
                    break
            colspan[level][col] = span
            col += span

    # Calculate rowspan for each cell (vertical merging)
    # A cell has rowspan > 1 if all cells in the same column below have the same value
    # AND if they would have the same colspan
    rowspan = [[1] * n_cols for _ in range(n_levels)]

    for col in range(n_cols):
        level = 0
        while level < n_levels:
            span = 1
            # Check if cells below have the same value AND same colspan
            while level + span < n_levels:
                if (
                    grid[level][col] == grid[level + span][col]
                    and colspan[level][col] == colspan[level + span][col]
                ):
                    span += 1
                else:
                    break
            rowspan[level][col] = span
            level += span

    # Build HTML rows
    # Track which cells are "covered" by rowspan from above
    covered = [[False] * n_cols for _ in range(n_levels)]

    html_parts = ["<thead>"]

    for level in range(n_levels):
        html_parts.append('<tr style="text-align: center;">')
        col = 0
        while col < n_cols:
            if covered[level][col]:
                # This cell is covered by a rowspan from above, skip it
                col += 1
                continue

            # Get cell value
            val = grid[level][col]
            val_str = str(val) if val is not None else ""
            if escape:
                val_str = html_lib.escape(val_str)

            # Get spans
            cs = colspan[level][col]
            rs = rowspan[level][col]

            # Mark covered cells
            for r_offset in range(rs):
                for c_offset in range(cs):
                    if r_offset > 0 or c_offset > 0:
                        if level + r_offset < n_levels and col + c_offset < n_cols:
                            covered[level + r_offset][col + c_offset] = True

            # Build th element with attributes
            attrs = []
            if cs > 1:
                attrs.append(f'colspan="{cs}"')
            if rs > 1:
                attrs.append(f'rowspan="{rs}"')

            attr_str = " " + " ".join(attrs) if attrs else ""
            html_parts.append(f"<th{attr_str}>{val_str}</th>")

            col += cs

        html_parts.append("</tr>")

    html_parts.append("</thead>")
    return "".join(html_parts)


def render_tbody_with_row_headers(
    tb_df: pd.DataFrame,
    row_header_cols: int = 0,
    na_rep: str = "—",
    escape: bool = False,
) -> str:
    """
    Render DataFrame body with support for row headers and cell merging.

    This function generates a proper <tbody> structure where:
    - Row header columns use <th scope="row"> instead of <td>
    - Horizontally adjacent identical values in row headers are merged with colspan
    - Vertically adjacent identical values in row headers are merged with rowspan
    - Merging respects hierarchical structure

    Args:
        tb_df: DataFrame to render
        row_header_cols: Number of leftmost columns to render as <th scope="row">
        na_rep: String representation for NaN values
        escape: Whether to HTML-escape values

    Returns:
        HTML string for the <tbody> element
    """
    import html as html_lib

    if row_header_cols <= 0:
        # No row headers - simple rendering without merging
        html_parts = ["<tbody>"]
        for _, row in tb_df.iterrows():
            html_parts.append("<tr>")
            for val in row:
                if pd.isna(val):
                    val_str = na_rep
                else:
                    val_str = str(val)
                if escape:
                    val_str = html_lib.escape(val_str)
                html_parts.append(f"<td>{val_str}</td>")
            html_parts.append("</tr>")
        html_parts.append("</tbody>")
        return "".join(html_parts)

    n_rows = len(tb_df)
    n_cols = len(tb_df.columns)

    if n_rows == 0:
        return "<tbody></tbody>"

    # Build 2D grid of values for row header columns
    # grid[row_idx][col_idx] = value
    grid = []
    for row_idx in range(n_rows):
        row_values = []
        for col_idx in range(row_header_cols):
            val = tb_df.iloc[row_idx, col_idx]
            if pd.isna(val):
                val = na_rep
            else:
                val = str(val)
            row_values.append(val)
        grid.append(row_values)

    # Calculate colspan for each cell (horizontal merging within same row)
    # colspan[row_idx][col_idx] = number of columns this cell spans
    colspan = [[1] * row_header_cols for _ in range(n_rows)]

    for row_idx in range(n_rows):
        col_idx = 0
        while col_idx < row_header_cols:
            span = 1
            while (
                col_idx + span < row_header_cols
                and grid[row_idx][col_idx] == grid[row_idx][col_idx + span]
            ):
                span += 1
            colspan[row_idx][col_idx] = span
            col_idx += span

    # Calculate rowspan for each cell (vertical merging)
    # Only calculate rowspan for cells that start a colspan group
    # rowspan[row_idx][col_idx] = number of rows this cell spans
    rowspan = [[1] * row_header_cols for _ in range(n_rows)]

    col_idx = 0
    while col_idx < row_header_cols:
        row_idx = 0
        while row_idx < n_rows:
            # Only process cells that start a colspan group (not covered by colspan from left)
            if col_idx > 0 and grid[row_idx][col_idx] == grid[row_idx][col_idx - 1]:
                row_idx += 1
                continue

            current_colspan = colspan[row_idx][col_idx]
            span = 1

            while row_idx + span < n_rows:
                # Check if the value matches
                if grid[row_idx][col_idx] != grid[row_idx + span][col_idx]:
                    break
                # Check if colspan in the next row also matches
                if colspan[row_idx + span][col_idx] != current_colspan:
                    break
                # Check if all parent columns (to the left) also have same rowspan behavior
                parent_match = True
                for parent_col in range(col_idx):
                    if grid[row_idx][parent_col] != grid[row_idx + span][parent_col]:
                        parent_match = False
                        break
                if parent_match:
                    span += 1
                else:
                    break

            rowspan[row_idx][col_idx] = span
            row_idx += span
        col_idx += 1

    # Track which cells are covered by rowspan from above or colspan from left
    covered = [[False] * row_header_cols for _ in range(n_rows)]

    # Mark cells covered by colspan (horizontal)
    for row_idx in range(n_rows):
        col_idx = 0
        while col_idx < row_header_cols:
            cs = colspan[row_idx][col_idx]
            for offset in range(1, cs):
                if col_idx + offset < row_header_cols:
                    covered[row_idx][col_idx + offset] = True
            col_idx += cs

    # Mark cells covered by rowspan (vertical)
    for row_idx in range(n_rows):
        for col_idx in range(row_header_cols):
            if covered[row_idx][col_idx]:
                continue  # Skip cells already covered by colspan
            rs = rowspan[row_idx][col_idx]
            for offset in range(1, rs):
                if row_idx + offset < n_rows:
                    # Mark all cells in the rowspan as covered
                    cs = colspan[row_idx][col_idx]
                    for c_offset in range(cs):
                        if col_idx + c_offset < row_header_cols:
                            covered[row_idx + offset][col_idx + c_offset] = True

    # Build HTML
    html_parts = ["<tbody>"]

    for row_idx in range(n_rows):
        html_parts.append("<tr>")

        # Render row header columns with rowspan/colspan
        for col_idx in range(row_header_cols):
            if covered[row_idx][col_idx]:
                # This cell is covered by a rowspan/colspan, skip it
                continue

            val_str = grid[row_idx][col_idx]
            if escape:
                val_str = html_lib.escape(val_str)

            rs = rowspan[row_idx][col_idx]
            cs = colspan[row_idx][col_idx]

            attrs = []
            if rs > 1:
                attrs.append(f'rowspan="{rs}"')
            if cs > 1:
                attrs.append(f'colspan="{cs}"')

            attr_str = " " + " ".join(attrs) if attrs else ""
            html_parts.append(f'<th scope="row"{attr_str}>{val_str}</th>')

        # Render data columns
        for col_idx in range(row_header_cols, n_cols):
            val = tb_df.iloc[row_idx, col_idx]
            if pd.isna(val):
                val_str = na_rep
            else:
                val_str = str(val)
            if escape:
                val_str = html_lib.escape(val_str)
            html_parts.append(f"<td>{val_str}</td>")

        html_parts.append("</tr>")

    html_parts.append("</tbody>")
    return "".join(html_parts)


def df2html(
    tb_df: pd.DataFrame,
    *,
    index: bool = False,
    classes: Union[str, List[str], None] = "table table-striped",
    na_rep: str = "—",
    escape: bool = False,
    row_header_cols: int = 0,
) -> str:
    """Convert DataFrame to HTML table.

    Supports:
    - MultiIndex columns with proper colspan/rowspan merging
    - Row headers (leftmost columns rendered as <th scope="row">)
    - Custom CSS classes and NA representation

    Args:
        tb_df: DataFrame to convert
        index: Whether to include the DataFrame index (not commonly used)
        classes: CSS classes to add to the table
        na_rep: String representation for NaN values
        escape: Whether to HTML-escape values
        row_header_cols: Number of leftmost columns to render as row headers (<th>).
                        These columns will use <th scope="row"> instead of <td>.

    Returns:
        HTML table string
    """
    class_str = (
        classes if isinstance(classes, str) else " ".join(classes) if classes else ""
    )

    # Check if columns are MultiIndex - use advanced rendering
    if isinstance(tb_df.columns, pd.MultiIndex):
        # Use specialized rendering for MultiIndex columns
        thead_html = render_multiindex_thead(tb_df.columns, escape=escape)
        tbody_html = render_tbody_with_row_headers(
            tb_df, row_header_cols, na_rep, escape
        )
        return f'<table class="dataframe {class_str}">{thead_html}{tbody_html}</table>'

    # Simple columns case
    if row_header_cols <= 0:
        # Use default pandas to_html for simple case without row headers
        table_html = tb_df.to_html(
            index=index,
            na_rep=na_rep,
            classes=classes,
            escape=escape,
            border=0,
            justify="center",
        )
        return table_html.replace("\n", "")

    # Simple columns with row headers - custom rendering
    import html as html_lib

    html_parts = [f'<table class="dataframe {class_str}">']

    # Build thead
    html_parts.append("<thead>")
    html_parts.append('<tr style="text-align: center;">')
    for col in tb_df.columns:
        col_str = str(col) if col is not None else ""
        if escape:
            col_str = html_lib.escape(col_str)
        html_parts.append(f"<th>{col_str}</th>")
    html_parts.append("</tr>")
    html_parts.append("</thead>")

    # Build tbody with row headers
    tbody_html = render_tbody_with_row_headers(tb_df, row_header_cols, na_rep, escape)
    html_parts.append(tbody_html)

    html_parts.append("</table>")

    return "".join(html_parts)
