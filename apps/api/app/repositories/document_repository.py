"""
Document data access for retrieval document lifecycle flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence, cast

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult

DocumentChunkRow = tuple[DocumentChunk, DocumentSection | None, JobResult]
DocumentChunkInspectionRow = tuple[DocumentChunk, DocumentSection | None, JobResult, int]
DocumentChunkScopeBounds = tuple[int, int]


@dataclass(frozen=True)
class DocumentChunkGrepRow:
    ordinal: int
    document_chunk_id: str
    chunk_id: str
    chunk_type: str
    section_path: str | None
    source_chunk_path: str | None
    file_path: str | None
    job_id: str | None
    start_offset: int | None
    end_offset: int | None
    snippet: str | None
    content: str | None = None


@dataclass(frozen=True)
class DocumentSectionChunkStats:
    section_id: str | None
    start_chunk: int | None
    end_chunk: int | None
    chunk_count: int
    type_counts: dict[str, int]


@dataclass(frozen=True)
class DocumentOutlineChunkStats:
    job_id: str | None
    total_chunks: int
    type_counts: dict[str, int]
    section_stats_by_id: dict[str | None, DocumentSectionChunkStats]


class DocumentRepository:
    async def list_by_user_namespace(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
    ) -> Sequence[Document]:
        result = await db.execute(
            select(Document)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status != "archived")
            .order_by(Document.updated_at.desc())
        )
        return result.scalars().all()

    async def get_document(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        user_id: str,
    ) -> Document | None:
        result = await db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .where(Document.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_active_document_in_namespace(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        user_id: str,
        namespace: str,
    ) -> Document | None:
        result = await db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == "active")
        )
        return result.scalar_one_or_none()

    async def archive_document(
        self,
        db: AsyncSession,
        *,
        document: Document,
    ) -> Document:
        document.status = "archived"
        document.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return document

    async def count_current_document_chunks(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        chunk_type: str | None = None,
    ) -> int:
        stmt = (
            select(func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(DocumentChunk.chunk_type) == chunk_type)

        result = await db.execute(stmt)
        return int(result.scalar_one())

    async def list_current_document_chunks(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        limit: int,
        offset: int,
        chunk_type: str | None = None,
    ) -> Sequence[DocumentChunkRow]:
        stmt = (
            select(DocumentChunk, DocumentSection, JobResult)
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .order_by(
                DocumentChunk.sort_order.asc(),
                DocumentChunk.created_at.asc(),
                DocumentChunk.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(DocumentChunk.chunk_type) == chunk_type)

        result = await db.execute(stmt)
        return cast(Sequence[DocumentChunkRow], result.all())

    async def list_current_document_sections(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
    ) -> Sequence[DocumentSection]:
        result = await db.execute(
            select(DocumentSection)
            .where(DocumentSection.document_id == document_id)
            .where(DocumentSection.job_result_id == job_result_id)
            .order_by(
                DocumentSection.sort_order.asc(),
                DocumentSection.created_at.asc(),
                DocumentSection.section_id.asc(),
            )
        )
        return result.scalars().all()

    async def get_current_document_outline_chunk_stats(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
    ) -> DocumentOutlineChunkStats:
        sql = """
        SELECT
            dc.section_id,
            lower(dc.chunk_type) AS chunk_type,
            count(*)::integer AS chunk_count,
            min(dc.ordinal)::integer AS start_chunk,
            max(dc.ordinal)::integer AS end_chunk,
            max(jr.job_id) AS job_id
        FROM document_chunks dc
        JOIN job_results jr
            ON jr.id = dc.job_result_id
        WHERE dc.document_id = :document_id
            AND dc.job_result_id = :job_result_id
        GROUP BY dc.section_id, lower(dc.chunk_type)
        ORDER BY min(dc.ordinal) ASC
        """
        result = await db.execute(
            text(sql),
            {
                "document_id": document_id,
                "job_result_id": job_result_id,
            },
        )
        section_stats_by_id: dict[str | None, DocumentSectionChunkStats] = {}
        type_counts: dict[str, int] = {}
        total_chunks = 0
        job_id: str | None = None

        for row in result.mappings().all():
            section_id = cast(str | None, row["section_id"])
            chunk_type = str(row["chunk_type"] or "").strip().lower()
            chunk_count = int(row["chunk_count"])
            start_chunk = int(row["start_chunk"])
            end_chunk = int(row["end_chunk"])
            row_job_id = cast(str | None, row["job_id"])
            if row_job_id and job_id is None:
                job_id = row_job_id
            total_chunks += chunk_count
            type_counts[chunk_type] = type_counts.get(chunk_type, 0) + chunk_count

            current_stats = section_stats_by_id.get(section_id)
            if current_stats is None:
                section_stats_by_id[section_id] = DocumentSectionChunkStats(
                    section_id=section_id,
                    start_chunk=start_chunk,
                    end_chunk=end_chunk,
                    chunk_count=chunk_count,
                    type_counts={chunk_type: chunk_count},
                )
                continue

            merged_type_counts = dict(current_stats.type_counts)
            merged_type_counts[chunk_type] = (
                merged_type_counts.get(chunk_type, 0) + chunk_count
            )
            section_stats_by_id[section_id] = DocumentSectionChunkStats(
                section_id=section_id,
                start_chunk=(
                    min(current_stats.start_chunk, start_chunk)
                    if current_stats.start_chunk is not None
                    else start_chunk
                ),
                end_chunk=(
                    max(current_stats.end_chunk, end_chunk)
                    if current_stats.end_chunk is not None
                    else end_chunk
                ),
                chunk_count=current_stats.chunk_count + chunk_count,
                type_counts=merged_type_counts,
            )

        return DocumentOutlineChunkStats(
            job_id=job_id,
            total_chunks=total_chunks,
            type_counts=type_counts,
            section_stats_by_id=section_stats_by_id,
        )

    async def list_current_document_chunks_for_inspection(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        limit: int,
        offset: int = 0,
        chunk_type: str | None = None,
        minimum_ordinal: int | None = None,
        maximum_ordinal: int | None = None,
        document_chunk_id: str | None = None,
        chunk_id: str | None = None,
        section_path: str | None = None,
        section_path_prefix: str | None = None,
    ) -> Sequence[DocumentChunkInspectionRow]:
        stmt = (
            select(
                DocumentChunk,
                DocumentSection,
                JobResult,
                DocumentChunk.ordinal,
            )
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .order_by(DocumentChunk.ordinal.asc())
            .limit(limit)
            .offset(offset)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(DocumentChunk.chunk_type) == chunk_type)
        if minimum_ordinal is not None:
            stmt = stmt.where(DocumentChunk.ordinal >= minimum_ordinal)
        if maximum_ordinal is not None:
            stmt = stmt.where(DocumentChunk.ordinal <= maximum_ordinal)
        if document_chunk_id is not None:
            stmt = stmt.where(DocumentChunk.id == document_chunk_id)
        if chunk_id is not None:
            stmt = stmt.where(DocumentChunk.chunk_id == chunk_id)
        stmt = _apply_section_filters(
            stmt,
            section_path=section_path,
            section_path_prefix=section_path_prefix,
        )

        result = await db.execute(stmt)
        return cast(Sequence[DocumentChunkInspectionRow], result.all())

    async def grep_current_document_chunks_for_inspection(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        pattern: str,
        is_regex: bool,
        is_case_sensitive: bool,
        limit: int,
        chunk_type: str | None = None,
        section_path_prefix: str | None = None,
        snippet_context_chars: int = 80,
        regex_statement_timeout_ms: int = 250,
    ) -> Sequence[DocumentChunkGrepRow]:
        filter_sql, filter_params = _build_grep_scope_filters(
            chunk_type=chunk_type,
            section_path_prefix=section_path_prefix,
        )
        params: dict[str, object] = {
            "document_id": document_id,
            "job_result_id": job_result_id,
            "pattern": pattern,
            "like_pattern": _build_contains_like_pattern(pattern),
            "limit": limit,
            "snippet_context_chars": snippet_context_chars,
            **filter_params,
        }
        sql = (
            _build_regex_grep_sql(filter_sql, is_case_sensitive=is_case_sensitive)
            if is_regex
            else _build_literal_grep_sql(filter_sql, is_case_sensitive=is_case_sensitive)
        )

        try:
            if is_regex:
                await db.execute(
                    text(
                        "SET LOCAL statement_timeout = "
                        f"{int(regex_statement_timeout_ms)}"
                    )
                )
            result = await db.execute(text(sql), params)
        except DBAPIError as exc:
            if is_regex:
                raise ValueError(
                    "Regex grep failed or timed out; use a narrower section, "
                    "a simpler regex, or literal search."
                ) from exc
            raise

        return [
            DocumentChunkGrepRow(
                ordinal=int(row["ordinal"]),
                document_chunk_id=str(row["document_chunk_id"]),
                chunk_id=str(row["chunk_id"]),
                chunk_type=str(row["chunk_type"]),
                section_path=cast(str | None, row["section_path"]),
                source_chunk_path=cast(str | None, row["source_chunk_path"]),
                file_path=cast(str | None, row["file_path"]),
                job_id=cast(str | None, row["job_id"]),
                start_offset=(
                    int(row["start_offset"]) if row["start_offset"] is not None else None
                ),
                end_offset=(
                    int(row["end_offset"]) if row["end_offset"] is not None else None
                ),
                snippet=cast(str | None, row["snippet"]),
                content=cast(str | None, row["content"]),
            )
            for row in result.mappings().all()
        ]

    async def get_current_document_chunk_scope_bounds(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        section_path: str | None = None,
    ) -> DocumentChunkScopeBounds | None:
        first_ordinal = await self._get_current_document_boundary_ordinal(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=section_path,
            descending=False,
        )
        if first_ordinal is None:
            return None

        last_ordinal = await self._get_current_document_boundary_ordinal(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=section_path,
            descending=True,
        )
        if last_ordinal is None:
            return None
        return first_ordinal, last_ordinal

    async def _get_current_document_boundary_ordinal(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        section_path: str | None,
        descending: bool,
    ) -> int | None:
        order_by = DocumentChunk.ordinal.desc() if descending else DocumentChunk.ordinal.asc()
        stmt = (
            select(DocumentChunk.ordinal)
            .select_from(DocumentChunk)
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .order_by(order_by)
            .limit(1)
        )
        stmt = _apply_section_filters(
            stmt,
            section_path=section_path,
            section_path_prefix=None,
        )

        result = await db.execute(stmt)
        ordinal = result.scalar_one_or_none()
        return int(ordinal) if ordinal is not None else None

    async def get_current_document_chunk(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        document_chunk_id: str,
    ) -> DocumentChunkRow | None:
        stmt = (
            select(DocumentChunk, DocumentSection, JobResult)
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.id == document_chunk_id)
            .limit(1)
        )

        result = await db.execute(stmt)
        row = result.first()
        return cast(DocumentChunkRow | None, row)


def _apply_section_filters(
    stmt,
    *,
    section_path: str | None,
    section_path_prefix: str | None,
):
    if section_path is not None:
        if section_path == "(root)":
            return stmt.where(DocumentChunk.section_id.is_(None))
        return stmt.where(DocumentSection.section_path == section_path)
    if section_path_prefix is not None:
        if section_path_prefix == "(root)":
            return stmt.where(DocumentChunk.section_id.is_(None))
        escaped_prefix = _escape_like_pattern(section_path_prefix)
        return stmt.where(
            or_(
                DocumentSection.section_path == section_path_prefix,
                DocumentSection.section_path.like(
                    f"{escaped_prefix} /%",
                    escape="\\",
                ),
            )
        )
    return stmt


def _escape_like_pattern(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _build_contains_like_pattern(value: str) -> str:
    return f"%{_escape_like_pattern(value)}%"


def _build_grep_scope_filters(
    *,
    chunk_type: str | None,
    section_path_prefix: str | None,
) -> tuple[str, dict[str, object]]:
    filters: list[str] = []
    params: dict[str, object] = {}
    if chunk_type is not None:
        filters.append("AND LOWER(dc.chunk_type) = :chunk_type")
        params["chunk_type"] = chunk_type
    if section_path_prefix is not None:
        if section_path_prefix == "(root)":
            filters.append("AND dc.section_id IS NULL")
        else:
            escaped_prefix = _escape_like_pattern(section_path_prefix)
            filters.append(
                """
                AND (
                    ds.section_path = :section_path_prefix
                    OR ds.section_path LIKE :section_path_like ESCAPE '\\'
                )
                """
            )
            params["section_path_prefix"] = section_path_prefix
            params["section_path_like"] = f"{escaped_prefix} /%"
    return "\n        ".join(filters), params


def _build_grep_base_cte(filter_sql: str) -> str:
    return f"""
    WITH scoped AS (
        SELECT
            dc.id AS document_chunk_id,
            dc.chunk_id,
            dc.section_id,
            dc.chunk_type,
            dc.content,
            dc.source_chunk_path,
            dc.file_path,
            ds.section_path,
            jr.job_id,
            dc.ordinal
        FROM document_chunks dc
        LEFT JOIN document_sections ds
            ON ds.section_id = dc.section_id
        JOIN job_results jr
            ON jr.id = dc.job_result_id
        WHERE dc.document_id = :document_id
            AND dc.job_result_id = :job_result_id
            AND dc.content IS NOT NULL
            AND dc.content <> ''
            {filter_sql}
    )
    """


def _build_literal_grep_sql(
    filter_sql: str,
    *,
    is_case_sensitive: bool,
) -> str:
    match_position_expr = (
        "strpos(sc.content, :pattern)"
        if is_case_sensitive
        else "strpos(lower(sc.content), lower(:pattern))"
    )
    match_filter_expr = (
        "sc.content LIKE :like_pattern ESCAPE E'\\\\'"
        if is_case_sensitive
        else "lower(sc.content) LIKE lower(:like_pattern) ESCAPE E'\\\\'"
    )
    return _build_grep_base_cte(filter_sql) + f"""
    , matched AS (
        SELECT
            sc.*,
            {match_position_expr} AS match_position
        FROM scoped sc
        WHERE {match_filter_expr}
    )
    SELECT
        matched.ordinal,
        matched.document_chunk_id,
        matched.chunk_id,
        matched.chunk_type,
        matched.section_path,
        matched.source_chunk_path,
        matched.file_path,
        matched.job_id,
        (matched.match_position - 1)::integer AS start_offset,
        (matched.match_position - 1 + char_length(:pattern))::integer AS end_offset,
        (
            CASE
                WHEN matched.match_position > (:snippet_context_chars + 1)
                THEN '...'
                ELSE ''
            END
            || btrim(regexp_replace(
                substring(
                    matched.content
                    from greatest(matched.match_position - :snippet_context_chars, 1)
                    for char_length(:pattern) + (:snippet_context_chars * 2)
                ),
                '\\s+',
                ' ',
                'g'
            ))
            || CASE
                WHEN (
                    matched.match_position
                    + char_length(:pattern)
                    + :snippet_context_chars
                    - 1
                ) < char_length(matched.content)
                THEN '...'
                ELSE ''
            END
        ) AS snippet,
        NULL::text AS content
    FROM matched
    ORDER BY matched.ordinal ASC
    LIMIT :limit
    """


def _build_regex_grep_sql(filter_sql: str, *, is_case_sensitive: bool) -> str:
    regex_operator = "~" if is_case_sensitive else "~*"
    return _build_grep_base_cte(filter_sql) + f"""
    SELECT
        sc.ordinal,
        sc.document_chunk_id,
        sc.chunk_id,
        sc.chunk_type,
        sc.section_path,
        sc.source_chunk_path,
        sc.file_path,
        sc.job_id,
        NULL::integer AS start_offset,
        NULL::integer AS end_offset,
        NULL::text AS snippet,
        sc.content
    FROM scoped sc
    WHERE sc.content {regex_operator} :pattern
    ORDER BY sc.ordinal ASC
    LIMIT :limit
    """
