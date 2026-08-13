"""Recover expired Redis broker reservations independently of task execution."""

from __future__ import annotations

from loguru import logger

from shared.core.celery_app import get_celery_app
from shared.services.redis.periodic_task_lock import periodic_task_lock

_RECOVERY_PERIOD_SECONDS: int = 30

celery_app = get_celery_app()


@celery_app.task(name="app.core.tasks.visibility_recovery.restore_expired_reservations")
def restore_expired_reservations() -> dict[str, str]:
    """Restore expired Redis reservations through Kombu's transport API."""
    with periodic_task_lock(
        "app.core.tasks.visibility_recovery.restore_expired_reservations",
        period_seconds=_RECOVERY_PERIOD_SECONDS,
        buffer_seconds=5,
    ) as acquired:
        if not acquired:
            return {"status": "skipped"}

        connection = celery_app.connection_for_read()
        try:
            connection.ensure_connection(max_retries=1)
            channel = connection.channel()
            try:
                channel.qos.restore_visible(interval=1)
            finally:
                channel.close()
        except Exception as exc:
            logger.error(f"Expired Celery reservation recovery failed: {exc}")
            return {"status": "error"}
        finally:
            connection.release()

    return {"status": "success"}
