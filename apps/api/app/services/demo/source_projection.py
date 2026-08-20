"""Projection logic for canonical Demo Source data."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

from shared.services.chunks.path_segments import (
    join_document_path,
    split_escaped_document_path,
)

_SOURCE_FILE_EXTENSIONS = (
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
)


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
        metadata = _page_assets_with_demo_urls(
            source=source,
            metadata=_metadata(chunk),
        )
        file_path = _file_path_for_chunk(chunk=chunk, metadata=metadata)
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
        return [_publication_chunk(source=source, chunk=chunk) for chunk in chunks]

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
        page_numbers = _page_numbers_from_chunk(chunk)
        page_citation_page_number = page_numbers[0] if page_numbers else None
        page_citation_asset_url = _page_citation_asset_url(
            source=source,
            chunk=chunk,
            page_number=page_citation_page_number,
        )
        payload: dict[str, Any] = {
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
                "section_path": str(chunk.get("path") or citation.section_path),
            },
        }
        if page_citation_page_number is not None:
            payload["page_citation_page_number"] = page_citation_page_number
        if page_numbers:
            payload["page_nums"] = page_numbers
        if page_citation_asset_url:
            payload["page_citation_asset_url"] = page_citation_asset_url
        return payload


def _publication_chunk(
    *,
    source: _DemoSourceDefinition,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    materialized_chunk = dict(chunk)
    metadata = _metadata(materialized_chunk)
    raw_path = _first_string(metadata.get("path"), materialized_chunk.get("path"))
    publication_path = _publication_path(source=source, raw_path=raw_path)
    file_path = _file_path_for_chunk(
        chunk=materialized_chunk,
        metadata=metadata,
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
    prefix = source.title
    raw = str(raw_path or "").strip()
    if not raw:
        return prefix

    if _is_client_asset_path(raw):
        return f"{prefix}/Assets/{raw}"

    parts = split_escaped_document_path(raw)
    if parts and parts[0] == source.title:
        return join_document_path(parts)
    if len(parts) >= 2 and parts[0] == "Default_Root":
        section_parts = parts[2:] if parts[1] == source.title else parts[1:]
        return join_document_path([prefix, *section_parts]) if section_parts else prefix
    if parts and _is_source_file_root(parts[0]):
        section_parts = parts[1:]
        return join_document_path([prefix, *section_parts]) if section_parts else prefix
    return prefix


def _is_source_file_root(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.endswith(_SOURCE_FILE_EXTENSIONS)


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
            metadata = _metadata(chunk)
            if normalized_content in _normalize_text(str(metadata.get("summary") or "")):
                return chunk

    for chunk in chunks:
        if str(chunk.get("path") or "") == citation.section_path:
            return chunk

    raise ValueError(
        "Demo citation does not resolve to a canonical chunk: "
        f"demo_source_id={source.demo_source_id}, section_path={citation.section_path}"
    )


def _page_numbers_from_chunk(chunk: dict[str, Any]) -> list[int]:
    metadata = _metadata(chunk)
    collected: list[int] = []
    _collect_page_numbers(metadata.get("page_nums"), collected)
    _collect_page_numbers(metadata.get("pageNums"), collected)
    unique = sorted({page for page in collected if page > 0})
    return unique


def _collect_page_numbers(value: object, collected: list[int]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        collected.append(value)
        return
    if isinstance(value, float) and value.is_integer():
        collected.append(int(value))
        return
    if isinstance(value, str):
        for part in value.split(","):
            stripped = part.strip()
            if stripped.isdigit():
                collected.append(int(stripped))
        return
    if isinstance(value, list):
        for item in value:
            _collect_page_numbers(item, collected)


def _page_citation_asset_url(
    *,
    source: _DemoSourceDefinition,
    chunk: dict[str, Any],
    page_number: int | None,
) -> str | None:
    metadata = _metadata(chunk)
    raw_assets = metadata.get("page_assets")
    if not isinstance(raw_assets, list):
        return None
    matching: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        artifact_ref = str(item.get("artifact_ref") or "").strip()
        if not artifact_ref.startswith("page_citation_assets/"):
            continue
        if fallback is None:
            fallback = item
        asset_page = item.get("page_num")
        if page_number is not None and asset_page == page_number:
            matching = item
            break
    chosen = matching or fallback
    if chosen is None:
        return None
    return _asset_url(
        source=source,
        file_path=str(chosen.get("artifact_ref") or "").strip(),
    )


def _metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


_CLIENT_ASSET_PREFIXES = ("images/", "tables/", "page_citation_assets/")


def _is_client_asset_path(value: str) -> bool:
    return value.startswith(_CLIENT_ASSET_PREFIXES)


def _is_media_chunk(chunk: dict[str, Any]) -> bool:
    return _normalize_chunk_type(chunk.get("type")) in {"image", "table"}


def _file_path_for_chunk(
    *,
    chunk: dict[str, Any],
    metadata: dict[str, Any],
) -> str | None:
    return _first_string(
        metadata.get("file_path"),
        metadata.get("filePath"),
        chunk.get("file_path"),
        chunk.get("path") if _is_media_chunk(chunk) else None,
        _first_page_asset_ref(metadata),
    )


def _first_page_asset_ref(metadata: dict[str, Any]) -> str | None:
    raw_assets = metadata.get("page_assets")
    if not isinstance(raw_assets, list):
        return None
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        artifact_ref = str(item.get("artifact_ref") or "").strip()
        if artifact_ref.startswith("page_citation_assets/"):
            return artifact_ref
    return None


def _page_assets_with_demo_urls(
    *,
    source: _DemoSourceDefinition,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    raw_assets = metadata.get("page_assets")
    if not isinstance(raw_assets, list):
        return metadata

    page_assets: list[dict[str, Any]] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        asset = dict(item)
        artifact_ref = str(asset.get("artifact_ref") or "").strip()
        if artifact_ref:
            asset["asset_url"] = _asset_url(source=source, file_path=artifact_ref)
        page_assets.append(asset)
    if not page_assets:
        return metadata

    enriched = dict(metadata)
    enriched["page_assets"] = page_assets
    return enriched


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
    return raw if raw in {"text", "image", "table", "page"} else "text"


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())
