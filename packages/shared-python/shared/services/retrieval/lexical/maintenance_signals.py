"""Dispatch lexical maintenance when its work is committed, instead of polling for it.

Four kinds of committed rows create maintenance work:

- a ``LexicalBuild`` whose ``cleanup_state`` becomes ``pending``;
- an archived ``Document`` whose ``index_cleanup_after`` is set;
- a ``DocumentEnrichmentPublication`` whose ``cleanup_after`` is set;
- a ``LexicalNamespaceIndex`` whose ``state`` becomes ``uncertain`` (a lifecycle
  request the node did not answer). Its resolution waits a little first, since
  such a request usually still finishes on the node.

``install()`` registers process-wide SQLAlchemy session listeners. ``after_flush``
records the identities a flush created, ``after_commit`` sends one Celery task per
identity, and a rollback discards them. A recognized row is recorded only when the
value first becomes due, so the maintenance tasks' own retry bookkeeping never
re-dispatches it.

Sending is best effort. A lost or rejected task leaves the row pending, and the
hourly ``maintainLexical`` sweep collects it. Nothing here raises into the
committing caller.

Install once in every process that commits these rows: the Celery worker
(``worker_init``) and the API (``lifespan``).
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from gevent.monkey import is_module_patched
from loguru import logger
from sqlalchemy import event
from sqlalchemy.orm import Session, attributes
from sqlalchemy.orm.unitofwork import UOWTransaction

from shared.models.database.document import Document
from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.background import BackgroundWorkPool
from shared.services.redis.periodic_task_lock import periodic_task_lock

_TASK_PREFIX: Final[str] = "shared.services.retrieval.lexical.maintenance."
CLEAN_BUILD_TASK: Final[str] = _TASK_PREFIX + "cleanBuild"
CLEAN_ARCHIVED_DOCUMENT_TASK: Final[str] = _TASK_PREFIX + "cleanArchivedDocument"
CLEAN_FAILED_ATTEMPT_TASK: Final[str] = _TASK_PREFIX + "cleanFailedAttempt"
RESOLVE_INDEX_TASK: Final[str] = _TASK_PREFIX + "resolveIndex"
TURNOVER_TASK: Final[str] = "shared.services.retrieval.lexical.turnover.turnOverLexicalIndexes"

# The first attempt runs shortly after commit; each task re-checks when its row is due.
_FIRST_ATTEMPT_DELAY_SECONDS: Final[float] = 5
# An unanswered close or open usually completes on the node within its own timeout,
# so resolution first gives it that long and then mostly just observes the result.
RESOLUTION_DELAY_SECONDS: Final[float] = 30
# Index opens can be frequent; one turnover check per window is enough below the hard limit.
_TURNOVER_REQUEST_WINDOW_SECONDS: Final[int] = 300
# Turnover may close the least-recently-used index. Running it right after an open can
# close the index that is still receiving its first bulk write, so it waits past the
# indexing deadline instead.
_TURNOVER_DELAY_SECONDS: Final[float] = 300
_PENDING_WORK_KEY: Final[str] = "lexical_maintenance_pending_work"
# Sends are short; a full pool drops work to the sweep instead of queueing in memory.
_DISPATCH_WORKERS: Final[int] = 4
# Task names whose commit-time dispatch the current context has taken over itself.
_SUPPRESSED_TASKS: ContextVar[frozenset[str]] = ContextVar("lexicalSuppressedTasks", default=frozenset())


@dataclass(frozen=True)
class _Work:
    taskName: str
    identity: str
    countdown: float = _FIRST_ATTEMPT_DELAY_SECONDS


def _becomes(instance: object, attributeName: str, isDue: Callable[[object], bool]) -> bool:
    history: attributes.History = attributes.get_history(instance, attributeName)
    return any(isDue(value) for value in history.added) and not any(isDue(value) for value in history.deleted)


def _isPending(value: object) -> bool:
    return value == "pending"


def _isSet(value: object) -> bool:
    return value is not None


def _isUncertain(value: object) -> bool:
    return value == "uncertain"


def _describe(instance: object) -> _Work | None:
    if isinstance(instance, LexicalBuild) and _becomes(instance, "cleanupState", _isPending):
        return _Work(CLEAN_BUILD_TASK, instance.id)
    if (isinstance(instance, Document) and instance.status == "archived"
            and _becomes(instance, "index_cleanup_after", _isSet)):
        return _Work(CLEAN_ARCHIVED_DOCUMENT_TASK, instance.document_id)
    if isinstance(instance, DocumentEnrichmentPublication) and _becomes(instance, "cleanup_after", _isSet):
        return _Work(CLEAN_FAILED_ATTEMPT_TASK, instance.id)
    if isinstance(instance, LexicalNamespaceIndex) and _becomes(instance, "state", _isUncertain):
        return _Work(RESOLVE_INDEX_TASK, instance.id, RESOLUTION_DELAY_SECONDS)
    return None


def _recordFlushedWork(session: Session, flushContext: UOWTransaction) -> None:
    del flushContext
    try:
        suppressed: frozenset[str] = _SUPPRESSED_TASKS.get()
        pending: set[_Work] = session.info.setdefault(_PENDING_WORK_KEY, set())
        for instance in (*session.new, *session.dirty):
            work: _Work | None = _describe(instance)
            if work is not None and work.taskName not in suppressed:
                pending.add(work)
    except Exception as error:
        logger.warning("Lexical maintenance work was not recorded; the hourly sweep owns it: error_type={}",
            type(error).__name__)


def _dispatchCommittedWork(session: Session) -> None:
    pending: set[_Work] | None = session.info.pop(_PENDING_WORK_KEY, None)
    for work in sorted(pending or (), key=lambda item: (item.taskName, item.identity)):
        LexicalMaintenanceSignals.dispatch(work.taskName, work.identity, countdown=work.countdown)


def _discardRolledBackWork(session: Session) -> None:
    session.info.pop(_PENDING_WORK_KEY, None)


_LISTENERS: Final[tuple[tuple[str, Callable[..., None]], ...]] = (
    ("after_flush", _recordFlushedWork),
    ("after_commit", _dispatchCommittedWork),
    ("after_rollback", _discardRolledBackWork),
)


class LexicalMaintenanceSignals:
    _isInstalled: bool = False
    _dispatchPool: BackgroundWorkPool | None = None

    @classmethod
    def install(cls) -> None:
        if cls._isInstalled:
            return
        for name, listener in _LISTENERS:
            event.listen(Session, name, listener)
        cls._isInstalled = True

    @classmethod
    def uninstall(cls) -> None:
        if cls._isInstalled:
            for name, listener in _LISTENERS:
                event.remove(Session, name, listener)
            cls._isInstalled = False
        if cls._dispatchPool is not None:
            cls._dispatchPool.close()
            cls._dispatchPool = None

    @classmethod
    def dispatch(cls, taskName: str, identity: str, *, countdown: float = _FIRST_ATTEMPT_DELAY_SECONDS,
                 hop: int = 0) -> bool:
        """Queue one maintenance task send; ``hop`` counts reschedules so a task cannot loop forever.

        Outside gevent the send runs on a background thread, because publishing to an
        unreachable broker blocks for many seconds. False means the work waits for the sweep.
        """
        arguments: list[object] = [identity] if hop == 0 else [identity, hop]

        def send() -> None:
            try:
                from shared.core.celery_app import get_celery_app
                get_celery_app().send_task(taskName, args=arguments, countdown=countdown, retry=False)
            except Exception as error:
                logger.warning("Lexical maintenance dispatch deferred to the hourly sweep: task={} identity={} error_type={} error={}",
                    taskName, identity, type(error).__name__, str(error)[:200])

        return cls._submit(send, description=taskName)

    @staticmethod
    @contextmanager
    def suppress(taskName: str) -> Iterator[None]:
        """Skip commit-time dispatch of ``taskName`` for work committed in this context.

        For a task that schedules its own next attempt: without this, its own failed
        attempt would commit the same trigger and start a second, unbounded chain.
        """
        token = _SUPPRESSED_TASKS.set(_SUPPRESSED_TASKS.get() | {taskName})
        try:
            yield
        finally:
            _SUPPRESSED_TASKS.reset(token)

    @classmethod
    def requestTurnover(cls) -> bool:
        """Ask for one delayed idle-index turnover after an index opens, without blocking admission."""
        return cls._submit(cls._sendTurnoverRequest, description=TURNOVER_TASK)

    @classmethod
    def _submit(cls, operation: Callable[[], None], *, description: str) -> bool:
        if is_module_patched("threading"):
            # Under gevent, Celery's producer pool and the shared Redis client belong to
            # the hub that created them; a native background thread cannot use them
            # ("Cannot switch to a different thread"). Sending inline only blocks the
            # current greenlet.
            operation()
            return True
        try:
            if cls._dispatchPool is None:
                cls._dispatchPool = BackgroundWorkPool(name="lexical-maintenance-dispatch",
                    maximumWorkers=_DISPATCH_WORKERS)
            if cls._dispatchPool.trySubmit(operation):
                return True
            logger.warning("Lexical maintenance dispatch pool is saturated; the hourly sweep owns it: task={}", description)
        except Exception as error:
            logger.warning("Lexical maintenance dispatch deferred to the hourly sweep: task={} error_type={} error={}",
                description, type(error).__name__, str(error)[:200])
        return False

    @staticmethod
    def _sendTurnoverRequest() -> None:
        try:
            with periodic_task_lock(TURNOVER_TASK + ":request", period_seconds=_TURNOVER_REQUEST_WINDOW_SECONDS,
                    buffer_seconds=0) as acquired:
                if not acquired:
                    return
                from shared.core.celery_app import get_celery_app
                get_celery_app().send_task(TURNOVER_TASK, countdown=_TURNOVER_DELAY_SECONDS, retry=False)
        except Exception as error:
            logger.warning("Lexical turnover request deferred to the hourly sweep: error_type={} error={}",
                type(error).__name__, str(error)[:200])
