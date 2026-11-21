"""Resolve eligible OpenSearch candidate bindings and exact retained chunk bodies."""

from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from shared.core.config import settings
from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
from shared.models.database.job_result import JobResult
from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.scope import LexicalScope


class AdditiveVisibility:
    @staticmethod
    def isEnabled(namespace: str) -> bool:
        return (namespace != DEMO_NAMESPACE and settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"
            and bool(getattr(settings, "KNOWHERE_ADDITIVE_PUBLICATION_ENABLED", False)))

    @staticmethod
    def allowsChunk() -> ColumnElement[bool]:
        """An attempt's retained body is enumerable only while it can serve."""
        publication = DocumentEnrichmentPublication
        return or_(DocumentChunk.enrichment_attempt_id.is_(None), select(publication.id).where(
            publication.id == DocumentChunk.enrichment_attempt_id,
            publication.state.in_(("writing", "published")),
        ).exists())

    @staticmethod
    def allowsFreshChunk(namespace: str) -> ColumnElement[bool]:
        if not AdditiveVisibility.isEnabled(namespace):
            return true()
        return and_(AdditiveVisibility.allowsChunk(), select(Document.document_id).where(
            Document.document_id == DocumentChunk.document_id,
            Document.current_job_result_id == DocumentChunk.job_result_id,
            Document.status == "active",
        ).correlate(DocumentChunk).exists())

    @staticmethod
    def allowsFreshSection(namespace: str) -> ColumnElement[bool]:
        if not AdditiveVisibility.isEnabled(namespace):
            return true()
        descendant = aliased(DocumentSection)
        return select(DocumentChunk.id).join(descendant, descendant.section_id == DocumentChunk.section_id).where(
            DocumentChunk.document_id == DocumentSection.document_id,
            DocumentChunk.job_result_id == DocumentSection.job_result_id,
            or_(descendant.section_id == DocumentSection.section_id,
                func.starts_with(descendant.section_path, DocumentSection.section_path + " / ")),
            AdditiveVisibility.allowsFreshChunk(namespace),
        ).correlate(DocumentSection).exists()

    @staticmethod
    async def filterCandidates(
        db: AsyncSession, scope: LexicalScope, candidates: tuple[tuple[str, str, str], ...],
    ) -> set[tuple[str, str, str]]:
        if not candidates:
            return set()
        documents, builds = {row[0] for row in candidates}, {row[2] for row in candidates}
        publication = DocumentEnrichmentPublication
        eligible = await db.execute(select(
            Document.document_id, Document.current_job_result_id,
            Document.current_indexing_build_id, publication.id,
        ).outerjoin(publication, and_(
            publication.id.in_(builds), publication.document_id == Document.document_id,
            publication.job_result_id == Document.current_job_result_id,
            publication.user_id == Document.user_id, publication.namespace == Document.namespace,
            publication.state.in_(("writing", "published")),
        )).where(
            Document.document_id.in_(documents), Document.user_id == scope.ownerId,
            Document.namespace == scope.namespace, Document.status == "active",
        ))
        returned = set(candidates)
        return {
            (documentId, revisionId, buildId)
            for documentId, revisionId, baselineId, attemptId in eligible
            for buildId in (baselineId, attemptId)
            if (documentId, revisionId, buildId) in returned
        }

    @staticmethod
    async def loadRows(
        db: AsyncSession, *, scope: LexicalScope, hits: tuple[LexicalHit, ...] = (),
        chunkTargets: tuple[tuple[str, str, str, str], ...] = (),
        baselineTargets: tuple[tuple[str, str, str], ...] = (),
    ) -> list[dict[str, Any]]:
        if not hits and not chunkTargets and not baselineTargets:
            return []
        chunk = DocumentChunk
        publication = DocumentEnrichmentPublication
        build = func.coalesce(chunk.enrichment_attempt_id, Document.current_indexing_build_id)
        if baselineTargets:
            matches = and_(chunk.enrichment_attempt_id.is_(None),
                tuple_(chunk.document_id, chunk.job_result_id, chunk.chunk_id).in_(baselineTargets))
        else:
            keys = tuple_(
                chunk.document_id, chunk.job_result_id, build, chunk.section_id if hits else chunk.chunk_id,
            )
            targets = (tuple((hit.unit.documentId, hit.unit.revisionId, hit.buildId, hit.unit.sectionId) for hit in hits)
                if hits else chunkTargets)
            matches = keys.in_(targets)
        output = await db.execute(select(
            chunk, DocumentSection.section_path, Document.source_file_name, JobResult.job_id,
            JobResult.document_metadata["result_raw_prefix"].as_string(), build,
            publication.scope_id, publication.state,
        ).select_from(chunk).join(Document, and_(
            chunk.document_id == Document.document_id, chunk.job_result_id == Document.current_job_result_id,
        )).outerjoin(DocumentSection, DocumentSection.section_id == chunk.section_id)
            .join(JobResult, JobResult.id == chunk.job_result_id)
            .outerjoin(publication, publication.id == chunk.enrichment_attempt_id)
            .where(
                Document.user_id == scope.ownerId, Document.namespace == scope.namespace, Document.status == "active",
                matches,
                or_(chunk.enrichment_attempt_id.is_(None), publication.state.in_(("writing", "published"))),
            ).order_by(chunk.sort_order, chunk.chunk_id, chunk.id))
        return [
            {
                "document_id": row.document_id,
                "job_result_id": row.job_result_id,
                "section_id": row.section_id,
                "section_path": sectionPath,
                "source_file_name": sourceFileName,
                "chunk_id": row.chunk_id,
                "source_row_id": row.id,
                "chunk_type": row.chunk_type,
                "content": row.content,
                "file_path": row.file_path,
                "source_chunk_path": row.source_chunk_path,
                "chunk_metadata": row.chunk_metadata or {},
                "sort_order": row.sort_order,
                "job_id": jobId,
                "result_raw_prefix": prefix,
                "build_id": buildId,
                "enrichment_attempt_id": row.enrichment_attempt_id,
                "enrichment_scope_id": scopeId,
                "enrichment_publication_state": state,
                "uses_additive_visibility": True,
            }
            for row, sectionPath, sourceFileName, jobId, prefix, buildId, scopeId, state in output
        ]
