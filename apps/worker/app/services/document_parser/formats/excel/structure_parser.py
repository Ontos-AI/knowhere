from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from openpyxl.utils import get_column_letter
from loguru import logger

from app.services.document_parser.formats.excel.formula_graph import build_formula_graph
from app.services.document_parser.formats.excel.header_detect import (
    HeaderSpec,
    TableRegion,
    build_region_visual,
    detect_headers,
)
from app.services.document_parser.formats.excel.html_render import (
    render_table_html,
    visible_columns,
)
from app.services.document_parser.formats.excel.luna_decisions import (
    split_sheet_regions,
)
from app.services.document_parser.formats.excel.region_split import (
    seed_title_for_region,
)
from app.services.document_parser.formats.excel.satellite_merge import (
    PlannedTable,
    TableFormulaLink,
    format_region_cells,
    resolve_formula_satellites_and_edges,
    table_key,
)
from app.services.document_parser.formats.excel.workbook_grid import (
    Region,
    SheetGrid,
    WorkbookGrid,
)
from shared.core.exceptions.domain_exceptions import TableParsingException


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
    include_hidden_sheets: bool = False,
) -> ExcelStructureResult:
    try:
        sheets = workbook.sheet_by_name()
        graph = build_formula_graph(workbook)
        sheet_regions = {}
        for sheet in workbook.sheets:
            selected_sheet_name = sheet.name
            if not include_hidden_sheets and sheet.hidden:
                logger.info(f"Sheet '{selected_sheet_name}' is hidden, skipping")
                continue
            if sheet.max_row == 0 or sheet.max_col == 0:
                logger.debug(f"Sheet '{selected_sheet_name}' is empty, skipping")
                continue
            classified = split_sheet_regions(
                sheet,
                formula_nodes=graph,
                defined_names=workbook.defined_names,
            )
            logger.debug(
                f"Sheet '{selected_sheet_name}': {len(classified)} regions after split"
            )
            sheet_regions[selected_sheet_name] = classified

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


def _parse_subtable(
    sheet: SheetGrid,
    planned: PlannedTable,
    *,
    subtable_index: int,
    subtable_count: int,
    seed_title: str,
) -> ParsedExcelTable | None:
    region = planned.region
    if region.max_row < region.min_row:
        return None
    visual = build_region_visual(sheet, region)
    spec = detect_headers(TableRegion(visual=visual))
    # Rows above the header start (titles, parameter notes) go to the caption,
    # not to <tr>: retrieval reads every <tr> as a grid row before headers.
    header_top = region.min_row + spec.header_start_row
    preamble = ""
    if header_top > region.min_row:
        preamble = format_region_cells(
            sheet,
            Region(
                min_row=region.min_row,
                min_col=region.min_col,
                max_row=header_top - 1,
                max_col=region.max_col,
            ),
        )
    spec = replace(
        spec,
        caption=_join_captions((preamble, *planned.satellites)),
    )
    table_region = Region(
        min_row=header_top,
        min_col=region.min_col,
        max_row=region.max_row,
        max_col=region.max_col,
    )
    html = render_table_html(sheet, table_region, spec, tfoot_rows=planned.tfoot_rows)
    if not html:
        return None
    emitted_headers, emitted_rhcc = _emitted_headers(spec, table_region, sheet)
    excel_range = (
        f"{get_column_letter(region.min_col)}{region.min_row}:"
        f"{get_column_letter(region.max_col)}{region.max_row}"
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


def _join_captions(items: Sequence[str]) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for item in items:
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
