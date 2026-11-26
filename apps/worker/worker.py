#!/usr/bin/env python3
# ruff: noqa: E402
# CRITICAL: Monkey patch MUST be the very first thing before ANY other imports.
# This patches stdlib (socket, ssl, threading, etc.) for cooperative greenlet scheduling.
import gevent.monkey

gevent.monkey.patch_all()

from app.core.runtime_limits import apply_native_thread_caps

apply_native_thread_caps()

# Cooperative psycopg2 under gevent, with a client-side bound on each statement.
# Must run before any database connection is opened.
from shared.core.config import settings
from shared.core.statement_deadline import StatementDeadline

StatementDeadline.install(
    warningSeconds=settings.DB_SYNC_STATEMENT_WARNING_SECONDS,
    timeoutSeconds=settings.DB_SYNC_STATEMENT_TIMEOUT_SECONDS,
)


def main() -> None:
    """Start the full worker bootstrap only in the main worker process."""
    from app.core.worker_bootstrap import run_worker

    run_worker()


if __name__ == "__main__":
    main()
