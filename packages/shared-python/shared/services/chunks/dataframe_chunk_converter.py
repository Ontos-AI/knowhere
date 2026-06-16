"""Convert parser DataFrames into canonical chunk payloads."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Sequence
from typing import Dict, Literal, Protocol, TypeAlias, TypedDict, Union, cast

import pandas as pd
from loguru import logger

from shared.services.chunks.chunk_connections import (
    ConnectionValue,
    RelationshipRef,
    build_resource_target_map,
    convert_refs_to_embed_connections,
    merge_connections,
    normalize_connect_to_targets,
    parse_relationship_refs,
)


class _ParserRow(Protocol):
    def get(self, key: str, default: object = ...) -> object: ...


class _ParserDataFrame(Protocol):
    def __len__(self) -> int: ...

    def iterrows(self) -> Iterable[tuple[object, _ParserRow]]: ...


JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = Union[
    JsonPrimitive,
    list["JsonValue"],
    dict[str, "JsonValue"],
]
ChunkType: TypeAlias = Literal["text", "image", "table", "page"]


class ChunkMetadata(TypedDict, total=False):
    keywords: list[str]
    summary: str
    length: int
    tokens: list[str]
    connect_to: list[ConnectionValue]
    page_nums: list[int]
    file_path: str
    original_name: str
    _relationship_refs: list[RelationshipRef]


class ChunkPayload(TypedDict):
    chunk_id: str
    type: ChunkType
    content: str
    path: str
    metadata: ChunkMetadata
    order: int
    know_id: str
    keywords: list[str]
    summary: str
    tokens: list[str]


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def _safe_int(value: object) -> int:
    if _is_missing(value):
        return 0
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return 0


def _safe_split_keywords(value: object) -> list[str]:
    if _is_missing(value):
        return []
    keyword_text = str(value)
    if ";" in keyword_text:
        keywords = [
            keyword.strip() for keyword in keyword_text.split(";") if keyword.strip()
        ]
    elif "," in keyword_text:
        keywords = [
            keyword.strip() for keyword in keyword_text.split(",") if keyword.strip()
        ]
    else:
        keywords = [keyword_text.strip()] if keyword_text.strip() else []
    return [keyword for keyword in keywords if len(keyword) > 1]


def _safe_parse_tokens(value: object) -> list[str]:
    if _is_missing(value):
        return []
    token_text = str(value).strip()
    if not token_text:
        return []
    if token_text.startswith("[") and token_text.endswith("]"):
        inner_text = token_text[1:-1].strip()
        if (inner_text.startswith("'") and inner_text.endswith("'")) or (
            inner_text.startswith('"') and inner_text.endswith('"')
        ):
            inner_text = inner_text[1:-1]
        token_text = inner_text
    if ";" in token_text:
        return [token.strip() for token in token_text.split(";") if token.strip()]
    if "->" in token_text:
        return [token.strip() for token in token_text.split("->") if token.strip()]
    return []


def _normalize_resource_ref(ref: RelationshipRef) -> str:
    if isinstance(ref, dict):
        ref_text = str(ref.get("ref") or "").strip()
    else:
        ref_text = str(ref or "").strip()
    if ref_text.startswith("[") and ref_text.endswith("]"):
        ref_text = ref_text[1:-1].strip()
    return ref_text


def _find_embedded_resource_path(
    refs: Sequence[RelationshipRef], resource_dir: str
) -> str:
    resource_prefix = f"{resource_dir}/"
    for ref in refs:
        normalized_ref = _normalize_resource_ref(ref)
        if normalized_ref.startswith(resource_prefix):
            return normalized_ref
    return ""


def _parse_connect_to(value: object) -> list[ConnectionValue]:
    if not value or _is_missing(value):
        return []
    if isinstance(value, list):
        return [
            cast(ConnectionValue, item)
            for item in value
            if isinstance(item, str) or isinstance(item, dict)
        ]
    connects_text = str(value).strip()
    if not connects_text:
        return []
    if connects_text.startswith("["):
        try:
            parsed: object = json.loads(connects_text)
            if isinstance(parsed, list):
                return [
                    cast(ConnectionValue, item)
                    for item in parsed
                    if isinstance(item, str) or isinstance(item, dict)
                ]
        except json.JSONDecodeError:
            pass
    return [
        {
            "target": connects_text,
            "relation": "related",
            "score": 1.0,
            "keywords": [],
        }
    ]


def _parse_page_numbers(value: object) -> list[int]:
    if _is_missing(value):
        return []
    try:
        return [
            int(page_number.strip())
            for page_number in str(value).split(",")
            if page_number.strip()
        ]
    except (ValueError, TypeError):
        return []


def _get_chunk_type(value: object) -> ChunkType:
    if not isinstance(value, str):
        return "text"
    normalized_type = value.strip().split("\n", 1)[0].lower()
    if normalized_type == "ptxt":
        return "text"
    if normalized_type == "image":
        return "image"
    if normalized_type == "table":
        return "table"
    if normalized_type == "page":
        return "page"
    return "text"


def _parse_extra_metadata(value: object) -> dict[str, JsonValue]:
    if not value or _is_missing(value):
        return {}
    if isinstance(value, dict):
        return cast(dict[str, JsonValue], value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return cast(dict[str, JsonValue], parsed)
    return {}


_RESERVED_METADATA_KEYS = {
    "keywords",
    "summary",
    "length",
    "tokens",
    "connect_to",
    "_relationship_refs",
    "page_nums",
    "file_path",
    "original_name",
}


def _merge_extra_metadata(
    metadata: ChunkMetadata,
    extra_metadata: dict[str, JsonValue],
) -> None:
    for key, value in extra_metadata.items():
        if key in _RESERVED_METADATA_KEYS:
            logger.warning("Ignoring extra_metadata reserved key: {}", key)
            continue
        metadata[key] = value


def _get_relationship_refs(type_value: object, content: str) -> list[RelationshipRef]:
    return parse_relationship_refs(type_value, content)


def _get_connect_to(metadata: ChunkMetadata) -> list[ConnectionValue]:
    connect_to = metadata.get("connect_to")
    return connect_to if isinstance(connect_to, list) else []


def dataframe_to_chunks(df: _ParserDataFrame | None) -> list[Dict[str, JsonValue]]:
    """Convert a parser DataFrame into chunk records."""
    if df is None or len(df) == 0:
        logger.warning("DataFrame is empty; returning an empty chunks list")
        return []

    logger.debug(f"Converting DataFrame to chunks: length={len(df)}")

    chunks: list[ChunkPayload] = []
    for index, (_, row) in enumerate(df.iterrows()):
        know_id = row.get("know_id")
        if know_id and not _is_missing(know_id):
            chunk_id = str(know_id)
        else:
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, str(uuid.uuid4())))

        content = str(row.get("content", ""))
        path = str(row.get("path", ""))
        type_value = row.get("type", "")
        chunk_type = _get_chunk_type(type_value)
        relationship_refs = _get_relationship_refs(type_value, content)

        metadata: ChunkMetadata = {
            "keywords": _safe_split_keywords(row.get("keywords")),
            "summary": str(row.get("summary", "")),
            "length": _safe_int(row.get("length")) or len(content),
            "tokens": _safe_parse_tokens(row.get("tokens")),
            "connect_to": _parse_connect_to(row.get("connectto")),
            "_relationship_refs": relationship_refs,
            "page_nums": _parse_page_numbers(row.get("page_nums", "")),
        }
        _merge_extra_metadata(metadata, _parse_extra_metadata(row.get("extra_metadata")))

        if chunk_type == "image":
            embedded_image_path = _find_embedded_resource_path(
                relationship_refs, "images"
            )
            if embedded_image_path:
                image_name = os.path.basename(embedded_image_path)
                metadata["file_path"] = embedded_image_path
                metadata["original_name"] = image_name
            else:
                normalized_path = path.replace("-->", "/")
                image_name = (
                    os.path.basename(normalized_path)
                    if normalized_path
                    else f"image_{chunk_id}.jpg"
                )
                _, image_extension = os.path.splitext(image_name)
                if not image_extension:
                    image_name += ".png"
                metadata["file_path"] = f"images/{image_name}"
                metadata["original_name"] = image_name
        elif chunk_type == "table":
            embedded_table_path = _find_embedded_resource_path(
                relationship_refs, "tables"
            )
            if embedded_table_path:
                metadata["file_path"] = embedded_table_path
            else:
                normalized_path = path.replace("-->", "/")
                table_name = (
                    os.path.basename(normalized_path)
                    if normalized_path
                    else f"table_{chunk_id}.html"
                )
                metadata["file_path"] = f"tables/{table_name}"

        chunks.append(
            {
                "chunk_id": chunk_id,
                "type": chunk_type,
                "content": content,
                "path": path,
                "metadata": metadata,
                "order": index,
                "know_id": str(know_id),
                "keywords": metadata["keywords"],
                "summary": metadata["summary"],
                "tokens": metadata["tokens"],
            }
        )

    resource_target_map = build_resource_target_map(chunks)
    for chunk in chunks:
        metadata = chunk["metadata"]
        relationship_refs = metadata.pop("_relationship_refs", [])
        if chunk["type"] != "text":
            continue
        embed_connections = convert_refs_to_embed_connections(
            relationship_refs, resource_target_map
        )
        metadata["connect_to"] = merge_connections(
            embed_connections,
            normalize_connect_to_targets(
                _get_connect_to(metadata),
                resource_target_map,
            ),
        )

    logger.debug(f"DataFrame conversion completed: chunk count={len(chunks)}")
    return cast(list[Dict[str, JsonValue]], chunks)
