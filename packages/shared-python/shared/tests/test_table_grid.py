from __future__ import annotations

from shared.services.retrieval.hydration.table_grid import (
    grid_sql_rows,
    html_to_grid,
    render_explore_table,
    sql_columns,
)
from shared.services.retrieval.settings import LARGE_TABLE_AXIS

FLAT_HTML = (
    "<table>"
    "<thead>"
    '<tr><th colspan="2">2024</th></tr>'
    "<tr><th>Q1</th><th>Q2</th></tr>"
    "</thead>"
    "<tbody>"
    '<tr><th scope="row">North</th><td>10</td><td>20</td></tr>'
    "</tbody>"
    "</table>"
)

FLAT_METADATA = {
    "header_row_count": 2,
    "row_header_col_count": 1,
    "column_headers": ["Item", "2024-->Q1", "2024-->Q2"],
}


def test_sql_columns_without_metadata_keeps_first_row_mapping() -> None:
    grid = [["Dose", "Unit"], ["30 mg", "tablet"]]
    assert sql_columns(grid) == ["row_header", "Unit"]
    assert grid_sql_rows(grid) == [["30 mg", "tablet"]]


def test_sql_columns_uses_flattened_headers_and_skips_header_rows() -> None:
    grid = html_to_grid(FLAT_HTML)
    columns = sql_columns(grid, FLAT_METADATA)
    rows = grid_sql_rows(grid, FLAT_METADATA)
    assert columns[0] == "row_header"
    assert "c_2024___Q1" in columns
    assert "c_2024___Q2" in columns
    assert rows == [["North", "10", "20"]]


def test_sql_columns_header_row_count_zero_uses_col_index() -> None:
    grid = [["Alice", "90"], ["Bob", "80"]]
    metadata = {
        "header_row_count": 0,
        "row_header_col_count": 0,
        "column_headers": [],
    }
    assert sql_columns(grid, metadata) == ["col_0", "col_1"]
    assert grid_sql_rows(grid, metadata) == [["Alice", "90"], ["Bob", "80"]]


def test_render_explore_table_large_preview_uses_flattened_headers() -> None:
    parts = ["<table>"]
    parts.append(
        "<tr>" + "".join(f"<th>H{col}</th>" for col in range(LARGE_TABLE_AXIS)) + "</tr>"
    )
    parts.append(
        "<tr>" + "".join(f"<td>R1C{col}</td>" for col in range(LARGE_TABLE_AXIS)) + "</tr>"
    )
    parts.append("</table>")
    text = render_explore_table(
        {
            "chunk_id": "chunk_flat",
            "document_id": "doc_a",
            "content": "".join(parts),
            "chunk_metadata": {
                "header_row_count": 1,
                "row_header_col_count": 0,
                "column_headers": ["Year-->Q1", "Year-->Q2", "Year-->Q3"],
            },
        }
    )
    assert "too large" in text
    assert "Year-->Q1" in text
    assert "Year-->Q2" in text
    assert "H0" not in text
