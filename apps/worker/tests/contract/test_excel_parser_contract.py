from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch


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
    from app.services.document_parser.parse_service import checkerboard_inject_parse

    workbook_path = tmp_path / "budget.xlsx"
    _write_contract_workbook(workbook_path)

    full_output_dir, parsed_df = checkerboard_inject_parse(
        file_full_path=str(workbook_path),
        filename="budget.xlsx",
        output_dir=str(tmp_path),
        internal_output_filename="budget.xlsx",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    assert full_output_dir.endswith("Default_Root/budget.xlsx")
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


def test_xlsx_parser_contract_accepts_missing_llm_parameters(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.excel_table_parser import parse_xlsx

    workbook_path = tmp_path / "default-parameters.xlsx"
    output_dir = tmp_path / "output"
    _write_contract_workbook(workbook_path)

    parsed_df = parse_xlsx(
        file_path=str(workbook_path),
        file_name="default-parameters.xlsx",
        output_dir=str(output_dir),
        baseurl="",
        base_llm_paras=None,
    )

    assert parsed_df["type"].tolist() == ["table"]
    assert parsed_df["path"].tolist() == ["tables/table-Visible.html"]
    assert (output_dir / "tables" / "table-Visible.html").exists()


def test_xlsx_parser_contract_falls_back_to_column_keywords_when_llm_summary_is_empty(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.services.document_parser.txt_parser as txt_parser
    from app.services.document_parser.excel_table_parser import parse_xlsx

    workbook_path = tmp_path / "empty-summary.xlsx"
    output_dir = tmp_path / "output"
    _write_contract_workbook(workbook_path)

    monkeypatch.setattr(
        txt_parser,
        "extract_title_keywords_summary",
        lambda *_args, **_kwargs: (None, "", ""),
    )

    parsed_df = parse_xlsx(
        file_path=str(workbook_path),
        file_name="empty-summary.xlsx",
        output_dir=str(output_dir),
        baseurl="",
        base_llm_paras={"summary_table": True, "stopwords": []},
    )

    keywords = str(parsed_df["keywords"].iloc[0])
    content = str(parsed_df["content"].iloc[0])

    assert "Region" in keywords
    assert "Value" in keywords
    assert "Main columns:" in content
    assert "Region" in content
    assert "Value" in content
