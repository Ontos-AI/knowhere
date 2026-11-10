from __future__ import annotations

import csv
import io
import math
from pathlib import Path

import openpyxl
from openpyxl.cell.cell import TYPE_STRING

from app.services.document_parser.conversion.legacy_converter import xls_to_xlsx
from shared.core.exceptions.domain_exceptions import TableParsingException

# OOXML worksheet title limit.
_EXCEL_SHEET_TITLE_MAX_LENGTH = 31
_TABLE_TEXT_ENCODINGS: tuple[str, ...] = (
    "utf-8-sig",
    "utf-8",
    "utf-16",
    "gb18030",
    "latin-1",
)
_WORK_DIR_NAME = ".work"


def normalize_table_source(path: str, filename: str, output_dir: str) -> str:
    """Return an xlsx work path for csv/tsv/xls/xlsx sources.

    Conversion follows the on-disk source extension (same input the router
    used). The original filename only names the work copy and sheet title.
    """
    extension = Path(path).suffix.lower()
    if extension == ".xlsx":
        return path
    if extension == ".xls":
        converted_path, _converted_name = xls_to_xlsx(path, output_dir)
        return converted_path
    if extension in {".csv", ".tsv"}:
        return _delimited_text_to_xlsx(
            source_path=path,
            output_dir=output_dir,
            delimiter="\t" if extension == ".tsv" else None,
            stem=Path(filename).stem,
        )
    raise TableParsingException(
        user_message=f"Unsupported spreadsheet source type: {extension}",
        reason="UNSUPPORTED_TABLE_SOURCE",
    )


def _delimited_text_to_xlsx(
    *,
    source_path: str,
    output_dir: str,
    delimiter: str | None,
    stem: str,
) -> str:
    text = _decode_table_text(Path(source_path).read_bytes())
    rows = _read_delimited_rows(text, delimiter=delimiter)
    destination = _work_xlsx_path(output_dir, stem)
    _write_xlsx_rows(
        destination=destination,
        sheet_title=_sheet_title_from_stem(stem),
        rows=rows,
    )
    return str(destination)


def _decode_table_text(data: bytes) -> str:
    last_decode_error: UnicodeDecodeError | None = None
    for encoding in _TABLE_TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            last_decode_error = exc
    raise TableParsingException(
        user_message="Could not decode the spreadsheet text file",
        reason="TABLE_TEXT_DECODE_FAILED",
        original_exception=last_decode_error,
    ) from last_decode_error


def _read_delimited_rows(
    text: str,
    *,
    delimiter: str | None,
) -> list[list[str]]:
    if text == "":
        return []
    stream = io.StringIO(text)
    if delimiter is None:
        try:
            dialect = csv.Sniffer().sniff(text)
        except csv.Error as exc:
            raise TableParsingException(
                user_message="Could not detect the delimiter of the CSV file",
                reason="CSV_DELIMITER_UNDETECTED",
                original_exception=exc,
            ) from exc
        reader = csv.reader(stream, dialect)
    else:
        reader = csv.reader(stream, delimiter=delimiter)
    return [list(row) for row in reader]


def _work_xlsx_path(output_dir: str, stem: str) -> Path:
    work_dir = Path(output_dir) / _WORK_DIR_NAME
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir / f"{stem}.xlsx"


def _sheet_title_from_stem(stem: str) -> str:
    title = stem[:_EXCEL_SHEET_TITLE_MAX_LENGTH]
    if not title:
        raise TableParsingException(
            user_message="Spreadsheet file name cannot be converted into an Excel sheet title",
            reason="INVALID_SHEET_TITLE",
        )
    return title


def _write_xlsx_rows(
    *,
    destination: Path,
    sheet_title: str,
    rows: list[list[str]],
) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.worksheets[0]
    try:
        sheet.title = sheet_title
    except ValueError as exc:
        raise TableParsingException(
            user_message="Spreadsheet file name is not a valid Excel sheet title",
            reason="INVALID_SHEET_TITLE",
            original_exception=exc,
        ) from exc

    for row_index, row in enumerate(rows, start=1):
        for column_index, field in enumerate(row, start=1):
            value = _cell_value(field)
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            if isinstance(value, str) and value.startswith("="):
                cell.data_type = TYPE_STRING

    workbook.save(destination)
    workbook.close()


def _cell_value(field: str) -> str | int | float | None:
    if field.startswith("="):
        return field
    if field == "":
        return None
    integer = _parse_int(field)
    if integer is not None:
        return integer
    number = _parse_float(field)
    if number is not None:
        return number
    return field


def _parse_int(field: str) -> int | None:
    try:
        return int(field, 10)
    except ValueError:
        return None


def _parse_float(field: str) -> float | None:
    try:
        number = float(field)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number
