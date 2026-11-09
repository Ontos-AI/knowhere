from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from loguru import logger

from shared.services.ai.llm_overrides import get_text_client
from shared.services.ai.response_process_service import eval_response

# Cell preview length from snap-fill ``build_multiline_rows_text``.
_CELL_TEXT_PREVIEW_CHARS = 30
_WHITESPACE_RE = re.compile(r"\s+")

_SYSTEM_PROMPT = (
    "你是表格结构分析专家。你的任务是判断表格区域的行列头边界，只输出 JSON。"
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
    header_row_count: int
    row_header_col_count: int
    column_headers: tuple[str, ...]
    caption: str | None
    data_end_row: int


class SpreadsheetHeaderDetector(Protocol):
    def detect_spreadsheet_headers(
        self, rows_text: str, rowspan_hint: int
    ) -> tuple[int, int, int] | None:
        """Return ``(header_row_count, row_header_col_count, data_end_row)`` or None."""


def build_region_visual(
    ws,
    row_range: tuple[int, int],
    col_range: tuple[int, int],
    merged_ranges: list,
) -> tuple[tuple[VisualCell | None, ...], ...]:
    """Build a visual grid for one geometric region from worksheet + merges."""
    row_start, row_end = row_range
    col_start, col_end = col_range
    if row_end < row_start or col_end < col_start:
        return ()

    n_rows = row_end - row_start + 1
    n_cols = col_end - col_start + 1
    grid: list[list[VisualCell | None]] = [
        [None] * n_cols for _ in range(n_rows)
    ]
    for row in range(row_start, row_end + 1):
        for col in range(col_start, col_end + 1):
            rel_row = row - row_start
            rel_col = col - col_start
            merge = _containing_merge(row, col, merged_ranges)
            if merge is not None:
                origin_row = merge.min_row
                origin_col = merge.min_col
                cell = VisualCell(
                    text=_visual_cell_text(ws.cell(origin_row, origin_col).value),
                    rowspan=merge.max_row - merge.min_row + 1,
                    colspan=merge.max_col - merge.min_col + 1,
                    origin_row=origin_row - row_start,
                    origin_col=origin_col - col_start,
                )
            else:
                cell = VisualCell(
                    text=_visual_cell_text(ws.cell(row, col).value),
                    rowspan=1,
                    colspan=1,
                    origin_row=rel_row,
                    origin_col=rel_col,
                )
            grid[rel_row][rel_col] = cell
    return tuple(tuple(row) for row in grid)


def render_region_rows_text(
    visual: Sequence[Sequence[VisualCell | None]],
) -> tuple[str, int]:
    """Port of snap-fill ``build_multiline_rows_text`` onto a region's visual cells."""
    if not visual:
        return "", 1

    max_cols = len(visual[0])
    rowspan_hint = 1
    lines: list[str] = []
    for rel_idx, row in enumerate(visual):
        cells_info: list[str] = []
        for col_idx in range(max_cols):
            cell = row[col_idx] if col_idx < len(row) else None
            if cell is None:
                cells_info.append('""')
                continue
            if rel_idx == 0 and col_idx == 0:
                rowspan_hint = cell.rowspan
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
    return "\n".join(lines), rowspan_hint


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
    visual = region.visual
    n_rows = len(visual)
    n_cols = len(visual[0]) if visual else 0
    last_row = max(n_rows - 1, 0)
    if n_rows == 0:
        return HeaderSpec(
            header_row_count=0,
            row_header_col_count=0,
            column_headers=(),
            caption=None,
            data_end_row=0,
        )

    detector = llm if llm is not None else TextClientSpreadsheetHeaderDetector()
    rows_text, rowspan_hint = render_region_rows_text(visual)
    triple: tuple[int, int, int] | None = None
    if rows_text:
        triple = detector.detect_spreadsheet_headers(rows_text, rowspan_hint)
    if triple is not None:
        header_row_count, row_header_col_count, data_end_row = triple
        if header_row_count == 0:
            row_header_col_count = 0
        triple = (header_row_count, row_header_col_count, data_end_row)
    triple = _validated_triple(triple, n_rows=n_rows, n_cols=n_cols)
    if triple is None:
        triple = (1, 0, last_row)

    header_row_count, row_header_col_count, data_end_row = triple
    column_headers = tuple(expand_column_headers(visual, header_row_count))
    return HeaderSpec(
        header_row_count=header_row_count,
        row_header_col_count=row_header_col_count,
        column_headers=column_headers,
        caption=None,
        data_end_row=data_end_row,
    )


class TextClientSpreadsheetHeaderDetector:
    def detect_spreadsheet_headers(
        self, rows_text: str, rowspan_hint: int
    ) -> tuple[int, int, int] | None:
        try:
            client, _ = get_text_client()
            raw = client.chat_completion(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _spreadsheet_header_user_prompt(
                            rows_text, rowspan_hint
                        ),
                    },
                ],
                timeout=60,
                usage_task="parser.excel.detect_spreadsheet_headers",
            )
            parsed = eval_response(raw)
        except Exception as exc:
            logger.warning(
                "Spreadsheet header detection LLM failed: {}",
                exc,
            )
            return None
        return _parse_header_triple(parsed)


def _containing_merge(row: int, col: int, merged_ranges: list):
    for merged_range in merged_ranges:
        if (
            merged_range.min_row <= row <= merged_range.max_row
            and merged_range.min_col <= col <= merged_range.max_col
        ):
            return merged_range
    return None


def _visual_cell_text(value: object) -> str:
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value).strip())


def _same_origin(left: VisualCell | None, right: VisualCell | None) -> bool:
    if left is None or right is None:
        return False
    return left.origin_row == right.origin_row and left.origin_col == right.origin_col


def _parse_header_triple(parsed: object) -> tuple[int, int, int] | None:
    if not isinstance(parsed, dict):
        return None
    header_row_count = _non_negative_int(parsed.get("header_row_count"))
    row_header_col_count = _non_negative_int(parsed.get("row_header_col_count"))
    data_end_row = _non_negative_int(parsed.get("data_end_row"))
    if (
        header_row_count is None
        or row_header_col_count is None
        or data_end_row is None
    ):
        return None
    return header_row_count, row_header_col_count, data_end_row


def _validated_triple(
    triple: tuple[int, int, int] | None,
    *,
    n_rows: int,
    n_cols: int,
) -> tuple[int, int, int] | None:
    if triple is None:
        return None
    header_row_count, row_header_col_count, data_end_row = triple
    if header_row_count > n_rows or row_header_col_count > n_cols:
        return None
    if data_end_row >= n_rows:
        return None
    if header_row_count >= 1 and data_end_row < header_row_count - 1:
        return None
    return triple


def _non_negative_int(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _spreadsheet_header_user_prompt(rows_text: str, rowspan_hint: int) -> str:
    rowspan_section = ""
    if rowspan_hint > 1:
        rowspan_section = f"""
        ## Rowspan 辅助信号
        第一列单元格的 rowspan = {rowspan_hint}，这意味着该单元格从第 0 行开始跨越 {rowspan_hint} 行。
        这通常表示：
        - 第一列是行表头 (row header)
        - 数据区的最后一行 = 第 0 行 + {rowspan_hint} - 1 = 第 {rowspan_hint - 1} 行
        请将此作为 data_end_row 的**强参考信号**（但以实际结构为准）。
        """

    return f"""# 🎯 任务目标
        你是表格结构分析专家。以下是一个表格区域的行文本摘要。
        请分析并确定该区域的精确边界。

        # 行文本摘要
        ```
        {rows_text}
        ```
        {rowspan_section}
        # 🔍 判断步骤

        ## Step 0. 前置判断：是否存在独立的列表头
        在给出具体行列表头数量前，先判断该区域**是否存在一行或多行专门作为列表头**

        - **存在独立列表头**：存在 1 行或多行专门承载**横向列名**（值列上是字段名），这些行之后才是若干行取值的数据记录
          → 按下面 Step 1 / Step 2 给出 `header_row_count >= 1` 与 `row_header_col_count`

        - **不存在独立列表头**：整块区域是若干条**并列的同构记录**，
          每一行形式和结构一致，只有取值在不同行之间变化
          字段名以**内嵌提示**的形式散落在每一行内部（即每一行自己就带着字段名/提示词）
          → 确认 `header_row_count = 0` 且 `row_header_col_count = 0`，跳过 Step 1 / Step 2，只继续判定 Step 3 的 `data_end_row`。

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

        ## Step 3. data_end_row（数据区最后一行的行号，始终需要给出）
        从第 0 行开始计数，数据区（取值记录行）的最后一行行号。
        - `header_row_count >= 1` 时，数据区从列表头之后开始；数据行特征：值列主要是已填的具体值（数字、名称、日期等），而不是新的列名
        - `header_row_count = 0` 时，数据区就是整个候选区域，每一行都是一条数据记录；
          `data_end_row` = 最后一条同构记录的行号（下一行若出现不同结构的 KV / 说明，则在它之前收尾）
        - 非数据行的特征：有和本区域结构明显不同的标签字段，或 cell 数量与前面数据行明显不同

        # 📤 输出格式
        返回 JSON 对象，必须包含下面三个字段：
        ```json
        {{
            "header_row_count": 1,
            "row_header_col_count": 1,
            "data_end_row": 4
        }}
        ```
        只返回 JSON，不要其他内容。"""
