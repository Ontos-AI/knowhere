from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.database.upload_parse_dispatch import UploadParseDispatch
from shared.utils.utc_now import utc_now_naive

_CLAIM_LEASE = timedelta(minutes=1)
_RETRY_DELAY = timedelta(seconds=30)


@dataclass(frozen=True)
class UploadParseClaim:
    job_id: str
    user_id: str
    attempt: int


class UploadParseOutbox:
    """Track publication attempts. Callers commit each write before publishing."""

    @staticmethod
    def stage(db: Session, *, job_id: str, user_id: str) -> UploadParseClaim:
        """Reserve the initial attempt in the upload confirmation transaction."""
        job_version = db.execute(
            select(Job.version).where(Job.job_id == job_id)
        ).scalar_one()
        db.add(
            UploadParseDispatch(
                job_id=job_id,
                job_version=job_version,
                attempt=1,
                available_at=utc_now_naive() + _CLAIM_LEASE,
            )
        )
        return UploadParseClaim(job_id=job_id, user_id=user_id, attempt=1)

    @staticmethod
    def claim_due(db: Session) -> UploadParseClaim | None:
        now = utc_now_naive()
        statement = (
            select(UploadParseDispatch, Job.user_id)
            .join(Job, Job.job_id == UploadParseDispatch.job_id)
            .where(
                UploadParseDispatch.published_at.is_(None),
                UploadParseDispatch.available_at <= now,
                Job.status == JobStatus.PENDING.value,
                # A later indexing retry can return this job to pending.
                Job.version == UploadParseDispatch.job_version,
            )
            .order_by(UploadParseDispatch.available_at, UploadParseDispatch.job_id)
            .limit(1)
            .with_for_update(of=UploadParseDispatch, skip_locked=True)
        )
        waiting_max_age = settings.JOB_WAITING_EXPIRE_SECONDS
        if waiting_max_age > 0 and settings.JOB_PROCESSING_EXPIRE_SECONDS > 0:
            statement = statement.where(
                Job.updated_at >= now - timedelta(seconds=waiting_max_age)
            )

        row = db.execute(statement).one_or_none()
        if row is None:
            return None
        dispatch, user_id = row
        dispatch.attempt += 1
        dispatch.available_at = now + _CLAIM_LEASE
        return UploadParseClaim(
            job_id=dispatch.job_id, user_id=user_id, attempt=dispatch.attempt
        )

    @staticmethod
    def finish(db: Session, claim: UploadParseClaim, *, published: bool) -> None:
        now = utc_now_naive()
        statement = update(UploadParseDispatch).where(
            UploadParseDispatch.job_id == claim.job_id,
            UploadParseDispatch.attempt == claim.attempt,
            UploadParseDispatch.published_at.is_(None),
        )
        if published:
            statement = statement.values(published_at=now)
        else:
            statement = statement.values(available_at=now + _RETRY_DELAY)
        db.execute(statement)
