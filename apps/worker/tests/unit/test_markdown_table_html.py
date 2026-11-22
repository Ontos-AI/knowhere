from __future__ import annotations

import os
import re

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.formats.markdown.parser import (  # noqa: E402
    clean_md_table_lines,
)
from app.services.document_parser.tables.table_text_parser import (  # noqa: E402
    extract_tables_by_forms,
    split_md_table_row,
)


def _cells(table_html: str | None) -> list[str]:
    assert table_html is not None
    return re.findall(r"<t[hd]>(.*?)</t[hd]>", table_html)


def _render(lines: list[str]) -> list[str]:
    cleaned, _errors = clean_md_table_lines(lines, start_line_num=0)
    return _cells(extract_tables_by_forms("\n".join(cleaned), form="md"))


def test_empty_and_na_cells_stay_as_written() -> None:
    assert _render(["|a|b|", "|---|---|", "|N/A|null|", "|NA||"]) == [
        "a", "b", "N/A", "null", "NA", "",
    ]


def test_numbers_keep_their_written_form() -> None:
    assert _render(["|id|amt|", "|---|---|", "|001|1.10|", "||12345678901|"]) == [
        "id", "amt", "001", "1.10", "", "12345678901",
    ]
    assert _render(["|id|amt|", "|1|2|", "|3|12345678901|"]) == [
        "id", "amt", "1", "2", "3", "12345678901",
    ]


def test_repeated_headers_are_not_renamed() -> None:
    assert _render(["| x | x |", "|---|---|", "| 1 | 2 |"]) == ["x", "x", "1", "2"]


def test_quote_in_a_cell_does_not_merge_rows() -> None:
    assert _render(['|a|b|', '|---|---|', '|"hello|world|', '|x"|y|']) == [
        "a", "b", "&quot;hello", "world", "x&quot;", "y",
    ]


def test_escaped_pipe_is_a_literal_pipe_in_its_cell() -> None:
    assert split_md_table_row(r"| x \| y | z |") == ["x | y", "z"]
    assert _render(["| a | b |", "|---|---|", r"| x \| y | z |"]) == [
        "a", "b", "x | y", "z",
    ]


def test_alignment_row_and_header_only_table() -> None:
    assert _render(["| a | b |", "|:---:|---:|"]) == ["a", "b"]


def test_rows_are_aligned_to_the_header_cell_count() -> None:
    cleaned, errors = clean_md_table_lines(
        ["| a | b | c |", "|---|---|---|", "| 1 |", "| 1 | 2 | 3 | 4 |"],
        start_line_num=10,
    )
    assert errors == [12, 13]
    assert [split_md_table_row(line) for line in cleaned[2:]] == [
        ["1", "", ""],
        ["1", "2", "3"],
    ]
