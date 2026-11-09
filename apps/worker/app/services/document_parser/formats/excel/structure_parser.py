from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openpyxl.utils import get_column_letter
from loguru import logger

from app.services.document_parser.formats.excel.formula_graph import build_formula_graph
from app.services.document_parser.formats.excel.header_detect import (
    HeaderSpec,
    SpreadsheetHeaderDetector,
    TableRegion,
    build_region_visual,
    detect_headers,
)
from app.services.document_parser.formats.excel.html_render import (
    render_table_html,
    visible_columns,
)
from app.services.document_parser.formats.excel.region_split import (
    classify_sheet_row,
    seed_title_for_region,
    split_sheet_regions,
)
from app.services.document_parser.formats.excel.satellite_merge import (
    PlannedTable,
    TableFormulaLink,
    resolve_formula_satellites_and_edges,
    table_key,
)
from app.services.document_parser.formats.excel.workbook_grid import (
    Region,
    SheetGrid,
    WorkbookGrid,
)
from shared.core.exceptions.domain_exceptions import TableParsingException

if TYPE_CHECKING:
    from app.services.document_parser.formats.excel.luna_decisions import (
        SpreadsheetRegionJudge,
    )


@dataclass(frozen=True)
class ParsedExcelTable:
    sheet_name: str
    subtable_title: str
    header_row_count: int
    row_header_col_count: int
    column_headers: tuple[str, ...]
    caption: str | None
    satellites: tuple[str, ...]
    html: str
    excel_range: str
    table_key: str

    def extra_metadata(self) -> dict[str, object]:
        meta: dict[str, object] = {
            "header_row_count": self.header_row_count,
            "row_header_col_count": self.row_header_col_count,
            "column_headers": [str(name) for name in self.column_headers],
            "sheet_name": self.sheet_name,
            "excel_range": self.excel_range,
        }
        caption = (self.caption or "").strip()
        if caption:
            meta["caption"] = caption
        if self.satellites:
            meta["satellites"] = [str(item) for item in self.satellites]
        return meta


@dataclass(frozen=True)
class ExcelStructureResult:
    tables: list[ParsedExcelTable]
    links: tuple[TableFormulaLink, ...]


def parse_excel_structure(
    workbook: WorkbookGrid,
    sheet_name: str | None = None,
    include_hidden_sheets: bool = False,
    header_detector: SpreadsheetHeaderDetector | None = None,
    judge: SpreadsheetRegionJudge | None = None,
) -> ExcelStructureResult:
    try:
        sheets = workbook.sheet_by_name()
        sheets_to_parse = (
            [sheet_name] if sheet_name else [sheet.name for sheet in workbook.sheets]
        )
        sheet_regions = {}
        for selected_sheet_name in sheets_to_parse:
            sheet = sheets.get(selected_sheet_name)
            if sheet is None:
                logger.warning(
                    f"Sheet '{selected_sheet_name}' not found in workbook, skipping"
                )
                continue
            if not include_hidden_sheets and sheet.hidden:
                logger.info(f"Sheet '{selected_sheet_name}' is hidden, skipping")
                continue
            if sheet.max_row == 0 or sheet.max_col == 0:
                logger.debug(f"Sheet '{selected_sheet_name}' is empty, skipping")
                continue
            classified = split_sheet_regions(
                sheet, defined_names=workbook.defined_names, judge=judge
            )
            logger.debug(
                f"Sheet '{selected_sheet_name}': {len(classified)} regions after split"
            )
            sheet_regions[selected_sheet_name] = classified

        graph = build_formula_graph(workbook)
        plan = resolve_formula_satellites_and_edges(workbook, sheet_regions, graph)
        results: list[ParsedExcelTable] = []
        by_sheet: dict[str, list[PlannedTable]] = {}
        for planned in plan.tables:
            by_sheet.setdefault(planned.sheet_name, []).append(planned)
        for selected_sheet_name, planned_tables in by_sheet.items():
            sheet = sheets[selected_sheet_name]
            count = len(planned_tables)
            for index, planned in enumerate(planned_tables):
                parsed = _parse_subtable(
                    sheet,
                    planned,
                    header_detector=header_detector,
                    subtable_index=index + 1,
                    subtable_count=count,
                    seed_title=seed_title_for_region(
                        sheet, planned.region, workbook.defined_names
                    ),
                )
                if parsed is None:
                    continue
                logger.debug(
                    f"Subtable '{selected_sheet_name}' #{index + 1}: "
                    f"{parsed.excel_range}, "
                    f"header_row_count={parsed.header_row_count}, "
                    f"row_header_col_count={parsed.row_header_col_count}"
                )
                results.append(parsed)
        return ExcelStructureResult(tables=results, links=plan.links)
    except Exception as exc:
        logger.error(f"Error parsing Excel structure: {exc}")
        raise TableParsingException(
            user_message="Failed to parse Excel file headers",
            reason="EXCEL_PRECISION_PARSE_FAILED",
            internal_message=str(exc),
            original_exception=exc,
        ) from exc


def _strip_leading_caption_and_blank_rows(
    sheet: SheetGrid,
    region: Region,
) -> tuple[Region, str | None]:
    """Strip leading banner-title and blank rows so min_row lands on the header."""
    row_start = region.min_row
    row_end = region.max_row
    col_start, col_end = region.min_col, region.max_col
    caption_parts: list[str] = []

    while row_start < row_end:
        kind, text = classify_sheet_row(sheet, row_start, col_start, col_end)
        if kind == "blank":
            row_start += 1
            continue
        if kind == "caption" and text:
            caption_parts.append(text)
            row_start += 1
            continue
        break

    caption = " / ".join(caption_parts) if caption_parts else None
    return (
        Region(
            min_row=row_start,
            min_col=col_start,
            max_row=row_end,
            max_col=col_end,
        ),
        caption,
    )


def _parse_subtable(
    sheet: SheetGrid,
    planned: PlannedTable,
    *,
    header_detector: SpreadsheetHeaderDetector | None,
    subtable_index: int,
    subtable_count: int,
    seed_title: str,
) -> ParsedExcelTable | None:
    stripped, leading_caption = _strip_leading_caption_and_blank_rows(
        sheet, planned.region
    )
    if stripped.max_row < stripped.min_row:
        return None
    visual = build_region_visual(sheet, stripped)
    caption = _join_captions(
        leading_caption, planned.caption_texts, planned.satellites
    )
    spec = detect_headers(
        TableRegion(visual=visual),
        llm=header_detector,
        caption=caption,
    )
    effective_row_end = stripped.min_row + spec.data_end_row
    if effective_row_end < stripped.min_row:
        return None
    table_region = Region(
        min_row=stripped.min_row,
        min_col=stripped.min_col,
        max_row=effective_row_end,
        max_col=stripped.max_col,
    )
    html = render_table_html(
        sheet, table_region, spec, tfoot_rows=planned.tfoot_rows
    )
    if not html:
        return None
    emitted_headers, emitted_rhcc = _emitted_headers(spec, table_region, sheet)
    excel_range = (
        f"{get_column_letter(table_region.min_col)}{table_region.min_row}:"
        f"{get_column_letter(table_region.max_col)}{table_region.max_row}"
    )
    return ParsedExcelTable(
        sheet_name=planned.sheet_name,
        subtable_title=_subtable_title(
            subtable_index=subtable_index,
            subtable_count=subtable_count,
            seed_title=seed_title,
        ),
        header_row_count=spec.header_row_count,
        row_header_col_count=emitted_rhcc,
        column_headers=emitted_headers,
        caption=spec.caption,
        satellites=planned.satellites,
        html=html,
        excel_range=excel_range,
        table_key=table_key(planned.sheet_name, planned.region),
    )


def _join_captions(
    leading: str | None,
    caption_texts: Sequence[str],
    satellites: Sequence[str],
) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for item in (leading, *caption_texts, *satellites):
        text = (item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return " / ".join(parts) if parts else None


def _emitted_headers(
    spec: HeaderSpec, region: Region, sheet: SheetGrid
) -> tuple[tuple[str, ...], int]:
    visible = visible_columns(sheet, region)
    emitted_rhcc = sum(
        1 for col in visible if col < region.min_col + spec.row_header_col_count
    )
    if spec.header_row_count == 0:
        return (), emitted_rhcc
    headers: list[str] = []
    for col in visible:
        index = col - region.min_col
        if 0 <= index < len(spec.column_headers):
            headers.append(spec.column_headers[index])
    return tuple(headers), emitted_rhcc


def _subtable_title(
    *, subtable_index: int, subtable_count: int, seed_title: str
) -> str:
    if subtable_count <= 1:
        return ""
    if seed_title:
        return seed_title
    return f"子表 {subtable_index}"
