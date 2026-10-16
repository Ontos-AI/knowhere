"""Build canonical chunk connection metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, TypeAlias, TypedDict

from shared.utils.chunk_refs import ChunkRefSpan, extract_chunk_ref_spans


class PositionPayload(TypedDict):
    start: int
    end: int


RELATION_EMBEDS: Final = "embeds"
RELATION_EMBEDDED_BY: Final = "embedded_by"
RELATION_RELATED: Final = "related"
# Reserved for future producers; no code path emits these yet.
RELATION_CROSS_DOC_ENTITY: Final = "cross_doc_entity"
RELATION_INTRA_DOC_REF: Final = "intra_doc_ref"

# Relations whose targets are expanded when hydrating or deduplicating a
# body chunk's connected assets. Reverse and future relations are excluded so
# they stay purely additive for existing consumers.
FORWARD_RELATIONS: Final = frozenset({RELATION_EMBEDS, RELATION_RELATED})

_ASSET_CHUNK_TYPES: Final = frozenset({"image", "table"})


class ConnectionPayload(TypedDict, total=False):
    target: str
    target_document_id: str
    relation: str
    ref: str
    position: PositionPayload


RelationshipRef: TypeAlias = str | ChunkRefSpan
ConnectionValue: TypeAlias = str | ConnectionPayload
ConnectionKey: TypeAlias = tuple[str, str, str]
PositionKey: TypeAlias = tuple[str, str]


def parse_relationship_refs(type_value: object, content: str) -> list[RelationshipRef]:
    parsed_relationships = _parse_type_relationship_refs(type_value)
    if parsed_relationships:
        return [relationship for relationship in parsed_relationships]
    return [span for span in extract_chunk_ref_spans(content)]


def build_resource_target_map(
    chunks: Sequence[Mapping[str, Any]],
    *,
    image_files_map: Mapping[str, Mapping[str, Any]] | None = None,
    table_files_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    target_map: dict[str, str] = {}
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or chunk.get("know_id") or "").strip()
        if not chunk_id:
            continue

        chunk_type = str(chunk.get("type", "")).strip().split("\n", 1)[0].lower()
        if chunk_type not in {"image", "table"}:
            continue

        metadata = chunk.get("metadata", {})
        file_path = ""
        if isinstance(metadata, dict):
            file_path = str(metadata.get("file_path") or "").strip()
        if not file_path:
            file_map = image_files_map if chunk_type == "image" else table_files_map
            file_info = file_map.get(chunk_id) if file_map else None
            if file_info:
                file_path = str(file_info.get("file_path") or "").strip()

        path_alias = str(chunk.get("path") or "").strip()
        aliases = {file_path, path_alias}
        for alias in list(aliases):
            if alias:
                aliases.add(f"[{alias}]")
        for alias in aliases:
            if alias:
                target_map[alias] = chunk_id
    return target_map


def convert_refs_to_embed_connections(
    refs: Sequence[RelationshipRef], target_map: Mapping[str, str]
) -> list[ConnectionPayload]:
    connections: list[ConnectionPayload] = []
    for ref in refs:
        if isinstance(ref, dict):
            ref_text = str(ref.get("ref") or "").strip()
            start = ref.get("start")
            end = ref.get("end")
        else:
            ref_text = str(ref or "").strip()
            start = None
            end = None
        if not ref_text:
            continue

        target_id = target_map.get(ref_text)
        if not target_id and ref_text.startswith("[") and ref_text.endswith("]"):
            target_id = target_map.get(ref_text[1:-1].strip())
        if not target_id:
            continue

        connection: ConnectionPayload = {
            "target": target_id,
            "relation": RELATION_EMBEDS,
            "ref": ref_text,
        }
        if isinstance(start, int) and isinstance(end, int):
            connection["position"] = {
                "start": start,
                "end": end,
            }
        connections.append(connection)
    return connections


def normalize_connect_to_targets(
    connects: object, target_map: Mapping[str, str]
) -> list[ConnectionPayload]:
    if connects is None or connects == "":
        return []

    raw_items = connects if isinstance(connects, list) else [connects]
    normalized: list[ConnectionPayload] = []
    for item in raw_items:
        if item is None or item == "":
            continue

        if isinstance(item, dict):
            target = str(item.get("target") or "").strip()
            normalized_target = target_map.get(target, target)
            if not normalized_target:
                continue

            normalized_item: ConnectionPayload = {
                "target": normalized_target,
                "relation": str(item.get("relation") or RELATION_RELATED),
            }
            target_document_id = item.get("target_document_id")
            if target_document_id:
                normalized_item["target_document_id"] = str(target_document_id)
            ref = item.get("ref")
            if ref:
                normalized_item["ref"] = str(ref)
            position = item.get("position")
            if isinstance(position, dict):
                start = position.get("start")
                end = position.get("end")
                if isinstance(start, int) and isinstance(end, int):
                    normalized_item["position"] = {"start": start, "end": end}
            normalized.append(normalized_item)
            continue

        target = str(item or "").strip()
        normalized_target = target_map.get(target, target)
        if normalized_target:
            normalized.append(
                {
                    "target": normalized_target,
                    "relation": RELATION_RELATED,
                }
            )
    return normalized


def merge_connections(
    *connection_lists: Sequence[ConnectionValue],
) -> list[ConnectionValue]:
    merged: list[ConnectionValue] = []
    unpositioned_indexes: dict[ConnectionKey, int] = {}
    positioned_keys: dict[ConnectionKey, set[PositionKey]] = {}
    for connection_list in connection_lists:
        for item in connection_list or []:
            if not isinstance(item, dict):
                continue
            key = _get_connection_key(item)
            position_key = _get_connection_position_key(item)
            if position_key is None:
                if key in unpositioned_indexes or key in positioned_keys:
                    continue
                unpositioned_indexes[key] = len(merged)
                merged.append(item)
                continue

            key_positions = positioned_keys.setdefault(key, set())
            if position_key in key_positions:
                continue
            key_positions.add(position_key)
            unpositioned_index = unpositioned_indexes.pop(key, None)
            if unpositioned_index is None:
                merged.append(item)
            else:
                merged[unpositioned_index] = item
    return merged


def attach_embedded_by_connections(chunks: Sequence[Mapping[str, Any]]) -> None:
    """Give every embedded image/table an ``embedded_by`` edge to its owner.

    Mutates each asset chunk's ``metadata["connect_to"]`` in place. ``chunks``
    must already carry their final forward ``embeds`` edges, with targets
    normalized to chunk ids.
    """
    assets_by_chunk_id: dict[str, list[Mapping[str, Any]]] = {}
    for chunk in chunks:
        if chunk.get("type") in _ASSET_CHUNK_TYPES:
            assets_by_chunk_id.setdefault(str(chunk["chunk_id"]), []).append(chunk)

    for owner in chunks:
        owner_id = str(owner["chunk_id"])
        for connection in owner["metadata"].get("connect_to") or []:
            if (
                not isinstance(connection, dict)
                or connection.get("relation") != RELATION_EMBEDS
            ):
                continue
            target_id = str(connection.get("target") or "")
            if target_id == owner_id:
                continue
            for asset in assets_by_chunk_id.get(target_id, ()):
                asset_metadata = asset["metadata"]
                asset_metadata["connect_to"] = merge_connections(
                    asset_metadata.get("connect_to") or [],
                    [{"target": owner_id, "relation": RELATION_EMBEDDED_BY}],
                )


def _parse_type_relationship_refs(type_value: object) -> list[str]:
    if not isinstance(type_value, str) or "\n" not in type_value:
        return []
    lines = [line.strip() for line in type_value.split("\n") if line.strip()]
    return [line for line in lines[1:] if line.upper() != "PTXT"]


def _get_connection_key(item: ConnectionValue) -> ConnectionKey:
    if not isinstance(item, dict):
        return ("", RELATION_RELATED, "")
    return (
        str(item.get("target") or ""),
        str(item.get("relation") or RELATION_RELATED),
        str(item.get("ref") or ""),
    )


def _get_connection_position_key(item: ConnectionValue) -> PositionKey | None:
    if not isinstance(item, dict):
        return None
    position = item.get("position")
    if not isinstance(position, dict):
        return None
    return (
        str(position.get("start", "")),
        str(position.get("end", "")),
    )
