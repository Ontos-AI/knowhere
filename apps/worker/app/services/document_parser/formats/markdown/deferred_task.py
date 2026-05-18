from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class ImageDeferredSummaryTask:
    row_index: int
    relative_path: str
    image_dir: str
    image_name: str
    image_suffix: str


@dataclass(frozen=True)
class TableDeferredSummaryTask:
    row_index: int
    table_html: str
    table_dir: str
    table_name: str
    table_count: int


@dataclass(frozen=True)
class TextDeferredSummaryTask:
    row_index: int
    content: str


MarkdownDeferredSummaryTask: TypeAlias = (
    ImageDeferredSummaryTask | TableDeferredSummaryTask | TextDeferredSummaryTask
)

__all__ = [
    "ImageDeferredSummaryTask",
    "MarkdownDeferredSummaryTask",
    "TableDeferredSummaryTask",
    "TextDeferredSummaryTask",
]
