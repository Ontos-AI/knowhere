"""Authorized reads of the initial directory without loading document bodies."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, noload

from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.document_directory import DocumentDirectoryResponse
from shared.services.jobs.lifecycle.directory_ready import capture_initial_directory
from shared.services.jobs.progressive_ingestion import read_progressive_state
from shared.services.retrieval.publication_models import DocumentPublicationScope


class DocumentDirectoryService:
    async def get_structure(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
        job_result_id: str | None = None,
        namespace: str | None = None,
    ) -> DocumentDirectoryResponse | None:
        # A single statement observes identity and outline in one committed view.
        # Deliberately select scalar metadata, avoiding JobResult's body relations.
        statement = (
            select(
                Document.document_id,
                Document.namespace,
                JobResult.id,
                JobResult.job_id,
                JobResult.document_metadata["initial_directory"],
            )
            .join(JobResult, JobResult.id == Document.current_job_result_id)
            .join(Job, Job.job_id == JobResult.job_id)
            .where(
                Document.document_id == document_id,
                Document.user_id == user_id,
                Document.status == "active",
                JobResult.document_id == Document.document_id,
                Job.user_id == user_id,
            )
        )
        if job_result_id is not None:
            statement = statement.where(JobResult.id == job_result_id)
        if namespace is not None:
            statement = statement.where(Document.namespace == namespace)
        row = (await db.execute(statement)).one_or_none()
        if row is None:
            return None
        current_document_id, current_namespace, revision_id, job_id, raw_directory = row
        if raw_directory is None:
            directory: DocumentDirectoryResponse | None = await db.run_sync(
                self._freeze_historical_structure,
                user_id=user_id,
                document_id=current_document_id,
                job_result_id=revision_id,
                namespace=current_namespace,
            )
            await db.commit()
            return directory
        directory = DocumentDirectoryResponse.model_validate(raw_directory)
        if (
            directory.document_id != current_document_id
            or directory.namespace != current_namespace
            or directory.job_result_id != revision_id
            or directory.job_id != job_id
        ):
            return None
        return directory

    def _freeze_historical_structure(
        self,
        db: Session,
        *,
        user_id: str,
        document_id: str,
        job_result_id: str,
        namespace: str,
    ) -> DocumentDirectoryResponse | None:
        # Match publication's lock order. A legacy outline is captured once;
        # concurrent Fine or a repeated Refresh cannot change its version later.
        document: Document | None = db.scalar(
            select(Document)
            .where(
                Document.document_id == document_id,
                Document.user_id == user_id,
                Document.status == "active",
                Document.namespace == namespace,
                Document.current_job_result_id == job_result_id,
            )
            .with_for_update()
        )
        if document is None:
            return None
        result: JobResult | None = db.scalar(
            select(JobResult)
            .options(noload("*"))
            .where(
                JobResult.id == job_result_id,
                JobResult.document_id == document_id,
            )
            .with_for_update(of=JobResult)
        )
        if result is None:
            return None
        metadata = dict(result.document_metadata or {})
        if metadata.get("initial_directory") is not None:
            return DocumentDirectoryResponse.model_validate(
                metadata["initial_directory"]
            )
        state = read_progressive_state(metadata)
        if metadata.get("enrichment_status") not in {"coarse", "complete"} and (
            state is None
            or state.stages.get("structure", {}).get("status") != "complete"
        ):
            return DocumentDirectoryResponse(
                document_id=document_id,
                namespace=namespace,
                job_result_id=job_result_id,
                job_id=result.job_id,
                directory_version=0,
                directory_committed_at=None,
            )
        directory: DocumentDirectoryResponse = capture_initial_directory(
            db,
            scope=DocumentPublicationScope(
                user_id=user_id,
                namespace=namespace,
                document_id=document_id,
                job_result_id=job_result_id,
                source_file_name=document.source_file_name,
            ),
            job_id=result.job_id,
        )
        result.document_metadata = {
            **metadata,
            "initial_directory": directory.model_dump(mode="json"),
        }
        return directory
