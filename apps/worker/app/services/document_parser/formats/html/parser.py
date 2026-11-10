# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false
"""HTML table helpers for Markdown table extraction."""

from bs4 import BeautifulSoup


def merge_html_tables(lines: list) -> list:
    """Merge multi-line HTML tables into single lines.

    Some parsers output HTML tables with line breaks inside.
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
    from app.services.document_parser.support.text_helpers import truncate_text

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
