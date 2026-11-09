from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.formats.excel.header_detect import (  # noqa: E402
    HeaderSpec,
    TableRegion,
    VisualCell,
    detect_headers,
    expand_column_headers,
    render_region_rows_text,
)


def _cell(
    text: str,
    *,
    rowspan: int = 1,
    colspan: int = 1,
    origin_row: int,
    origin_col: int,
) -> VisualCell:
    return VisualCell(
        text=text,
        rowspan=rowspan,
        colspan=colspan,
        origin_row=origin_row,
        origin_col=origin_col,
    )


def test_render_region_rows_text_marks_merge_continuations() -> None:
    visual = (
        (
            _cell("Item", origin_row=0, origin_col=0),
            _cell("2024", colspan=2, origin_row=0, origin_col=1),
            _cell("2024", colspan=2, origin_row=0, origin_col=1),
        ),
        (
            _cell("", origin_row=1, origin_col=0),
            _cell("Q1", origin_row=1, origin_col=1),
            _cell("Q2", origin_row=1, origin_col=2),
        ),
    )
    rows_text, rowspan_hint = render_region_rows_text(visual)
    assert rowspan_hint == 1
    assert "[colspan-continued]" in rows_text
    assert 'cs=2' in rows_text
    assert 'Row 0 (3 cells, U):' in rows_text
    assert 'Row 1 (3 cells, U):' in rows_text


def test_expand_column_headers_joins_parent_to_child() -> None:
    visual = (
        (
            _cell("Item", origin_row=0, origin_col=0),
            _cell("2024", colspan=2, origin_row=0, origin_col=1),
            _cell("2024", colspan=2, origin_row=0, origin_col=1),
        ),
        (
            _cell("", origin_row=1, origin_col=0),
            _cell("Q1", origin_row=1, origin_col=1),
            _cell("Q2", origin_row=1, origin_col=2),
        ),
    )
    assert expand_column_headers(visual, 2) == [
        "Item",
        "2024-->Q1",
        "2024-->Q2",
    ]
    assert expand_column_headers(visual, 0) == []


def test_detect_headers_uses_llm_triple() -> None:
    visual = (
        (
            _cell("Name", origin_row=0, origin_col=0),
            _cell("Score", origin_row=0, origin_col=1),
        ),
        (
            _cell("Alice", origin_row=1, origin_col=0),
            _cell("90", origin_row=1, origin_col=1),
        ),
    )

    class _Stub:
        def detect_spreadsheet_headers(
            self, rows_text: str, rowspan_hint: int
        ) -> tuple[int, int, int] | None:
            assert "Row 0" in rows_text
            return (1, 1, 1)

    spec = detect_headers(TableRegion(visual=visual), llm=_Stub())
    assert spec == HeaderSpec(
        header_row_count=1,
        row_header_col_count=1,
        column_headers=("Name", "Score"),
        caption=None,
        data_end_row=1,
    )


def test_detect_headers_llm_failure_uses_one_header_row_to_last_row() -> None:
    visual = (
        (
            _cell("2024", origin_row=0, origin_col=0),
            _cell("Amount", origin_row=0, origin_col=1),
        ),
        (
            _cell("North", origin_row=1, origin_col=0),
            _cell("10", origin_row=1, origin_col=1),
        ),
        (
            _cell("South", origin_row=2, origin_col=0),
            _cell("20", origin_row=2, origin_col=1),
        ),
    )

    class _Fail:
        def detect_spreadsheet_headers(
            self, rows_text: str, rowspan_hint: int
        ) -> tuple[int, int, int] | None:
            return None

    spec = detect_headers(TableRegion(visual=visual), llm=_Fail())
    assert spec.header_row_count == 1
    assert spec.row_header_col_count == 0
    assert spec.data_end_row == 2
    assert spec.column_headers == ("2024", "Amount")


def test_detect_headers_rejects_out_of_range_triple() -> None:
    visual = (
        (
            _cell("A", origin_row=0, origin_col=0),
            _cell("B", origin_row=0, origin_col=1),
        ),
        (
            _cell("1", origin_row=1, origin_col=0),
            _cell("2", origin_row=1, origin_col=1),
        ),
    )

    class _Bad:
        def detect_spreadsheet_headers(
            self, rows_text: str, rowspan_hint: int
        ) -> tuple[int, int, int] | None:
            return (1, 0, 9)

    spec = detect_headers(TableRegion(visual=visual), llm=_Bad())
    assert spec.header_row_count == 1
    assert spec.row_header_col_count == 0
    assert spec.data_end_row == 1


def test_detect_headers_forces_row_header_count_zero_when_no_column_header() -> None:
    visual = (
        (
            _cell("Alice", origin_row=0, origin_col=0),
            _cell("90", origin_row=0, origin_col=1),
        ),
        (
            _cell("Bob", origin_row=1, origin_col=0),
            _cell("80", origin_row=1, origin_col=1),
        ),
    )

    class _Stub:
        def detect_spreadsheet_headers(
            self, rows_text: str, rowspan_hint: int
        ) -> tuple[int, int, int] | None:
            return (0, 2, 1)

    spec = detect_headers(TableRegion(visual=visual), llm=_Stub())
    assert spec.header_row_count == 0
    assert spec.row_header_col_count == 0
    assert spec.column_headers == ()
    assert spec.data_end_row == 1
