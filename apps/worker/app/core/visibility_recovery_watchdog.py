"""Run expired-reservation recovery outside the saturated Celery task pool."""

from __future__ import annotations

import signal
from threading import Event
from types import FrameType

from loguru import logger

from app.core.visibility_recovery import (
    VisibilityRecoveryResult,
    restore_expired_reservations,
)
from shared.core.config import app_config
from shared.core.logging import setup_logging
from shared.services.worker_health import (
    remove_visibility_recovery_heartbeat,
    write_visibility_recovery_heartbeat,
)

_stop_event: Event = Event()


def _request_stop(signal_number: int, frame: FrameType | None) -> None:
    """Request watchdog shutdown after the current bounded recovery attempt."""
    logger.info(f"Visibility recovery watchdog stopping on signal {signal_number}")
    _stop_event.set()


def run_visibility_recovery_watchdog() -> None:
    """Attempt recovery periodically and remain healthy across broker errors."""
    setup_logging(service_name="knowhere-worker")
    _stop_event.clear()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    period_seconds: float = float(app_config.VISIBILITY_RECOVERY_PERIOD_SECONDS)

    write_visibility_recovery_heartbeat()
    logger.info(
        "Visibility recovery watchdog started: "
        f"period={period_seconds:.0f}s"
    )

    try:
        while True:
            try:
                result: VisibilityRecoveryResult = restore_expired_reservations()
                if result["status"] == "attempted":
                    logger.bind(**result).debug(
                        "Expired Celery reservation recovery sweep attempted"
                    )
            except Exception:
                logger.exception("Expired Celery reservation recovery sweep failed")
            finally:
                write_visibility_recovery_heartbeat()

            if _stop_event.wait(timeout=period_seconds):
                break
    finally:
        remove_visibility_recovery_heartbeat()
        logger.info("Visibility recovery watchdog stopped")


if __name__ == "__main__":
    run_visibility_recovery_watchdog()
