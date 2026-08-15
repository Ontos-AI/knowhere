from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch


def test_should_require_main_worker_and_visibility_recovery_heartbeats(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from shared.services import worker_health

    worker_heartbeat_path = tmp_path / "worker.json"
    recovery_heartbeat_path = tmp_path / "visibility-recovery.json"
    monkeypatch.setattr(worker_health, "HEARTBEAT_PATH", worker_heartbeat_path)
    monkeypatch.setattr(
        worker_health,
        "VISIBILITY_RECOVERY_HEARTBEAT_PATH",
        recovery_heartbeat_path,
    )
    worker_health.write_worker_heartbeat()
    worker_health.write_visibility_recovery_heartbeat()
    worker_health.assert_worker_healthy()

    recovery_heartbeat_path.unlink()

    with pytest.raises(SystemExit, match="Visibility recovery heartbeat file not found"):
        worker_health.assert_worker_healthy()


def test_should_remove_visibility_recovery_heartbeat(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from shared.services import worker_health

    recovery_heartbeat_path = tmp_path / "visibility-recovery.json"
    monkeypatch.setattr(
        worker_health,
        "VISIBILITY_RECOVERY_HEARTBEAT_PATH",
        recovery_heartbeat_path,
    )

    worker_health.write_visibility_recovery_heartbeat()
    worker_health.remove_visibility_recovery_heartbeat()

    assert not recovery_heartbeat_path.exists()


def test_should_reject_a_stale_visibility_recovery_heartbeat(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from shared.services import worker_health

    worker_heartbeat_path: Path = tmp_path / "worker.json"
    recovery_heartbeat_path: Path = tmp_path / "visibility-recovery.json"
    monkeypatch.setattr(worker_health, "HEARTBEAT_PATH", worker_heartbeat_path)
    monkeypatch.setattr(
        worker_health,
        "VISIBILITY_RECOVERY_HEARTBEAT_PATH",
        recovery_heartbeat_path,
    )
    monkeypatch.setattr(
        worker_health,
        "HEARTBEAT_STALE_AFTER_SECONDS",
        worker_health.VISIBILITY_RECOVERY_HEARTBEAT_STALE_AFTER_SECONDS + 2,
    )

    worker_health.write_worker_heartbeat()
    worker_health.write_visibility_recovery_heartbeat()
    recovery_mtime: float = recovery_heartbeat_path.stat().st_mtime
    stale_time: float = (
        recovery_mtime
        + worker_health.VISIBILITY_RECOVERY_HEARTBEAT_STALE_AFTER_SECONDS
        + 1
    )
    monkeypatch.setattr(worker_health.time, "time", lambda: stale_time)

    with pytest.raises(SystemExit, match="Visibility recovery heartbeat stale"):
        worker_health.assert_worker_healthy()
