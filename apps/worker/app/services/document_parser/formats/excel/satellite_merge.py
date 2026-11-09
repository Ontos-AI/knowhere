from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from openpyxl.utils import get_column_letter

from app.services.document_parser.formats.excel.formula_graph import (
    CellRange,
    FormulaNode,
)
from app.services.document_parser.formats.excel.region_split import (
    ClassifiedRegion,
    classify_sheet_row,
    columns_overlap,
    in_region,
)
from app.services.document_parser.formats.excel.workbook_grid import (
    Region,
    SheetGrid,
    WorkbookGrid,
    cell_has_value_or_formula,
    display_value,
    merge_at,
)

_ROLE_TABLE = "table"


@dataclass(frozen=True)
class PlannedTable:
    sheet_name: str
    region: Region
    locked: bool
    caption_texts: tuple[str, ...]
    satellites: tuple[str, ...]
    tfoot_rows: tuple[int, ...]


@dataclass(frozen=True)
class TableFormulaLink:
    source_key: str
    target_key: str
    formula: str


@dataclass(frozen=True)
class WorkbookTablePlan:
    tables: tuple[PlannedTable, ...]
    links: tuple[TableFormulaLink, ...]


def table_key(sheet_name: str, region: Region) -> str:
    return (
        f"{sheet_name}!"
        f"{get_column_letter(region.min_col)}{region.min_row}:"
        f"{get_column_letter(region.max_col)}{region.max_row}"
    )


def resolve_formula_satellites_and_edges(
    workbook: WorkbookGrid,
    sheet_regions: Mapping[str, Sequence[ClassifiedRegion]],
    graph: Sequence[FormulaNode],
) -> WorkbookTablePlan:
    sheets = workbook.sheet_by_name()
    tables: list[tuple[str, ClassifiedRegion]] = []
    fragments: list[tuple[str, ClassifiedRegion]] = []
    for sheet_name, classified in sheet_regions.items():
        for item in classified:
            if item.role == _ROLE_TABLE:
                tables.append((sheet_name, item))
            else:
                fragments.append((sheet_name, item))

    attachments: dict[str, _Attachment] = {
        table_key(sheet_name, item.region): _Attachment()
        for sheet_name, item in tables
    }
    owned_regions: dict[str, list[Region]] = {
        table_key(sheet_name, item.region): [item.region]
        for sheet_name, item in tables
    }
    absorbed: set[tuple[str, Region]] = set()
    nodes_by_cell = {(node.sheet, node.row, node.col): node for node in graph}

    while True:
        progressed = False
        for sheet_name, fragment in fragments:
            if (sheet_name, fragment.region) in absorbed:
                continue
            sheet = sheets.get(sheet_name)
            if sheet is None:
                continue
            hosts = _formula_hosts(
                fragment.region,
                sheet_name=sheet_name,
                tables=tables,
                owned_regions=owned_regions,
                nodes_by_cell=nodes_by_cell,
            )
            if not hosts:
                continue
            text = _format_satellite(sheet, fragment.region)
            for host_sheet, host in hosts:
                key = table_key(host_sheet, host.region)
                attachment = attachments[key]
                if _is_aligned_totals(sheet, fragment.region, host.region, host_sheet):
                    attachment.tfoot_rows.extend(
                        _occupied_rows(sheet, fragment.region)
                    )
                elif text:
                    attachment.satellites.append(text)
                if sheet_name == host_sheet and len(hosts) == 1:
                    owned_regions[key].append(fragment.region)
            absorbed.add((sheet_name, fragment.region))
            progressed = True
        if not progressed:
            break

    for sheet_name, fragment in fragments:
        if (sheet_name, fragment.region) in absorbed:
            continue
        sheet = sheets.get(sheet_name)
        if sheet is None:
            continue
        host = _title_host_below(sheet, fragment, tables)
        if host is None:
            continue
        title = _title_text(sheet, fragment.region)
        if not title:
            continue
        attachments[table_key(host[0], host[1].region)].caption_texts.append(title)
        absorbed.add((sheet_name, fragment.region))

    planned: list[PlannedTable] = []
    for sheet_name, item in tables:
        key = table_key(sheet_name, item.region)
        attachment = attachments[key]
        planned.append(
            PlannedTable(
                sheet_name=sheet_name,
                region=item.region,
                locked=item.locked,
                caption_texts=tuple(_unique(attachment.caption_texts)),
                satellites=tuple(_unique(attachment.satellites)),
                tfoot_rows=tuple(sorted(set(attachment.tfoot_rows))),
            )
        )
    for sheet_name, fragment in fragments:
        if (sheet_name, fragment.region) in absorbed:
            continue
        planned.append(
            PlannedTable(
                sheet_name=sheet_name,
                region=fragment.region,
                locked=False,
                caption_texts=(),
                satellites=(),
                tfoot_rows=(),
            )
        )

    planned.sort(key=lambda item: (item.sheet_name, item.region.min_row, item.region.min_col))
    links = _table_formula_links(tables, owned_regions, nodes_by_cell)
    planned, links = _drop_unlabeled_numeric_strips(sheets, planned, links, graph)
    return WorkbookTablePlan(tables=tuple(planned), links=tuple(links))


class _Attachment:
    def __init__(self) -> None:
        self.caption_texts: list[str] = []
        self.satellites: list[str] = []
        self.tfoot_rows: list[int] = []


def _drop_unlabeled_numeric_strips(
    sheets: Mapping[str, SheetGrid],
    planned: Sequence[PlannedTable],
    links: Sequence[TableFormulaLink],
    graph: Sequence[FormulaNode],
) -> tuple[list[PlannedTable], tuple[TableFormulaLink, ...]]:
    kept: list[PlannedTable] = []
    for item in planned:
        sheet = sheets.get(item.sheet_name)
        if sheet is not None and _is_unlabeled_numeric_strip(sheet, item, graph):
            continue
        kept.append(item)
    kept_keys = {table_key(item.sheet_name, item.region) for item in kept}
    kept_links = tuple(
        link
        for link in links
        if link.source_key in kept_keys and link.target_key in kept_keys
    )
    return kept, kept_links


def _is_unlabeled_numeric_strip(
    sheet: SheetGrid,
    planned: PlannedTable,
    graph: Sequence[FormulaNode],
) -> bool:
    if planned.locked:
        return False
    region = planned.region
    if region.max_row != region.min_row and region.max_col != region.min_col:
        return False
    for row in range(region.min_row, region.max_row + 1):
        for col in range(region.min_col, region.max_col + 1):
            cell = sheet.cells.get((row, col))
            if cell is None:
                continue
            if cell.formula:
                return False
            shown = display_value(cell)
            if shown is None:
                continue
            if isinstance(shown, str) and not shown.strip():
                continue
            if isinstance(shown, bool) or not isinstance(shown, (int, float)):
                return False
    for node in graph:
        if node.unresolved:
            continue
        if node.sheet == planned.sheet_name and in_region(
            node.row, node.col, region
        ):
            continue
        for ref in node.refs:
            if _ref_hits_table(ref, planned.sheet_name, region):
                return False
    return True


def _formula_hosts(
    fragment: Region,
    *,
    sheet_name: str,
    tables: Sequence[tuple[str, ClassifiedRegion]],
    owned_regions: Mapping[str, Sequence[Region]],
    nodes_by_cell: Mapping[tuple[str, int, int], FormulaNode],
) -> list[tuple[str, ClassifiedRegion]]:
    hosts: list[tuple[str, ClassifiedRegion]] = []
    seen: set[str] = set()
    for row in range(fragment.min_row, fragment.max_row + 1):
        for col in range(fragment.min_col, fragment.max_col + 1):
            node = nodes_by_cell.get((sheet_name, row, col))
            if node is None or node.unresolved:
                continue
            for ref in node.refs:
                for table_sheet, table in tables:
                    key = table_key(table_sheet, table.region)
                    if not _ref_hits_any_region(
                        ref, table_sheet, owned_regions.get(key, (table.region,))
                    ):
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    hosts.append((table_sheet, table))
    return hosts


def _title_host_below(
    sheet: SheetGrid,
    fragment: ClassifiedRegion,
    tables: Sequence[tuple[str, ClassifiedRegion]],
) -> tuple[str, ClassifiedRegion] | None:
    if _fragment_has_formula(sheet, fragment.region):
        return None
    if not _is_plain_title(sheet, fragment.region):
        return None
    best: tuple[str, ClassifiedRegion] | None = None
    for sheet_name, table in tables:
        if sheet_name != sheet.name:
            continue
        if table.region.min_row <= fragment.region.max_row:
            continue
        if not columns_overlap(fragment.region, table.region):
            continue
        if best is None or table.region.min_row < best[1].region.min_row:
            best = (sheet_name, table)
    return best


def _is_aligned_totals(
    fragment_sheet: SheetGrid,
    fragment: Region,
    table: Region,
    table_sheet_name: str,
) -> bool:
    if fragment_sheet.name != table_sheet_name:
        return False
    occupied_cols = _occupied_columns(fragment_sheet, fragment)
    if not occupied_cols:
        return False
    table_cols = set(range(table.min_col, table.max_col + 1))
    return occupied_cols <= table_cols


def _table_formula_links(
    tables: Sequence[tuple[str, ClassifiedRegion]],
    owned_regions: Mapping[str, Sequence[Region]],
    nodes_by_cell: Mapping[tuple[str, int, int], FormulaNode],
) -> list[TableFormulaLink]:
    links: list[TableFormulaLink] = []
    seen: set[tuple[str, str, str]] = set()
    for source_sheet, source in tables:
        source_id = table_key(source_sheet, source.region)
        for source_region in owned_regions.get(source_id, (source.region,)):
            for row in range(source_region.min_row, source_region.max_row + 1):
                for col in range(source_region.min_col, source_region.max_col + 1):
                    node = nodes_by_cell.get((source_sheet, row, col))
                    if node is None or node.unresolved:
                        continue
                    for ref in node.refs:
                        for target_sheet, target in tables:
                            if (
                                source_sheet == target_sheet
                                and source.region == target.region
                            ):
                                continue
                            target_id = table_key(target_sheet, target.region)
                            if not _ref_hits_any_region(
                                ref,
                                target_sheet,
                                owned_regions.get(target_id, (target.region,)),
                            ):
                                continue
                            key = (source_id, target_id, node.formula)
                            if key in seen:
                                continue
                            seen.add(key)
                            links.append(
                                TableFormulaLink(
                                    source_key=source_id,
                                    target_key=target_id,
                                    formula=node.formula,
                                )
                            )
    return links


def _ref_hits_any_region(
    ref: CellRange, sheet_name: str, regions: Sequence[Region]
) -> bool:
    return any(_ref_hits_table(ref, sheet_name, region) for region in regions)


def _ref_hits_table(ref: CellRange, sheet_name: str, region: Region) -> bool:
    if ref.sheet != sheet_name:
        return False
    return not (
        ref.max_row < region.min_row
        or region.max_row < ref.min_row
        or ref.max_col < region.min_col
        or region.max_col < ref.min_col
    )


def _fragment_has_formula(sheet: SheetGrid, region: Region) -> bool:
    for (row, col), cell in sheet.cells.items():
        if in_region(row, col, region) and cell.formula:
            return True
    return False


def _is_plain_title(sheet: SheetGrid, region: Region) -> bool:
    caption_count = 0
    for row in range(region.min_row, region.max_row + 1):
        kind, text = classify_sheet_row(
            sheet, row, region.min_col, region.max_col
        )
        if kind == "blank":
            continue
        if kind != "caption" or not text:
            return False
        caption_count += 1
    return caption_count >= 1


def _title_text(sheet: SheetGrid, region: Region) -> str:
    parts: list[str] = []
    for row in range(region.min_row, region.max_row + 1):
        kind, text = classify_sheet_row(
            sheet, row, region.min_col, region.max_col
        )
        if kind == "caption" and text:
            parts.append(text)
    return " / ".join(parts)


def _format_satellite(sheet: SheetGrid, region: Region) -> str:
    labels: list[str] = []
    values: list[str] = []
    seen_origins: set[tuple[int, int]] = set()
    for row in range(region.min_row, region.max_row + 1):
        for col in range(region.min_col, region.max_col + 1):
            origin = _origin(sheet, row, col)
            if origin in seen_origins:
                continue
            seen_origins.add(origin)
            cell = sheet.cells.get(origin)
            if cell is None:
                continue
            shown = display_value(cell)
            text = "" if shown is None else str(shown).strip()
            if cell.formula:
                body = cell.formula[1:] if cell.formula.startswith("=") else cell.formula
                cached = "" if cell.value is None else str(cell.value).strip()
                if cached and cached != cell.formula:
                    values.append(f"{cached} (= {body})")
                else:
                    values.append(f"(= {body})")
                continue
            if text:
                labels.append(text)
    return " ".join([*labels, *values]).strip()


def _origin(sheet: SheetGrid, row: int, col: int) -> tuple[int, int]:
    merge = merge_at(sheet, row, col)
    if merge is None:
        return row, col
    return merge[0], merge[1]


def _occupied_columns(sheet: SheetGrid, region: Region) -> set[int]:
    occupied: set[int] = set()
    for col in range(region.min_col, region.max_col + 1):
        for row in range(region.min_row, region.max_row + 1):
            if cell_has_value_or_formula(sheet, row, col):
                occupied.add(col)
                break
    return occupied


def _occupied_rows(sheet: SheetGrid, region: Region) -> list[int]:
    rows: list[int] = []
    for row in range(region.min_row, region.max_row + 1):
        if any(
            cell_has_value_or_formula(sheet, row, col)
            for col in range(region.min_col, region.max_col + 1)
        ):
            rows.append(row)
    return rows


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique
