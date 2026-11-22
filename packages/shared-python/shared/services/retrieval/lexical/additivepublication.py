"""Persist and index one isolated enrichment attempt alongside the initial body."""

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.lexical.additivecleanup import NATIVE_WRITE_SETTLEMENT
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.unit import LexicalUnit
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_scope_content import append_document_scope_content
from shared.services.retrieval.scoring.knowhere_provider import SectionRow, UnitRow


class AdditivePublication:
    @staticmethod
    def storeBody(
        db: Session, *, scope: DocumentPublicationScope, scopeId: str, attemptId: str,
        chunks: list[dict[str, Any]], sectionSummaries: dict[str, str] | None = None,
        rawPrefix: str | None = None,
    ) -> None:
        """Commit only after acquiring the unit's current claim in this transaction."""
        if not AdditivePublication._isCurrent(db, scope):
            raise LexicalIndexError("Enrichment document revision is no longer active")
        existing = AdditivePublication._load(db, scope, attemptId)
        if existing is not None:
            if existing.state == "writing" and existing.scope_id == scopeId:
                return
            raise LexicalIndexError("Enrichment attempt already settled")
        db.add(DocumentEnrichmentPublication(
            id=attemptId, document_id=scope.document_id, job_result_id=scope.job_result_id,
            user_id=scope.user_id, namespace=scope.namespace, scope_id=scopeId,
            state="writing", raw_prefix=rawPrefix,
        ))
        db.flush()
        append_document_scope_content(db, scope=scope, scope_id=scopeId, attempt_id=attemptId,
            chunks=chunks, section_summaries=sectionSummaries)

    @staticmethod
    def writeIndex(
        *, scope: DocumentPublicationScope, attemptId: str, assertOwned: Callable[[], None] | None = None,
    ) -> None:
        sessions = get_sync_session_factory()
        units = AdditivePublication._prepareUnits(sessions, scope, attemptId, state="writing")
        lexicalScope, native = AdditivePublication._native(scope)
        try:
            indexUuid, epoch = LexicalCoordination(sessions).ensureIndex(
                scope=lexicalScope, native=native, updateWarmIndexRecency=False,
            )
            with sessions.begin() as db:
                publication = AdditivePublication._load(db, scope, attemptId, lock=True)
                if publication is None or publication.state != "writing" or not AdditivePublication._isCurrent(db, scope):
                    raise LexicalIndexError("Enrichment attempt cannot write its index")
                if publication.index_uuid not in (None, indexUuid):
                    raise LexicalIndexError("Enrichment index identity changed")
                publication.index_uuid = indexUuid
                publication.expected_index_count = len(units)

            def checkOwnership() -> None:
                if assertOwned is not None:
                    assertOwned()
                with sessions.begin() as db:
                    publication = AdditivePublication._load(db, scope, attemptId)
                    if publication is None or publication.state != "writing" or not AdditivePublication._isCurrent(db, scope):
                        raise LexicalIndexError("Enrichment attempt lost write authority")
                    control = db.get(LexicalBackendControl, 1)
                    registry = db.get(LexicalNamespaceIndex, lexicalScope.fingerprint)
                    if (
                        control is None or control.epoch != epoch or control.mode != "serving"
                        or control.backend != "opensearch" or registry is None
                        or registry.state != "open" or registry.indexUuid != indexUuid
                    ):
                        raise LexicalIndexError("Enrichment lost its native index or serving epoch", isRetryable=True)

            checkOwnership()
            native.writeBuild(buildId=attemptId, units=units, beforeBatch=checkOwnership)
            if native.getIndexUuid() != indexUuid:
                raise LexicalIndexError("Enrichment was written to a replaced native index")
        finally:
            native.close()

    @staticmethod
    def hasCompleteIndex(*, scope: DocumentPublicationScope, attemptId: str) -> bool:
        with get_sync_session_factory().begin() as db:
            publication = AdditivePublication._load(db, scope, attemptId)
            if (publication is None or publication.state != "writing" or publication.expected_index_count is None
                or not AdditivePublication._isCurrent(db, scope)):
                return False
            expected, indexUuid = publication.expected_index_count, publication.index_uuid
        _, native = AdditivePublication._native(scope)
        try:
            return native.getIndexUuid() == indexUuid and native.countBuild(attemptId) == expected
        finally:
            native.close()

    @staticmethod
    def complete(db: Session, *, scope: DocumentPublicationScope, attemptId: str) -> bool:
        publication = AdditivePublication._load(db, scope, attemptId, lock=True)
        if publication is None or publication.state != "writing" or publication.expected_index_count is None:
            return False
        if not AdditivePublication._isCurrent(db, scope):
            return False
        publication.state = "published"
        return True

    @staticmethod
    def fail(db: Session, *, scope: DocumentPublicationScope, attemptId: str) -> None:
        publication = AdditivePublication._load(db, scope, attemptId, lock=True)
        if publication is None or publication.state != "writing":
            return
        publication.state = "failed"
        if publication.index_uuid is not None:
            publication.cleanup_after = LexicalCoordination.getDatabaseTime(db) + NATIVE_WRITE_SETTLEMENT

    @staticmethod
    def cleanup(*, scope: DocumentPublicationScope, attemptId: str) -> bool:
        with get_sync_session_factory().begin() as db:
            publication = AdditivePublication._load(db, scope, attemptId)
            if publication is None or publication.state != "failed":
                return False
            if publication.index_uuid is None:
                return True
        _, native = AdditivePublication._native(scope)
        try:
            # Keep the scheduled second pass if an earlier bulk request was in flight.
            return native.deleteBuild(attemptId)
        finally:
            native.close()

    @staticmethod
    def _load(
        db: Session, scope: DocumentPublicationScope, attemptId: str, *, lock: bool = False,
    ) -> DocumentEnrichmentPublication | None:
        publication = DocumentEnrichmentPublication
        statement = select(publication).where(
            publication.id == attemptId, publication.document_id == scope.document_id,
            publication.job_result_id == scope.job_result_id, publication.user_id == scope.user_id,
            publication.namespace == scope.namespace,
        )
        return db.scalar(statement.with_for_update() if lock else statement)

    @staticmethod
    def _isCurrent(db: Session, scope: DocumentPublicationScope) -> bool:
        return db.scalar(select(Document.document_id).where(
            Document.document_id == scope.document_id, Document.current_job_result_id == scope.job_result_id,
            Document.user_id == scope.user_id, Document.namespace == scope.namespace, Document.status == "active",
        )) is not None

    @staticmethod
    def preparePublishedInputs(
        sessions: sessionmaker[Session], *, scope: DocumentPublicationScope,
    ) -> tuple[tuple[str, tuple[LexicalUnit, ...]], ...]:
        """Reconstruct completed publications from their retained, exact bodies."""
        with sessions.begin() as db:
            if not AdditivePublication._isCurrent(db, scope):
                return ()
            publication = DocumentEnrichmentPublication
            attempts = tuple(db.scalars(select(publication.id).where(
                publication.document_id == scope.document_id, publication.job_result_id == scope.job_result_id,
                publication.user_id == scope.user_id, publication.namespace == scope.namespace,
                publication.state == "published",
            ).order_by(publication.id)))
        return tuple((identity, AdditivePublication._prepareUnits(sessions, scope, identity, state="published"))
            for identity in attempts)

    @staticmethod
    def _prepareUnits(
        sessions: sessionmaker[Session], scope: DocumentPublicationScope, attemptId: str, *, state: str,
    ) -> tuple[LexicalUnit, ...]:
        sections, chunks = AdditivePublication._readBody(sessions, scope, attemptId, state=state)
        payload = LexicalPreparation.prepare(documentId=scope.document_id, revisionId=scope.job_result_id,
            sections=sections, chunks=chunks)
        populated = {chunk.section_id for chunk in chunks}
        return tuple(unit for unit in payload.units if unit.sectionId in populated)

    @staticmethod
    def _readBody(
        sessions: sessionmaker[Session], scope: DocumentPublicationScope, attemptId: str, *, state: str,
    ) -> tuple[tuple[SectionRow, ...], tuple[UnitRow, ...]]:
        with sessions.begin() as db:
            publication = AdditivePublication._load(db, scope, attemptId)
            if publication is None or publication.state != state or not AdditivePublication._isCurrent(db, scope):
                raise LexicalIndexError("Enrichment has no eligible stored body")
            chunk = DocumentChunk
            section = DocumentSection
            body = tuple(db.scalars(select(chunk).where(chunk.enrichment_attempt_id == attemptId)
                .order_by(chunk.sort_order, chunk.chunk_id, chunk.id)))
            tree = select(section.section_id, section.parent_section_id).where(section.section_id.in_(
                {row.section_id for row in body if row.section_id is not None}
            )).cte(recursive=True)
            tree = tree.union(select(section.section_id, section.parent_section_id)
                .join(tree, tree.c.parent_section_id == section.section_id))
            sections = tuple(
                SectionRow(row.section_id, row.parent_section_id, row.section_path, row.section_title or "",
                    row.section_level, row.summary or "", row.sort_order)
                for row in db.scalars(select(section).where(section.section_id.in_(select(tree.c.section_id)))
                    .order_by(section.sort_order, section.section_id))
            )
            return sections, tuple(
                UnitRow(row.chunk_id, row.section_id, row.chunk_type, row.content or "", row.sort_order,
                    row.source_chunk_path or "", row.file_path or "", dict(row.chunk_metadata or {}))
                for row in body
            )

    @staticmethod
    def _native(scope: DocumentPublicationScope) -> tuple[LexicalScope, OpenSearchLexicalIndex]:
        lexicalScope = LexicalScope(ownerId=scope.user_id, namespace=scope.namespace)
        native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=lexicalScope,
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
            maximumBulkBytes=settings.KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES)
        return lexicalScope, native
