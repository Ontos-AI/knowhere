from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from pandas import Index

from shared.core.config import settings

PARSER_ROW_COLUMNS: tuple[str, ...] = tuple(settings.ALL_DF_COLS.split(","))


@dataclass(frozen=True)
class ParsedRow:
    content: str
    path: str
    type: str
    know_id: str
    addtime: str
    keywords: str = ""
    summary: str = ""
    tokens: str = ""
    connectto: str = ""
    page_nums: str = ""
    length: int | None = None

    def to_list(self) -> list[object]:
        content_length = self.length if self.length is not None else len(self.content)
        return [
            self.content,
            self.path,
            self.type,
            content_length,
            self.keywords,
            self.summary,
            self.know_id,
            self.tokens,
            self.connectto,
            self.addtime,
            self.page_nums,
        ]

    def to_dict(self) -> dict[str, object]:
        return dict(zip(PARSER_ROW_COLUMNS, self.to_list()))


class ParsedRowsBuilder:
    def __init__(self) -> None:
        self._rows: list[ParsedRow] = []

    def append(self, row: ParsedRow) -> None:
        self._rows.append(row)

    def extend(self, rows: list[ParsedRow]) -> None:
        self._rows.extend(rows)

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(
            [row.to_list() for row in self._rows],
            columns=Index(PARSER_ROW_COLUMNS),
        )
