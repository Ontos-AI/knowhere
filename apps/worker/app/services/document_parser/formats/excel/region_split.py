from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.document_parser.formats.excel.formula_graph import parse_formula_refs
from app.services.document_parser.formats.excel.workbook_grid import (
    DefinedNameDef,
    Region,
    SheetGrid,
    cell_has_value_or_formula,
    merge_at,
    sheet_display_value,
)

if TYPE_CHECKING:
    from app.services.document_parser.formats.excel.luna_decisions import (
        SpreadsheetRegionJudge,
    )


@dataclass(frozen=True)
class ClassifiedRegion:
    region: Region
    role: str
    locked: bool = False


def split_sheet_regions(
    sheet: SheetGrid,
    *,
    defined_names: Sequence[DefinedNameDef] = (),
    judge: SpreadsheetRegionJudge | None = None,
) -> list[ClassifiedRegion]:
    """Split one sheet into atomic empty-row/col blocks, then Luna-classify them."""
    if sheet.max_row == 0 or sheet.max_col == 0:
        return []

    seeds = _seed_regions(sheet, defined_names)
    leftover = _split_sheet_recursive(
        sheet,
        (1, sheet.max_row),
        (1, sheet.max_col),
        claimed=seeds,
    )
    leftover = [region for region in leftover if not overlaps_any(region, seeds)]
    from app.services.document_parser.formats.excel.luna_decisions import (
        evaluate_sheet_blocks,
    )

    decisions = evaluate_sheet_blocks(
        sheet,
        [*seeds, *leftover],
        judge=judge,
        locked=seeds,
    )
    return list(decisions.regions)


def classify_sheet_row(
    sheet: SheetGrid, row: int, col_start: int, col_end: int
) -> tuple[str, str | None]:
    """Classify a row as ``blank``, ``caption``, or ``other``.

    Caption is a single-origin, rowspan-1 banner across a region at least
    two columns wide.
    """
    region_width = col_end - col_start + 1
    non_empty_origins: dict[tuple[int, int], tuple[str, int, int]] = {}
    for col in range(col_start, col_end + 1):
        merge = merge_at(sheet, row, col)
        if merge is not None:
            origin_row, origin_col, max_row, max_col = merge
            value = sheet_display_value(sheet, origin_row, origin_col)
            text = str(value).strip() if value is not None else ""
            if text:
                non_empty_origins[(origin_row, origin_col)] = (
                    text,
                    max_row - origin_row + 1,
                    max_col - origin_col + 1,
                )
            continue
        value = sheet_display_value(sheet, row, col)
        text = str(value).strip() if value is not None else ""
        if text:
            non_empty_origins[(row, col)] = (text, 1, 1)
            if len(non_empty_origins) > 1:
                return "other", None

    if not non_empty_origins:
        return "blank", None
    if len(non_empty_origins) != 1 or region_width < 2:
        return "other", None
    only_text, rowspan, _colspan = next(iter(non_empty_origins.values()))
    if rowspan != 1:
        return "other", None
    return "caption", only_text


def bounding_region(regions: Sequence[Region]) -> Region:
    return Region(
        min_row=min(region.min_row for region in regions),
        min_col=min(region.min_col for region in regions),
        max_row=max(region.max_row for region in regions),
        max_col=max(region.max_col for region in regions),
    )


def regions_overlap(left: Region, right: Region) -> bool:
    return not (
        left.max_row < right.min_row
        or right.max_row < left.min_row
        or left.max_col < right.min_col
        or right.max_col < left.min_col
    )


def columns_overlap(left: Region, right: Region) -> bool:
    return not (left.max_col < right.min_col or right.max_col < left.min_col)


def overlaps_any(region: Region, others: Sequence[Region]) -> bool:
    return any(regions_overlap(region, other) for other in others)


def in_region(row: int, col: int, region: Region) -> bool:
    return (
        region.min_row <= row <= region.max_row
        and region.min_col <= col <= region.max_col
    )


def region_contains(outer: Region, inner: Region) -> bool:
    return (
        outer.min_row <= inner.min_row
        and outer.max_row >= inner.max_row
        and outer.min_col <= inner.min_col
        and outer.max_col >= inner.max_col
    )


def seed_title_for_region(
    sheet: SheetGrid,
    region: Region,
    defined_names: Sequence[DefinedNameDef] = (),
) -> str:
    for table in sheet.excel_tables:
        table_region = Region(
            min_row=table.min_row,
            min_col=table.min_col,
            max_row=table.max_row,
            max_col=table.max_col,
        )
        if region_contains(region, table_region) and table.name:
            return table.name
    for name in defined_names:
        seed = _rectangular_defined_name_region(name, sheet)
        if seed is not None and region_contains(region, seed) and name.name:
            return name.name
    return ""


def _seed_regions(
    sheet: SheetGrid, defined_names: Sequence[DefinedNameDef]
) -> list[Region]:
    seeds: list[Region] = []
    for table in sheet.excel_tables:
        region = Region(
            min_row=table.min_row,
            min_col=table.min_col,
            max_row=table.max_row,
            max_col=table.max_col,
        )
        if _valid_bounds(region):
            seeds.append(region)
    for name in defined_names:
        region = _rectangular_defined_name_region(name, sheet)
        if region is None or overlaps_any(region, seeds):
            continue
        seeds.append(region)
    return seeds


def _rectangular_defined_name_region(
    name: DefinedNameDef, sheet: SheetGrid
) -> Region | None:
    if name.unresolved or not name.attr_text:
        return None
    if name.sheet is not None and name.sheet != sheet.name:
        return None
    text = name.attr_text.strip().lstrip("=")
    if not text or "(" in text or "," in text:
        return None
    if not any(char.isalpha() for char in text) or not any(
        char.isdigit() for char in text
    ):
        return None
    refs = parse_formula_refs(
        f"={text}",
        sheet.name,
        [],
        sheet_bounds={sheet.name: (sheet.max_row, sheet.max_col)},
    )
    if len(refs) != 1:
        return None
    ref = refs[0]
    if ref.sheet != sheet.name:
        return None
    region = Region(
        min_row=ref.min_row,
        min_col=ref.min_col,
        max_row=ref.max_row,
        max_col=ref.max_col,
    )
    if not _valid_bounds(region):
        return None
    return region


def _split_sheet_recursive(
    sheet: SheetGrid,
    row_range: tuple[int, int],
    col_range: tuple[int, int],
    *,
    claimed: Sequence[Region],
) -> list[Region]:
    effective = _find_effective_range(sheet, row_range, col_range, claimed=claimed)
    if effective is None:
        return []
    (effective_row_start, effective_row_end), (
        effective_col_start,
        effective_col_end,
    ) = effective

    # One-column strips keep empty-row gaps; one-row strips keep empty-column gaps.
    separator_rows = []
    if effective_col_start != effective_col_end:
        for row in range(effective_row_start + 1, effective_row_end):
            if _is_true_separator_row(
                sheet, row, (effective_col_start, effective_col_end), claimed=claimed
            ):
                separator_rows.append(row)

    separator_cols = []
    if effective_row_start != effective_row_end:
        for col in range(effective_col_start + 1, effective_col_end):
            if _is_true_separator_col(
                sheet, col, (effective_row_start, effective_row_end), claimed=claimed
            ):
                separator_cols.append(col)

    row_groups = _find_separator_groups(separator_rows)
    col_groups = _find_separator_groups(separator_cols)

    should_split_rows = len(row_groups) > 0 and (
        len(col_groups) == 0 or len(row_groups) <= len(col_groups)
    )
    should_split_cols = len(col_groups) > 0 and not should_split_rows

    if should_split_rows:
        subtables: list[Region] = []
        previous_end = effective_row_start
        for group in row_groups:
            if group[0] > previous_end:
                subtables.extend(
                    _split_sheet_recursive(
                        sheet,
                        (previous_end, group[0] - 1),
                        (effective_col_start, effective_col_end),
                        claimed=claimed,
                    )
                )
            previous_end = group[-1] + 1
        if previous_end <= effective_row_end:
            subtables.extend(
                _split_sheet_recursive(
                    sheet,
                    (previous_end, effective_row_end),
                    (effective_col_start, effective_col_end),
                    claimed=claimed,
                )
            )
        return subtables

    if should_split_cols:
        subtables = []
        previous_end = effective_col_start
        for group in col_groups:
            if group[0] > previous_end:
                subtables.extend(
                    _split_sheet_recursive(
                        sheet,
                        (effective_row_start, effective_row_end),
                        (previous_end, group[0] - 1),
                        claimed=claimed,
                    )
                )
            previous_end = group[-1] + 1
        if previous_end <= effective_col_end:
            subtables.extend(
                _split_sheet_recursive(
                    sheet,
                    (effective_row_start, effective_row_end),
                    (previous_end, effective_col_end),
                    claimed=claimed,
                )
            )
        return subtables

    return [
        Region(
            min_row=effective_row_start,
            min_col=effective_col_start,
            max_row=effective_row_end,
            max_col=effective_col_end,
        )
    ]


def _find_effective_range(
    sheet: SheetGrid,
    row_range: tuple[int, int],
    col_range: tuple[int, int],
    *,
    claimed: Sequence[Region],
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    row_start, row_end = row_range
    col_start, col_end = col_range
    effective_row_start = None
    effective_row_end = None
    effective_col_start = None
    effective_col_end = None

    for row in range(row_start, row_end + 1):
        for col in range(col_start, col_end + 1):
            if not _leftover_occupied(sheet, row, col, claimed):
                continue
            if effective_row_start is None:
                effective_row_start = row
            effective_row_end = row
            if effective_col_start is None or col < effective_col_start:
                effective_col_start = col
            if effective_col_end is None or col > effective_col_end:
                effective_col_end = col

    if (
        effective_row_start is None
        or effective_row_end is None
        or effective_col_start is None
        or effective_col_end is None
    ):
        return None
    return (
        (effective_row_start, effective_row_end),
        (effective_col_start, effective_col_end),
    )


def _leftover_occupied(
    sheet: SheetGrid, row: int, col: int, claimed: Sequence[Region]
) -> bool:
    if any(in_region(row, col, region) for region in claimed):
        return False
    if cell_has_value_or_formula(sheet, row, col):
        return True
    merge = merge_at(sheet, row, col)
    if merge is None:
        return False
    origin_row, origin_col, _max_row, _max_col = merge
    if any(in_region(origin_row, origin_col, region) for region in claimed):
        return False
    return cell_has_value_or_formula(sheet, origin_row, origin_col)


def _is_true_separator_row(
    sheet: SheetGrid,
    row: int,
    effective_col_range: tuple[int, int],
    *,
    claimed: Sequence[Region],
) -> bool:
    col_start, col_end = effective_col_range
    for col in range(col_start, col_end + 1):
        if _leftover_occupied(sheet, row, col, claimed):
            return False
        merge = merge_at(sheet, row, col)
        if merge is not None and not any(
            in_region(merge[0], merge[1], region) for region in claimed
        ):
            return False
    return True


def _is_true_separator_col(
    sheet: SheetGrid,
    col: int,
    effective_row_range: tuple[int, int],
    *,
    claimed: Sequence[Region],
) -> bool:
    if col in sheet.hidden_columns:
        return False
    row_start, row_end = effective_row_range
    for row in range(row_start, row_end + 1):
        if _leftover_occupied(sheet, row, col, claimed):
            return False
        merge = merge_at(sheet, row, col)
        if merge is not None and not any(
            in_region(merge[0], merge[1], region) for region in claimed
        ):
            return False
    return True


def _find_separator_groups(items: list[int]) -> list[list[int]]:
    if not items:
        return []
    groups = []
    current_group = [items[0]]
    for index in range(1, len(items)):
        if items[index] == items[index - 1] + 1:
            current_group.append(items[index])
        else:
            groups.append(current_group)
            current_group = [items[index]]
    groups.append(current_group)
    return groups


def _valid_bounds(region: Region) -> bool:
    return region.min_row <= region.max_row and region.min_col <= region.max_col
