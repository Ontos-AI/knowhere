from __future__ import annotations

import os
from pathlib import Path


def _write_contract_workbook(workbook_path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    visible_sheet = workbook.active
    visible_sheet.title = "Visible"
    visible_sheet["A1"] = "Region"
    visible_sheet["B1"] = "Value"
    visible_sheet["A2"] = "North"
    visible_sheet["B2"] = 10

    hidden_sheet = workbook.create_sheet("Hidden")
    hidden_sheet.sheet_state = "hidden"
    hidden_sheet["A1"] = "Secret"
    hidden_sheet["B1"] = "Value"
    hidden_sheet["A2"] = "Hidden"
    hidden_sheet["B2"] = 99

    workbook.save(workbook_path)


def test_xlsx_parser_contract_uses_stable_entrypoint_and_ignores_hidden_sheets(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.parse_service import checkerboard_parse_output

    workbook_path = tmp_path / "budget.xlsx"
    output_root = tmp_path / "parser-output"
    _write_contract_workbook(workbook_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(workbook_path),
        filename="budget.xlsx",
        output_dir=str(output_root),
        internal_output_filename="budget.xlsx",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df
    assert full_output_dir.endswith("budget.xlsx")
    assert parsed_df is not None
    assert parsed_df["type"].tolist() == ["table"]
    assert parsed_df["path"].tolist() == ["tables/table-Visible.html"]
    assert parsed_df["summary"].tolist() == ["table-Visible"]
    assert "Region" in parsed_df["keywords"].iloc[0]
    assert "Value" in parsed_df["keywords"].iloc[0]

    table_html = Path(full_output_dir) / "tables" / "table-Visible.html"
    table_html_text = table_html.read_text(encoding="utf-8")

    assert "North" in table_html_text
    assert "10" in table_html_text
    assert "Secret" not in table_html_text
    assert "Hidden" not in table_html_text


def test_parser_maps_document_name_to_task_local_path_segment(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.parse_service import checkerboard_parse_output

    workbook_path = tmp_path / "budget.xlsx"
    output_root = tmp_path / "parser-output"
    _write_contract_workbook(workbook_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(workbook_path),
        filename="/tmp/../images.xlsx",
        output_dir=str(output_root),
        internal_output_filename="../../images.xlsx",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df
    assert (
        os.path.commonpath([str(output_root.resolve()), full_output_dir])
        == str(output_root.resolve())
    )
    assert full_output_dir.endswith("images.xlsx")
    assert parsed_df is not None
    assert parsed_df["path"].tolist() == ["tables/table-Visible.html"]
