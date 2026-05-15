"""Canonical Demo Source catalog and payload shaping."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class DemoCitationDefinition:
    section_path: str
    description: str
    content: str


@dataclass(frozen=True)
class DemoExampleDefinition:
    id: str
    question: str
    answer: str
    citations: tuple[DemoCitationDefinition, ...]


@dataclass(frozen=True)
class DemoSourceDefinition:
    demo_source_id: str
    canonical_document_id: str
    title: str
    mime_type: str
    size_bytes: int
    asset_directory: str
    chunk_count: int
    examples: tuple[DemoExampleDefinition, ...]


_DATA_ROOT = Path(__file__).resolve().parents[1] / "data" / "demo_documents"
_ASSET_DIRECTORY_NAMES = frozenset({"images", "tables"})
_DEMO_SOURCE_DEFINITIONS: tuple[DemoSourceDefinition, ...] = (
    DemoSourceDefinition(
        demo_source_id="demo-tsla-q4-2025",
        canonical_document_id="demo-doc-tsla-q4-2025",
        title="TSLA-Q4-2025-Update.pdf",
        mime_type="application/pdf",
        size_bytes=5_648_867,
        asset_directory="tsla-q4-2025",
        chunk_count=70,
        examples=(
            DemoExampleDefinition(
                id="demo-tsla-q4-2025-xai",
                question="What does the document say about Tesla's xAI investment?",
                answer=(
                    "Tesla entered an agreement on January 16, 2026 to invest "
                    "approximately $2 billion in xAI Series E Preferred Stock.\n\n"
                    "The document also says Tesla and xAI entered a framework "
                    "agreement to evaluate AI collaboration, with the investment "
                    "expected to close in Q1 2026 subject to customary regulatory "
                    "conditions."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path=(
                            "Default_Root/TSLA-Q4-2025-Update.pdf-->OTHER UPDATES"
                        ),
                        description="xAI investment",
                        content=(
                            "On January 16, 2026, Tesla entered into an agreement "
                            "to invest approximately"
                        ),
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-tsla-q4-2025-energy-storage",
                question="What does the document say about energy storage?",
                answer=(
                    "Tesla achieved its highest quarterly energy storage "
                    "deployments, driven by record Megapack deployments.\n\n"
                    "Energy gross profit reached a record $1.1 billion, marking "
                    "the fifth consecutive record quarter.\n\n"
                    "Tesla also plans to begin Megapack 3 and Megablock "
                    "production at Megafactory Houston in 2026."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path=(
                            "Default_Root/TSLA-Q4-2025-Update.pdf-->SUMMARY-->"
                            "Energy generation and storage"
                        ),
                        description="Storage deployment growth",
                        content=(
                            "We achieved our highest quarterly energy storage "
                            "deployments, driven by record Megapack deployments."
                        ),
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-tsla-q4-2025-production-plans",
                question="What production plans does Tesla mention for 2026?",
                answer=(
                    "Tesla says Cybercab, Tesla Semi, and Megapack 3 are on "
                    "schedule for volume production starting in 2026.\n\n"
                    "The same product update also notes that first-generation "
                    "Optimus production lines are being installed before volume "
                    "production."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path=(
                            "Default_Root/TSLA-Q4-2025-Update.pdf-->OUTLOOK-->"
                            "Product"
                        ),
                        description="2026 production plans",
                        content=(
                            "Cybercab, Tesla Semi and Megapack 3 are on schedule "
                            "for volume production starting in 2026."
                        ),
                    ),
                ),
            ),
        ),
    ),
)


class DemoSourceCatalog:
    def get_catalog(self) -> dict[str, Any]:
        return {
            "sources": [
                self._source_catalog_payload(source)
                for source in _DEMO_SOURCE_DEFINITIONS
            ],
        }

    def list_chunks(
        self,
        *,
        demo_source_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any] | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        chunks = _load_source_chunks(source)
        start = (page - 1) * page_size
        page_chunks = chunks[start : start + page_size]
        return {
            "demo_source_id": source.demo_source_id,
            "canonical_document_id": source.canonical_document_id,
            "title": source.title,
            "mime_type": source.mime_type,
            "chunks": [
                _chunk_payload(source=source, chunk=chunk)
                for chunk in page_chunks
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": len(chunks),
                "total_pages": math.ceil(len(chunks) / page_size) if chunks else 0,
            },
        }

    def get_chunk(
        self,
        *,
        demo_source_id: str,
        demo_chunk_id: str,
    ) -> dict[str, Any] | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        for chunk in _load_source_chunks(source):
            if demo_chunk_id in {_canonical_chunk_id(source, chunk), chunk["chunk_id"]}:
                return {
                    "demo_source_id": source.demo_source_id,
                    "canonical_document_id": source.canonical_document_id,
                    "chunk": _chunk_payload(source=source, chunk=chunk),
                }

        return None

    def get_original_file_path(self, *, demo_source_id: str) -> Path | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        file_path = self.source_directory(source) / "original.pdf"
        return file_path if file_path.is_file() else None

    def get_asset_file_path(
        self,
        *,
        demo_source_id: str,
        asset_path: str,
    ) -> Path | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        source_directory = self.source_directory(source).resolve()
        normalized_asset_path = _normalize_asset_path(asset_path)
        if normalized_asset_path is None:
            return None

        candidate = (source_directory / normalized_asset_path).resolve()
        if not candidate.is_relative_to(source_directory):
            return None

        return candidate if candidate.is_file() else None

    def require_source(self, demo_source_id: str) -> DemoSourceDefinition:
        source = self.get_source(demo_source_id)
        if source is None:
            raise KeyError(demo_source_id)
        return source

    def get_source(self, demo_source_id: str) -> DemoSourceDefinition | None:
        return next(
            (
                source
                for source in _DEMO_SOURCE_DEFINITIONS
                if source.demo_source_id == demo_source_id
            ),
            None,
        )

    def source_directory(self, source: DemoSourceDefinition) -> Path:
        return _DATA_ROOT / source.asset_directory

    def publication_chunks(self, source: DemoSourceDefinition) -> list[dict[str, Any]]:
        return [
            _publication_chunk(source=source, chunk=chunk)
            for chunk in _load_source_chunks(source)
        ]

    def _source_catalog_payload(self, source: DemoSourceDefinition) -> dict[str, Any]:
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
                self._example_payload(source=source, example=example)
                for example in source.examples
            ],
        }

    def _example_payload(
        self,
        *,
        source: DemoSourceDefinition,
        example: DemoExampleDefinition,
    ) -> dict[str, Any]:
        return {
            "id": example.id,
            "question": example.question,
            "answer": example.answer,
            "citations": [
                _citation_payload(source=source, citation=citation)
                for citation in example.citations
            ],
        }


def _publication_chunk(
    *,
    source: DemoSourceDefinition,
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
    source: DemoSourceDefinition,
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


def _normalize_asset_path(asset_path: str) -> Path | None:
    normalized = str(asset_path or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or parts[0] not in _ASSET_DIRECTORY_NAMES:
        return None
    if any(part == ".." or part.startswith(".") for part in parts):
        return None
    return Path(*parts)


def _citation_payload(
    *,
    source: DemoSourceDefinition,
    citation: DemoCitationDefinition,
) -> dict[str, Any]:
    chunk = _resolve_citation_chunk(source=source, citation=citation)
    return {
        "demo_source_id": source.demo_source_id,
        "canonical_document_id": source.canonical_document_id,
        "canonical_chunk_id": _canonical_chunk_id(source, chunk),
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


def _resolve_citation_chunk(
    *,
    source: DemoSourceDefinition,
    citation: DemoCitationDefinition,
) -> dict[str, Any]:
    chunks = _load_source_chunks(source)
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


def _chunk_payload(
    *,
    source: DemoSourceDefinition,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    metadata = _metadata(chunk)
    file_path = _first_string(
        metadata.get("file_path"),
        metadata.get("filePath"),
        chunk.get("file_path"),
        chunk.get("path") if _is_media_chunk(chunk) else None,
    )
    return {
        "id": _canonical_chunk_id(source, chunk),
        "chunk_id": chunk["chunk_id"],
        "chunk_type": _normalize_chunk_type(chunk.get("type")),
        "content": str(chunk.get("content") or ""),
        "section_path": str(chunk.get("path") or "") or None,
        "source_chunk_path": str(chunk.get("path") or "") or None,
        "file_path": file_path,
        "sort_order": _sort_order(source=source, chunk=chunk),
        "metadata": metadata,
        "asset_url": _asset_url(source=source, file_path=file_path),
        "created_at": None,
    }


def _sort_order(
    *,
    source: DemoSourceDefinition,
    chunk: dict[str, Any],
) -> int:
    try:
        return _load_source_chunks(source).index(chunk)
    except ValueError:
        return 0


def _metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _is_media_chunk(chunk: dict[str, Any]) -> bool:
    return _normalize_chunk_type(chunk.get("type")) in {"image", "table"}


def _canonical_chunk_id(
    source: DemoSourceDefinition,
    chunk: dict[str, Any],
) -> str:
    return f"{source.demo_source_id}:{chunk['chunk_id']}"


def _asset_url(
    *,
    source: DemoSourceDefinition,
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


@lru_cache(maxsize=8)
def _load_source_chunks(source: DemoSourceDefinition) -> tuple[dict[str, Any], ...]:
    chunks_path = (_DATA_ROOT / source.asset_directory) / "chunks.json"
    with chunks_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(chunks, list):
        return ()

    return tuple(
        dict(chunk)
        for chunk in chunks
        if isinstance(chunk, dict) and isinstance(chunk.get("chunk_id"), str)
    )
