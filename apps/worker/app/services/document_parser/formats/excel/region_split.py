from __future__ import annotations

from collections.abc import Sequence

from loguru import logger

from app.services.document_parser.formats.excel.formula_graph import parse_formula_refs
from app.services.document_parser.formats.excel.workbook_grid import (
    DefinedNameDef,
    Region,
    SheetGrid,
    cell_has_value_or_formula,
    merge_at,
    sheet_display_value,
)

_MIN_ROWS = 2
_MIN_COLS = 2
_MIN_CELLS = 4
_MAX_CAPTION_ROWS = 2
_MAX_FRAGMENT_GAP = 1
_BORDER_SIDES_LIKE_TABLE = 2


def split_sheet_regions(
    sheet: SheetGrid,
    *,
    defined_names: Sequence[DefinedNameDef] = (),
) -> list[Region]:
    """Split one sheet into table regions: seeds first, then geometry on the rest."""
    if sheet.max_row == 0 or sheet.max_col == 0:
        return []

    seeds = _seed_regions(sheet, defined_names)
    leftover = _split_sheet_recursive(
        sheet,
        (1, sheet.max_row),
        (1, sheet.max_col),
        claimed=seeds,
    )
    leftover = _merge_border_connected(sheet, leftover)
    leftover = [region for region in leftover if not _overlaps_any(region, seeds)]
    regions = _attach_captions(sheet, [*seeds, *leftover])
    regions = _merge_small_subtables(sheet, regions)
    regions.sort(key=lambda region: (region.min_row, region.min_col))
    return regions


def classify_sheet_row(
    sheet: SheetGrid, row: int, col_start: int, col_end: int
) -> tuple[str, str | None]:
    """Classify a row as ``blank``, ``caption``, or ``other``.

    Caption matches the existing leading-title rule: one origin, region
    width ≥ 2, rowspan 1.
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
        if region is None or _overlaps_any(region, seeds):
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
    min_rows: int = _MIN_ROWS,
    min_cols: int = _MIN_COLS,
) -> list[Region]:
    effective = _find_effective_range(sheet, row_range, col_range, claimed=claimed)
    if effective is None:
        return []
    (effective_row_start, effective_row_end), (
        effective_col_start,
        effective_col_end,
    ) = effective

    if (
        effective_row_end - effective_row_start + 1 < min_rows
        or effective_col_end - effective_col_start + 1 < min_cols
    ):
        return [
            Region(
                min_row=effective_row_start,
                min_col=effective_col_start,
                max_row=effective_row_end,
                max_col=effective_col_end,
            )
        ]

    separator_rows = []
    for row in range(effective_row_start + 1, effective_row_end):
        if _is_true_separator_row(
            sheet, row, (effective_col_start, effective_col_end), claimed=claimed
        ):
            separator_rows.append(row)

    separator_cols = []
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
                        min_rows=min_rows,
                        min_cols=min_cols,
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
                    min_rows=min_rows,
                    min_cols=min_cols,
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
                        min_rows=min_rows,
                        min_cols=min_cols,
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
                    min_rows=min_rows,
                    min_cols=min_cols,
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
    if _in_any_region(row, col, claimed):
        return False
    return cell_has_value_or_formula(sheet, row, col)


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
        if merge is not None and not _in_any_region(merge[0], merge[1], claimed):
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
        if merge is not None and not _in_any_region(merge[0], merge[1], claimed):
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


def _merge_border_connected(
    sheet: SheetGrid, regions: list[Region]
) -> list[Region]:
    if len(regions) <= 1:
        return regions
    bordered = {
        (row, col)
        for (row, col), cell in sheet.cells.items()
        if cell.border_sides > _BORDER_SIDES_LIKE_TABLE
    }
    if not bordered:
        return regions
    parent = list(range(len(regions)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    cell_region: dict[tuple[int, int], int] = {}
    for index, region in enumerate(regions):
        for row in range(region.min_row, region.max_row + 1):
            for col in range(region.min_col, region.max_col + 1):
                if (row, col) in bordered:
                    cell_region[(row, col)] = index

    seen: set[tuple[int, int]] = set()
    for start in bordered:
        if start in seen:
            continue
        stack = [start]
        hit: set[int] = set()
        while stack:
            row, col = stack.pop()
            if (row, col) in seen:
                continue
            seen.add((row, col))
            region_index = cell_region.get((row, col))
            if region_index is not None:
                hit.add(region_index)
            for delta_row, delta_col in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = (row + delta_row, col + delta_col)
                if neighbor in bordered and neighbor not in seen:
                    stack.append(neighbor)
        if len(hit) < 2:
            continue
        first = next(iter(hit))
        for index in hit:
            union(first, index)

    grouped: dict[int, list[Region]] = {}
    for index, region in enumerate(regions):
        grouped.setdefault(find(index), []).append(region)
    merged = [_bounding_region(group) for group in grouped.values()]
    merged.sort(key=lambda region: (region.min_row, region.min_col))
    return merged


def _attach_captions(sheet: SheetGrid, regions: list[Region]) -> list[Region]:
    if len(regions) <= 1:
        return regions
    captions = [region for region in regions if _is_caption_region(sheet, region)]
    if not captions:
        return regions
    tables = [region for region in regions if region not in captions]
    if not tables:
        return regions
    captions.sort(key=lambda region: region.min_row, reverse=True)
    attached: set[Region] = set()
    updated = list(tables)
    for caption in captions:
        target_index = _nearest_table_below(caption, updated)
        if target_index is None:
            continue
        updated[target_index] = _bounding_region([caption, updated[target_index]])
        attached.add(caption)
    leftover_captions = [region for region in captions if region not in attached]
    return [*updated, *leftover_captions]


def _is_caption_region(sheet: SheetGrid, region: Region) -> bool:
    if _region_has_formula(sheet, region):
        return False
    caption_count = 0
    for row in range(region.min_row, region.max_row + 1):
        kind, _text = classify_sheet_row(
            sheet, row, region.min_col, region.max_col
        )
        if kind == "blank":
            continue
        if kind != "caption":
            return False
        caption_count += 1
        if caption_count > _MAX_CAPTION_ROWS:
            return False
    return 1 <= caption_count <= _MAX_CAPTION_ROWS


def _nearest_table_below(caption: Region, tables: Sequence[Region]) -> int | None:
    best_index: int | None = None
    best_row: int | None = None
    for index, table in enumerate(tables):
        if table.min_row <= caption.max_row:
            continue
        if not _columns_overlap(caption, table):
            continue
        if best_row is None or table.min_row < best_row:
            best_row = table.min_row
            best_index = index
    return best_index


def _merge_small_subtables(
    sheet: SheetGrid,
    subtables: list[Region],
    min_cells: int = _MIN_CELLS,
) -> list[Region]:
    if len(subtables) <= 1:
        return subtables

    items: list[dict] = []
    for region in subtables:
        items.append(
            {
                "region": region,
                "cells": _count_non_empty_cells(sheet, region),
                "has_formula": _region_has_formula(sheet, region),
            }
        )

    changed = True
    while changed and len(items) > 1:
        changed = False
        source_index = None
        target_index = None
        for index, item in enumerate(items):
            if item["cells"] >= min_cells or item["has_formula"]:
                continue
            nearest = _nearest_item_index(index, items)
            if nearest is None:
                continue
            distance = _region_gap(item["region"], items[nearest]["region"])
            if distance > _MAX_FRAGMENT_GAP:
                continue
            merged = _bounding_region([item["region"], items[nearest]["region"]])
            if _bbox_overlaps_other_large(
                merged,
                source_index=index,
                target_index=nearest,
                items=items,
                min_cells=min_cells,
            ):
                continue
            if source_index is None or item["cells"] < items[source_index]["cells"]:
                source_index = index
                target_index = nearest

        if source_index is None or target_index is None:
            break

        source = items[source_index]
        target = items[target_index]
        merged_region = _bounding_region([source["region"], target["region"]])
        items[target_index] = {
            "region": merged_region,
            "cells": source["cells"] + target["cells"],
            "has_formula": source["has_formula"] or target["has_formula"],
        }
        logger.debug(
            f"Merged small fragment (rows={source['region'].min_row}:{source['region'].max_row}, "
            f"cols={source['region'].min_col}:{source['region'].max_col}, "
            f"cells={source['cells']}) into neighbor "
            f"(rows={target['region'].min_row}:{target['region'].max_row}, "
            f"cols={target['region'].min_col}:{target['region'].max_col})"
        )
        del items[source_index]
        changed = True

    return [item["region"] for item in items]


def _nearest_item_index(source_index: int, items: Sequence[dict]) -> int | None:
    source = items[source_index]["region"]
    best_index = None
    best_distance = None
    for index, item in enumerate(items):
        if index == source_index:
            continue
        distance = _region_gap(source, item["region"])
        if best_distance is None or distance < best_distance or (
            distance == best_distance
            and item["cells"] > items[best_index]["cells"]
        ):
            best_distance = distance
            best_index = index
    return best_index


def _bbox_overlaps_other_large(
    merged: Region,
    *,
    source_index: int,
    target_index: int,
    items: Sequence[dict],
    min_cells: int,
) -> bool:
    for index, item in enumerate(items):
        if index in {source_index, target_index}:
            continue
        if item["cells"] < min_cells:
            continue
        if _regions_overlap(merged, item["region"]):
            return True
    return False


def _count_non_empty_cells(sheet: SheetGrid, region: Region) -> int:
    count = 0
    for row in range(region.min_row, region.max_row + 1):
        for col in range(region.min_col, region.max_col + 1):
            if cell_has_value_or_formula(sheet, row, col):
                count += 1
    return count


def _region_has_formula(sheet: SheetGrid, region: Region) -> bool:
    for (row, col), cell in sheet.cells.items():
        if not _in_region(row, col, region):
            continue
        if cell.formula:
            return True
    return False


def _region_gap(left: Region, right: Region) -> int:
    row_gap = max(0, right.min_row - left.max_row - 1, left.min_row - right.max_row - 1)
    col_gap = max(0, right.min_col - left.max_col - 1, left.min_col - right.max_col - 1)
    return row_gap + col_gap


def _bounding_region(regions: Sequence[Region]) -> Region:
    return Region(
        min_row=min(region.min_row for region in regions),
        min_col=min(region.min_col for region in regions),
        max_row=max(region.max_row for region in regions),
        max_col=max(region.max_col for region in regions),
    )


def _in_region(row: int, col: int, region: Region) -> bool:
    return (
        region.min_row <= row <= region.max_row
        and region.min_col <= col <= region.max_col
    )


def _in_any_region(row: int, col: int, regions: Sequence[Region]) -> bool:
    return any(_in_region(row, col, region) for region in regions)


def _regions_overlap(left: Region, right: Region) -> bool:
    return not (
        left.max_row < right.min_row
        or right.max_row < left.min_row
        or left.max_col < right.min_col
        or right.max_col < left.min_col
    )


def _columns_overlap(left: Region, right: Region) -> bool:
    return not (left.max_col < right.min_col or right.max_col < left.min_col)


def _overlaps_any(region: Region, others: Sequence[Region]) -> bool:
    return any(_regions_overlap(region, other) for other in others)


def _valid_bounds(region: Region) -> bool:
    return region.min_row <= region.max_row and region.min_col <= region.max_col


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
