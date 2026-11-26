"""Client-side statement deadline for psycopg2 in gevent green mode.

The worker reaches PostgreSQL through psycopg2 with a process-wide wait callback,
so gevent can switch greenlets while a query waits on the socket. Neon ignores
``statement_timeout`` passed at connection startup, and psycopg2 has no
client-side command timeout like asyncpg's ``command_timeout`` in the API. Without
a bound, a runaway worker statement holds its connection and greenlet indefinitely.

``StatementDeadline.install`` replaces psycogreen's callback with the same poll
loop plus two thresholds per statement (each ``execute`` or ``commit``):

- after ``warningSeconds`` it logs the statement head and keeps waiting;
- after ``timeoutSeconds`` it sends a cancel request (``connection.cancel()``, as
  psycopg2's own ``extras.wait_select`` does), and the statement then raises
  ``QueryCanceled``; the caller's transaction aborts and rolls back as usual.

Connection setup runs through the same callback but is never cancelled: libpq
cannot cancel a connection attempt. Cancellation is best effort; the server may
finish the statement first.
"""

from contextvars import ContextVar
import time
from typing import Any, Final

from gevent.socket import wait_read, wait_write
from loguru import logger
import psycopg2
from psycopg2 import extensions
from sqlalchemy import event
from sqlalchemy.engine import Engine

_STATEMENT_HEAD_LENGTH: Final[int] = 200
# The statement the current greenlet is executing, for the warning log only.
_CURRENT_STATEMENT: ContextVar[str] = ContextVar("currentStatement", default="")


class StatementDeadline:
    _warningSeconds: float = 30.0
    _timeoutSeconds: float = 0.0

    @classmethod
    def install(cls, *, warningSeconds: float, timeoutSeconds: float) -> None:
        """Register the deadline wait callback; ``timeoutSeconds`` 0 only warns."""
        if warningSeconds <= 0 or timeoutSeconds < 0:
            raise ValueError("Statement deadline thresholds must be positive")
        cls._warningSeconds = warningSeconds
        cls._timeoutSeconds = timeoutSeconds
        extensions.set_wait_callback(cls.wait)
        if not event.contains(Engine, "before_cursor_execute", cls._recordStatement):
            event.listen(Engine, "before_cursor_execute", cls._recordStatement)
            event.listen(Engine, "after_cursor_execute", cls._clearStatement)

    @classmethod
    def uninstall(cls) -> None:
        """Remove the wait callback and statement listeners (blocking psycopg2 again)."""
        # Each ``cls.wait`` access is a new bound method, so compare by equality.
        if extensions.get_wait_callback() == cls.wait:
            extensions.set_wait_callback(None)
        if event.contains(Engine, "before_cursor_execute", cls._recordStatement):
            event.remove(Engine, "before_cursor_execute", cls._recordStatement)
            event.remove(Engine, "after_cursor_execute", cls._clearStatement)

    @classmethod
    def wait(cls, connection: Any) -> None:
        """Wait for one libpq operation without blocking the gevent hub."""
        isConnecting: bool = connection.status == extensions.STATUS_SETUP
        startedAt: float = time.monotonic()
        boundaries: list[tuple[float, bool]] = [] if isConnecting else cls._boundaries(startedAt)
        while True:
            state: int = connection.poll()
            if state == extensions.POLL_OK:
                return
            remaining: float | None = (max(0.0, boundaries[0][0] - time.monotonic())
                if boundaries else None)
            try:
                if state == extensions.POLL_READ:
                    wait_read(connection.fileno(), timeout=remaining)
                elif state == extensions.POLL_WRITE:
                    wait_write(connection.fileno(), timeout=remaining)
                else:
                    raise psycopg2.OperationalError(f"Bad result from poll: {state!r}")
            except TimeoutError:
                _, isCancel = boundaries.pop(0)
                cls._report(connection, time.monotonic() - startedAt, isCancel=isCancel)
                if isCancel:
                    connection.cancel()
                    boundaries.clear()

    @classmethod
    def _boundaries(cls, startedAt: float) -> list[tuple[float, bool]]:
        """Ordered (monotonic time, is cancel) thresholds for one statement."""
        boundaries: list[tuple[float, bool]] = []
        if cls._timeoutSeconds == 0 or cls._warningSeconds < cls._timeoutSeconds:
            boundaries.append((startedAt + cls._warningSeconds, False))
        if cls._timeoutSeconds > 0:
            boundaries.append((startedAt + cls._timeoutSeconds, True))
        return boundaries

    @classmethod
    def _report(cls, connection: Any, elapsedSeconds: float, *, isCancel: bool) -> None:
        backendPid: int | None = None
        try:
            backendPid = connection.info.backend_pid
        except Exception:
            pass
        logger.warning(
            "Worker database statement {}: elapsed_s={:.1f} warning_s={} timeout_s={} backend_pid={} statement={}",
            "cancelled at its deadline" if isCancel else "is slow",
            elapsedSeconds, cls._warningSeconds, cls._timeoutSeconds or "off", backendPid,
            _CURRENT_STATEMENT.get() or "(commit, rollback or a driver call)")

    @staticmethod
    def _recordStatement(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any,
                         executemany: bool) -> None:
        del conn, cursor, parameters, context, executemany
        _CURRENT_STATEMENT.set(" ".join(statement.split())[:_STATEMENT_HEAD_LENGTH])

    @staticmethod
    def _clearStatement(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any,
                        executemany: bool) -> None:
        del conn, cursor, statement, parameters, context, executemany
        _CURRENT_STATEMENT.set("")
