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
from app.services.document_parser.formats.excel.region_split import (  # noqa: E402
    ClassifiedRegion,
)
from app.services.document_parser.formats.excel.satellite_merge import (  # noqa: E402
    resolve_formula_satellites_and_edges,
    table_key,
)
from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    Region,
    load_workbook_grid,
)


def _save(path: Path, workbook: Workbook) -> Path:
    workbook.save(path)
    workbook.close()
    return path


def test_sum_fragment_becomes_tfoot_on_host_table(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["B5"] = "=SUM(B2:B3)"
    path = _save(tmp_path / "sum.xlsx", workbook)

    workbook_grid = load_workbook_grid(str(path))
    table = ClassifiedRegion(
        region=Region(min_row=1, min_col=1, max_row=3, max_col=2),
        role="table",
    )
    fragment = ClassifiedRegion(
        region=Region(min_row=5, min_col=2, max_row=5, max_col=2),
        role="fragment",
    )
    plan = resolve_formula_satellites_and_edges(
        workbook_grid,
        {"Data": [table, fragment]},
        build_formula_graph(workbook_grid),
    )
    assert len(plan.tables) == 1
    assert plan.tables[0].tfoot_rows == (5,)
    assert plan.tables[0].satellites == ()


def test_cross_sheet_kpi_hangs_as_satellite(tmp_path: Path) -> None:
    workbook = Workbook()
    data = workbook.active
    data.title = "Data"
    data["A1"] = "Name"
    data["B1"] = "Amount"
    data["A2"] = "North"
    data["B2"] = 10
    data["A3"] = "East"
    data["B3"] = 20
    summary = workbook.create_sheet("Summary")
    summary["A1"] = "Gross Margin"
    summary["B1"] = "=(Data!B2-Data!B3)/Data!B2"
    path = _save(tmp_path / "kpi.xlsx", workbook)

    workbook_grid = load_workbook_grid(str(path))
    data_table = ClassifiedRegion(
        region=Region(min_row=1, min_col=1, max_row=3, max_col=2),
        role="table",
    )
    kpi = ClassifiedRegion(
        region=Region(min_row=1, min_col=1, max_row=1, max_col=2),
        role="fragment",
    )
    plan = resolve_formula_satellites_and_edges(
        workbook_grid,
        {"Data": [data_table], "Summary": [kpi]},
        build_formula_graph(workbook_grid),
    )
    assert len(plan.tables) == 1
    assert plan.tables[0].sheet_name == "Data"
    assert plan.tables[0].tfoot_rows == ()
    assert any("Gross Margin" in item for item in plan.tables[0].satellites)


def test_fragment_can_belong_to_two_tables(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["D1"] = "Name"
    sheet["E1"] = "Amount"
    sheet["D2"] = "South"
    sheet["E2"] = 20
    sheet["C4"] = "=B2+E2"
    path = _save(tmp_path / "dual.xlsx", workbook)

    workbook_grid = load_workbook_grid(str(path))
    left = ClassifiedRegion(
        region=Region(min_row=1, min_col=1, max_row=2, max_col=2),
        role="table",
    )
    right = ClassifiedRegion(
        region=Region(min_row=1, min_col=4, max_row=2, max_col=5),
        role="table",
    )
    fragment = ClassifiedRegion(
        region=Region(min_row=4, min_col=3, max_row=4, max_col=3),
        role="fragment",
    )
    plan = resolve_formula_satellites_and_edges(
        workbook_grid,
        {"Data": [left, right, fragment]},
        build_formula_graph(workbook_grid),
    )
    assert len(plan.tables) == 2
    assert all(item.satellites for item in plan.tables)


def test_vlookup_between_tables_emits_formula_links(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Rate"
    sheet["A2"] = "USD"
    sheet["B2"] = 7
    sheet["D1"] = "Name"
    sheet["E1"] = "Amount"
    sheet["D2"] = "USD"
    sheet["E2"] = "=VLOOKUP(D2,A1:B2,2,FALSE)"
    path = _save(tmp_path / "vlookup.xlsx", workbook)

    workbook_grid = load_workbook_grid(str(path))
    rates = ClassifiedRegion(
        region=Region(min_row=1, min_col=1, max_row=2, max_col=2),
        role="table",
    )
    sales = ClassifiedRegion(
        region=Region(min_row=1, min_col=4, max_row=2, max_col=5),
        role="table",
    )
    plan = resolve_formula_satellites_and_edges(
        workbook_grid,
        {"Data": [rates, sales]},
        build_formula_graph(workbook_grid),
    )
    assert len(plan.tables) == 2
    assert len(plan.links) == 1
    link = plan.links[0]
    assert link.source_key == table_key("Data", sales.region)
    assert link.target_key == table_key("Data", rates.region)
    assert "VLOOKUP" in link.formula
