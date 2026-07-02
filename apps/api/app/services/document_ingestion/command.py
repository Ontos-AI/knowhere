"""Resolved document-ingestion command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from shared.models.schemas.job import JobCreateBase
from shared.models.schemas.page_memory_config import PageMemoryConfig

ApiVersion = Literal["v1", "v2"]
ParseTrack = Literal["chunk", "page_memory"]
ProcessingGeneration = Literal["legacy_chunk", "page_memory"]
_PAGE_MEMORY_PARSE_TRACK_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".pptx"})


@dataclass(frozen=True)
class DocumentIngestionCommand:
    payload: JobCreateBase
    api_version: ApiVersion
    parse_track: ParseTrack
    processing_generation: ProcessingGeneration
    page_memory_config: PageMemoryConfig | None = None


def build_v1_ingestion_command(*, payload: JobCreateBase) -> DocumentIngestionCommand:
    """Build the legacy chunk ingestion command for v1 jobs."""
    return _build_ingestion_command(
        payload=payload,
        api_version="v1",
        parse_track="chunk",
    )


def build_v2_ingestion_command(
    *,
    payload: JobCreateBase,
    file_extension: str,
) -> DocumentIngestionCommand:
    """Build the v2 ingestion command, using page-memory where it is supported."""
    return _build_ingestion_command(
        payload=payload,
        api_version="v2",
        parse_track=_resolve_v2_parse_track(file_extension=file_extension),
    )


def _build_ingestion_command(
    *,
    payload: JobCreateBase,
    api_version: ApiVersion,
    parse_track: ParseTrack,
) -> DocumentIngestionCommand:
    processing_generation: ProcessingGeneration = (
        "page_memory" if parse_track == "page_memory" else "legacy_chunk"
    )
    page_memory_config = (
        PageMemoryConfig.default() if parse_track == "page_memory" else None
    )
    return DocumentIngestionCommand(
        payload=payload,
        api_version=api_version,
        parse_track=parse_track,
        processing_generation=processing_generation,
        page_memory_config=page_memory_config,
    )


def _resolve_v2_parse_track(*, file_extension: str) -> ParseTrack:
    if file_extension.lower() in _PAGE_MEMORY_PARSE_TRACK_EXTENSIONS:
        return "page_memory"
    return "chunk"
