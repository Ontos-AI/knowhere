from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from openpyxl.utils import get_column_letter

from app.services.document_parser.formats.excel.formula_graph import (
    CellRange,
    FormulaNode,
    range_hits_region,
)
from app.services.document_parser.formats.excel.region_split import (
    ROLE_TABLE,
    ClassifiedRegion,
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


@dataclass(frozen=True)
class PlannedTable:
    sheet_name: str
    region: Region
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
    """Plan emitted tables: attach detached regions to main tables, link tables.

    Main tables are locked seeds and shape-tables that contain text. Every other
    region is a candidate: formula edges in either direction attach it to the
    tables it touches; an isolated candidate without text is noise once the
    workbook has a main table; an isolated candidate with text becomes a
    satellite of its nearest same-sheet table(s).
    """
    sheets = workbook.sheet_by_name()
    tables: list[tuple[str, ClassifiedRegion]] = []
    candidates: list[tuple[str, ClassifiedRegion]] = []
    for sheet_name, classified in sheet_regions.items():
        sheet = sheets[sheet_name]
        for item in classified:
            if item.role == ROLE_TABLE and (
                item.locked or _has_text(sheet, item.region)
            ):
                tables.append((sheet_name, item))
            else:
                candidates.append((sheet_name, item))

    attachments: dict[str, _Attachment] = {
        table_key(sheet_name, item.region): _Attachment()
        for sheet_name, item in tables
    }
    owned_regions: dict[str, list[Region]] = {
        table_key(sheet_name, item.region): [item.region]
        for sheet_name, item in tables
    }
    absorbed: set[tuple[str, Region]] = set()

    while True:
        progressed = False
        for sheet_name, candidate in candidates:
            if (sheet_name, candidate.region) in absorbed:
                continue
            sheet = sheets[sheet_name]
            hosts = _formula_hosts(
                candidate.region,
                sheet_name=sheet_name,
                tables=tables,
                owned_regions=owned_regions,
                graph=graph,
            )
            if not hosts:
                continue
            text = format_region_cells(sheet, candidate.region)
            for host_sheet, host in hosts:
                key = table_key(host_sheet, host.region)
                attachment = attachments[key]
                if _is_aligned_totals(sheet, candidate.region, host.region, host_sheet):
                    attachment.tfoot_rows.extend(
                        _occupied_rows(sheet, candidate.region)
                    )
                elif text:
                    attachment.satellites.append(text)
                if sheet_name == host_sheet and len(hosts) == 1:
                    owned_regions[key].append(candidate.region)
            absorbed.add((sheet_name, candidate.region))
            progressed = True
        if not progressed:
            break

    dropped: set[tuple[str, Region]] = set()
    for sheet_name, candidate in candidates:
        if (sheet_name, candidate.region) in absorbed:
            continue
        sheet = sheets[sheet_name]
        if not _has_text(sheet, candidate.region):
            if tables:
                dropped.add((sheet_name, candidate.region))
            continue
        nearest = _nearest_hosts(candidate.region, sheet_name=sheet_name, tables=tables)
        if not nearest:
            continue
        text = format_region_cells(sheet, candidate.region)
        for host_sheet, host in nearest:
            attachments[table_key(host_sheet, host.region)].satellites.append(text)
        absorbed.add((sheet_name, candidate.region))

    planned: list[PlannedTable] = []
    for sheet_name, item in tables:
        attachment = attachments[table_key(sheet_name, item.region)]
        planned.append(
            PlannedTable(
                sheet_name=sheet_name,
                region=item.region,
                satellites=tuple(_unique(attachment.satellites)),
                tfoot_rows=tuple(sorted(set(attachment.tfoot_rows))),
            )
        )
    for sheet_name, candidate in candidates:
        key = (sheet_name, candidate.region)
        if key in absorbed or key in dropped:
            continue
        planned.append(
            PlannedTable(
                sheet_name=sheet_name,
                region=candidate.region,
                satellites=(),
                tfoot_rows=(),
            )
        )

    planned.sort(key=lambda item: (item.sheet_name, item.region.min_row, item.region.min_col))
    nodes_by_cell = {(node.sheet, node.row, node.col): node for node in graph}
    links = _table_formula_links(tables, owned_regions, nodes_by_cell)
    return WorkbookTablePlan(tables=tuple(planned), links=tuple(links))


class _Attachment:
    def __init__(self) -> None:
        self.satellites: list[str] = []
        self.tfoot_rows: list[int] = []


def _has_text(sheet: SheetGrid, region: Region) -> bool:
    """Whether a non-formula cell in ``region`` holds a value that is not a number."""
    for (row, col), cell in sheet.cells.items():
        if cell.formula or not in_region(row, col, region):
            continue
        value = cell.value
        if isinstance(value, str) and value.strip() and not _is_number_text(value):
            return True
    return False


def _is_number_text(value: str) -> bool:
    try:
        float(value.strip())
    except ValueError:
        return False
    return True


def _nearest_hosts(
    region: Region,
    *,
    sheet_name: str,
    tables: Sequence[tuple[str, ClassifiedRegion]],
) -> list[tuple[str, ClassifiedRegion]]:
    """Same-sheet tables at the smallest row-gap plus column-gap; ties all win."""
    gaps = [
        (_region_gap(region, table.region), (table_sheet, table))
        for table_sheet, table in tables
        if table_sheet == sheet_name
    ]
    if not gaps:
        return []
    best = min(gap for gap, _host in gaps)
    return [host for gap, host in gaps if gap == best]


def _region_gap(left: Region, right: Region) -> int:
    row_gap = max(0, right.min_row - left.max_row - 1, left.min_row - right.max_row - 1)
    col_gap = max(0, right.min_col - left.max_col - 1, left.min_col - right.max_col - 1)
    return row_gap + col_gap


def _formula_hosts(
    region: Region,
    *,
    sheet_name: str,
    tables: Sequence[tuple[str, ClassifiedRegion]],
    owned_regions: Mapping[str, Sequence[Region]],
    graph: Sequence[FormulaNode],
) -> list[tuple[str, ClassifiedRegion]]:
    """Tables this region's formulas read, plus tables whose formulas read it."""
    hosts: list[tuple[str, ClassifiedRegion]] = []
    seen: set[str] = set()
    for node in graph:
        if node.unresolved:
            continue
        node_in_region = node.sheet == sheet_name and in_region(
            node.row, node.col, region
        )
        if not node_in_region and not any(
            range_hits_region(ref, sheet_name, region) for ref in node.refs
        ):
            continue
        for table_sheet, table in tables:
            key = table_key(table_sheet, table.region)
            if key in seen:
                continue
            owned = owned_regions[key]
            if node_in_region:
                touches = any(
                    _ref_hits_any_region(ref, table_sheet, owned) for ref in node.refs
                )
            else:
                touches = node.sheet == table_sheet and any(
                    in_region(node.row, node.col, owned_region)
                    for owned_region in owned
                )
            if touches:
                seen.add(key)
                hosts.append((table_sheet, table))
    return hosts


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
    return any(range_hits_region(ref, sheet_name, region) for region in regions)


def format_region_cells(sheet: SheetGrid, region: Region) -> str:
    """Cell-addressed text, e.g. ``J5=38610.09 M4=599020.59 (=K4+L4)``.

    Addresses let a host table's ``data-formula`` resolve against these values.
    """
    parts: list[str] = []
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
            address = f"{get_column_letter(origin[1])}{origin[0]}"
            if cell.formula:
                body = cell.formula[1:] if cell.formula.startswith("=") else cell.formula
                cached = "" if cell.value is None else str(cell.value).strip()
                if cached and cached != cell.formula:
                    parts.append(f"{address}={cached} (={body})")
                else:
                    parts.append(f"{address} (={body})")
                continue
            shown = display_value(cell)
            text = "" if shown is None else str(shown).strip()
            if text:
                parts.append(f"{address}={text}")
    return " ".join(parts)


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
