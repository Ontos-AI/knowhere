"""Canonical Demo Source catalog and file access."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.demo.official_library_catalog import (
    get_official_library_catalog,
    get_official_library_source_payload_by_demo_source_id,
)
from app.services.demo.source_projection import DemoSourceProjection


@dataclass(frozen=True)
class DemoCitationDefinition:
    section_path: str
    description: str
    content: str
    page_number: int | None = None


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
    original_file_name: str | None = "original.pdf"


_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "demo_documents"
_ASSET_DIRECTORY_NAMES = frozenset({"images", "tables", "page_citation_assets"})
_DEMO_SOURCE_DEFINITIONS: tuple[DemoSourceDefinition, ...] = (
    DemoSourceDefinition(
        demo_source_id="demo-spacex-s1",
        canonical_document_id="demo-doc-spacex-s1",
        title="spacex-s1.pdf",
        mime_type="application/pdf",
        size_bytes=7_441_414,
        asset_directory="spacex-s1",
        chunk_count=227,
        examples=(
            DemoExampleDefinition(
                id="demo-spacex-s1-starlink-scale",
                question="What does the filing say about Starlink's scale?",
                answer=(
                    "The filing says SpaceX operates a high-speed, low-latency "
                    "global broadband network powered by about 9,600 Starlink "
                    "broadband and mobile satellites in Low-Earth Orbit. "
                    "[[cite:1]]\n\n"
                    "It says that network serves consumer, enterprise, and "
                    "government customers across 164 countries, territories, "
                    "and other markets as of March 31, 2026. [[cite:1]]"
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path="spacex-s1.pdf/Root",
                        description="Starlink network scale",
                        content=(
                            "approximately 9,600 Starlink broadband "
                            "and mobile satellites"
                        ),
                        page_number=28,
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-spacex-s1-launch-reusability",
                question="How does the filing describe SpaceX's launch reusability?",
                answer=(
                    "The filing says Falcon 9 reusability gave SpaceX a "
                    "step-function cost advantage in space access. [[cite:1]]\n\n"
                    "It also says Falcon 9 first stages had demonstrated the "
                    "ability to refly 34 times as of March 31, 2026. [[cite:1]]"
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path="spacex-s1.pdf/Root",
                        description="Falcon 9 booster reuse",
                        content=(
                            "refly a first-stage 34 times"
                        ),
                        page_number=32,
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-spacex-s1-offering",
                question="What offering does the filing describe?",
                answer=(
                    "The filing describes an initial public offering of shares "
                    "of Space Exploration Technologies Corp. Class A common stock. "
                    "[[cite:1]]"
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path="spacex-s1.pdf/Root",
                        description="Class A common stock offering",
                        content=(
                            "This is the initial public offering of shares of "
                            "Class A common stock"
                        ),
                        page_number=2,
                    ),
                ),
            ),
        ),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-micron-report-530bd7ed",
        canonical_document_id="demo-doc-financial-micron-report-530bd7ed",
        title="Micron investor report 530bd7ed.pdf",
        mime_type="application/pdf",
        size_bytes=4_497_791,
        asset_directory="financial-micron-report-530bd7ed",
        chunk_count=77,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-micron-report-9c0becf5",
        canonical_document_id="demo-doc-financial-micron-report-9c0becf5",
        title="Micron investor report 9c0becf5.pdf",
        mime_type="application/pdf",
        size_bytes=4_383_420,
        asset_directory="financial-micron-report-9c0becf5",
        chunk_count=82,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-transformers-tutorial",
        canonical_document_id="demo-doc-stem-transformers-tutorial",
        title="Transformers Tutorial.pdf",
        mime_type="application/pdf",
        size_bytes=12_824_695,
        asset_directory="stem-transformers-tutorial",
        chunk_count=301,
        examples=(),
        original_file_name=None,
    ),
)


class DemoSourceCatalog:
    def __init__(self, *, projection: DemoSourceProjection | None = None) -> None:
        self._projection = projection or DemoSourceProjection()

    def list_sources(self) -> tuple[DemoSourceDefinition, ...]:
        return _DEMO_SOURCE_DEFINITIONS

    def get_catalog(self) -> dict[str, Any]:
        source_payloads = [
            self._projection.source_catalog_payload(
                source=source,
                chunks=_load_source_chunks(source) if source.examples else (),
            )
            for source in _DEMO_SOURCE_DEFINITIONS
        ]
        library_source_payload_by_demo_source_id = (
            get_official_library_source_payload_by_demo_source_id()
        )
        for payload in source_payloads:
            demo_source_id = str(payload["demo_source_id"])
            library_payload = library_source_payload_by_demo_source_id.get(
                demo_source_id
            )
            if library_payload is not None:
                payload["official_library"] = library_payload

        return {
            "sources": source_payloads,
            "official_library": get_official_library_catalog(
                {str(payload["demo_source_id"]): payload for payload in source_payloads}
            ),
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
        if source.original_file_name is None:
            return None

        file_path = self.source_directory(source) / source.original_file_name
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
