"""Settle abandoned phase state without authorizing another execution."""

from datetime import datetime, timedelta
from typing import Literal

from loguru import logger
from sqlalchemy import and_, false, func, literal, or_, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicallease import LexicalLease
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox


class IndexingSettlement:
    @staticmethod
    def sweep(sessions: sessionmaker[Session], *, maximumAttempts: int = 100) -> dict[str, int]:
        """The existing stale-job tick settles bounded intent; it never dispatches.

        The obsolete dispatch timestamp now provides a failure cooldown for
        this scan. A corrupt or temporarily locked row cannot starve later work.
        """
        with sessions.begin() as db:
            db.execute(select(func.set_config("statement_timeout", "2000ms", True)))
            now: datetime = db.scalars(select(func.clock_timestamp())).one()
            control: LexicalBackendControl | None = db.get(LexicalBackendControl, 1)
            cutoff: datetime = now - timedelta(seconds=settings.KNOWHERE_INDEXING_UNSTARTED_TIMEOUT_SECONDS)
            candidates: list[str] = list(db.scalars(select(Job.job_id).join(IndexingAttempt,
                Job.current_indexing_attempt_id == IndexingAttempt.id).where(
                Job.parsing_status == "completed", Job.status.notin_(("done", "failed")),
                or_(IndexingAttempt.nextDispatchAt.is_(None), IndexingAttempt.nextDispatchAt <= now),
                or_(IndexingAttempt.state.notin_(("pending", "running")),
                    and_(IndexingAttempt.executionCount == 0, or_(IndexingAttempt.admissionDeadlineAt <= now,
                        and_(IndexingAttempt.admissionDeadlineAt.is_(None), IndexingAttempt.createdAt <= cutoff))),
                    and_(IndexingAttempt.executionCount > 0, or_(IndexingAttempt.executionDeadlineAt.is_(None),
                        IndexingAttempt.executionDeadlineAt <= now)),
                    IndexingAttempt.backendEpoch != control.epoch if control is not None else false(),
                    literal(control.mode != "serving") if control is not None else false()),
            ).order_by(func.coalesce(IndexingAttempt.executionDeadlineAt,
                IndexingAttempt.admissionDeadlineAt, IndexingAttempt.createdAt), IndexingAttempt.id)
                .limit(maximumAttempts)))
        counts: dict[str, int] = {"expired": 0, "deferred": 0}
        for jobId in candidates:
            try:
                with sessions.begin() as db:
                    db.execute(select(func.set_config("lock_timeout", "500ms", True),
                        func.set_config("statement_timeout", "2000ms", True)))
                    # Commit the scan cooldown with the terminal transition.
                    attempt: IndexingAttempt | None = db.scalar(select(IndexingAttempt).join(Job,
                        Job.current_indexing_attempt_id == IndexingAttempt.id).where(Job.job_id == jobId))
                    if attempt is not None:
                        attempt.nextDispatchAt = now + timedelta(minutes=5)
                    outcome, eventId = IndexingSettlement.settleExpired(db, jobId)
                SyncJobWebhookOutbox().enqueue_event_id_after_commit(eventId)
                counts["expired" if outcome == "failed" else "deferred"] += 1
            except Exception as error:
                counts["deferred"] += 1
                logger.error("Indexing stale settlement deferred: job_id={}, error_type={}", jobId, type(error).__name__)
                # A separate short transaction retains fairness after rollback.
                try:
                    with sessions.begin() as db:
                        db.execute(select(func.set_config("lock_timeout", "100ms", True),
                            func.set_config("statement_timeout", "500ms", True)))
                        attempt = db.scalar(select(IndexingAttempt).join(Job,
                            Job.current_indexing_attempt_id == IndexingAttempt.id).where(Job.job_id == jobId).with_for_update(of=IndexingAttempt))
                        if attempt is not None:
                            attempt.nextDispatchAt = now + timedelta(minutes=5)
                except Exception:
                    logger.warning("Indexing scan cooldown could not be recorded: job_id={}", jobId)
        return counts

    @staticmethod
    def settleExpired(db: Session, jobId: str, *, attemptId: str | None = None) -> tuple[Literal["completed", "failed", "skipped"] | None, str | None]:
        job: Job | None = db.scalar(select(Job).where(Job.job_id == jobId).with_for_update()
            .execution_options(populate_existing=True))
        if job is None:
            return "skipped", None
        if job.status in ("done", "failed"):
            return ("completed" if job.status == "done" else "failed"), None
        if attemptId is not None and job.current_indexing_attempt_id != attemptId:
            return "skipped", None
        attempt: IndexingAttempt | None = db.scalar(select(IndexingAttempt).where(
            IndexingAttempt.id == job.current_indexing_attempt_id).with_for_update())
        if attempt is None:
            return None, None
        now: datetime = db.scalars(select(func.clock_timestamp())).one()
        control: LexicalBackendControl | None = db.get(LexicalBackendControl, 1)
        isInterrupted: bool = control is not None and (control.mode != "serving" or control.epoch != attempt.backendEpoch)
        deadline: datetime | None = attempt.executionDeadlineAt if attempt.executionCount else (
            attempt.admissionDeadlineAt or attempt.createdAt + timedelta(seconds=settings.KNOWHERE_INDEXING_UNSTARTED_TIMEOUT_SECONDS))
        if attempt.state in ("pending", "running") and not isInterrupted and deadline is not None and deadline > now:
            return None, None
        reason: str = "Indexing interrupted by backend maintenance" if isInterrupted else "Indexing execution or unstarted intent expired"
        eventId: str | None = IndexingSettlement.fail(db, job, attempt,
            message=reason, code="UNAVAILABLE" if isInterrupted else "DEADLINE_EXCEEDED")
        return "failed", eventId

    @staticmethod
    def fail(db: Session, job: Job, attempt: IndexingAttempt, *, message: str, code: str) -> str | None:
        if job.status == "done" or job.current_indexing_attempt_id != attempt.id:
            return None
        now: datetime = db.scalars(select(func.clock_timestamp())).one()
        if attempt.currentBuildId:
            build: LexicalBuild | None = db.get(LexicalBuild, attempt.currentBuildId)
            if build is not None and build.state == "writing":
                build.state = "failed"
                build.cleanupState = "pending"
            for lease in db.scalars(select(LexicalLease).where(
                LexicalLease.buildId == attempt.currentBuildId, LexicalLease.state == "active")):
                lease.state = "uncertain"
        attempt.state = "failed"
        attempt.errorMessage = message
        attempt.finishedAt = now
        attempt.ownerToken = None
        attempt.claimExpiresAt = None
        job.indexing_status = "failed"
        job.failure_stage = "indexing"
        if job.status != "failed":
            publicMessage: str = ("Indexing input exceeds a supported size limit. The parsed result is still available."
                if code == LexicalIndexError.INPUT_TOO_LARGE else "Indexing could not be completed. Retry indexing for this job.")
            outcome = SyncStateMachineService().mark_failed_outcome(db, job.job_id,
                error_message=publicMessage, error_code=code)
            if not outcome.succeeded:
                raise RuntimeError("Indexing failure transition was rejected")
        event = SyncJobWebhookOutbox().create_event(db, job_id=job.job_id, event_type="job.failed")
        return event.event_id if event is not None else None
