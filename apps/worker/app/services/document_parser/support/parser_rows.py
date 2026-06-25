from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
from pandas import Index

from shared.core.config import settings

PARSER_ROW_COLUMNS: tuple[str, ...] = tuple(settings.ALL_DF_COLS.split(","))


def serialize_entities(entities: Any) -> str:
    """Serialize typed entities (§4.4) into the JSON string stored in the row.

    Accepts a list of ``{"text","type"}`` dicts (or objects exposing
    ``to_dict``). Returns ``""`` for an empty/invalid value so empty stays empty
    rather than becoming ``"[]"`` noise in the column.
    """
    if not entities:
        return ""
    normalized: list[dict[str, str]] = []
    for item in entities:
        if hasattr(item, "to_dict"):
            item = item.to_dict()
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            normalized.append({"text": text, "type": str(item.get("type", "")).strip()})
    if not normalized:
        return ""
    return json.dumps(normalized, ensure_ascii=False)


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
    entities: str = ""
    asset_title: str = ""

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
            self.entities,
            self.asset_title,
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


# Column indices into the positional raw-row lists (markdown track). Named here
# so callers stop poking magic offsets like row[4]/row[5] (audit P5). Kept in
# sync with ``ParsedRow.to_list`` / ``PARSER_ROW_COLUMNS``.
COL_KEYWORDS = PARSER_ROW_COLUMNS.index("keywords")
COL_SUMMARY = PARSER_ROW_COLUMNS.index("summary")
COL_ENTITIES = PARSER_ROW_COLUMNS.index("entities")
COL_ASSET_TITLE = PARSER_ROW_COLUMNS.index("asset_title")


def apply_body_summary(row: list[Any], result: Any) -> None:
    """Write a ``BodySummary`` (Contract A) onto a positional raw row by name.

    Sets ``summary`` and ``entities`` (and the transitional flattened
    ``keywords``). Replaces fragile ``row[5] = ...`` writes (audit §4.5).
    """
    _ensure_row_width(row)
    row[COL_SUMMARY] = result.summary or ""
    row[COL_ENTITIES] = serialize_entities(getattr(result, "entities", None))
    row[COL_KEYWORDS] = result.keywords_str()


def apply_asset_summary(row: list[Any], result: Any) -> None:
    """Write an ``AssetSummary`` (Contract B) onto a positional raw row by name.

    Sets ``asset_title``, ``summary``, ``entities`` and the transitional
    ``keywords``. The caller owns any file-rename logic keyed off the title.
    """
    _ensure_row_width(row)
    row[COL_ASSET_TITLE] = result.title or ""
    row[COL_SUMMARY] = result.summary or ""
    row[COL_ENTITIES] = serialize_entities(getattr(result, "entities", None))
    row[COL_KEYWORDS] = result.keywords_str()


def _ensure_row_width(row: list[Any]) -> None:
    """Pad a legacy 11-column row out to the current schema width in place."""
    while len(row) < len(PARSER_ROW_COLUMNS):
        row.append("")
