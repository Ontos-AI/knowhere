"""Progressive spreadsheet baseline: one coarse table per sheet, no LLM.

Each parseable sheet is rendered whole, without region splitting, header
detection, satellite merging or summaries. Sheets are grouped by resolved
cross-sheet formula references; every group is one refinement scope, so a
retrieval hit on any of its sheets refines the whole group together.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pandas as pd

from app.services.document_parser.assets.asset_file_names import (
    ASSET_KIND_TABLE,
    build_asset_file_stem,
    build_asset_index_label,
)
from app.services.document_parser.formats.excel.formula_graph import build_formula_graph
from app.services.document_parser.formats.excel.header_detect import HeaderSpec
from app.services.document_parser.formats.excel.html_render import (
    render_table_html,
    visible_columns,
)
from app.services.document_parser.formats.excel.region_split import find_effective_range
from app.services.document_parser.formats.excel.sheet_groups import (
    INCLUDE_HIDDEN_SHEETS,
    parseable_sheet_names,
    plan_sheet_groups,
)
from app.services.document_parser.formats.excel.table_parser import (
    build_excel_table_path,
    excel_rows_to_dataframe,
    excel_table_label,
)
from app.services.document_parser.formats.excel.workbook_grid import (
    Region,
    SheetGrid,
    load_workbook_grid,
)
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import ParsedRow
from app.services.document_parser.support.path_helpers import remove_spaces
from app.services.document_parser.tables.table_asset_writer import (
    TableAssetInput,
    write_table_asset,
)
from openpyxl.utils import get_column_letter
from shared.models.database.document import SCOPE_KIND_SHEET_GROUP
from shared.services.jobs.lifecycle.scope_enrichments import COARSE_SCOPE_STATE
from shared.services.retrieval.search.lexical_text import COARSE_SEARCH_TEXT_KEY

# Normalized workbook kept beside the baseline so refinement parses the same
# sheets the baseline grouped (xls/csv conversion is not re-run).
WORKBOOK_REL = "source_workbook.xlsx"

_WHOLE_SHEET_HEADERS = HeaderSpec(
    header_start_row=0,
    header_row_count=0,
    row_header_col_count=0,
    column_headers=(),
    caption=None,
)


def parse_excel_baseline(
    xlsx_path: str,
    *,
    file_name: str,
    output_dir: str,
    relative_root: str | None,
) -> pd.DataFrame:
    """Emit one coarse table row per parseable sheet, tagged with its sheet group."""
    os.makedirs(os.path.join(output_dir, "tables"), exist_ok=True)
    persisted_workbook = os.path.join(output_dir, WORKBOOK_REL)
    work_path = Path(xlsx_path).resolve()
    if work_path != Path(persisted_workbook).resolve():
        # xls/csv conversions are written under output_dir: move them so the
        # published output holds one workbook copy; an xlsx source is copied.
        if Path(output_dir).resolve() in work_path.parents:
            os.replace(work_path, persisted_workbook)
        else:
            shutil.copyfile(work_path, persisted_workbook)

    workbook = load_workbook_grid(persisted_workbook)
    sheet_names = parseable_sheet_names(
        workbook, include_hidden_sheets=INCLUDE_HIDDEN_SHEETS
    )
    groups = plan_sheet_groups(workbook, sheet_names, build_formula_graph(workbook))
    group_by_sheet = {
        name: group for group in groups for name in group.sheet_names
    }
    ordinals = {sheet.name: index for index, sheet in enumerate(workbook.sheets, 1)}
    sheets = workbook.sheet_by_name()
    document_root = str(relative_root or file_name)
    time_stamp = get_str_time()

    rows: list[ParsedRow] = []
    for sheet_name in sheet_names:
        sheet = sheets[sheet_name]
        # Occupied extent only: openpyxl dimensions also count styled empty cells.
        effective = find_effective_range(
            sheet, (1, sheet.max_row), (1, sheet.max_col), claimed=()
        )
        if effective is None:
            continue
        (min_row, max_row), (min_col, max_col) = effective
        region = Region(min_row=min_row, min_col=min_col, max_row=max_row, max_col=max_col)
        html = render_table_html(sheet, region, _WHOLE_SHEET_HEADERS)
        if not html:
            continue
        group = group_by_sheet[sheet_name]
        ordinal = ordinals[sheet_name]
        table_name = (
            build_asset_file_stem(ASSET_KIND_TABLE, ordinal, remove_spaces(sheet_name))
            + ".html"
        )
        rows.append(
            write_table_asset(
                TableAssetInput(
                    html=html,
                    output_dir=output_dir,
                    table_name=table_name,
                    summary=build_asset_index_label(ASSET_KIND_TABLE, ordinal),
                    keywords="",
                    know_id=gen_str_codes(
                        html + excel_table_label(sheet_name=sheet_name, subtable_title="")
                    ),
                    addtime=time_stamp,
                    path=build_excel_table_path(
                        document_root=document_root,
                        sheet_name=sheet_name,
                        subtable_title="",
                    ),
                    asset_path=f"tables/{table_name}",
                    extra_metadata={
                        "header_row_count": 0,
                        "row_header_col_count": 0,
                        "column_headers": [],
                        "sheet_name": sheet_name,
                        "excel_range": (
                            f"{get_column_letter(min_col)}{min_row}:"
                            f"{get_column_letter(max_col)}{max_row}"
                        ),
                        "scope_id": group.scope_id,
                        "scope_kind": SCOPE_KIND_SHEET_GROUP,
                        "sheet_names": list(group.sheet_names),
                        "enrichment_state": COARSE_SCOPE_STATE,
                        COARSE_SEARCH_TEXT_KEY: _coarse_search_text(sheet, region),
                    },
                )
            )
        )
    return excel_rows_to_dataframe(rows)


def _coarse_search_text(sheet: SheetGrid, region: Region) -> str:
    """Deduplicated text cells of the rendered region, in reading order.

    A text cell is one whose cached value Excel typed as a string; numeric,
    date, boolean and uncomputed-formula cells are left out.
    """
    columns = visible_columns(sheet, region)
    texts: dict[str, None] = {}
    for row in range(region.min_row, region.max_row + 1):
        for col in columns:
            cell = sheet.cells.get((row, col))
            if cell is None or not isinstance(cell.value, str):
                continue
            text = cell.value.strip()
            if text:
                texts[text] = None
    return "\n".join(texts)

