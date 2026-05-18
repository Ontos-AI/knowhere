"""Canonical Demo Source catalog and file access."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.demo.source_projection import DemoSourceProjection


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


_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "demo_documents"
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
                            "TSLA-Q4-2025-Update.pdf-->OTHER UPDATES"
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
                            "TSLA-Q4-2025-Update.pdf-->SUMMARY-->"
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
                            "TSLA-Q4-2025-Update.pdf-->OUTLOOK-->"
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
    def __init__(self, *, projection: DemoSourceProjection | None = None) -> None:
        self._projection = projection or DemoSourceProjection()

    def get_catalog(self) -> dict[str, Any]:
        return {
            "sources": [
                self._projection.source_catalog_payload(
                    source=source,
                    chunks=_load_source_chunks(source),
                )
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
                self._projection.chunk_payload(
                    source=source,
                    chunk=chunk,
                    sort_order=start + index,
                )
                for index, chunk in enumerate(page_chunks)
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

        chunks = _load_source_chunks(source)
        for sort_order, chunk in enumerate(chunks):
            if self._projection.matches_chunk_id(
                source=source,
                chunk=chunk,
                demo_chunk_id=demo_chunk_id,
            ):
                return {
                    "demo_source_id": source.demo_source_id,
                    "canonical_document_id": source.canonical_document_id,
                    "chunk": self._projection.chunk_payload(
                        source=source,
                        chunk=chunk,
                        sort_order=sort_order,
                    ),
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
        return self._projection.publication_chunks(
            source=source,
            chunks=_load_source_chunks(source),
        )


def _normalize_asset_path(asset_path: str) -> Path | None:
    normalized = str(asset_path or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or parts[0] not in _ASSET_DIRECTORY_NAMES:
        return None
    if any(part == ".." or part.startswith(".") for part in parts):
        return None
    return Path(*parts)


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
