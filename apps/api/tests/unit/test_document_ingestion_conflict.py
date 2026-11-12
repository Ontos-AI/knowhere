"""The active-document unique index race returns the documented 409 body.

Two creates can both pass the active-job pre-check, so HTTP contract tests
cannot reach this path deterministically. The repository seam simulates the
losing INSERT instead.
"""

from typing import cast

import pytest
from app.repositories.job_repository import JobRepository
from app.services.document_ingestion.command import DocumentIngestionCommand
from app.services.document_ingestion.creation_service import (
    DocumentIngestionCreationService,
    ResolvedDocumentIngestionScope,
)
from app.services.rate_limit.data_structures import CurrentUser
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import ConflictException
from shared.models.database.job import Job
from shared.models.schemas.job import JobCreate
from shared.services.storage.file_upload_service import FileUploadService


class _ActiveDocumentRaceJobRepository(JobRepository):
    """Simulates a concurrent create winning the active-document index."""

    def __init__(self) -> None:
        pass

    async def create_job(self, *args: object, **kwargs: object) -> Job | None:
        raise IntegrityError(
            "INSERT INTO jobs",
            None,
            Exception(
                'duplicate key value violates unique constraint "uq_jobs_user_active_document"'
            ),
        )


class _UnusedFileUploadService(FileUploadService):
    def __init__(self) -> None:
        pass


@pytest.mark.asyncio
async def test_should_omit_active_job_id_when_the_active_document_index_rejects_a_create() -> None:
    document_id: str = "doc_race_target"
    service = DocumentIngestionCreationService(
        job_repository=_ActiveDocumentRaceJobRepository(),
        file_upload_service=_UnusedFileUploadService(),
    )
    command = DocumentIngestionCommand(
        payload=JobCreate(
            source_type="file", file_name="report.pdf", document_id=document_id
        ),
        api_version="v1",
        parse_track="page_memory",
        processing_generation="page_memory",
    )
    scope = ResolvedDocumentIngestionScope(
        job_metadata={"document_id": document_id, "namespace": "default"},
        document_id=document_id,
        namespace="default",
    )

    with pytest.raises(ConflictException) as raised:
        await service.create_job(
            cast(AsyncSession, None),
            command=command,
            job_id="job_race_loser",
            current_user=CurrentUser(user_id="user_race", user_tier="free"),
            scope=scope,
        )

    assert raised.value.http_status_code == 409
    assert raised.value.user_message == "Document already has an active ingestion job."
    assert raised.value.details == {
        "reason": "ABORTED",
        "resource": "Document",
        "id": document_id,
    }
