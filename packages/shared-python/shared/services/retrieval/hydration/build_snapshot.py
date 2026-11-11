"""Persist and hydrate chunk rows by the native build that made them searchable."""

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.scope import LexicalScope

_CAPTURE_HEADER: str = """
    INSERT INTO lexical_hydration_snapshots (build_id, source_file_name, job_id, result_raw_prefix)
    SELECT build.id, COALESCE(job.job_metadata ->> 'source_file_name', document.source_file_name),
           result.job_id, result.document_metadata ->> 'result_raw_prefix'
    FROM document_indexing_builds AS build
    JOIN documents AS document ON document.document_id = build.document_id
    JOIN job_results AS result ON result.id = build.revision_id
    JOIN jobs AS job ON job.job_id = result.job_id
    WHERE build.id = :buildId AND build.namespace_index_id = :scopeId
        AND document.user_id = :ownerId AND document.namespace = :namespace
        AND document.status = 'active' AND document.current_indexing_build_id = build.id
        AND document.current_job_result_id = build.revision_id
    FOR UPDATE OF document
    ON CONFLICT (build_id) DO NOTHING
    RETURNING build_id
"""

_FIND_HEADER: str = """
    SELECT snapshot.build_id
    FROM lexical_hydration_snapshots AS snapshot
    JOIN document_indexing_builds AS build ON build.id = snapshot.build_id
    WHERE build.id = :buildId AND build.namespace_index_id = :scopeId
"""

_CAPTURE_ROWS: str = """
    WITH source AS MATERIALIZED (
        SELECT chunk.id, chunk.job_result_id AS revision_id, chunk.chunk_id, chunk.section_id, chunk.sort_order,
               section.section_path, chunk.chunk_type, chunk.content, chunk.source_chunk_path,
               chunk.file_path, chunk.chunk_metadata,
               sha256(convert_to(CAST(json_build_array(
                   section.section_path, chunk.chunk_type, chunk.content, chunk.source_chunk_path,
                   chunk.file_path, chunk.chunk_metadata
               ) AS text), 'UTF8')) AS payload_hash
        FROM document_indexing_builds AS build
        JOIN document_chunks AS chunk
            ON chunk.document_id = build.document_id AND chunk.job_result_id = build.revision_id
        LEFT JOIN document_sections AS section
            ON section.section_id = chunk.section_id AND section.document_id = chunk.document_id
            AND section.job_result_id = chunk.job_result_id
        WHERE build.id = :buildId
    ), inserted_payloads AS (
        INSERT INTO lexical_hydration_payloads (revision_id, payload_hash, section_path, chunk_type,
            content, source_chunk_path, file_path, chunk_metadata)
        SELECT revision_id, payload_hash, section_path, chunk_type,
               content, source_chunk_path, file_path, chunk_metadata
        FROM source
        ON CONFLICT (revision_id, payload_hash) DO NOTHING
    )
    INSERT INTO lexical_hydration_rows (build_id, source_row_id, revision_id, payload_hash,
        chunk_id, section_id, sort_order)
    SELECT :buildId, id, revision_id, payload_hash, chunk_id, section_id, sort_order
    FROM source
"""

_REQUESTED_KEYS: str = """
    WITH section_targets AS (
        SELECT * FROM unnest(CAST(:sectionBuildIds AS varchar[]), CAST(:sectionIds AS varchar[]))
            AS target(build_id, section_id)
    ), chunk_targets AS (
        SELECT * FROM unnest(CAST(:chunkBuildIds AS varchar[]), CAST(:chunkIds AS varchar[]))
            AS target(build_id, chunk_id)
    ), snapshot_rows AS (
        SELECT member.build_id, member.source_row_id
        FROM section_targets
        JOIN lexical_hydration_rows AS member USING (build_id, section_id)
        UNION
        SELECT member.build_id, member.source_row_id
        FROM chunk_targets
        JOIN lexical_hydration_rows AS member USING (build_id, chunk_id)
    )
"""

_CURRENT_KEYS: str = """
    , current_builds AS MATERIALIZED (
        SELECT build.id, build.document_id, build.revision_id, document.source_file_name
        FROM (
            SELECT build_id FROM section_targets
            UNION
            SELECT build_id FROM chunk_targets
        ) AS targets
        JOIN document_indexing_builds AS build ON build.id = targets.build_id
        JOIN documents AS document ON document.document_id = build.document_id
        WHERE build.namespace_index_id = :scopeId AND build.state = 'published'
            AND document.user_id = :ownerId AND document.namespace = :namespace
            AND document.status = 'active' AND document.current_indexing_build_id = build.id
            AND document.current_job_result_id = build.revision_id
            AND NOT EXISTS (SELECT 1 FROM lexical_hydration_snapshots WHERE build_id = build.id)
    ), current_rows AS (
        SELECT target.build_id, chunk.id AS source_row_id
        FROM section_targets AS target
        JOIN current_builds AS build ON build.id = target.build_id
        JOIN document_chunks AS chunk ON chunk.document_id = build.document_id
            AND chunk.job_result_id = build.revision_id AND chunk.section_id = target.section_id
        UNION
        SELECT target.build_id, chunk.id AS source_row_id
        FROM chunk_targets AS target
        JOIN current_builds AS build ON build.id = target.build_id
        JOIN document_chunks AS chunk ON chunk.document_id = build.document_id
            AND chunk.job_result_id = build.revision_id AND chunk.chunk_id = target.chunk_id
    )
"""

_LOAD_SNAPSHOT_ROWS: str = """
    SELECT build.id AS build_id, build.document_id, build.revision_id AS job_result_id,
           member.chunk_id, member.section_id, member.sort_order, payload.section_path, payload.chunk_type,
           payload.content, payload.source_chunk_path, payload.file_path, payload.chunk_metadata,
           snapshot.source_file_name, snapshot.job_id, snapshot.result_raw_prefix, member.source_row_id
    FROM snapshot_rows
    JOIN lexical_hydration_rows AS member USING (build_id, source_row_id)
    JOIN lexical_hydration_payloads AS payload USING (revision_id, payload_hash)
    JOIN lexical_hydration_snapshots AS snapshot ON snapshot.build_id = member.build_id
    JOIN document_indexing_builds AS build ON build.id = member.build_id
    WHERE build.namespace_index_id = :scopeId
"""

_REQUIRE_ACTIVE_DOCUMENT: str = """
    AND EXISTS (
        SELECT 1 FROM documents AS document
        WHERE document.document_id = build.document_id AND document.user_id = :ownerId
            AND document.namespace = :namespace AND document.status = 'active'
    )
"""

_LOAD_CURRENT_ROWS: str = """
    UNION ALL
    SELECT build.id, build.document_id, build.revision_id,
           chunk.chunk_id, chunk.section_id, chunk.sort_order, section.section_path, chunk.chunk_type,
           chunk.content, chunk.source_chunk_path, chunk.file_path, chunk.chunk_metadata,
           COALESCE(job.job_metadata ->> 'source_file_name', build.source_file_name),
           result.job_id, result.document_metadata ->> 'result_raw_prefix', chunk.id
    FROM current_rows AS candidate
    JOIN current_builds AS build ON build.id = candidate.build_id
    JOIN document_chunks AS chunk ON chunk.id = candidate.source_row_id
    LEFT JOIN document_sections AS section ON section.section_id = chunk.section_id
        AND section.document_id = chunk.document_id AND section.job_result_id = chunk.job_result_id
    JOIN job_results AS result ON result.id = build.revision_id
    JOIN jobs AS job ON job.job_id = result.job_id
"""


class BuildSnapshot:
    @staticmethod
    def capture(db: Session, *, scope: LexicalScope, buildId: str) -> None:
        """Capture the current build in the caller's publication transaction."""
        db.flush()
        storage: CorpusStorage = CorpusStorage(
            "DEMO" if scope.corpusKind == "demo" else "PRIVATE"
        )
        parameters: dict[str, str] = {
            "buildId": buildId,
            "scopeId": scope.fingerprint,
            "ownerId": scope.ownerId,
            "namespace": scope.namespace,
        }
        insertedBuildId: str | None = db.execute(
            text(storage.compile_sql(_CAPTURE_HEADER)),
            parameters,
        ).scalar_one_or_none()
        if insertedBuildId is not None:
            db.execute(text(storage.compile_sql(_CAPTURE_ROWS)), {"buildId": buildId})
            return
        if db.execute(text(_FIND_HEADER), parameters).scalar_one_or_none() is None:
            raise LexicalIndexError(
                f"Cannot capture hydration for non-current lexical build {buildId}"
            )

    @staticmethod
    async def loadRows(
        db: AsyncSession,
        *,
        scope: LexicalScope,
        sectionKeys: Sequence[tuple[str, str]] = (),
        chunkKeys: Sequence[tuple[str, str]] = (),
        allowCurrent: bool = False,
    ) -> list[dict[str, object]]:
        """Load matching rows, allowing legacy builds only while their pointer is current."""
        if not sectionKeys and not chunkKeys:
            return []
        statement: str = _REQUESTED_KEYS + _LOAD_SNAPSHOT_ROWS
        if allowCurrent:
            storage: CorpusStorage = CorpusStorage(
                "DEMO" if scope.corpusKind == "demo" else "PRIVATE"
            )
            # The pointer check and live body read must share one MVCC statement.
            statement = storage.compile_sql(
                _REQUESTED_KEYS
                + _CURRENT_KEYS
                + _LOAD_SNAPSHOT_ROWS
                + _REQUIRE_ACTIVE_DOCUMENT
                + _LOAD_CURRENT_ROWS
            )
        result = await db.execute(
            text(statement + " ORDER BY build_id, sort_order, chunk_id, source_row_id"),
            {
                "scopeId": scope.fingerprint,
                "ownerId": scope.ownerId,
                "namespace": scope.namespace,
                "sectionBuildIds": [buildId for buildId, _ in sectionKeys],
                "sectionIds": [sectionId for _, sectionId in sectionKeys],
                "chunkBuildIds": [buildId for buildId, _ in chunkKeys],
                "chunkIds": [chunkId for _, chunkId in chunkKeys],
            },
        )
        rows: list[dict[str, object]] = []
        for row in result.mappings():
            hydrationRow: dict[str, object] = dict(row)
            hydrationRow.pop("source_row_id")
            if allowCurrent:
                hydrationRow["uses_inline_build"] = True
            metadata: object = hydrationRow["chunk_metadata"]
            hydrationRow["chunk_metadata"] = (
                metadata if isinstance(metadata, dict) else {}
            )
            hydrationRow["score"] = None
            rows.append(hydrationRow)
        return rows
