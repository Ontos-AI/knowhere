"""Chunk projection for Knowhere ZIP result packages."""

from __future__ import annotations

from typing import Any

from shared.services.chunks.chunk_connections import (
    build_resource_target_map,
    convert_refs_to_embed_connections,
    merge_connections,
    normalize_connect_to_targets,
    parse_relationship_refs,
)


class ZipChunkSchemaBuilder:
    def calculate_statistics(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        total_chunks = len(chunks)
        text_chunks = 0
        image_chunks = 0
        table_chunks = 0

        for chunk in chunks:
            chunk_type = chunk.get("type", "")
            normalized_type = _normalize_chunk_type(chunk_type)
            if normalized_type == "image":
                image_chunks += 1
            elif normalized_type == "table":
                table_chunks += 1
            else:
                text_chunks += 1

        return {
            "total_chunks": total_chunks,
            "text_chunks": text_chunks,
            "image_chunks": image_chunks,
            "table_chunks": table_chunks,
            "total_pages": None,
        }

    def format_chunks(
        self,
        chunks: list[dict[str, Any]],
        image_files_map: dict[str, dict[str, Any]],
        table_files_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resource_target_map = build_resource_target_map(
            chunks,
            image_files_map=image_files_map,
            table_files_map=table_files_map,
        )

        formatted: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or chunk.get("know_id"))
            chunk_type_str = chunk.get("type", "")
            normalized_type = _normalize_chunk_type(chunk_type_str)
            image_info = image_files_map.get(chunk_id)

            if normalized_type == "image":
                chunk_type = "image"
            elif normalized_type == "table":
                chunk_type = "table"
            else:
                chunk_type = "text"

            content = chunk.get("text") or chunk.get("content", "")
            path = chunk.get("path", "")
            existing_metadata = chunk.get("metadata", {})
            metadata = _base_chunk_metadata(existing_metadata, chunk, content)

            if chunk_type == "text":
                metadata.update(
                    _format_text_metadata(
                        chunk=chunk,
                        chunk_type_str=chunk_type_str,
                        content=str(content),
                        existing_metadata=existing_metadata,
                        resource_target_map=resource_target_map,
                    )
                )
            elif chunk_type == "image":
                if image_info:
                    metadata["file_path"] = image_info["file_path"]
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                metadata["tokens"] = []
            elif chunk_type == "table":
                metadata["file_path"] = _resolve_table_file_path(
                    chunk=chunk,
                    chunk_id=chunk_id,
                    path=str(path),
                    existing_metadata=existing_metadata,
                    table_files_map=table_files_map,
                )
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                metadata["tokens"] = []

            formatted.append(
                {
                    "chunk_id": chunk_id,
                    "type": chunk_type,
                    "content": content,
                    "path": path,
                    "metadata": metadata,
                }
            )

        return formatted


def _normalize_chunk_type(value: Any) -> str:
    raw_type = str(value).strip()
    return raw_type.split("\n", 1)[0].lower()


def _base_chunk_metadata(
    existing_metadata: dict[str, Any],
    chunk: dict[str, Any],
    content: Any,
) -> dict[str, Any]:
    metadata = {
        "length": existing_metadata.get("length") or len(content),
        "summary": existing_metadata.get("summary") or chunk.get("summary", ""),
        "page_nums": existing_metadata.get("page_nums", []),
    }
    document_top_summary = str(existing_metadata.get("document_top_summary") or "").strip()
    if document_top_summary:
        metadata["document_top_summary"] = document_top_summary
    return metadata


def _format_text_metadata(
    *,
    chunk: dict[str, Any],
    chunk_type_str: Any,
    content: str,
    existing_metadata: dict[str, Any],
    resource_target_map: dict[str, str],
) -> dict[str, Any]:
    relationship_refs = parse_relationship_refs(
        chunk.get("type_raw") or chunk_type_str,
        content,
    )
    embed_connections = convert_refs_to_embed_connections(
        relationship_refs,
        resource_target_map,
    )
    related_connections = normalize_connect_to_targets(
        existing_metadata.get("connect_to")
        or chunk.get("connect_to")
        or chunk.get("connectto"),
        resource_target_map,
    )
    return {
        "tokens": existing_metadata.get("tokens") or chunk.get("tokens", 0),
        "keywords": existing_metadata.get("keywords") or chunk.get("keywords", []),
        "connect_to": merge_connections(embed_connections, related_connections),
    }


def _resolve_table_file_path(
    *,
    chunk: dict[str, Any],
    chunk_id: str,
    path: str,
    existing_metadata: dict[str, Any],
    table_files_map: dict[str, dict[str, Any]],
) -> Any:
    file_path = existing_metadata.get("file_path")
    if file_path:
        return file_path

    table_info = table_files_map.get(chunk_id)
    if table_info:
        return table_info["file_path"]

    table_name = path.split("/")[-1] if "/" in path else f"table_{chunk_id}.html"
    return f"tables/{table_name}"
