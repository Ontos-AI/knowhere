"""Admit an enhancement retry against the current, authorized publication."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    NotFoundException,
)
from shared.models.database.document import Document, DocumentScopeEnrichment
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import EnhancementRetryResponse
from shared.services.jobs.progressive_ingestion import (
    read_progressive_state,
    write_progressive_state,
)
from shared.services.jobs.progressive_retry import renew_failed_enhancement
from shared.utils.utc_now import utc_now_naive


async def retry_job_enrichment(
    db: AsyncSession,
    *,
    user_id: str,
    job_id: str,
) -> EnhancementRetryResponse:
    job: Job | None = (
        await db.execute(
            select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundException(resource="Job", resource_id=job_id)
    result: JobResult | None = (
        await db.execute(select(JobResult).where(JobResult.job_id == job_id))
    ).scalar_one_or_none()
    if result is None or job.status != "done" or not result.document_id:
        raise _build_conflict(job_id)
    # Match worker publication lock order: document, then result. The second
    # read refreshes metadata after a concurrent completion released its lock.
    document: Document | None = (
        await db.execute(
            select(Document)
            .where(
                Document.document_id == result.document_id, Document.user_id == user_id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        document is None
        or document.status != "active"
        or document.current_job_result_id != result.id
    ):
        raise _build_conflict(job_id)
    result = (
        await db.execute(
            select(JobResult)
            .where(JobResult.id == result.id)
            .with_for_update(of=JobResult)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    state = read_progressive_state(result.document_metadata)
    if state is None:
        raise _build_conflict(job_id)
    renewed = renew_failed_enhancement(state, job_id=job_id)
    if renewed.enhancement.attempt != state.enhancement.attempt:
        result.document_metadata = write_progressive_state(
            result.document_metadata, renewed
        )
        await db.execute(
            update(DocumentScopeEnrichment)
            .where(
                DocumentScopeEnrichment.job_result_id == result.id,
                DocumentScopeEnrichment.state == "failed",
            )
            .values(
                state="coarse",
                attempts=0,
                claim_generation=None,
                updated_at=utc_now_naive(),
            )
        )
    revision_id: str = result.id
    response = EnhancementRetryResponse(job_id=job_id, enhancement=renewed.enhancement)
    await db.commit()
    from shared.services.jobs.lifecycle.scope_enrichments import (
        enqueue_progressive_recovery_after_commit,
    )

    enqueue_progressive_recovery_after_commit(revision_id)
    return response


def _build_conflict(job_id: str) -> ConflictException:
    return ConflictException(
        user_message="Only the current completed progressive document can retry enhancement.",
        resource="Job",
        resource_id=job_id,
    )
