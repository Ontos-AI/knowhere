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
    HeaderStart,
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
    rows_text = render_region_rows_text(visual)
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


def test_detect_headers_uses_llm_pair() -> None:
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
        def locate_header_start(self, rows_text: str) -> HeaderStart | None:
            return HeaderStart(row=0)

        def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
            assert "Row 0" in rows_text
            return (1, 1)

    spec = detect_headers(TableRegion(visual=visual), llm=_Stub())
    assert spec == HeaderSpec(
        header_start_row=0,
        header_row_count=1,
        row_header_col_count=1,
        column_headers=("Name", "Score"),
        caption=None,
    )


def test_detect_headers_llm_failure_uses_one_header_row() -> None:
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
        def locate_header_start(self, rows_text: str) -> HeaderStart | None:
            return HeaderStart(row=0)

        def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
            return None

    spec = detect_headers(TableRegion(visual=visual), llm=_Fail())
    assert spec.header_row_count == 1
    assert spec.row_header_col_count == 0
    assert spec.column_headers == ("2024", "Amount")


def test_detect_headers_rejects_out_of_range_pair() -> None:
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
        def locate_header_start(self, rows_text: str) -> HeaderStart | None:
            return HeaderStart(row=0)

        def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
            return (9, 0)

    spec = detect_headers(TableRegion(visual=visual), llm=_Bad())
    assert spec.header_row_count == 1
    assert spec.row_header_col_count == 0


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
        def locate_header_start(self, rows_text: str) -> HeaderStart | None:
            return HeaderStart(row=0)

        def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
            return (0, 2)

    spec = detect_headers(TableRegion(visual=visual), llm=_Stub())
    assert spec.header_row_count == 0
    assert spec.row_header_col_count == 0
    assert spec.column_headers == ()


def _grid(rows: list[list[str]]) -> tuple[tuple[VisualCell | None, ...], ...]:
    return tuple(
        tuple(
            _cell(text, origin_row=row_index, origin_col=col_index)
            for col_index, text in enumerate(row)
        )
        for row_index, row in enumerate(rows)
    )


class _Recorder:
    def __init__(self, start: HeaderStart | None, pair: tuple[int, int] | None) -> None:
        self.start = start
        self.pair = pair
        self.locate_texts: list[str] = []
        self.count_texts: list[str] = []

    def locate_header_start(self, rows_text: str) -> HeaderStart | None:
        self.locate_texts.append(rows_text)
        return self.start

    def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
        self.count_texts.append(rows_text)
        return self.pair


def test_header_start_skips_title_and_parameter_rows() -> None:
    visual = _grid(
        [
            ["Pile Report", "", ""],
            ["Cover", "0.05", ""],
            ["No", "Type", "Depth"],
            ["1", "ZH1", "15"],
        ]
    )
    recorder = _Recorder(HeaderStart(row=2), (1, 1))
    spec = detect_headers(TableRegion(visual=visual), llm=recorder)
    assert spec.header_start_row == 2
    assert spec.header_row_count == 1
    assert spec.column_headers == ("No", "Type", "Depth")
    assert recorder.count_texts[0].startswith('Row 0 (3 cells, U): "No"')


def test_rule_a_lifts_leaf_header_row_to_first_column_merge_top() -> None:
    visual = (
        (
            _cell("Note", origin_row=0, origin_col=0),
            _cell("", origin_row=0, origin_col=1),
        ),
        (
            _cell("No", rowspan=2, origin_row=1, origin_col=0),
            _cell("Size", origin_row=1, origin_col=1),
        ),
        (
            _cell("No", rowspan=2, origin_row=1, origin_col=0),
            _cell("Diameter", origin_row=2, origin_col=1),
        ),
        (
            _cell("1", origin_row=3, origin_col=0),
            _cell("2.1", origin_row=3, origin_col=1),
        ),
    )
    spec = detect_headers(
        TableRegion(visual=visual), llm=_Recorder(HeaderStart(row=2), (2, 0))
    )
    assert spec.header_start_row == 1
    assert spec.column_headers == ("No", "Size-->Diameter")
    assert spec.row_header_col_count == 1


def test_no_list_head_skips_header_count() -> None:
    visual = _grid([["Alice", "90"], ["Bob", "80"]])
    recorder = _Recorder(HeaderStart(row=None), (1, 1))
    spec = detect_headers(TableRegion(visual=visual), llm=recorder)
    assert spec.header_start_row == 0
    assert spec.header_row_count == 0
    assert spec.row_header_col_count == 0
    assert recorder.count_texts == []


def test_locate_failure_or_out_of_window_row_starts_at_region_top() -> None:
    visual = _grid([[f"R{row}", str(row)] for row in range(30)])
    for start in (None, HeaderStart(row=25)):
        spec = detect_headers(
            TableRegion(visual=visual), llm=_Recorder(start, (1, 0))
        )
        assert spec.header_start_row == 0


def test_header_windows_hold_ten_rows_and_extend_over_crossing_merge() -> None:
    plain = _grid([[f"R{row}", str(row)] for row in range(30)])
    recorder = _Recorder(HeaderStart(row=0), (1, 0))
    detect_headers(TableRegion(visual=plain), llm=recorder)
    assert "Row 9 " in recorder.locate_texts[0]
    assert "Row 10 " not in recorder.locate_texts[0]

    rows = [list(row) for row in plain]
    rows[9][0] = _cell("Group", rowspan=3, origin_row=9, origin_col=0)
    rows[10][0] = _cell("Group", rowspan=3, origin_row=9, origin_col=0)
    rows[11][0] = _cell("Group", rowspan=3, origin_row=9, origin_col=0)
    merged = tuple(tuple(row) for row in rows)
    recorder = _Recorder(HeaderStart(row=0), (1, 0))
    detect_headers(TableRegion(visual=merged), llm=recorder)
    assert "Row 11 " in recorder.locate_texts[0]
    assert "Row 12 " not in recorder.locate_texts[0]
