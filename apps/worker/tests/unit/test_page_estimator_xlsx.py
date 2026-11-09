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

from app.services.document_ingestion.page_estimator import (  # noqa: E402
    CELLS_PER_PAGE,
    PageEstimator,
)


def _save_workbook(path: Path, workbook: Workbook) -> str:
    workbook.save(path)
    workbook.close()
    return str(path)


def _fill_cells(worksheet, count: int, *, start_row: int = 1) -> None:
    for index in range(count):
        worksheet.cell(row=start_row + index, column=1, value=index + 1)


def test_empty_workbook_bills_one_page(tmp_path: Path) -> None:
    path = tmp_path / "empty.xlsx"
    estimate = PageEstimator.estimate_workload(_save_workbook(path, Workbook()))
    assert estimate.used_fallback is False
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 1


def test_thousand_cells_bill_one_page(tmp_path: Path) -> None:
    workbook = Workbook()
    _fill_cells(workbook.active, CELLS_PER_PAGE)
    estimate = PageEstimator.estimate_workload(
        _save_workbook(tmp_path / "one-page.xlsx", workbook)
    )
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 1


def test_thousand_and_one_cells_bill_two_pages(tmp_path: Path) -> None:
    workbook = Workbook()
    _fill_cells(workbook.active, CELLS_PER_PAGE + 1)
    estimate = PageEstimator.estimate_workload(
        _save_workbook(tmp_path / "two-pages.xlsx", workbook)
    )
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 2


def test_two_visible_sheets_sum_cells_not_sheet_count(tmp_path: Path) -> None:
    workbook = Workbook()
    _fill_cells(workbook.active, 600)
    second = workbook.create_sheet("second")
    _fill_cells(second, 600)
    estimate = PageEstimator.estimate_workload(
        _save_workbook(tmp_path / "two-sheets.xlsx", workbook)
    )
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 2


def test_csv_cells_bill_as_xlsx_cells(tmp_path: Path) -> None:
    lines = ["left,right"] + [f"{index},x" for index in range(CELLS_PER_PAGE)]
    source = tmp_path / "cells.csv"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    estimate = PageEstimator.estimate_workload(str(source))
    assert estimate.used_fallback is False
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 3


def test_tsv_cells_are_not_unknown_file_type(tmp_path: Path) -> None:
    source = tmp_path / "budget.tsv"
    source.write_text("Region\tValue\nNorth\t10\n", encoding="utf-8")
    estimate = PageEstimator.estimate_workload(str(source))
    assert estimate.used_fallback is False
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 1


def test_hidden_sheet_cells_are_not_billed(tmp_path: Path) -> None:
    workbook = Workbook()
    workbook.active["A1"] = "visible"
    hidden = workbook.create_sheet("secret")
    hidden.sheet_state = "hidden"
    _fill_cells(hidden, CELLS_PER_PAGE + 50)
    estimate = PageEstimator.estimate_workload(
        _save_workbook(tmp_path / "hidden.xlsx", workbook)
    )
    assert estimate.method == "xlsx_cells"
    assert estimate.page_count == 1


