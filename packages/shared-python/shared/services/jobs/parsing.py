"""Durable parsing disposition, independent of cached source information."""

from typing import Literal
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.database_sync import get_sync_session_factory
from shared.core.exceptions.domain_exceptions import NotFoundException
from shared.models.database.job import Job
from shared.services.jobs.parsing_error import ParsingOwnerError
from shared.services.retrieval.demo_job_scope import validate_job_corpus


class ParsingOwnership:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()

    def getDisposition(self, jobId: str) -> Literal["parse", "parsed", "terminal"]:
        with self.sessions.begin() as db:
            job: Job | None = db.scalar(select(Job).where(Job.job_id == jobId))
            if job is None:
                raise NotFoundException(resource="Job", resource_id=jobId)
            if job.status in ("done", "failed"):
                return "terminal"
            return "parsed" if job.parsing_status == "completed" else "parse"

    def claim(self, jobId: str, ownerToken: str) -> bool:
        with self.sessions.begin() as db:
            job: Job = self._lockJob(db, jobId)
            now: datetime = db.scalars(select(func.clock_timestamp())).one()
            if job.status in ("done", "failed") or job.parsing_status == "completed":
                return False
            if (job.parsing_owner_token not in (None, ownerToken)
                and job.parsing_owner_expires_at is not None and job.parsing_owner_expires_at > now):
                return False
            job.parsing_owner_token = ownerToken
            job.parsing_owner_expires_at = now + timedelta(seconds=60)
            job.parsing_status = "running"
            return True

    def assertOwned(self, jobId: str, ownerToken: str) -> None:
        with self.sessions.begin() as db:
            self.assertInTransaction(db, jobId, ownerToken)

    def assertInTransaction(self, db: Session, jobId: str, ownerToken: str) -> Job:
        job: Job = self._lockJob(db, jobId)
        self.assertJobOwned(job, ownerToken, db.scalars(select(func.clock_timestamp())).one())
        return job

    @staticmethod
    def assertJobOwned(job: Job, ownerToken: str, now: datetime) -> None:
        if (job.parsing_status == "completed" or job.status in ("done", "failed")
            or job.parsing_owner_token != ownerToken or job.parsing_owner_expires_at is None
            or job.parsing_owner_expires_at <= now):
            raise ParsingOwnerError("Parsing execution lost its ownership")

    def renew(self, jobId: str, ownerToken: str) -> bool:
        try:
            with self.sessions.begin() as db:
                job: Job = self._lockJob(db, jobId)
                if job.parsing_status == "completed":
                    return True
                now: datetime = db.scalars(select(func.clock_timestamp())).one()
                self.assertJobOwned(job, ownerToken, now)
                job.parsing_owner_expires_at = now + timedelta(seconds=60)
            return True
        except ParsingOwnerError:
            return False

    def release(self, jobId: str, ownerToken: str) -> None:
        with self.sessions.begin() as db:
            job: Job | None = db.scalar(select(Job).where(Job.job_id == jobId).with_for_update())
            if job is not None and job.parsing_owner_token == ownerToken:
                job.parsing_owner_expires_at = None
                # Retain the last identity for the original Celery failure
                # callback. A subsequent claim overwrites it atomically.

    @staticmethod
    def _lockJob(db: Session, jobId: str) -> Job:
        source: Job | None = db.scalar(select(Job).where(Job.job_id == jobId))
        if source is None:
            raise NotFoundException(resource="Job", resource_id=jobId)
        storage = validate_job_corpus(db, job=source, lock_document=False)
        metadata: dict[str, object] = dict(source.job_metadata or {})
        document = db.scalar(select(storage.Document).where(
            storage.Document.document_id == str(metadata.get("document_id") or ""),
            storage.Document.user_id == storage.resolve_owner(str(source.user_id))).with_for_update())
        if document is not None and (document.status == "archived" or document.accepted_job_id not in (None, jobId)):
            raise ParsingOwnerError("Parsing execution lost its document ownership")
        return db.scalars(select(Job).where(Job.job_id == jobId).with_for_update()
            .execution_options(populate_existing=True)).one()
