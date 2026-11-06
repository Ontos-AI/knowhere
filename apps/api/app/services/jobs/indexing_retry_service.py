"""One explicit retry stays in its original request through publication."""

import asyncio

from fastapi import Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from app.services.jobs.read_service import get_job_result_for_user
from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import ConflictException, TimeoutException, UnavailableException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode
from shared.models.database.job import Job
from shared.models.schemas.job import JobResultResponse
from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
from shared.services.retrieval.lexical.asyncworker import AsyncIndexingWorker
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.retry import IndexingRetry


def _attachContext(error: KnowhereException, job: JobResultResponse, *, hasPendingSettlement: bool = False) -> KnowhereException:
    error.details.update({"job_id": job.job_id, "job_result_id": job.job_result_id,
        "parsing_status": job.parsing_status, "indexing_status": job.indexing_status,
        "failure_stage": job.failure_stage, "job": job.model_dump(mode="json"),
        "failure_settlement_pending": hasPendingSettlement})
    return error


async def _rollback(db: AsyncSession, deadline: IndexingDeadline) -> None:
    try:
        remaining: float = deadline.startSettlement(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS).getRemainingSeconds()
        async with asyncio.timeout(remaining):
            await db.rollback()
    except Exception as error:
        logger.warning("Retry transaction cleanup deferred to request session: error_type={}", type(error).__name__)


async def retryIndexing(db: AsyncSession, *, jobId: str, userId: str,
                        response: Response) -> JobResultResponse:
    deadline: IndexingDeadline = IndexingDeadline(settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS
        if settings.KNOWHERE_LEXICAL_BACKEND == "opensearch" else settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS)
    retained: JobResultResponse | None = None
    worker: AsyncIndexingWorker | None = None
    try:
        async with asyncio.timeout(deadline.getRemainingSeconds()):
            retained = await get_job_result_for_user(db, job_id=jobId, user_id=userId, shouldSignResultURL=False)
            await db.commit()
            retained = await get_job_result_for_user(db, job_id=jobId, user_id=userId)
            await db.commit()
            if retained.status == "done":
                response.status_code = 200
                return retained
            bind: AsyncEngine | AsyncConnection | None = db.bind
            if bind is None:
                raise RuntimeError("Indexing retry requires its request's async database bind")
            database: AsyncLexicalDatabase = AsyncLexicalDatabase(bind)
            worker = AsyncIndexingWorker(database, deadline)
            await worker.settleExpired(jobId)
            db.expire_all()
            retained = await get_job_result_for_user(db, job_id=jobId, user_id=userId)

            def admit(session: Session) -> tuple[bool, str | None]:
                isCreated: bool = IndexingRetry().request(session, jobId=jobId, userId=userId, deadline=deadline)
                job: Job | None = session.get(Job, jobId)
                return isCreated, job.current_indexing_attempt_id if job is not None else None

            isCreated, attemptId = await db.run_sync(admit)
            await db.commit()
            if not isCreated:
                response.status_code = 200
                return retained
            if attemptId is None:
                raise RuntimeError("Indexing retry admission did not retain an attempt identity")
            # The committed admission is the latest known state if subsequent
            # DB settlement is unavailable. This snapshot never claims success.
            retained = retained.model_copy(update={"status": "pending", "indexing_status": "pending", "failure_stage": None, "error": None})
        from shared.services.retrieval.lexical.stage_observer import StageObserver
        with StageObserver.measure("indexing_operation_ms", intervalName="indexing_intervals_utc"):
            outcome: str = await worker.run(jobId, attemptId=attemptId)
        if outcome in ("pending", "skipped") and worker.errorCode is None:
            conflict: KnowhereException = ConflictException("Indexing attempt is already running or changed", reason="ABORTED")
            conflict.details["reason"] = "INDEXING_ALREADY_RUNNING" if outcome == "pending" else "INDEXING_SUPERSEDED"
            raise conflict
        # Native failure settlement has its separately bounded budget. Reading
        # the resulting snapshot cannot extend forward indexing work.
        observation: IndexingDeadline = deadline.startSettlement(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS)
        hasObservedPhase: bool = False
        try:
            async with asyncio.timeout(observation.getRemainingSeconds()):
                db.expire_all()
                retained = await get_job_result_for_user(db, job_id=jobId, user_id=userId, shouldSignResultURL=False)
                await db.commit()
                hasObservedPhase = True
                retained = await get_job_result_for_user(db, job_id=jobId, user_id=userId)
                await db.commit()
        except Exception as observationError:
            # URL renewal can fail independently of the committed phase. Keep
            # the primary execution's classification and authorized revision.
            if hasObservedPhase and retained.status == "done":
                raise
            worker.hasPendingSettlement = worker.hasPendingSettlement or not hasObservedPhase
            logger.warning("Indexing result observation incomplete: job_id={}, error_type={}", jobId, type(observationError).__name__)
        if retained.status == "done":
            response.status_code = 200
            return retained
        code: str = worker.errorCode or (retained.error.code if retained.error is not None else "UNAVAILABLE")
        if code == "DEADLINE_EXCEEDED":
            failure: KnowhereException = TimeoutException("Indexing execution exceeded its deadline", retry_after=1)
        elif code in ("INTERNAL_ERROR", LexicalIndexError.INPUT_TOO_LARGE):
            isSizeLimit: bool = code == LexicalIndexError.INPUT_TOO_LARGE
            failure = KnowhereException(code=ErrorCode.INTERNAL_ERROR,
                internal_message="Indexing input exceeds its supported size" if isSizeLimit else "Retained indexing input or mapping is invalid",
                user_message="Indexing input exceeds a supported size limit. The parsed result is still available." if isSizeLimit else None,
                details={"reason": code} if isSizeLimit else None)
        else:
            failure = UnavailableException("Indexing could not be completed", retry_after=1)
        raise _attachContext(failure, retained, hasPendingSettlement=worker.hasPendingSettlement)
    except asyncio.CancelledError:
        # Await cancellation cleanup in the same task; no detached continuation.
        raise
    except KnowhereException as error:
        await _rollback(db, deadline)
        if retained is not None:
            raise _attachContext(error, retained, hasPendingSettlement=worker.hasPendingSettlement if worker else False)
        raise
    except TimeoutError as error:
        await _rollback(db, deadline)
        timeout: KnowhereException = TimeoutException("Indexing retry request exceeded its deadline", retry_after=1, original_exception=error)
        if retained is not None:
            _attachContext(timeout, retained, hasPendingSettlement=worker.hasPendingSettlement if worker else True)
        raise timeout from error
    except Exception as error:
        await _rollback(db, deadline)
        unavailable: KnowhereException = UnavailableException("Indexing retry could not be observed", retry_after=1,
            original_exception=error)
        if retained is not None:
            _attachContext(unavailable, retained, hasPendingSettlement=True)
        raise unavailable from error
