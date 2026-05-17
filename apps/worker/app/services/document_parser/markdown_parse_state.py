from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.services.document_parser.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.parser_rows import ParsedRow, ParsedRowsBuilder

ParserRowValues = list[str | int]

RowUpdater = Callable[
    [list[ParserRowValues], list[str], str, dict[str, Any], str, str, int, bool],
    list[ParserRowValues],
]


@dataclass
class MarkdownParseState:
    relative_root: str
    split_char: str
    llm_parameters: dict[str, Any]
    timestamp: str
    row_updater: RowUpdater
    rows: list[ParserRowValues] = field(default_factory=list)
    content_items: list[str] = field(default_factory=list)
    path_stack: list[tuple[str, int]] = field(default_factory=list)
    inner_paths: list[str] = field(default_factory=list)
    error_line_numbers: list[int] = field(default_factory=list)
    table_lines: list[str] = field(default_factory=list)
    current_page_number: int = 0
    chunk_pages: set[int] = field(default_factory=set)
    base_level: int | None = None
    path: str = ""
    path_counter: dict[str, int] = field(default_factory=dict)
    deferred_llm_tasks: list[tuple[Any, ...]] = field(default_factory=list)
    seen_images: dict[str, dict[str, str]] = field(default_factory=dict)
    image_count: int = 1
    table_count: int = 1

    def __post_init__(self) -> None:
        if not self.path:
            self.path = self.relative_root

    def record_page_marker(self, line: str) -> bool:
        if "<!--" not in line or "-->" not in line:
            return False
        if "page" not in line and "Slide number" not in line:
            return False

        page_match = re.search(r"page\s+(\d+)", line)
        if page_match:
            self.current_page_number = int(page_match.group(1))
        else:
            self.current_page_number += 1
        self.chunk_pages.add(self.current_page_number)
        return True

    def flush_current_content(self) -> None:
        page_numbers = self._format_chunk_pages()
        self.rows = self.row_updater(
            self.rows,
            self.content_items,
            self.path,
            self.llm_parameters,
            self.timestamp,
            page_numbers,
            1500,
            True,
        )
        self.content_items = []
        self.chunk_pages = set()
        if self.current_page_number > 0:
            self.chunk_pages.add(self.current_page_number)

    def flush_placeholder_chunk(self) -> None:
        page_numbers = self._format_chunk_pages()
        self.rows = self.row_updater(
            self.rows,
            [],
            self.path,
            self.llm_parameters,
            self.timestamp,
            page_numbers,
            1500,
            True,
        )

    def enter_heading(self, heading: str, level: int) -> None:
        if self.base_level is None:
            self.base_level = level
        elif level < self.base_level:
            self.base_level = level

        adjusted_level = level - self.base_level + 1
        self.path_stack = [
            (item_heading, item_level)
            for item_heading, item_level in self.path_stack
            if item_level < adjusted_level
        ]

        current_heading = (
            heading.replace(self.split_char, "∕")
            if self.split_char in heading
            else heading
        )
        tentative_names = [item_heading for item_heading, _ in self.path_stack]
        tentative_names.append(current_heading)
        tentative_path_parts = [self.relative_root] if self.relative_root else []
        tentative_path_parts.extend(tentative_names)
        tentative_path = self.split_char.join(tentative_path_parts)

        if tentative_path in self.path_counter:
            self.path_counter[tentative_path] += 1
            current_heading = f"{current_heading}_{self.path_counter[tentative_path]}"
        else:
            self.path_counter[tentative_path] = 1

        self.path_stack.append((current_heading, adjusted_level))
        heading_names = [item_heading for item_heading, _ in self.path_stack]
        path_parts = [self.relative_root] if self.relative_root else []
        path_parts.extend(heading_names)
        self.inner_paths.append(self.split_char.join(heading_names))
        self.path = self.split_char.join(path_parts)

    def append_content_item(self, item: str) -> None:
        self.content_items.append(item)

    def append_plain_text(self, text: str) -> None:
        self.content_items.append(text.strip())
        if self.current_page_number > 0:
            self.chunk_pages.add(self.current_page_number)

    def append_row(self, row: ParserRowValues) -> None:
        self.rows.append(row)

    def schedule_deferred_task(self, task: tuple[Any, ...]) -> None:
        self.deferred_llm_tasks.append(task)

    def collect_text_summary_tasks(self, summary_len: int) -> None:
        if not self.llm_parameters.get("summary_txt"):
            return

        for index, entry in enumerate(self.rows):
            marker = entry[2]
            if isinstance(marker, str) and marker.strip().split("\n", 1)[0].lower() in {
                "image",
                "table",
            }:
                continue
            content = str(entry[0])
            if len(content) > summary_len and not entry[4] and not entry[5]:
                self.deferred_llm_tasks.append(("text", index, content))

    def to_dataframe(self) -> pd.DataFrame:
        rows_builder = ParsedRowsBuilder()
        for row_values in self.rows:
            rows_builder.append(
                ParsedRow(
                    content=str(row_values[0]),
                    path=str(row_values[1]),
                    type=str(row_values[2]),
                    length=int(row_values[3]),
                    keywords=str(row_values[4]),
                    summary=str(row_values[5]),
                    know_id=str(row_values[6]),
                    tokens=str(row_values[7]),
                    connectto=str(row_values[8]),
                    addtime=str(row_values[9]),
                    page_nums=str(row_values[10]),
                )
            )
        return process_dup_paths_df(rows_builder.to_dataframe())

    def _format_chunk_pages(self) -> str:
        return ",".join(str(page) for page in sorted(self.chunk_pages))
