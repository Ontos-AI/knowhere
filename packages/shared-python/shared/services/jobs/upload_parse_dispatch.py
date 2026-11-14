from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from shared.core.config import settings
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.database.upload_parse_dispatch import UploadParseDispatch
from shared.utils.utc_now import utc_now_naive

_CLAIM_LEASE = timedelta(minutes=1)
_RETRY_DELAY = timedelta(seconds=30)
_MIN_GUARDED_DISPATCH_WINDOW_SECONDS = 120

has_source_integrity = Job.job_metadata["source_integrity"].as_string().is_not(None)


def within_upload_parse_dispatch_window(
    *, now: datetime, waiting_max_age: int
) -> ColumnElement[bool]:
    """Keep confirmed guarded uploads eligible through the first broker retry."""
    legacy_cutoff = now - timedelta(seconds=waiting_max_age)
    if waiting_max_age >= _MIN_GUARDED_DISPATCH_WINDOW_SECONDS:
        return Job.updated_at >= legacy_cutoff
    guarded_cutoff = now - timedelta(seconds=_MIN_GUARDED_DISPATCH_WINDOW_SECONDS)
    current_dispatch_exists = (
        select(UploadParseDispatch.job_id)
        .where(
            UploadParseDispatch.job_id == Job.job_id,
            UploadParseDispatch.job_version == Job.version,
        )
        .correlate(Job)
        .exists()
    )
    return or_(
        Job.updated_at >= legacy_cutoff,
        and_(
            Job.status == JobStatus.PENDING.value,
            has_source_integrity,
            current_dispatch_exists,
            Job.updated_at >= guarded_cutoff,
        ),
    )


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
                within_upload_parse_dispatch_window(now=now, waiting_max_age=waiting_max_age)
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
