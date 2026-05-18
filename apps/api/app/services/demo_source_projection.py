"""Projection logic for canonical Demo Source data."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote


class _DemoCitationDefinition(Protocol):
    @property
    def section_path(self) -> str:
        raise NotImplementedError

    @property
    def description(self) -> str:
        raise NotImplementedError

    @property
    def content(self) -> str:
        raise NotImplementedError


class _DemoExampleDefinition(Protocol):
    @property
    def id(self) -> str:
        raise NotImplementedError

    @property
    def question(self) -> str:
        raise NotImplementedError

    @property
    def answer(self) -> str:
        raise NotImplementedError

    @property
    def citations(self) -> tuple[_DemoCitationDefinition, ...]:
        raise NotImplementedError


class _DemoSourceDefinition(Protocol):
    @property
    def demo_source_id(self) -> str:
        raise NotImplementedError

    @property
    def canonical_document_id(self) -> str:
        raise NotImplementedError

    @property
    def title(self) -> str:
        raise NotImplementedError

    @property
    def mime_type(self) -> str:
        raise NotImplementedError

    @property
    def size_bytes(self) -> int:
        raise NotImplementedError

    @property
    def chunk_count(self) -> int:
        raise NotImplementedError

    @property
    def examples(self) -> tuple[_DemoExampleDefinition, ...]:
        raise NotImplementedError


class DemoSourceProjection:
    def source_catalog_payload(
        self,
        *,
        source: _DemoSourceDefinition,
        chunks: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        return {
            "demo_source_id": source.demo_source_id,
            "canonical_document_id": source.canonical_document_id,
            "title": source.title,
            "mime_type": source.mime_type,
            "size_bytes": source.size_bytes,
            "status": "ready",
            "chunk_count": source.chunk_count,
            "original_file": {
                "url": f"/api/v1/demo/sources/{source.demo_source_id}/original",
                "mime_type": source.mime_type,
                "size_bytes": source.size_bytes,
                "can_download": False,
            },
            "examples": [
                self._example_payload(source=source, example=example, chunks=chunks)
                for example in source.examples
            ],
        }

    def chunk_payload(
        self,
        *,
        source: _DemoSourceDefinition,
        chunk: dict[str, Any],
        sort_order: int,
    ) -> dict[str, Any]:
        metadata = _metadata(chunk)
        file_path = _first_string(
            metadata.get("file_path"),
            metadata.get("filePath"),
            chunk.get("file_path"),
            chunk.get("path") if _is_media_chunk(chunk) else None,
        )
        return {
            "id": self.canonical_chunk_id(source=source, chunk=chunk),
            "chunk_id": chunk["chunk_id"],
            "chunk_type": _normalize_chunk_type(chunk.get("type")),
            "content": str(chunk.get("content") or ""),
            "section_path": str(chunk.get("path") or "") or None,
            "source_chunk_path": str(chunk.get("path") or "") or None,
            "file_path": file_path,
            "sort_order": sort_order,
            "metadata": metadata,
            "asset_url": _asset_url(source=source, file_path=file_path),
            "created_at": None,
        }

    def publication_chunks(
        self,
        *,
        source: _DemoSourceDefinition,
        chunks: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        return [
            _publication_chunk(source=source, chunk=chunk)
            for chunk in chunks
        ]

    def canonical_chunk_id(
        self,
        *,
        source: _DemoSourceDefinition,
        chunk: dict[str, Any],
    ) -> str:
        return f"{source.demo_source_id}:{chunk['chunk_id']}"

    def matches_chunk_id(
        self,
        *,
        source: _DemoSourceDefinition,
        chunk: dict[str, Any],
        demo_chunk_id: str,
    ) -> bool:
        return demo_chunk_id in {
            self.canonical_chunk_id(source=source, chunk=chunk),
            chunk["chunk_id"],
        }

    def _example_payload(
        self,
        *,
        source: _DemoSourceDefinition,
        example: _DemoExampleDefinition,
        chunks: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        return {
            "id": example.id,
            "question": example.question,
            "answer": example.answer,
            "citations": [
                self._citation_payload(source=source, citation=citation, chunks=chunks)
                for citation in example.citations
            ],
        }

    def _citation_payload(
        self,
        *,
        source: _DemoSourceDefinition,
        citation: _DemoCitationDefinition,
        chunks: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        chunk = _resolve_citation_chunk(source=source, citation=citation, chunks=chunks)
        return {
            "demo_source_id": source.demo_source_id,
            "canonical_document_id": source.canonical_document_id,
            "canonical_chunk_id": self.canonical_chunk_id(source=source, chunk=chunk),
            "chunk_id": chunk["chunk_id"],
            "chunk_type": _normalize_chunk_type(chunk.get("type")),
            "content": citation.content,
            "description": citation.description,
            "source": {
                "document_id": source.canonical_document_id,
                "source_file_name": source.title,
                "section_path": citation.section_path,
            },
        }


def _publication_chunk(
    *,
    source: _DemoSourceDefinition,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    materialized_chunk = dict(chunk)
    metadata = _metadata(materialized_chunk)
    raw_path = _first_string(metadata.get("path"), materialized_chunk.get("path"))
    publication_path = _publication_path(source=source, raw_path=raw_path)
    file_path = _first_string(
        metadata.get("file_path"),
        metadata.get("filePath"),
        materialized_chunk.get("file_path"),
        materialized_chunk.get("path") if _is_media_chunk(materialized_chunk) else None,
    )

    metadata["path"] = publication_path
    if file_path:
        metadata["file_path"] = file_path
        materialized_chunk["file_path"] = file_path
    materialized_chunk["path"] = publication_path
    materialized_chunk["metadata"] = metadata
    return materialized_chunk


def _publication_path(
    *,
    source: _DemoSourceDefinition,
    raw_path: str | None,
) -> str:
    prefix = f"Default_Root/{source.title}"
    raw = str(raw_path or "").strip()
    if not raw:
        return prefix

    if "-->" in raw:
        sections = [part.strip() for part in raw.split("-->")[1:] if part.strip()]
        return "/".join([prefix, *sections]) if sections else prefix

    if raw.startswith("images/") or raw.startswith("tables/"):
        return f"{prefix}/Assets/{raw}"

    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if len(parts) >= 2 and parts[0] == "Default_Root":
        return raw
    return prefix


def _resolve_citation_chunk(
    *,
    source: _DemoSourceDefinition,
    citation: _DemoCitationDefinition,
    chunks: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    normalized_content = _normalize_text(citation.content)
    if normalized_content:
        for chunk in chunks:
            if normalized_content in _normalize_text(str(chunk.get("content") or "")):
                return chunk

    for chunk in chunks:
        if str(chunk.get("path") or "") == citation.section_path:
            return chunk

    raise ValueError(
        "Demo citation does not resolve to a canonical chunk: "
        f"demo_source_id={source.demo_source_id}, section_path={citation.section_path}"
    )


def _metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _is_media_chunk(chunk: dict[str, Any]) -> bool:
    return _normalize_chunk_type(chunk.get("type")) in {"image", "table"}


def _asset_url(
    *,
    source: _DemoSourceDefinition,
    file_path: str | None,
) -> str | None:
    if not file_path:
        return None
    encoded_path = quote(file_path, safe="/")
    return f"/api/v1/demo/sources/{source.demo_source_id}/assets/{encoded_path}"


def _normalize_chunk_type(value: object) -> str:
    raw = str(value or "").strip().split("\n", 1)[0].lower()
    return raw if raw in {"text", "image", "table"} else "text"


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())
