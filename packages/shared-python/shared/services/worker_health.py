"""
Local-only worker heartbeat used by container health probes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import gevent
from gevent.event import Event
from gevent.lock import Semaphore
from loguru import logger

from shared.core.config import app_config

HEARTBEAT_PATH = Path(
    os.getenv("WORKER_HEARTBEAT_FILE", "/tmp/knowhere-worker-heartbeat.json")
)
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"))
HEARTBEAT_STALE_AFTER_SECONDS = float(
    os.getenv("WORKER_HEARTBEAT_STALE_AFTER_SECONDS", "45")
)
VISIBILITY_RECOVERY_HEARTBEAT_PATH = Path(
    os.getenv(
        "VISIBILITY_RECOVERY_HEARTBEAT_FILE",
        "/tmp/knowhere-visibility-recovery-heartbeat.json",
    )
)
VISIBILITY_RECOVERY_HEARTBEAT_STALE_AFTER_SECONDS: float = float(
    max(app_config.VISIBILITY_RECOVERY_PERIOD_SECONDS * 3, 90)
)

_heartbeat_greenlet: Optional[gevent.Greenlet] = None
_heartbeat_stop_event = Event()
_heartbeat_lock = Semaphore()


def _write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(str(os.getpid()), encoding="utf-8")
    os.replace(temp_path, path)


def write_worker_heartbeat() -> None:
    _write_heartbeat(HEARTBEAT_PATH)


def write_visibility_recovery_heartbeat() -> None:
    _write_heartbeat(VISIBILITY_RECOVERY_HEARTBEAT_PATH)


def remove_visibility_recovery_heartbeat() -> None:
    VISIBILITY_RECOVERY_HEARTBEAT_PATH.unlink(missing_ok=True)


def _heartbeat_loop() -> None:
    while not _heartbeat_stop_event.is_set():
        try:
            write_worker_heartbeat()
        except Exception as exc:
            logger.warning(f"Failed to write worker heartbeat: {exc}")
        gevent.sleep(HEARTBEAT_INTERVAL_SECONDS)


def start_worker_heartbeat() -> None:
    global _heartbeat_greenlet

    with _heartbeat_lock:
        if _heartbeat_greenlet is not None and not _heartbeat_greenlet.dead:
            return
        _heartbeat_stop_event.clear()
        write_worker_heartbeat()
        _heartbeat_greenlet = gevent.spawn(_heartbeat_loop)
        logger.info(
            "Worker heartbeat started: "
            f"path={HEARTBEAT_PATH}, interval={HEARTBEAT_INTERVAL_SECONDS}s"
        )


def stop_worker_heartbeat() -> None:
    global _heartbeat_greenlet

    with _heartbeat_lock:
        _heartbeat_stop_event.set()
        heartbeat_greenlet = _heartbeat_greenlet
        _heartbeat_greenlet = None

    if heartbeat_greenlet is not None and not heartbeat_greenlet.dead:
        heartbeat_greenlet.kill(block=True, timeout=1)

    try:
        HEARTBEAT_PATH.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning(f"Failed to remove worker heartbeat file: {exc}")


def _assert_heartbeat_is_fresh(
    *,
    name: str,
    path: Path,
    stale_after_seconds: float,
) -> None:
    if not path.exists():
        raise SystemExit(f"{name} heartbeat file not found: {path}")

    age_seconds = time.time() - path.stat().st_mtime
    if age_seconds > stale_after_seconds:
        raise SystemExit(
            f"{name} heartbeat stale: "
            f"path={path}, age={age_seconds:.1f}s, "
            f"threshold={stale_after_seconds:.1f}s"
        )


def assert_worker_healthy() -> None:
    _assert_heartbeat_is_fresh(
        name="Worker",
        path=HEARTBEAT_PATH,
        stale_after_seconds=HEARTBEAT_STALE_AFTER_SECONDS,
    )
    _assert_heartbeat_is_fresh(
        name="Visibility recovery",
        path=VISIBILITY_RECOVERY_HEARTBEAT_PATH,
        stale_after_seconds=VISIBILITY_RECOVERY_HEARTBEAT_STALE_AFTER_SECONDS,
    )
