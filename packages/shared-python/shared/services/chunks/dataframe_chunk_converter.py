"""Convert parser DataFrames into canonical chunk payloads.

DataFrame conversion is the Text Track adapter. Page Memory should call
``rows_to_chunks`` / ``row_dict_to_chunk`` directly.
"""

from __future__ import annotations

from typing import Dict

from loguru import logger

from shared.services.chunks.canonical_chunk_builder import (
    ChunkMetadata,
    ChunkPayload,
    ChunkType,
    JsonPrimitive,
    JsonValue,
    chunks_as_json,
    finalize_chunk_connections,
    row_dict_to_chunk,
    rows_to_chunks,
)

__all__ = [
    "ChunkMetadata",
    "ChunkPayload",
    "ChunkType",
    "JsonPrimitive",
    "JsonValue",
    "dataframe_to_chunks",
    "finalize_chunk_connections",
    "row_dict_to_chunk",
    "rows_to_chunks",
]


def dataframe_to_chunks(df: object | None) -> list[Dict[str, JsonValue]]:
    """Convert a parser DataFrame into chunk records."""
    if df is None or len(df) == 0:  # type: ignore[arg-type]
        logger.warning("DataFrame is empty; returning an empty chunks list")
        return []

    logger.debug(f"Converting DataFrame to chunks: length={len(df)}")  # type: ignore[arg-type]
    rows = [dict(row) for _, row in df.iterrows()]  # type: ignore[union-attr]
    chunks = rows_to_chunks(rows)
    logger.debug(f"DataFrame conversion completed: chunk count={len(chunks)}")
    return chunks_as_json(chunks)
