"""Settle overdue guarded PUTs using the same storage evidence as confirmation."""

from __future__ import annotations

from datetime import datetime, timedelta
from time import monotonic

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import and_, select

from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    StorageServiceException,
    UnavailableException,
)
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.jobs.source_upload_deadline import (
    UPLOAD_EXPIRED_CODE,
    UPLOAD_EXPIRED_MESSAGE,
    UPLOAD_INTEGRITY_MISMATCH_CODE,
    UPLOAD_INTEGRITY_MISMATCH_MESSAGE,
    classify_guarded_source_upload,
    get_guarded_source_upload_lifetime_seconds,
)
from shared.services.jobs.upload_parse_dispatch import UploadParseOutbox, has_source_integrity
from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.utc_now import utc_now_naive

_MAX_GUARDED_JOBS_PER_SWEEP = 32
_SWEEP_BUDGET_SECONDS = 60

guarded_waiting_upload = and_(
    Job.status == JobStatus.WAITING_FILE.value,
    has_source_integrity,
)


def sweep_guarded_uploads(now: datetime) -> tuple[int, int]:
    cutoff = now - timedelta(seconds=get_guarded_source_upload_lifetime_seconds())
    with get_sync_db_context() as db:
        candidates = db.scalars(
            select(Job)
            .where(guarded_waiting_upload, Job.created_at <= cutoff)
            .order_by(Job.updated_at, Job.job_id)
            .limit(_MAX_GUARDED_JOBS_PER_SWEEP)
        ).all()

    state_machine = SyncStateMachineService()
    storage = JobFileStorage()
    expires_at = monotonic() + _SWEEP_BUDGET_SECONDS
    expired_count = 0
    skipped_count = 0
    for candidate in candidates:
        if monotonic() >= expires_at:
            break
        try:
            expired = _settle_guarded_upload(state_machine, storage, candidate)
        except Exception as exc:
            logger.warning(
                "Guarded upload sweep will retry job {} after {}",
                candidate.job_id,
                type(exc).__name__,
            )
            expired = None
        expired_count += expired is True
        skipped_count += expired is None
    return expired_count, skipped_count


def _settle_guarded_upload(
    state_machine: SyncStateMachineService, storage: JobFileStorage, candidate: Job
) -> bool | None:
    """Return True if failed, False if recovered, or None if deferred or raced."""
    try:
        integrity = JobMetadataHelper.get_source_integrity(candidate.job_metadata)
        if integrity is None:
            return None
        if candidate.s3_key:
            verification = storage.verify_upload_integrity(candidate.s3_key, integrity)
            verdict = classify_guarded_source_upload(candidate.created_at, verification)
        else:
            verdict = "expired"
    except (StorageServiceException, UnavailableException, ValidationError) as exc:
        logger.warning(
            "Guarded upload sweep could not verify job {} ({})",
            candidate.job_id,
            type(exc).__name__,
        )
        verdict = "deferred"

    with get_sync_db_context() as db:
        job = db.scalar(
            select(Job)
            .where(
                Job.job_id == candidate.job_id,
                Job.status == JobStatus.WAITING_FILE.value,
                Job.version == candidate.version,
            )
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        if verdict in {"missing", "deferred"}:
            # Rotate temporary failures behind other overdue jobs. The deadline
            # is based on created_at, so this only changes sweep ordering.
            job.updated_at = utc_now_naive()
            return None

        is_failed = verdict != "matched"
        status = JobStatus.FAILED.value if is_failed else JobStatus.PENDING.value
        metadata = {"sweeper": True, "stale_status": JobStatus.WAITING_FILE.value}
        if is_failed:
            code, message = (
                (UPLOAD_INTEGRITY_MISMATCH_CODE, UPLOAD_INTEGRITY_MISMATCH_MESSAGE)
                if verdict == "mismatch"
                else (UPLOAD_EXPIRED_CODE, UPLOAD_EXPIRED_MESSAGE)
            )
            job.error_code = code
            job.error_message = message
            metadata.update(error_code=code, error_message=message)

        transition = state_machine.transition_outcome(
            db,
            candidate.job_id,
            status,
            "mark_failed" if is_failed else "guarded_upload_recovered",
            metadata=metadata,
            publish_cache=False,
        )
        if not transition.succeeded:
            db.rollback()
            return None
        if not is_failed:
            # Let the existing outbox recovery task publish after commit.
            UploadParseOutbox.stage(db, job_id=candidate.job_id, user_id=candidate.user_id)

    state_machine.publish_status_cache(candidate.job_id, status, metadata)
    return is_failed
