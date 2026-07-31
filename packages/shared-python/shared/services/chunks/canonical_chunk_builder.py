"""Canonical chunk payload builder shared by Page Memory and Text Track."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Literal, TypeAlias, TypedDict, Union, cast

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

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = Union[
    JsonPrimitive,
    list["JsonValue"],
    dict[str, "JsonValue"],
]
ChunkType: TypeAlias = Literal["text", "image", "table", "page"]


class ChunkMetadata(TypedDict, total=False):
    keywords: list[str]
    entities: list[dict[str, str]]
    asset_title: str
    summary: str
    length: int
    tokens: list[str]
    connect_to: list[ConnectionValue]
    page_nums: list[int]
    owned_page_nums: list[int]
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


_RESERVED_METADATA_KEYS = {
    "keywords",
    "entities",
    "asset_title",
    "summary",
    "length",
    "tokens",
    "connect_to",
    "_relationship_refs",
    "page_nums",
    "owned_page_nums",
    "file_path",
    "original_name",
}


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        import pandas as pd

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
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
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


def _safe_parse_entities(value: object) -> list[dict[str, str]]:
    if _is_missing(value) or not value:
        return []
    parsed: object = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    entities: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        entity_text = str(item.get("text", "")).strip()
        if not entity_text:
            continue
        entities.append(
            {"text": entity_text, "type": str(item.get("type", "")).strip()}
        )
    return entities


def _safe_parse_tokens(value: object) -> list[str]:
    if _is_missing(value):
        return []
    if isinstance(value, list):
        return [str(token).strip() for token in value if str(token).strip()]
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
    if isinstance(value, list):
        pages: list[int] = []
        for item in value:
            try:
                pages.append(int(item))
            except (TypeError, ValueError):
                continue
        return pages
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


def _merge_extra_metadata(
    metadata: ChunkMetadata,
    extra_metadata: dict[str, JsonValue],
) -> None:
    for key, value in extra_metadata.items():
        if key in _RESERVED_METADATA_KEYS:
            logger.warning("Ignoring extra_metadata reserved key: {}", key)
            continue
        metadata[key] = value  # type: ignore[literal-required]


def _get_relationship_refs(type_value: object, content: str) -> list[RelationshipRef]:
    return parse_relationship_refs(type_value, content)


def _get_connect_to(metadata: ChunkMetadata) -> list[ConnectionValue]:
    connect_to = metadata.get("connect_to")
    return connect_to if isinstance(connect_to, list) else []


def row_dict_to_chunk(
    row: Mapping[str, Any],
    *,
    order: int,
) -> ChunkPayload:
    """Convert one parser/page-memory row dict into a canonical chunk."""
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
        "connect_to": _parse_connect_to(row.get("connectto") or row.get("connect_to")),
        "_relationship_refs": relationship_refs,
        "page_nums": _parse_page_numbers(row.get("page_nums", "")),
    }
    owned_page_nums = _parse_page_numbers(row.get("owned_page_nums", ""))
    extra_metadata = _parse_extra_metadata(row.get("extra_metadata"))
    if not owned_page_nums:
        owned_page_nums = _parse_page_numbers(extra_metadata.pop("owned_page_nums", None))
    if owned_page_nums:
        metadata["owned_page_nums"] = owned_page_nums
    entities = _safe_parse_entities(row.get("entities"))
    if entities:
        metadata["entities"] = entities
    asset_title = str(row.get("asset_title", "") or "").strip()
    if asset_title:
        metadata["asset_title"] = asset_title
    if chunk_type != "page":
        metadata["tokens"] = _safe_parse_tokens(row.get("tokens"))
    _merge_extra_metadata(metadata, extra_metadata)

    file_path = str(row.get("file_path") or "").strip()
    original_name = str(row.get("original_name") or "").strip()
    if file_path:
        metadata["file_path"] = file_path
    if original_name:
        metadata["original_name"] = original_name

    if chunk_type == "image" and "file_path" not in metadata:
        embedded_image_path = _find_embedded_resource_path(
            relationship_refs, "images"
        )
        if embedded_image_path:
            image_name = os.path.basename(embedded_image_path)
            metadata["file_path"] = embedded_image_path
            metadata["original_name"] = image_name
        else:
            image_name = os.path.basename(path) if path else f"image_{chunk_id}.jpg"
            _, image_extension = os.path.splitext(image_name)
            if not image_extension:
                image_name += ".png"
            metadata["file_path"] = f"images/{image_name}"
            metadata["original_name"] = image_name
    elif chunk_type == "table" and "file_path" not in metadata:
        embedded_table_path = _find_embedded_resource_path(
            relationship_refs, "tables"
        )
        if embedded_table_path:
            metadata["file_path"] = embedded_table_path
        else:
            table_name = os.path.basename(path) if path else f"table_{chunk_id}.html"
            metadata["file_path"] = f"tables/{table_name}"

    return {
        "chunk_id": chunk_id,
        "type": chunk_type,
        "content": content,
        "path": path,
        "metadata": metadata,
        "order": order,
    }


def finalize_chunk_connections(chunks: list[ChunkPayload]) -> list[ChunkPayload]:
    """Resolve embed/related connections onto chunk ids."""
    resource_target_map = build_resource_target_map(chunks)
    for chunk in chunks:
        metadata = chunk["metadata"]
        relationship_refs = metadata.pop("_relationship_refs", [])
        if chunk["type"] not in {"text", "page", "table"}:
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
    return chunks


def rows_to_chunks(rows: Sequence[Mapping[str, Any]]) -> list[ChunkPayload]:
    """Convert ordered parser/page-memory rows into finalized canonical chunks."""
    if not rows:
        return []
    chunks = [row_dict_to_chunk(row, order=index) for index, row in enumerate(rows)]
    return finalize_chunk_connections(chunks)


def chunks_as_json(chunks: Sequence[ChunkPayload]) -> list[Dict[str, JsonValue]]:
    return cast(list[Dict[str, JsonValue]], list(chunks))
