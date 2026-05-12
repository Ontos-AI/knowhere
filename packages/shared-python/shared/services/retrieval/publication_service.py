"""
Canonical retrieval publication service.

This module owns the retrieval-specific publication work that happens during
job finalization. The job lifecycle service should orchestrate transaction
boundaries and call this service, not define retrieval state construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.retrieval.graph_service import DocumentGraphService, GraphScope
from shared.services.retrieval.lexical_text import (
    build_content_lexical_text,
    build_content_search_text,
    build_path_lexical_text,
    build_path_search_text,
    build_term_search_text,
    section_path_from_chunk_path,
)


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RetrievalPublicationService:
    # ── Public API ──────────────────────────────────────────────────────

    def get_existing_document_scope(
        self,
        db: Session,
        *,
        job_id: str,
    ) -> Optional[Dict[str, str]]:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            return None

        metadata = job.job_metadata or {}
        document_id = metadata.get("document_id")
        if not document_id:
            return None

        document = db.execute(
            select(Document).where(Document.document_id == document_id)
        ).scalar_one_or_none()
        if not document:
            return None

        return {"document_id": document.document_id, "namespace": document.namespace}

    def publish_document_state(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
        chunks: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            logger.warning(f"Job not found for document publication: {job_id}")
            return None

        return self._publish_document_state_for_job(
            db,
            job=job,
            job_result_id=job_result_id,
            chunks=chunks,
        )

    def _publish_document_state_for_job(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
        chunks: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:

        job_metadata = job.job_metadata or {}
        namespace = job_metadata.get("namespace") or "default"
        document_id = job_metadata.get("document_id")
        source_file_name = job_metadata.get("source_file_name") or job_metadata.get("file_name")

        deduped_chunks = chunks

        # If ALL chunks are duplicates → skip document creation entirely
        if not deduped_chunks:
            logger.warning(
                f"⏭️  All chunks are duplicates of existing documents. "
                f"Skipping document creation for job_id={job.job_id}."
            )
            return {
                "user_id": str(job.user_id),
                "namespace": namespace,
                "document_id": None,
                "skipped_all_duplicate": True,
            }

        # ── Document upsert (original logic, but only for deduped chunks) ──
        document = None
        if document_id:
            document = db.execute(
                select(Document)
                .where(
                    Document.document_id == document_id,
                    Document.user_id == str(job.user_id),
                )
                .with_for_update()
            ).scalar_one_or_none()

        if document is None:
            document = Document(
                document_id=document_id or f"doc_{uuid4().hex[:12]}",
                user_id=str(job.user_id),
                namespace=namespace,
                status="active",
                current_job_result_id=job_result_id,
                source_file_name=source_file_name,
            )
            db.add(document)
        else:
            namespace = namespace or document.namespace
            if self._is_stale_document_completion(
                db,
                document=document,
                job=job,
            ):
                logger.warning(
                    f"Skipping stale document publication: job_id={job.job_id}, document_id={document.document_id}"
                )
                return None
            document.status = "active"
            document.archived_at = None
            document.current_job_result_id = job_result_id
            document.source_file_name = source_file_name or document.source_file_name
            document.updated_at = utc_now_naive()

        db.flush()
        document_id = document.document_id
        result = db.execute(select(JobResult).where(JobResult.id == job_result_id))
        job_result = result.scalar_one_or_none()
        if job_result:
            job_result.document_id = document_id
        namespace = namespace or document.namespace

        db.execute(
            delete(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
        )
        db.execute(
            delete(DocumentSection)
            .where(DocumentSection.document_id == document_id)
            .where(DocumentSection.job_result_id == job_result_id)
        )

        # ── Insert only deduped (non-duplicate) chunks ──────────────────
        sections_by_path: Dict[str, DocumentSection] = {}
        for index, chunk in enumerate(deduped_chunks):
            chunk_metadata = chunk.get("metadata") or {}
            source_path = chunk_metadata.get("path") or chunk.get("path")
            section_path = section_path_from_chunk_path(source_path)
            section = sections_by_path.get(section_path)
            if section is None:
                path_parts = [p for p in section_path.split(" / ") if p]
                # Ensure all ancestor sections exist (top-down)
                for depth in range(1, len(path_parts) + 1):
                    ancestor_path = " / ".join(path_parts[:depth])
                    if ancestor_path in sections_by_path:
                        continue
                    ancestor_parent_id = None
                    if depth > 1:
                        parent_path = " / ".join(path_parts[:depth - 1])
                        parent = sections_by_path.get(parent_path)
                        if parent is not None:
                            ancestor_parent_id = parent.section_id
                    ancestor_section = DocumentSection(
                        user_id=str(job.user_id),
                        namespace=namespace,
                        document_id=document_id,
                        job_result_id=job_result_id,
                        parent_section_id=ancestor_parent_id,
                        section_path=ancestor_path,
                        section_title=path_parts[depth - 1],
                        section_level=depth,
                        section_metadata={},
                        sort_order=len(sections_by_path),
                    )
                    db.add(ancestor_section)
                    db.flush()
                    sections_by_path[ancestor_path] = ancestor_section
                section = sections_by_path[section_path]

            chunk_id = chunk.get("chunk_id") or f"chunk_{uuid4().hex[:12]}"
            section_summary = section.summary if section else None
            section_path_str = section.section_path if section else "Root"
            section_title_str = section.section_title if section else None
            path_text = f"{source_file_name or ''} {section_path_str}".strip()

            db.add(
                DocumentChunk(
                    id=f"dchk_{uuid4().hex[:12]}",
                    chunk_id=chunk_id,
                    user_id=str(job.user_id),
                    namespace=namespace,
                    document_id=document_id,
                    job_result_id=job_result_id,
                    section_id=section.section_id,
                    chunk_type=chunk.get("type") or chunk.get("chunk_type") or "text",
                    content=chunk.get("content") or chunk.get("text"),
                    content_lexical_text=build_content_lexical_text(chunk),
                    path_lexical_text=build_path_lexical_text(source_path),
                    content_search_text=build_content_search_text(
                        chunk, section_summary=section_summary
                    ),
                    path_search_text=build_path_search_text(
                        source_file_name=source_file_name,
                        section_path=section_path_str,
                        section_title=section_title_str,
                        section_summary=section_summary,
                    ),
                    term_search_text=build_term_search_text(chunk, path_text=path_text),
                    source_chunk_path=source_path,
                    file_path=chunk_metadata.get("file_path") or chunk.get("file_path"),
                    chunk_metadata=chunk_metadata,
                    sort_order=chunk.get("order", index),
                )
            )

        db.flush()
        return {
            "user_id": str(job.user_id),
            "namespace": namespace,
            "document_id": document_id,
        }

    def publish_document_graph(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
    ) -> None:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            raise RuntimeError(f"Job not found for graph publication: {job_id}")

        self._publish_document_graph_for_job(db, job=job, job_result_id=job_result_id)

    def _publish_document_graph_for_job(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
    ) -> None:

        metadata = job.job_metadata or {}
        namespace = metadata.get("namespace") or "default"
        document_id = metadata.get("document_id")
        if not document_id:
            document = db.execute(
                select(Document).where(Document.current_job_result_id == job_result_id)
            ).scalar_one_or_none()
            document_id = document.document_id if document else None
        if not document_id:
            raise RuntimeError(
                f"Document not found for graph publication: job_id={job.job_id}"
            )

        DocumentGraphService().publish_document_graph(
            db,
            user_id=str(job.user_id),
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
        )

    def remove_document_graph(
        self,
        db: Session,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
    ) -> None:
        DocumentGraphService().remove_document_graph(
            db,
            scope=GraphScope(user_id=user_id, namespace=namespace),
            document_id=document_id,
        )

    def _is_stale_document_completion(
        self,
        db: Session,
        *,
        document: Document,
        job: Job,
    ) -> bool:
        current_job_result_id = getattr(document, "current_job_result_id", None)
        if not current_job_result_id:
            return False

        current_job_result = db.execute(
            select(JobResult).where(JobResult.id == current_job_result_id)
        ).scalar_one_or_none()
        current_job_id = getattr(current_job_result, "job_id", None)
        if current_job_result is None or not current_job_id:
            return False

        current_job = db.execute(
            select(Job).where(Job.job_id == current_job_id)
        ).scalar_one_or_none()
        if current_job is None:
            return False

        current_created_at = getattr(current_job, "created_at", None)
        candidate_created_at = getattr(job, "created_at", None)
        if current_created_at is None or candidate_created_at is None:
            return False

        return current_created_at > candidate_created_at
