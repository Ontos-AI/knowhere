from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from loguru import logger

from app.services.document_parser.formats.excel.workbook_grid import (
    Region,
    SheetGrid,
    display_value,
    expand_visual_grid,
)
from shared.services.ai.llm_overrides import get_text_client
from shared.services.ai.response_process_service import eval_response

# Cell preview length from snap-fill ``build_multiline_rows_text``.
_CELL_TEXT_PREVIEW_CHARS = 30
# Rows shown to each header prompt, counted from the scan start; a vertical
# merge crossing the last row extends the window to the merge bottom.
_HEADER_SCAN_ROWS = 10
_WHITESPACE_RE = re.compile(r"\s+")

_SYSTEM_PROMPT = (
    "你是表格结构分析专家。你的任务是判断表格区域的行列头边界，只输出 JSON。"
)
_LOCATE_SYSTEM_PROMPT = (
    "你是表格结构分析专家。任务是在行文本摘要内定位列表区域的起始行（LIST-HEAD）。"
    "名单列表与矩阵交叉表都算列表区域。只输出 JSON。"
)


@dataclass(frozen=True)
class VisualCell:
    text: str
    rowspan: int
    colspan: int
    origin_row: int
    origin_col: int


@dataclass(frozen=True)
class TableRegion:
    visual: tuple[tuple[VisualCell | None, ...], ...]


@dataclass(frozen=True)
class HeaderSpec:
    """Header layout of one region; row counts are relative to the region.

    ``header_start_row`` is the first column-header row (snap-fill LIST_HEAD);
    rows above it are not part of the rendered table body.
    """

    header_start_row: int
    header_row_count: int
    row_header_col_count: int
    column_headers: tuple[str, ...]
    caption: str | None


@dataclass(frozen=True)
class HeaderStart:
    """Phase 0 answer: ``row`` is None when the region has no column header."""

    row: int | None


class SpreadsheetHeaderDetector(Protocol):
    def locate_header_start(self, rows_text: str) -> HeaderStart | None:
        """Return the Phase 0 answer, or None when the call fails."""

    def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
        """Return ``(header_row_count, row_header_col_count)`` or None."""


def build_region_visual(
    sheet: SheetGrid,
    region: Region,
) -> tuple[tuple[VisualCell | None, ...], ...]:
    """Build a visual grid for one geometric region from the sheet grid."""
    expanded = expand_visual_grid(sheet, region)
    if not expanded:
        return ()
    visual: list[list[VisualCell | None]] = []
    for rel_row, row_cells in enumerate(expanded):
        visual_row: list[VisualCell | None] = []
        for rel_col, cell in enumerate(row_cells):
            if cell is None:
                visual_row.append(
                    VisualCell(
                        text="",
                        rowspan=1,
                        colspan=1,
                        origin_row=rel_row,
                        origin_col=rel_col,
                    )
                )
                continue
            if cell.merge_origin is None:
                origin_row = rel_row
                origin_col = rel_col
            else:
                origin_row = cell.merge_origin[0] - region.min_row
                origin_col = cell.merge_origin[1] - region.min_col
            visual_row.append(
                VisualCell(
                    text=_visual_cell_text(display_value(cell)),
                    rowspan=cell.rowspan,
                    colspan=cell.colspan,
                    origin_row=origin_row,
                    origin_col=origin_col,
                )
            )
        visual.append(visual_row)
    return tuple(tuple(row) for row in visual)


def render_region_rows_text(
    visual: Sequence[Sequence[VisualCell | None]],
) -> str:
    """Port of snap-fill ``build_multiline_rows_text`` onto a region's visual cells."""
    if not visual:
        return ""

    max_cols = len(visual[0])
    lines: list[str] = []
    for rel_idx, row in enumerate(visual):
        cells_info: list[str] = []
        for col_idx in range(max_cols):
            cell = row[col_idx] if col_idx < len(row) else None
            if cell is None:
                cells_info.append('""')
                continue
            is_colspan_continuation = col_idx > 0 and _same_origin(
                cell, row[col_idx - 1] if col_idx - 1 < len(row) else None
            )
            is_rowspan_continuation = (
                not is_colspan_continuation
                and rel_idx > 0
                and col_idx < len(visual[rel_idx - 1])
                and _same_origin(cell, visual[rel_idx - 1][col_idx])
            )
            if is_colspan_continuation:
                cell_desc = "[colspan-continued]"
            elif is_rowspan_continuation:
                cell_desc = "[rowspan-covered]"
            else:
                text = cell.text
                cell_desc = f'"{text[:_CELL_TEXT_PREVIEW_CHARS]}"' if text else '""'
                if cell.colspan > 1:
                    cell_desc += f" cs={cell.colspan}"
                if cell.rowspan > 1:
                    cell_desc += f" rs={cell.rowspan}"
            cells_info.append(cell_desc)
        lines.append(
            f"Row {rel_idx} ({max_cols} cells, U): {' | '.join(cells_info)}"
        )
    return "\n".join(lines)


def expand_column_headers(
    visual: Sequence[Sequence[VisualCell | None]],
    header_row_count: int,
) -> list[str]:
    """Deterministic parent-->child flatten of the first ``header_row_count`` visual rows."""
    if header_row_count <= 0 or not visual:
        return []

    header_rows = visual[:header_row_count]
    max_cols = len(header_rows[0]) if header_rows else 0
    if max_cols == 0:
        return []

    headers: list[str] = []
    for col_idx in range(max_cols):
        parts: list[str] = []
        prev_text: str | None = None
        for row in header_rows:
            cell = row[col_idx] if col_idx < len(row) else None
            text = cell.text if cell is not None else ""
            if text and text != prev_text:
                parts.append(text)
                prev_text = text
        headers.append("-->".join(parts) if parts else "")
    return headers


def detect_headers(
    region: TableRegion,
    *,
    llm: SpreadsheetHeaderDetector | None = None,
) -> HeaderSpec:
    """snap-fill header flow: Phase 0 start row, Rule A, Phase 0.5 counts."""
    visual = region.visual
    n_rows = len(visual)
    n_cols = len(visual[0]) if visual else 0
    if n_rows == 0:
        return HeaderSpec(
            header_start_row=0,
            header_row_count=0,
            row_header_col_count=0,
            column_headers=(),
            caption=None,
        )

    detector = llm if llm is not None else TextClientSpreadsheetHeaderDetector()
    locate_end = _scan_window_end(visual, 0)
    answer = detector.locate_header_start(
        render_region_rows_text(visual[: locate_end + 1])
    )
    start_row = 0
    if answer is not None and answer.row is None:
        return HeaderSpec(
            header_start_row=0,
            header_row_count=0,
            row_header_col_count=0,
            column_headers=(),
            caption=None,
        )
    if answer is not None and answer.row is not None and answer.row <= locate_end:
        start_row = _expand_start_by_first_column_merge(visual, answer.row)

    count_end = _scan_window_end(visual, start_row)
    header_visual = visual[start_row:]
    pair = detector.detect_spreadsheet_headers(
        render_region_rows_text(visual[start_row : count_end + 1])
    )
    if pair is not None:
        header_row_count, row_header_col_count = pair
        if header_row_count == 0:
            row_header_col_count = 0
        pair = (header_row_count, row_header_col_count)
    pair = _validated_pair(pair, n_rows=count_end - start_row + 1, n_cols=n_cols)
    if pair is None:
        pair = (1, 0)

    header_row_count, row_header_col_count = pair
    if header_row_count > 0:
        row_header_col_count = max(
            row_header_col_count,
            _first_column_label_colspan(visual, start_row),
        )
    column_headers = tuple(expand_column_headers(header_visual, header_row_count))
    return HeaderSpec(
        header_start_row=start_row,
        header_row_count=header_row_count,
        row_header_col_count=row_header_col_count,
        column_headers=column_headers,
        caption=None,
    )


def _scan_window_end(
    visual: Sequence[Sequence[VisualCell | None]], start_row: int
) -> int:
    """Last row of a header prompt window starting at ``start_row``.

    The window holds ``_HEADER_SCAN_ROWS`` rows and grows to the bottom of any
    merge that crosses its last row, so a merged header is never cut in half.
    """
    last_row = len(visual) - 1
    end_row = min(last_row, start_row + _HEADER_SCAN_ROWS - 1)
    while True:
        merge_bottom = max(
            (
                cell.origin_row + cell.rowspan - 1
                for cell in visual[end_row]
                if cell is not None
            ),
            default=end_row,
        )
        if merge_bottom <= end_row:
            return end_row
        end_row = min(last_row, merge_bottom)
        if end_row == last_row:
            return end_row


def _expand_start_by_first_column_merge(
    visual: Sequence[Sequence[VisualCell | None]], start_row: int
) -> int:
    """snap-fill Rule A: a first-column merge ending on the start row lifts it.

    When the model picks the leaf row of a multi-level header, the left-column
    label (e.g. ``序号``) spans from the parent row down to that leaf row; the
    start moves up to the top of that merge. It never moves down.
    """
    cell = visual[start_row][0] if visual[start_row] else None
    if cell is None:
        return start_row
    merge_bottom = cell.origin_row + cell.rowspan - 1
    if cell.origin_row < start_row and merge_bottom == start_row:
        return max(cell.origin_row, 0)
    return start_row


def _first_column_label_colspan(
    visual: Sequence[Sequence[VisualCell | None]], start_row: int
) -> int:
    """snap-fill region label: a vertically merged first-column label at the
    header start counts its full width as row-header columns."""
    cell = visual[start_row][0] if visual[start_row] else None
    if cell is None or not cell.text or cell.rowspan < 2:
        return 0
    if cell.origin_row != start_row or cell.origin_col != 0:
        return 0
    return cell.colspan


class TextClientSpreadsheetHeaderDetector:
    def locate_header_start(self, rows_text: str) -> HeaderStart | None:
        parsed = _ask_json(
            system_prompt=_LOCATE_SYSTEM_PROMPT,
            user_prompt=_header_start_user_prompt(rows_text),
            usage_task="parser.excel.locate_header_start",
        )
        return _parse_header_start(parsed)

    def detect_spreadsheet_headers(self, rows_text: str) -> tuple[int, int] | None:
        parsed = _ask_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_spreadsheet_header_user_prompt(rows_text),
            usage_task="parser.excel.detect_spreadsheet_headers",
        )
        return _parse_header_pair(parsed)


def _ask_json(*, system_prompt: str, user_prompt: str, usage_task: str) -> object:
    try:
        client, _ = get_text_client()
        raw = client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=60,
            usage_task=usage_task,
        )
        return eval_response(raw)
    except Exception as exc:
        logger.warning("Spreadsheet header LLM call {} failed: {}", usage_task, exc)
        return None


def _visual_cell_text(value: object) -> str:
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value).strip())


def _same_origin(left: VisualCell | None, right: VisualCell | None) -> bool:
    if left is None or right is None:
        return False
    return left.origin_row == right.origin_row and left.origin_col == right.origin_col


def _parse_header_start(parsed: object) -> HeaderStart | None:
    if not isinstance(parsed, dict) or "list_head_row" not in parsed:
        return None
    raw = parsed["list_head_row"]
    if raw is None:
        return HeaderStart(row=None)
    row = _non_negative_int(raw)
    if row is None:
        return None
    return HeaderStart(row=row)


def _parse_header_pair(parsed: object) -> tuple[int, int] | None:
    if not isinstance(parsed, dict):
        return None
    header_row_count = _non_negative_int(parsed.get("header_row_count"))
    row_header_col_count = _non_negative_int(parsed.get("row_header_col_count"))
    if header_row_count is None or row_header_col_count is None:
        return None
    return header_row_count, row_header_col_count


def _validated_pair(
    pair: tuple[int, int] | None,
    *,
    n_rows: int,
    n_cols: int,
) -> tuple[int, int] | None:
    if pair is None:
        return None
    header_row_count, row_header_col_count = pair
    if header_row_count > n_rows or row_header_col_count > n_cols:
        return None
    return pair


def _non_negative_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _spreadsheet_header_user_prompt(rows_text: str) -> str:
    return f"""# 🎯 任务目标
        你是表格结构分析专家。以下是一个表格区域的行文本摘要。
        请分析并确定该区域的行列表头。

        # 行文本摘要
        ```
        {rows_text}
        ```

        # 🔍 判断步骤

        ## Step 0. 前置判断：是否存在独立的列表头
        在给出具体行列表头数量前，先判断该区域**是否存在一行或多行专门作为列表头**

        - **存在独立列表头**：存在 1 行或多行专门承载**横向列名**（值列上是字段名），这些行之后才是若干行取值的数据记录
          → 按下面 Step 1 / Step 2 给出 `header_row_count >= 1` 与 `row_header_col_count`

        - **不存在独立列表头**：整块区域是若干条**并列的同构记录**，
          每一行形式和结构一致，只有取值在不同行之间变化
          字段名以**内嵌提示**的形式散落在每一行内部（即每一行自己就带着字段名/提示词）
          → 确认 `header_row_count = 0` 且 `row_header_col_count = 0`，跳过 Step 1 / Step 2。

        ## Step 1. header_row_count（列表头行数，仅在Step0判断存在独立列表头时给出，否则=0）
        从第 0 行开始，连续多少行属于**列表头**（即列名/字段名行）。
        - 通常 = 1（只有一行列名）
        - 列表头行必须是**列名字行**：去掉左侧行表头列之后，其余**值列**应主要是字段名/列名，而不是取值行里的具体数值或业务文本
        - 多级列名（>1）仅当后续行在**值列**上仍是列名/子列名时成立（如上一行大类、下一行更细列名）
        - 若某行只有左侧少数格有标签文字，值列是具体取值 / `[rowspan-covered]` / `[colspan-continued]`，
          这是**数据行上的行标签**，不是列表头——不要因此增大 `header_row_count`
        - 相邻两行 cell 数量不同，并不足以证明后一行仍是列表头；仍要以值列是否承载列名为准

        ## Step 2. row_header_col_count（行表头列数，仅在Step0判断存在独立列表头时给出，否则=0）
        从左侧第 0 列起，连续多少列属于**行表头**（即行标签列）。
        - 行表头是纵向标签/分区名/序号，和列表头（横向列名）不是一回事
        - 序号列（1, 2, 3...）一般算行表头
        - 左侧 rowspan 区域标签、以及数据行里左侧的角色/分组标签，都计入行表头列，而不是列表头行

        # 📤 输出格式
        返回 JSON 对象，必须包含下面两个字段：
        ```json
        {{
            "header_row_count": 1,
            "row_header_col_count": 1
        }}
        ```
        只返回 JSON，不要其他内容。"""


def _header_start_user_prompt(rows_text: str) -> str:
    """Port of snap-fill ``classify_list_head_flags_batch`` for one region."""
    return f"""# 概念定义
        **列表区域**：有一行共享列约束（LIST-HEAD），其下连续多行按同一组列填写。
        下列两类都是列表区域。

        ## 类型 A · 名单列表（每行一条同类记录）
        LIST-HEAD = 列名行；其下每行一条同类记录。

        ## 类型 B · 矩阵交叉表（列头 × 行头）
        LIST-HEAD = 列名行（第 0 格常是角落标签）；其下第 0 列为行标签，其余为交叉格。
        **不要因为第 0 列有字就否决。**

        共用：LIST-HEAD 是起始行（纯列名，或列名与首条记录混排）；整块是共享列约束下的连续区域。

        不属于列表区域：
        - 横向一问一答（标签|值|标签|值），纵向不共享同一组列重复
        - 自由描述、问句、说明、贯穿整表的标题/长文格

        # 任务目标
        找出下面行文本摘要中列表区域的 LIST-HEAD 行号（即 `Row N` 的 N）。
        LIST-HEAD 可能是第 0 行，也可能在中间。

        # 行文本摘要
        `[colspan-continued]` / `[rowspan-covered]` 表示被左侧或上方合并单元格覆盖的格；
        `cs=` / `rs=` 为合并单元格的列跨度 / 行跨度。
        ```
        {rows_text}
        ```

        # 判断规则
        - 类型 A / B 都返回列头行的行号；矩阵表不要返回 null
        - 仅当这些行里确实不是列表区域时返回 `list_head_row: null`
        - `list_head_row` 必须是摘要中某个 `Row N` 的 N

        # 输出格式
        返回 JSON 对象：
        ```json
        {{"list_head_row": 3}}
        ```
        或
        ```json
        {{"list_head_row": null}}
        ```
        只返回 JSON，不要其他内容。"""
