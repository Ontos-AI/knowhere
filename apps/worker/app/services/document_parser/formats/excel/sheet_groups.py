"""Group spreadsheet sheets that resolved formulas connect across sheets.

Each group is one progressive refinement scope: refining all of its sheets in
one structure pass yields the same satellites and cross-table links as a
whole-workbook pass, because no resolved formula crosses group boundaries.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger

from app.services.document_parser.formats.excel.formula_graph import FormulaNode
from app.services.document_parser.formats.excel.workbook_grid import WorkbookGrid
from shared.models.database.document import SHEET_GROUP_SCOPE_PREFIX

# Full parse, progressive baseline and group refinement read the same sheets.
INCLUDE_HIDDEN_SHEETS = False


@dataclass(frozen=True)
class SheetGroup:
    # 1-based workbook position of the group's first sheet.
    ordinal: int
    sheet_names: tuple[str, ...]

    @property
    def scope_id(self) -> str:
        return f"{SHEET_GROUP_SCOPE_PREFIX}{self.ordinal}"


def parseable_sheet_names(
    workbook: WorkbookGrid,
    *,
    include_hidden_sheets: bool,
) -> list[str]:
    """Sheets the structure parser reads, in workbook order."""
    names: list[str] = []
    for sheet in workbook.sheets:
        if not include_hidden_sheets and sheet.hidden:
            logger.info(f"Sheet '{sheet.name}' is hidden, skipping")
            continue
        if sheet.max_row == 0 or sheet.max_col == 0:
            logger.debug(f"Sheet '{sheet.name}' is empty, skipping")
            continue
        names.append(sheet.name)
    return names


def plan_sheet_groups(
    workbook: WorkbookGrid,
    sheet_names: Sequence[str],
    graph: Sequence[FormulaNode],
) -> list[SheetGroup]:
    """Union parsed sheets linked by resolved formula references.

    Unresolved formula nodes are skipped, as the structure parser skips them
    when it attaches satellites and builds table links.
    """
    parent: dict[str, str] = {name: name for name in sheet_names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for node in graph:
        if node.unresolved or node.sheet not in parent:
            continue
        for ref in node.refs:
            if ref.sheet == node.sheet or ref.sheet not in parent:
                continue
            source_root, target_root = find(node.sheet), find(ref.sheet)
            if source_root != target_root:
                parent[target_root] = source_root

    ordinals = {sheet.name: index for index, sheet in enumerate(workbook.sheets, 1)}
    members: dict[str, list[str]] = {}
    for name in sheet_names:
        members.setdefault(find(name), []).append(name)
    groups = [
        SheetGroup(ordinal=ordinals[names[0]], sheet_names=tuple(names))
        for names in members.values()
    ]
    groups.sort(key=lambda group: group.ordinal)
    return groups
