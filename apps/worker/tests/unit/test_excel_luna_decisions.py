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

    def choose(self, *, state, required_ids, **kwargs):
        self.states.append(state)
        answers = {}
        for question_id in required_ids:
            if question_id.startswith("merge::"):
                answers[question_id] = "separate"
            else:
                answers[question_id] = "table"
        return answers


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
    sheet["N2"] = 1
    path = _save(tmp_path / "wide.xlsx", workbook)

    grid = load_workbook_grid(str(path)).sheets[0]
    judge = _RecordingJudge()
    evaluate_sheet_blocks(
        grid,
        [
            Region(min_row=1, min_col=1, max_row=12, max_col=12),
            Region(min_row=1, min_col=14, max_row=2, max_col=14),
        ],
        judge=judge,
    )
    assert judge.states
    center = judge.states[0]["center"]
    grid_text = str(center["grid"])
    assert "omitted" in grid_text
    assert "..." in grid_text
    assert center["omitted_rows"] == 2
    assert center["omitted_cols"] == 2
    assert "R1C1" in grid_text
    assert "R12C12" in grid_text
    assert "R6C6" not in grid_text
