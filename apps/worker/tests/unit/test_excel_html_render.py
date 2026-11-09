from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from openpyxl import Workbook  # noqa: E402

from app.services.document_parser.formats.excel.header_detect import (  # noqa: E402
    HeaderSpec,
)
from app.services.document_parser.formats.excel.html_render import (  # noqa: E402
    render_table_html,
    visible_columns,
)
from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    Region,
    load_workbook_grid,
)


def _save(path: Path, workbook: Workbook) -> Path:
    workbook.save(path)
    workbook.close()
    return path


def test_render_table_html_keeps_merge_formula_and_tfoot(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "P&L"
    sheet.merge_cells("B1:C1")
    sheet["A1"] = "Item"
    sheet["B1"] = 2024
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["C2"] = "=B2"
    path = _save(tmp_path / "merge-formula.xlsx", workbook)

    grid = load_workbook_grid(str(path)).sheets[0]
    region = Region(min_row=1, min_col=1, max_row=2, max_col=3)
    html = render_table_html(
        grid,
        region,
        HeaderSpec(
            header_row_count=1,
            row_header_col_count=1,
            column_headers=("Item", "2024", ""),
            caption=None,
            data_end_row=1,
        ),
    )
    assert 'data-sheet="P&amp;L"' in html
    assert 'data-range="A1:C2"' in html
    assert 'colspan="2"' in html
    assert 'data-formula="=B2"' in html
    assert 'scope="row"' in html
    assert "<tfoot></tfoot>" in html
    assert "<thead>" in html
    assert "<tbody>" in html


def test_visible_columns_omit_hidden_empty(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "序号"
    sheet["B1"] = "单价"
    sheet.column_dimensions["C"].hidden = True
    sheet["D1"] = "合价"
    sheet["A2"] = 1
    sheet["B2"] = 2.5
    sheet["D2"] = 100.0
    path = _save(tmp_path / "hidden.xlsx", workbook)

    grid = load_workbook_grid(str(path)).sheets[0]
    region = Region(min_row=1, min_col=1, max_row=2, max_col=4)
    assert visible_columns(grid, region) == (1, 2, 4)
    html = render_table_html(
        grid,
        region,
        HeaderSpec(
            header_row_count=1,
            row_header_col_count=1,
            column_headers=("序号", "单价", "", "合价"),
            caption="工程量清单",
            data_end_row=1,
        ),
    )
    assert "<caption>工程量清单</caption>" in html
    assert "<th>序号</th><th>单价</th><th>合价</th>" in html
    assert "<td>100</td>" in html
