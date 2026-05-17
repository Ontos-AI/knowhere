from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.document_parser.docx_asset_store import DocxAssetStore


ImageHandler = Callable[
    [
        list[list[object]],
        dict[str, Any],
        DocxAssetStore,
        list[dict[str, Any]],
        str,
        int,
        bool,
        dict[str, dict[str, str]],
    ],
    tuple[list[dict[str, Any]], list[list[object]], bool],
]

TableHandler = Callable[..., tuple[list[dict[str, Any]], list[list[object]], int]]


@dataclass
class DocxAssetAccumulator:
    asset_store: DocxAssetStore
    should_summary_image: bool
    should_summary_table: bool
    image_handler: ImageHandler
    table_handler: TableHandler
    _rows: list[list[object]] = field(default_factory=list)
    _image_count: int = 0
    _table_count: int = 0
    _seen_images: dict[str, dict[str, str]] = field(default_factory=dict)

    def append_image(
        self,
        image_meta: dict[str, Any],
        headings_stack: list[dict[str, Any]],
        current_heading: str,
    ) -> list[dict[str, Any]]:
        headings_stack, self._rows, is_new_image = self.image_handler(
            self._rows,
            image_meta,
            self.asset_store,
            headings_stack,
            current_heading,
            self._image_count,
            self.should_summary_image,
            self._seen_images,
        )
        if is_new_image:
            self._image_count += 1
        return headings_stack

    def append_table(
        self,
        block: Any,
        headings_stack: list[dict[str, Any]],
        current_heading: str,
        cell_images: Any,
    ) -> list[dict[str, Any]]:
        headings_stack, self._rows, self._image_count = self.table_handler(
            self._rows,
            block,
            self.asset_store,
            headings_stack,
            current_heading,
            self._table_count,
            summary_table=self.should_summary_table,
            summary_image=self.should_summary_image,
            cell_images=cell_images,
            img_count=self._image_count,
            seen_images=self._seen_images,
        )
        self._table_count += 1
        return headings_stack

    def rows(self) -> list[list[object]]:
        return self._rows
