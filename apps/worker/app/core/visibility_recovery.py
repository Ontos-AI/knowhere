"""Recover expired Redis broker reservations through a fresh Kombu channel."""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict, cast

from celery import Celery
from kombu import Connection

from shared.core.config import app_config
from shared.core.celery_app import get_celery_app
from shared.services.redis.periodic_task_lock import periodic_task_lock

_RECOVERY_LOCK_NAME: str = "visibility-recovery-watchdog"
_RECOVERY_LOCK_BUFFER_SECONDS: int = 5

class VisibilityRecoveryAttemptedResult(TypedDict):
    """Describe one bounded recovery sweep request."""

    status: Literal["attempted"]
    batch_count: int
    batch_size: int
    recovery_limit: int


class VisibilityRecoverySkippedResult(TypedDict):
    """Describe a sweep skipped because another replica holds the lock."""

    status: Literal["skipped"]


VisibilityRecoveryResult = (
    VisibilityRecoveryAttemptedResult | VisibilityRecoverySkippedResult
)


class VisibilityRecoveryQualityOfService(Protocol):
    """Expose the Kombu QoS operation required by recovery."""

    def restore_visible(self, *, num: int, interval: int) -> None: ...


class VisibilityRecoveryChannel(Protocol):
    """Expose the minimal channel interface required by recovery."""

    qos: VisibilityRecoveryQualityOfService

    def close(self) -> None: ...


celery_app: Celery = get_celery_app()


def restore_expired_reservations() -> VisibilityRecoveryResult:
    """Attempt a bounded restoration sweep through Kombu's Redis transport."""
    period_seconds: int = app_config.VISIBILITY_RECOVERY_PERIOD_SECONDS
    batch_size: int = app_config.VISIBILITY_RECOVERY_BATCH_SIZE
    batch_count: int = app_config.VISIBILITY_RECOVERY_BATCH_COUNT
    recovery_limit: int = batch_size * batch_count

    with periodic_task_lock(
        _RECOVERY_LOCK_NAME,
        period_seconds=period_seconds,
        buffer_seconds=_RECOVERY_LOCK_BUFFER_SECONDS,
    ) as acquired:
        if not acquired:
            return {"status": "skipped"}

        connection: Connection = celery_app.connection_for_read()
        try:
            connection.ensure_connection(max_retries=1)
            channel: VisibilityRecoveryChannel = cast(
                VisibilityRecoveryChannel,
                connection.channel(),
            )
            try:
                _batch_index: int
                for _batch_index in range(batch_count):
                    channel.qos.restore_visible(
                        num=batch_size,
                        interval=1,
                    )
            finally:
                channel.close()
        finally:
            connection.release()

    return {
        "status": "attempted",
        "batch_count": batch_count,
        "batch_size": batch_size,
        "recovery_limit": recovery_limit,
    }
