"""
Periodic-task idempotency lock for Celery Beat scheduled tasks.

When multiple Celery Beat processes are alive simultaneously (startup burst,
rolling deploy, etc.) they can each fire the same periodic task within the
same scheduling window and enqueue duplicate invocations.

This module provides ``periodic_task_lock`` — a context manager that each
task body wraps itself with.  On entry it attempts a Redis ``SET NX EX``
keyed by the task name.  Only the first caller within the window acquires the
lock and proceeds; all subsequent callers see the key already set and return
immediately without executing the task body.

The TTL is set to ``period_seconds - buffer_seconds`` so the lock expires
before the next firing window, allowing the following scheduled run to execute
normally.

Usage::

    from shared.services.redis.periodic_task_lock import periodic_task_lock

    @celery_app.task(name="my.task")
    def my_periodic_task() -> dict:
        with periodic_task_lock("my.task", period_seconds=1800) as acquired:
            if not acquired:
                return {"status": "skipped", "reason": "duplicate firing"}

            # ... actual task body ...
            return {"status": "success"}
"""

from __future__ import annotations

import contextlib
from typing import Generator

from loguru import logger

# Key prefix for periodic task locks.
# SyncRedisService._build_key() already prepends "knowhere-api:" so the final
# Redis key will be "knowhere-api:periodic-lock:{safe_task_name}".
_KEY_PREFIX = "periodic-lock:"

# Subtract this from the period to give the next firing a grace window even
# if clocks drift slightly between the lock writer and the reader.
_DEFAULT_BUFFER_SECONDS: int = 30


def _build_key(task_name: str) -> str:
    """Build the Redis key for a periodic task idempotency lock."""
    # Normalise forward-slash and dot separators so the key is Redis-safe.
    safe_name = task_name.replace("/", ":").replace(" ", "_")
    return f"{_KEY_PREFIX}{safe_name}"


@contextlib.contextmanager
def periodic_task_lock(
    task_name: str,
    period_seconds: int,
    buffer_seconds: int = _DEFAULT_BUFFER_SECONDS,
) -> Generator[bool, None, None]:
    """Context manager that guards a periodic task body against duplicate execution.

    The lock key lives in Redis for ``period_seconds - buffer_seconds``.  If
    the key already exists this context manager yields ``False``; the caller
    should treat that as a skip signal and return early.  If the key is absent
    the lock is acquired and the context manager yields ``True``.

    The lock is **not** released on exit — it is intentionally left to expire
    so that the next legitimately-scheduled run can proceed.  This is correct
    because:
     - The lock TTL is always shorter than the task period.
     - We do not want a crash mid-task to allow an immediate re-run (that
       would be the same duplicate problem in a different guise).

    Args:
        task_name: Unique Celery task name (used as part of the Redis key).
        period_seconds: Nominal task period in seconds (from ``beat_schedule``).
        buffer_seconds: Subtracted from ``period_seconds`` to compute TTL.
            Prevents the lock from expiring right as the next firing arrives.

    Yields:
        bool: ``True`` if this execution holds the lock; ``False`` if skipped.
    """
    # Lazy import keeps the module loadable even in environments without gevent.
    from shared.services.redis.redis_sync_service import SyncRedisServiceFactory

    ttl: int = max(period_seconds - buffer_seconds, 10)
    lock_key: str = _build_key(task_name)

    redis_service = SyncRedisServiceFactory.get_service()

    try:
        acquired: bool = redis_service.set_nx(lock_key, "1", ttl)
    except Exception as exc:
        # If Redis is unavailable we proceed rather than silently skipping the
        # task — a missed periodic sweep is worse than a duplicate.
        logger.warning(
            f"periodic_task_lock: Redis error for task='{task_name}' "
            f"— proceeding without lock: {exc}"
        )
        yield True
        return

    if acquired:
        logger.debug(f"periodic_task_lock: acquired for task='{task_name}', ttl={ttl}s")
    else:
        logger.debug(
            f"periodic_task_lock: task='{task_name}' already running in this "
            "window (duplicate Beat firing) — skipping"
        )

    yield acquired
