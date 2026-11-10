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

from app.services.document_parser.formats.excel.formula_graph import (  # noqa: E402
    build_formula_graph,
)
from app.services.document_parser.formats.excel.luna_decisions import (  # noqa: E402
    evaluate_sheet_blocks,
)
from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    Region,
    load_workbook_grid,
)


class _RecordingJudge:
    def __init__(self) -> None:
        self.states: list[object] = []
        self.questions: list[object] = []

    def choose(self, *, state, questions, required_ids, **kwargs):
        self.states.append(state)
        self.questions.append(questions)
        return {question_id: "separate" for question_id in required_ids}


def _save(path: Path, workbook: Workbook) -> Path:
    workbook.save(path)
    workbook.close()
    return path


def test_large_center_preview_omits_interior_rows_and_cols(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Wide"
    for row in range(1, 13):
        for col in range(1, 13):
            sheet.cell(row, col, value=f"R{row}C{col}")
    sheet["N1"] = "Side"
    for row in range(2, 13):
        sheet.cell(row, 14, value=row)
    path = _save(tmp_path / "wide.xlsx", workbook)

    grid = load_workbook_grid(str(path)).sheets[0]
    judge = _RecordingJudge()
    evaluate_sheet_blocks(
        grid,
        [
            Region(min_row=1, min_col=1, max_row=12, max_col=12),
            Region(min_row=1, min_col=14, max_row=12, max_col=14),
        ],
        formula_nodes=(),
        judge=judge,
    )
    assert judge.states
    center = str(judge.states[0]["center"])
    assert "omitted" in center
    assert "..." in center
    assert "R1C1" in center
    assert "R12C12" in center
    assert "R6C6" not in center
    assert judge.questions
    right = str(judge.questions[0]["merge::right"]["instructions"])
    assert "R1C1" in right
    assert "Side" in right
    assert "A1:" not in right


def test_one_column_strip_is_never_a_merge_center(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["C1"] = "Extra"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["C2"] = 1
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["C3"] = 2
    sheet["A4"] = "West"
    sheet["B4"] = 12
    sheet["C4"] = 3
    sheet["E1"] = 1
    sheet["E3"] = 1
    sheet["E4"] = 1
    path = _save(tmp_path / "center.xlsx", workbook)

    grid = load_workbook_grid(str(path)).sheets[0]
    judge = _RecordingJudge()
    evaluate_sheet_blocks(
        grid,
        [
            Region(min_row=1, min_col=1, max_row=4, max_col=3),
            Region(min_row=1, min_col=5, max_row=4, max_col=5),
        ],
        formula_nodes=(),
        judge=judge,
    )
    assert judge.states
    center = str(judge.states[0]["center"])
    assert "Name" in center
    assert "North" in center


class _ExplodingJudge:
    def choose(self, **kwargs):
        raise AssertionError("Luna must not be asked")


def test_formula_range_spanning_blocks_merges_without_luna(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["A5"] = "South"
    sheet["B5"] = 20
    sheet["A6"] = "Total"
    sheet["C6"] = "=SUM(B2:B5)"
    path = _save(tmp_path / "span.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    classified = evaluate_sheet_blocks(
        grid.sheets[0],
        [
            Region(min_row=1, min_col=1, max_row=3, max_col=2),
            Region(min_row=5, min_col=1, max_row=6, max_col=3),
        ],
        formula_nodes=build_formula_graph(grid),
        judge=_ExplodingJudge(),
    )
    assert [item.region for item in classified] == [
        Region(min_row=1, min_col=1, max_row=6, max_col=3)
    ]


def test_neighbor_sticking_out_of_center_edge_is_not_asked(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["C1"] = "Extra"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["C2"] = 1
    sheet["B4"] = "Note"
    sheet["D4"] = 5
    path = _save(tmp_path / "staggered.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    classified = evaluate_sheet_blocks(
        grid.sheets[0],
        [
            Region(min_row=1, min_col=1, max_row=2, max_col=3),
            Region(min_row=4, min_col=2, max_row=4, max_col=4),
        ],
        formula_nodes=build_formula_graph(grid),
        judge=_ExplodingJudge(),
    )
    assert [item.region for item in classified] == [
        Region(min_row=1, min_col=1, max_row=2, max_col=3),
        Region(min_row=4, min_col=2, max_row=4, max_col=4),
    ]


def test_narrower_nested_neighbor_is_asked(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["C1"] = "Note"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A4"] = "South"
    sheet["B4"] = 20
    path = _save(tmp_path / "nested.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    judge = _RecordingJudge()
    evaluate_sheet_blocks(
        grid.sheets[0],
        [
            Region(min_row=1, min_col=1, max_row=2, max_col=3),
            Region(min_row=4, min_col=1, max_row=4, max_col=2),
        ],
        formula_nodes=build_formula_graph(grid),
        judge=judge,
    )
    assert list(judge.questions[0]) == ["merge::down"]
