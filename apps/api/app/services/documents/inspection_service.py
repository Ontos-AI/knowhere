"""Server-side document inspection helpers for MCP tools."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import regex as regex_engine
from app.repositories.document_repository import (
    DocumentOutlineChunkStats,
    DocumentRepository,
    DocumentSectionChunkStats,
)
from pydantic import BaseModel, Field
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.hydration.assets import enrich_rows_with_retrieval_asset_urls
from shared.services.retrieval.outline_snapshot import get_valid_mcp_outline_snapshot

DEFAULT_READ_CHUNK_LIMIT = 12
MAX_READ_CHUNK_LIMIT = 40
DEFAULT_GREP_RESULT_LIMIT = 20
MAX_GREP_RESULT_LIMIT = 50
GREP_SNIPPET_CONTEXT_CHARS = 80
MAX_REGEX_PATTERN_LENGTH = 256
REGEX_SEARCH_TIMEOUT_SECONDS = 0.01
REGEX_GREP_STATEMENT_TIMEOUT_MS = 250


class DocumentSummary(BaseModel):
    document_id: str
    namespace: str
    status: str
    current_job_result_id: str | None = None
    source_file_name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None


class DocumentListResponse(BaseModel):
    namespace: str
    documents: list[DocumentSummary] = Field(default_factory=list)


class DocumentSectionOutline(BaseModel):
    section_id: str
    section_path: str
    section_title: str | None = None
    section_level: int
    summary: str | None = None
    start_chunk: int | None = None
    end_chunk: int | None = None
    chunk_count: int
    type_counts: dict[str, int] = Field(default_factory=dict)


class DocumentOutlineResponse(BaseModel):
    namespace: str
    document: DocumentSummary
    job_result_id: str | None = None
    job_id: str | None = None
    total_chunks: int
    type_counts: dict[str, int] = Field(default_factory=dict)
    sections: list[DocumentSectionOutline] = Field(default_factory=list)


class DocumentReadChunk(BaseModel):
    position: int
    document_chunk_id: str
    chunk_id: str
    chunk_type: str
    content: str
    section_id: str | None = None
    section_path: str | None = None
    source_chunk_path: str | None = None
    file_path: str | None = None
    asset_url: str | None = None
    sort_order: int
    metadata: dict[str, object] = Field(default_factory=dict)


class DocumentReadChunksResponse(BaseModel):
    namespace: str
    document_id: str
    job_result_id: str | None = None
    job_id: str | None = None
    chunks: list[DocumentReadChunk] = Field(default_factory=list)
    next_chunk: int | None = None


class DocumentGrepMatch(BaseModel):
    position: int
    document_chunk_id: str
    chunk_id: str
    chunk_type: str
    section_path: str | None = None
    source_chunk_path: str | None = None
    file_path: str | None = None
    start_offset: int
    end_offset: int
    snippet: str


class DocumentGrepChunksResponse(BaseModel):
    namespace: str
    document_id: str
    job_result_id: str | None = None
    job_id: str | None = None
    matches: list[DocumentGrepMatch] = Field(default_factory=list)
    truncated: bool
    scanned_chunks: int


class DocumentInspectionService:
    def __init__(self, repository: DocumentRepository | None = None) -> None:
        self._repository = repository or DocumentRepository()

    async def list_documents(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
    ) -> DocumentListResponse:
        documents = await self._repository.list_by_user_namespace(
            db,
            user_id=user_id,
            namespace=namespace,
        )
        return DocumentListResponse(
            namespace=namespace,
            documents=[_to_document_summary(document) for document in documents],
        )

    async def get_document_outline(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
    ) -> DocumentOutlineResponse | None:
        document = await self._get_active_document(
            db,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
        )
        if document is None:
            return None

        job_result_id = document.current_job_result_id
        if not job_result_id:
            return DocumentOutlineResponse(
                namespace=namespace,
                document=_to_document_summary(document),
                total_chunks=0,
            )

        job_result = await self._repository.get_job_result(
            db,
            job_result_id=job_result_id,
        )
        if job_result is not None:
            outline_snapshot = get_valid_mcp_outline_snapshot(
                job_result.document_metadata,
                expected_job_result_id=job_result_id,
            )
            snapshot_response = _create_outline_response_from_snapshot(
                outline_snapshot,
                namespace=namespace,
                document=document,
            )
            if snapshot_response is not None:
                return snapshot_response

        sections = await self._repository.list_current_document_sections(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
        )
        chunk_stats = await self._repository.get_current_document_outline_chunk_stats(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
        )
        return DocumentOutlineResponse(
            namespace=namespace,
            document=_to_document_summary(document),
            job_result_id=job_result_id,
            job_id=chunk_stats.job_id,
            total_chunks=chunk_stats.total_chunks,
            type_counts=chunk_stats.type_counts,
            sections=_create_section_outlines_from_stats(
                sections=sections,
                chunk_stats=chunk_stats,
            ),
        )

    async def read_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
        section_path: str | None = None,
        start_chunk: int | None = None,
        end_chunk: int | None = None,
        document_chunk_id: str | None = None,
        chunk_id: str | None = None,
    ) -> DocumentReadChunksResponse | None:
        document = await self._get_active_document(
            db,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
        )
        if document is None:
            return None

        job_result_id = document.current_job_result_id
        if not job_result_id:
            return DocumentReadChunksResponse(namespace=namespace, document_id=document_id)

        normalized_section_path = _normalize_optional_string(section_path)
        normalized_document_chunk_id = _normalize_optional_string(document_chunk_id)
        normalized_chunk_id = _normalize_optional_string(chunk_id)
        if normalized_document_chunk_id or normalized_chunk_id:
            rows = await self._repository.list_current_document_chunks_for_inspection(
                db,
                document_id=document_id,
                job_result_id=job_result_id,
                document_chunk_id=normalized_document_chunk_id,
                chunk_id=normalized_chunk_id,
                section_path=normalized_section_path,
                limit=1,
            )
            return DocumentReadChunksResponse(
                namespace=namespace,
                document_id=document_id,
                job_result_id=job_result_id,
                job_id=_read_job_id(rows),
                chunks=await _create_read_chunks_from_rows(rows),
                next_chunk=None,
            )

        selected_rows, next_rows = await _read_chunk_range_rows(
            repository=self._repository,
            db=db,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=normalized_section_path,
            start_chunk=start_chunk,
            end_chunk=end_chunk,
        )
        selected_chunks = await _create_read_chunks_from_rows(selected_rows)
        return DocumentReadChunksResponse(
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
            job_id=_read_job_id(selected_rows) or _read_job_id(next_rows),
            chunks=selected_chunks,
            next_chunk=_read_first_row_position(next_rows),
        )

    async def grep_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
        pattern: str,
        is_regex: bool = False,
        is_case_sensitive: bool = False,
        max_results: int | None = None,
        chunk_type: str | None = None,
        section_path_prefix: str | None = None,
    ) -> DocumentGrepChunksResponse | None:
        document = await self._get_active_document(
            db,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
        )
        if document is None:
            return None

        job_result_id = document.current_job_result_id
        if not job_result_id:
            return DocumentGrepChunksResponse(
                namespace=namespace,
                document_id=document_id,
                matches=[],
                truncated=False,
                scanned_chunks=0,
            )

        section_prefix = _normalize_optional_string(section_path_prefix)
        normalized_chunk_type = _normalize_chunk_type_filter(chunk_type)
        result_limit = _normalize_positive_integer(
            max_results,
            fallback=DEFAULT_GREP_RESULT_LIMIT,
            maximum=MAX_GREP_RESULT_LIMIT,
        )
        matcher = (
            _create_chunk_matcher(
                pattern=pattern,
                is_regex=True,
                is_case_sensitive=is_case_sensitive,
            )
            if is_regex
            else None
        )
        if not is_regex:
            _validate_literal_pattern(pattern)
        rows = await self._repository.grep_current_document_chunks_for_inspection(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            pattern=pattern.strip(),
            is_regex=is_regex,
            is_case_sensitive=is_case_sensitive,
            limit=result_limit + 1,
            chunk_type=normalized_chunk_type,
            section_path_prefix=section_prefix,
            snippet_context_chars=GREP_SNIPPET_CONTEXT_CHARS,
            regex_statement_timeout_ms=REGEX_GREP_STATEMENT_TIMEOUT_MS,
        )
        matches = _collect_grep_row_matches(
            rows[:result_limit],
            matcher=matcher,
        )

        return DocumentGrepChunksResponse(
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
            job_id=_read_grep_job_id(rows),
            matches=matches,
            truncated=len(rows) > result_limit,
            scanned_chunks=len(rows),
        )

    async def _get_active_document(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
    ) -> Document | None:
        return await self._repository.get_active_document_in_namespace(
            db,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
        )


def _to_document_summary(document: Document) -> DocumentSummary:
    return DocumentSummary(
        document_id=document.document_id,
        namespace=document.namespace,
        status=document.status,
        current_job_result_id=document.current_job_result_id,
        source_file_name=document.source_file_name,
        created_at=_datetime_payload(document.created_at),
        updated_at=_datetime_payload(document.updated_at),
        archived_at=_datetime_payload(document.archived_at),
    )


def _datetime_payload(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _read_job_id(rows: Sequence[object]) -> str | None:
    if len(rows) == 0:
        return None
    row = _unpack_chunk_row(rows[0])
    if row is None:
        return None
    _chunk, _section, job_result, _position = row
    job_id = getattr(job_result, "job_id", None)
    return str(job_id) if job_id else None


async def _create_read_chunks_from_rows(
    rows: Sequence[object],
) -> list[DocumentReadChunk]:
    payloads = _create_read_chunk_payloads(rows)
    enriched_payloads = await enrich_rows_with_retrieval_asset_urls(
        payloads,
        log_context="MCP read chunk",
    )
    return [_create_read_chunk_from_payload(payload) for payload in enriched_payloads]


def _create_read_chunk_payloads(rows: Sequence[object]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        unpacked_row = _unpack_chunk_row(row)
        if unpacked_row is None:
            continue
        chunk, section, job_result, position = unpacked_row
        if not isinstance(chunk, DocumentChunk):
            continue
        section_payload = section if isinstance(section, DocumentSection) else None
        job_id = getattr(job_result, "job_id", None)
        payloads.append(
            {
                "position": position or index + 1,
                "document_chunk_id": chunk.id,
                "chunk_id": chunk.chunk_id,
                "chunk_type": _normalize_chunk_type(chunk.chunk_type),
                "content": chunk.content or "",
                "section_id": chunk.section_id,
                "section_path": section_payload.section_path if section_payload else None,
                "source_chunk_path": chunk.source_chunk_path,
                "file_path": chunk.file_path,
                "sort_order": chunk.sort_order,
                "metadata": chunk.chunk_metadata or {},
                "job_id": str(job_id) if job_id else None,
            }
        )
    return payloads


def _create_read_chunk_from_payload(payload: Mapping[str, Any]) -> DocumentReadChunk:
    metadata_value = payload.get("metadata")
    metadata = (
        {str(key): value for key, value in metadata_value.items()}
        if isinstance(metadata_value, Mapping)
        else {}
    )
    return DocumentReadChunk(
        position=int(payload["position"]),
        document_chunk_id=str(payload["document_chunk_id"]),
        chunk_id=str(payload["chunk_id"]),
        chunk_type=_normalize_chunk_type(payload["chunk_type"]),
        content=str(payload.get("content") or ""),
        section_id=_read_optional_payload_string(payload.get("section_id")),
        section_path=_read_optional_payload_string(payload.get("section_path")),
        source_chunk_path=_read_optional_payload_string(
            payload.get("source_chunk_path")
        ),
        file_path=_read_optional_payload_string(payload.get("file_path")),
        asset_url=_read_optional_payload_string(payload.get("asset_url")),
        sort_order=int(payload["sort_order"]),
        metadata=metadata,
    )


def _read_first_row_position(rows: Sequence[object]) -> int | None:
    if not rows:
        return None
    unpacked_row = _unpack_chunk_row(rows[0])
    if unpacked_row is None:
        return None
    _chunk, _section, _job_result, position = unpacked_row
    return position


def _unpack_chunk_row(row: object) -> tuple[object, object, object, int | None] | None:
    if not isinstance(row, Sequence) or len(row) < 3:
        return None
    position = int(row[3]) if len(row) > 3 and row[3] is not None else None
    return row[0], row[1], row[2], position


def _create_section_outlines_from_stats(
    *,
    sections: Sequence[DocumentSection],
    chunk_stats: DocumentOutlineChunkStats,
) -> list[DocumentSectionOutline]:
    outlines: list[DocumentSectionOutline] = []
    for section in sections:
        stats = chunk_stats.section_stats_by_id.get(section.section_id)
        outlines.append(
            DocumentSectionOutline(
                section_id=section.section_id,
                section_path=section.section_path,
                section_title=section.section_title,
                section_level=section.section_level,
                summary=section.summary,
                start_chunk=stats.start_chunk if stats else None,
                end_chunk=stats.end_chunk if stats else None,
                chunk_count=stats.chunk_count if stats else 0,
                type_counts=stats.type_counts if stats else {},
            )
        )
    if outlines:
        return outlines

    root_stats = _merge_section_chunk_stats(
        list(chunk_stats.section_stats_by_id.values())
    )
    return [
        DocumentSectionOutline(
            section_id="root",
            section_path="(root)",
            section_title="(root)",
            section_level=0,
            start_chunk=root_stats.start_chunk if root_stats else None,
            end_chunk=root_stats.end_chunk if root_stats else None,
            chunk_count=root_stats.chunk_count if root_stats else 0,
            type_counts=root_stats.type_counts if root_stats else {},
        )
    ]


def _merge_section_chunk_stats(
    stats_values: Sequence[DocumentSectionChunkStats],
) -> DocumentSectionChunkStats | None:
    values = list(stats_values)
    if not values:
        return None
    type_counts: dict[str, int] = {}
    for stats in values:
        for chunk_type, count in stats.type_counts.items():
            type_counts[chunk_type] = type_counts.get(chunk_type, 0) + count
    return DocumentSectionChunkStats(
        section_id=None,
        start_chunk=min(
            stats.start_chunk for stats in values if stats.start_chunk is not None
        ),
        end_chunk=max(stats.end_chunk for stats in values if stats.end_chunk is not None),
        chunk_count=sum(stats.chunk_count for stats in values),
        type_counts=type_counts,
    )


def _create_outline_response_from_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    namespace: str,
    document: Document,
) -> DocumentOutlineResponse | None:
    if snapshot is None:
        return None
    try:
        raw_sections = snapshot.get("sections")
        if not isinstance(raw_sections, list):
            return None
        return DocumentOutlineResponse(
            namespace=namespace,
            document=_to_document_summary(document),
            job_result_id=_read_optional_snapshot_string(snapshot.get("job_result_id")),
            job_id=_read_optional_snapshot_string(snapshot.get("job_id")),
            total_chunks=_read_required_snapshot_int(snapshot.get("total_chunks")),
            type_counts=_read_snapshot_type_counts(snapshot.get("type_counts")),
            sections=[
                _create_section_outline_from_snapshot(raw_section)
                for raw_section in raw_sections
            ],
        )
    except (TypeError, ValueError, ValidationError):
        return None


def _create_section_outline_from_snapshot(raw_section: object) -> DocumentSectionOutline:
    if not isinstance(raw_section, Mapping):
        raise ValueError("Outline snapshot section must be an object.")
    return DocumentSectionOutline(
        section_id=_read_required_snapshot_string(raw_section.get("section_id")),
        section_path=_read_required_snapshot_string(raw_section.get("section_path")),
        section_title=_read_optional_snapshot_string(raw_section.get("section_title")),
        section_level=_read_required_snapshot_int(raw_section.get("section_level")),
        summary=_read_optional_snapshot_string(raw_section.get("summary")),
        start_chunk=_read_optional_snapshot_int(raw_section.get("start_chunk")),
        end_chunk=_read_optional_snapshot_int(raw_section.get("end_chunk")),
        chunk_count=_read_required_snapshot_int(raw_section.get("chunk_count")),
        type_counts=_read_snapshot_type_counts(raw_section.get("type_counts")),
    )


def _read_required_snapshot_string(value: object) -> str:
    result = _read_optional_snapshot_string(value)
    if result is None:
        raise ValueError("Required outline snapshot string is missing.")
    return result


def _read_optional_snapshot_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _read_required_snapshot_int(value: object) -> int:
    result = _read_optional_snapshot_int(value)
    if result is None:
        raise ValueError("Required outline snapshot integer is missing.")
    return result


def _read_optional_snapshot_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int | float | str):
        raise ValueError("Outline snapshot integer is invalid.")
    return int(value)


def _read_snapshot_type_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("Outline snapshot type_counts must be an object.")
    type_counts: dict[str, int] = {}
    for chunk_type, count in value.items():
        if not isinstance(count, int | float | str):
            raise ValueError("Outline snapshot type count is invalid.")
        type_counts[str(chunk_type)] = int(count)
    return type_counts


async def _read_chunk_range_rows(
    *,
    repository: DocumentRepository,
    db: AsyncSession,
    document_id: str,
    job_result_id: str,
    section_path: str | None,
    start_chunk: int | None,
    end_chunk: int | None,
) -> tuple[Sequence[object], Sequence[object]]:
    bounds = await repository.get_current_document_chunk_scope_bounds(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        section_path=section_path,
    )
    if bounds is None:
        return [], []

    first_position, last_position = bounds
    start = _clamp_integer(start_chunk, first_position, last_position, first_position)
    requested_end = end_chunk if end_chunk is not None else start + DEFAULT_READ_CHUNK_LIMIT - 1
    end = min(
        _clamp_integer(requested_end, start, last_position, start),
        start + MAX_READ_CHUNK_LIMIT - 1,
    )
    selected_rows = await repository.list_current_document_chunks_for_inspection(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        section_path=section_path,
        minimum_position=start,
        maximum_position=end,
        limit=MAX_READ_CHUNK_LIMIT,
    )
    next_rows = await repository.list_current_document_chunks_for_inspection(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        section_path=section_path,
        minimum_position=end + 1,
        limit=1,
    )
    return selected_rows, next_rows


def _create_chunk_matcher(
    *,
    pattern: str,
    is_regex: bool,
    is_case_sensitive: bool,
) -> Callable[[str], tuple[int, int] | None]:
    normalized_pattern = pattern.strip()
    if not normalized_pattern:
        raise ValueError("Pattern must not be empty.")
    if is_regex:
        if len(normalized_pattern) > MAX_REGEX_PATTERN_LENGTH:
            raise ValueError(
                f"Regex pattern must be {MAX_REGEX_PATTERN_LENGTH} characters or fewer."
            )
        flags = 0 if is_case_sensitive else regex_engine.IGNORECASE
        regex: Any = regex_engine.compile(normalized_pattern, flags)

        def match_regex(content: str) -> tuple[int, int] | None:
            try:
                match = regex.search(content, timeout=REGEX_SEARCH_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise ValueError(
                    "Regex grep timed out; use a narrower section, a simpler regex, "
                    "or literal search."
                ) from exc
            if match is None:
                return None
            return match.start(), match.end()

        return match_regex

    literal = normalized_pattern if is_case_sensitive else normalized_pattern.lower()

    def match_literal(content: str) -> tuple[int, int] | None:
        haystack = content if is_case_sensitive else content.lower()
        start_offset = haystack.find(literal)
        if start_offset < 0:
            return None
        return start_offset, start_offset + len(normalized_pattern)

    return match_literal


def _validate_literal_pattern(pattern: str) -> None:
    if not pattern.strip():
        raise ValueError("Pattern must not be empty.")


def _collect_grep_row_matches(
    rows: Sequence[object],
    *,
    matcher: Callable[[str], tuple[int, int] | None] | None,
) -> list[DocumentGrepMatch]:
    matches: list[DocumentGrepMatch] = []
    for row in rows:
        if matcher is None:
            start_offset = getattr(row, "start_offset", None)
            end_offset = getattr(row, "end_offset", None)
            snippet = getattr(row, "snippet", None)
            if start_offset is None or end_offset is None or snippet is None:
                continue
        else:
            content = str(getattr(row, "content", "") or "")
            match = matcher(content)
            if match is None:
                continue
            start_offset, end_offset = match
            snippet = _create_snippet(content, start_offset, end_offset)
        matches.append(
            DocumentGrepMatch(
                position=int(getattr(row, "position")),
                document_chunk_id=str(getattr(row, "document_chunk_id")),
                chunk_id=str(getattr(row, "chunk_id")),
                chunk_type=_normalize_chunk_type(getattr(row, "chunk_type")),
                section_path=getattr(row, "section_path"),
                source_chunk_path=getattr(row, "source_chunk_path"),
                file_path=getattr(row, "file_path"),
                start_offset=int(start_offset),
                end_offset=int(end_offset),
                snippet=str(snippet),
            )
        )
    return matches


def _read_grep_job_id(rows: Sequence[object]) -> str | None:
    if not rows:
        return None
    job_id = getattr(rows[0], "job_id", None)
    return str(job_id) if job_id else None


def _create_snippet(content: str, start_offset: int, end_offset: int) -> str:
    before_start = max(0, start_offset - 80)
    after_end = min(len(content), end_offset + 80)
    prefix = "..." if before_start > 0 else ""
    suffix = "..." if after_end < len(content) else ""
    normalized = re.sub(r"\s+", " ", content[before_start:after_end]).strip()
    return f"{prefix}{normalized}{suffix}"


def _normalize_positive_integer(
    value: int | None,
    *,
    fallback: int,
    maximum: int,
) -> int:
    if value is None:
        return fallback
    return min(max(1, int(value)), maximum)


def _clamp_integer(
    value: int | None,
    minimum: int,
    maximum: int,
    fallback: int,
) -> int:
    if value is None:
        return fallback
    return min(max(int(value), minimum), maximum)


def _normalize_chunk_type(raw: object) -> str:
    return str(raw or "").strip().split("\n", 1)[0].lower()


def _normalize_chunk_type_filter(raw: str | None) -> str | None:
    normalized = _normalize_optional_string(raw)
    return _normalize_chunk_type(normalized) if normalized else None


def _read_optional_payload_string(raw: object) -> str | None:
    if raw is None:
        return None
    normalized = str(raw).strip()
    return normalized or None


def _normalize_optional_string(raw: str | None) -> str | None:
    if raw is None:
        return None
    normalized = raw.strip()
    return normalized or None
