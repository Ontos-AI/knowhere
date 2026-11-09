from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from loguru import logger

from app.services.document_parser.formats.excel.region_split import (
    ClassifiedRegion,
    atomic_sheet_blocks,
    bounding_region,
    classify_sheet_row,
    columns_overlap,
    regions_overlap,
)
from app.services.document_parser.formats.excel.workbook_grid import (
    DefinedNameDef,
    Region,
    SheetGrid,
    display_value,
    merge_at,
)
from shared.services.http.client_pool import get_sync_client

_CLOCKWISE_DIRS = ("up", "right", "down", "left")
_BOUNDARY_EDGE = 5
_LUNA_MODEL = "openai/gpt-6-luna-decisions"
_LUNA_EVALUATE_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
_ROLE_TABLE = "table"
_ROLE_FRAGMENT = "fragment"
_CHOICE_MERGE = "merge"
_CHOICE_SEPARATE = "separate"


class SpreadsheetRegionJudge(Protocol):
    def choose(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, Mapping[str, object]],
        required_ids: Sequence[str],
        allowed: Mapping[str, frozenset[str]],
    ) -> Mapping[str, str] | None:
        """Return question id → choice, or None to fail closed."""


@dataclass(frozen=True)
class SheetBlockDecisions:
    regions: tuple[ClassifiedRegion, ...]


@dataclass
class _OpenBlock:
    region: Region
    locked: bool


def split_sheet_regions(
    sheet: SheetGrid,
    *,
    defined_names: Sequence[DefinedNameDef] = (),
    judge: SpreadsheetRegionJudge | None = None,
) -> list[ClassifiedRegion]:
    """Split one sheet into atomic empty-row/col blocks, then classify them."""
    seeds, leftover = atomic_sheet_blocks(sheet, defined_names=defined_names)
    if not seeds and not leftover:
        return []
    return list(
        evaluate_sheet_blocks(
            sheet,
            [*seeds, *leftover],
            judge=judge,
            locked=seeds,
        ).regions
    )


def evaluate_sheet_blocks(
    sheet: SheetGrid,
    blocks: Sequence[Region],
    *,
    judge: SpreadsheetRegionJudge | None = None,
    locked: Sequence[Region] = (),
) -> SheetBlockDecisions:
    """Pointer-walk atomic blocks for merge-or-separate, then role by shape."""
    if not blocks:
        return SheetBlockDecisions(regions=())

    locked_set = set(locked)
    remaining = [
        _OpenBlock(region=region, locked=region in locked_set) for region in blocks
    ]
    remaining.sort(key=lambda item: (item.region.min_row, item.region.min_col))
    resolved_judge = judge if judge is not None else GatewaySpreadsheetRegionJudge()
    finalized: list[_OpenBlock] = []

    while remaining:
        remaining.sort(key=lambda item: (item.region.min_row, item.region.min_col))
        center_index = next(
            (
                index
                for index, item in enumerate(remaining)
                if item.locked or _is_two_dimensional_table(sheet, item.region)
            ),
            None,
        )
        if center_index is None:
            finalized.extend(remaining)
            remaining.clear()
            break
        center = remaining.pop(center_index)
        rejected: set[Region] = set()
        while True:
            neighbors = _four_neighbors(
                center,
                remaining,
                obstacles=[*remaining, *finalized],
                rejected=rejected,
            )
            if not neighbors:
                break
            answers = _merge_answers(sheet, center, neighbors, resolved_judge)
            for direction, choice in answers.items():
                if choice == _CHOICE_SEPARATE and direction in neighbors:
                    rejected.add(neighbors[direction].region)
            merged = _apply_merges(
                center,
                neighbors,
                remaining,
                finalized,
                answers,
            )
            if merged is None:
                break
            center = merged
        finalized.append(center)

    classified = _roles_from_shape(sheet, finalized)
    classified.sort(key=lambda item: (item.region.min_row, item.region.min_col))
    return SheetBlockDecisions(regions=tuple(classified))


def _merge_answers(
    sheet: SheetGrid,
    center: _OpenBlock,
    neighbors: Mapping[str, _OpenBlock],
    judge: SpreadsheetRegionJudge,
) -> Mapping[str, str]:
    directions = tuple(direction for direction in _CLOCKWISE_DIRS if direction in neighbors)
    fallback = {
        direction: _fallback_merge_choice(sheet, center, direction, neighbors[direction])
        for direction in directions
    }
    center_grid = _block_preview(sheet, center.region)
    neighbor_grids = {
        direction: _block_preview(sheet, neighbors[direction].region)
        for direction in directions
    }
    questions = {
        f"merge::{direction}": {
            "type": "choice",
            "instructions": (
                "Center:\n"
                f"{center_grid}\n"
                f"Neighbor ({direction}):\n"
                f"{neighbor_grids[direction]}\n"
                "Pick merge if they are the same spreadsheet table split by an empty "
                "row or column. Pick separate if they are independent."
            ),
            "criteria": {
                _CHOICE_MERGE: "They are the same table; combine them.",
                _CHOICE_SEPARATE: "They are independent tables or a detached fragment.",
            },
        }
        for direction in directions
    }
    allowed = {
        f"merge::{direction}": frozenset({_CHOICE_MERGE, _CHOICE_SEPARATE})
        for direction in directions
    }
    state = {
        "center": center_grid,
        **neighbor_grids,
    }
    chosen = judge.choose(
        state=state,
        questions=questions,
        required_ids=tuple(questions),
        allowed=allowed,
    )
    if chosen is None:
        return fallback
    answers: dict[str, str] = {}
    for direction in directions:
        question_id = f"merge::{direction}"
        choice = chosen.get(question_id)
        if choice not in {_CHOICE_MERGE, _CHOICE_SEPARATE}:
            return fallback
        answers[direction] = choice
    return answers


def _apply_merges(
    center: _OpenBlock,
    neighbors: Mapping[str, _OpenBlock],
    remaining: list[_OpenBlock],
    finalized: Sequence[_OpenBlock],
    answers: Mapping[str, str],
) -> _OpenBlock | None:
    grown = center
    merged_any = False
    for direction in _CLOCKWISE_DIRS:
        if answers.get(direction) != _CHOICE_MERGE or direction not in neighbors:
            continue
        candidate = neighbors[direction]
        if candidate not in remaining:
            continue
        union = bounding_region([grown.region, candidate.region])
        others = [
            item.region
            for item in [*remaining, *finalized]
            if item is not candidate
        ]
        if any(regions_overlap(union, other) for other in others):
            continue
        remaining.remove(candidate)
        grown = _OpenBlock(
            region=union,
            locked=grown.locked or candidate.locked,
        )
        merged_any = True
    if not merged_any:
        return None
    return grown


def _four_neighbors(
    center: _OpenBlock,
    remaining: Sequence[_OpenBlock],
    *,
    obstacles: Sequence[_OpenBlock] | None = None,
    rejected: set[Region] | None = None,
) -> dict[str, _OpenBlock]:
    all_obstacles = obstacles if obstacles is not None else remaining
    neighbors: dict[str, _OpenBlock] = {}
    for direction in _CLOCKWISE_DIRS:
        best: _OpenBlock | None = None
        best_distance: int | None = None
        for candidate in remaining:
            if rejected is not None and candidate.region in rejected:
                continue
            if center.locked and candidate.locked:
                continue
            distance = _direction_distance(center.region, candidate.region, direction)
            if distance is None:
                continue
            if not _axis_aligned(center.region, candidate.region, direction):
                continue
            union = bounding_region([center.region, candidate.region])
            swallows = any(
                other is not candidate and regions_overlap(union, other.region)
                for other in all_obstacles
            )
            if swallows:
                continue
            if best_distance is None or distance < best_distance:
                best = candidate
                best_distance = distance
        if best is not None:
            neighbors[direction] = best
    return neighbors


def _direction_distance(center: Region, candidate: Region, direction: str) -> int | None:
    if direction == "up":
        if candidate.max_row >= center.min_row:
            return None
        return center.min_row - candidate.max_row - 1
    if direction == "down":
        if candidate.min_row <= center.max_row:
            return None
        return candidate.min_row - center.max_row - 1
    if direction == "left":
        if candidate.max_col >= center.min_col:
            return None
        return center.min_col - candidate.max_col - 1
    if direction == "right":
        if candidate.min_col <= center.max_col:
            return None
        return candidate.min_col - center.max_col - 1
    return None


def _axis_aligned(center: Region, candidate: Region, direction: str) -> bool:
    if direction in {"up", "down"}:
        return columns_overlap(center, candidate) or _columns_adjacent(center, candidate)
    return _rows_overlap(center, candidate) or _rows_adjacent(center, candidate)


def _columns_adjacent(left: Region, right: Region) -> bool:
    return left.max_col + 1 == right.min_col or right.max_col + 1 == left.min_col


def _rows_overlap(left: Region, right: Region) -> bool:
    return not (left.max_row < right.min_row or right.max_row < left.min_row)


def _rows_adjacent(left: Region, right: Region) -> bool:
    return left.max_row + 1 == right.min_row or right.max_row + 1 == left.min_row


def _roles_from_shape(
    sheet: SheetGrid, blocks: Sequence[_OpenBlock]
) -> list[ClassifiedRegion]:
    return [
        ClassifiedRegion(
            region=item.region,
            role=(
                _ROLE_TABLE
                if item.locked or _is_two_dimensional_table(sheet, item.region)
                else _ROLE_FRAGMENT
            ),
            locked=item.locked,
        )
        for item in blocks
    ]


def _fallback_merge_choice(
    sheet: SheetGrid,
    center: _OpenBlock,
    direction: str,
    neighbor: _OpenBlock,
) -> str:
    if direction == "up" and _adjacent_caption_above(sheet, neighbor.region, center.region):
        return _CHOICE_MERGE
    if direction == "down" and _adjacent_caption_above(sheet, center.region, neighbor.region):
        return _CHOICE_MERGE
    return _CHOICE_SEPARATE


def _adjacent_caption_above(
    sheet: SheetGrid, upper: Region, lower: Region
) -> bool:
    if upper.max_row + 1 != lower.min_row:
        return False
    if not _is_two_dimensional_table(sheet, lower):
        return False
    if not _is_full_width_title_row(sheet, upper):
        return False
    return columns_overlap(upper, lower)


def _is_two_dimensional_table(sheet: SheetGrid, region: Region) -> bool:
    row_count = region.max_row - region.min_row + 1
    col_count = region.max_col - region.min_col + 1
    if row_count < 2 or col_count < 2:
        return False
    return not _is_single_merge_region(sheet, region)


def _is_single_merge_region(sheet: SheetGrid, region: Region) -> bool:
    covering = merge_at(sheet, region.min_row, region.min_col)
    if covering is None:
        return region.min_row == region.max_row and region.min_col == region.max_col
    min_row, min_col, max_row, max_col = covering
    return (
        min_row == region.min_row
        and min_col == region.min_col
        and max_row == region.max_row
        and max_col == region.max_col
    )


def _is_full_width_title_row(sheet: SheetGrid, region: Region) -> bool:
    if region.min_row != region.max_row:
        return False
    kind, text = classify_sheet_row(
        sheet, region.min_row, region.min_col, region.max_col
    )
    return kind == "caption" and bool(text)


def _block_preview(sheet: SheetGrid, region: Region) -> str:
    row_indexes = _edge_indexes(region.min_row, region.max_row, _BOUNDARY_EDGE)
    col_indexes = _edge_indexes(region.min_col, region.max_col, _BOUNDARY_EDGE)
    lines: list[str] = []
    previous_row: int | None = None
    for row in row_indexes:
        if previous_row is not None and row > previous_row + 1:
            skipped = row - previous_row - 1
            lines.append(f"... (omitted {skipped} rows) ...")
        cells: list[str] = []
        previous_col: int | None = None
        for col in col_indexes:
            if previous_col is not None and col > previous_col + 1:
                cells.append("...")
            cells.append(_cell_preview(sheet, row, col))
            previous_col = col
        lines.append(" | ".join(cells))
        previous_row = row
    return "\n".join(lines)


def _edge_indexes(start: int, end: int, edge: int) -> list[int]:
    count = end - start + 1
    if count <= edge * 2:
        return list(range(start, end + 1))
    top = list(range(start, start + edge))
    bottom = list(range(end - edge + 1, end + 1))
    return top + bottom


def _cell_preview(sheet: SheetGrid, row: int, col: int) -> str:
    merge = merge_at(sheet, row, col)
    origin_row, origin_col = (row, col) if merge is None else (merge[0], merge[1])
    cell = sheet.cells.get((origin_row, origin_col))
    text = display_value(cell)
    shown = "" if text is None else str(text).strip()
    formula = "" if cell is None or not cell.formula else cell.formula
    span = ""
    if merge is not None:
        min_row, min_col, max_row, max_col = merge
        rowspan = max_row - min_row + 1
        colspan = max_col - min_col + 1
        if rowspan > 1 or colspan > 1:
            span = f" rs={rowspan} cs={colspan}"
    if formula:
        return f'"{shown}" formula={formula}{span}'
    return f'"{shown}"{span}'


def _gateway_api_key() -> str:
    from shared.core.config import settings

    vercel = str(getattr(settings, "VERCEL_KEY", "") or "").strip()
    if vercel:
        return vercel
    return str(os.environ.get("AI_GATEWAY_API_KEY", "") or "").strip()


class GatewaySpreadsheetRegionJudge:
    def choose(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, Mapping[str, object]],
        required_ids: Sequence[str],
        allowed: Mapping[str, frozenset[str]],
    ) -> Mapping[str, str] | None:
        from shared.core.config import settings

        api_key = _gateway_api_key()
        if not api_key or not questions:
            return None
        try:
            timeout_seconds = float(
                getattr(settings, "OPENAI_CLIENT_TIMEOUT", 300) or 300
            )
            response = get_sync_client().post(
                _LUNA_EVALUATE_URL,
                json={
                    "model": _LUNA_MODEL,
                    "state": state,
                    "questions": questions,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_seconds,
            )
            if not response.is_success:
                logger.warning(
                    "Luna evaluate HTTP {}: {}",
                    response.status_code,
                    response.reason_phrase,
                )
                return None
            payload = response.json()
        except Exception as exc:
            logger.warning("Luna evaluate failed: {}", exc)
            return None
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            return None
        chosen: dict[str, str] = {}
        for question_id in required_ids:
            item = answers.get(question_id)
            choice = item.get("choice") if isinstance(item, dict) else None
            allowed_choices = allowed.get(question_id, frozenset())
            if not isinstance(choice, str) or choice not in allowed_choices:
                return None
            chosen[question_id] = choice
        return chosen
