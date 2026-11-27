"""Periodic database work shares one slot so a scale-to-zero database can suspend."""

import contextlib
import os
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-lazy-maintenance-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")


class _InlinePool:
    """Runs submitted sends on the caller's thread so a test can assert them."""

    def trySubmit(self, operation: object) -> bool:
        assert callable(operation)
        operation()
        return True

    def close(self) -> None:
        return None


def test_periodic_database_work_runs_in_one_hourly_slot() -> None:
    from celery.schedules import crontab
    from shared.core.celery_app import PERIODIC_DATABASE_MINUTE, celery_app

    schedule: dict[str, dict[str, object]] = celery_app.conf.beat_schedule
    assert "turn-over-lexical-indexes" not in schedule
    assert "sweep-progressive-enrichments" not in schedule
    hourly: tuple[str, ...] = ("maintain-lexical-builds", "expire-stale-jobs", "recover-orphaned-webhooks")
    for name in hourly:
        entry: object = schedule[name]["schedule"]
        assert isinstance(entry, crontab)
        assert entry.minute == {PERIODIC_DATABASE_MINUTE}
        assert len(entry.hour) == 24
    upload: object = schedule["recover-upload-parse-dispatches"]["schedule"]
    assert isinstance(upload, crontab)
    assert upload.minute == {PERIODIC_DATABASE_MINUTE, PERIODIC_DATABASE_MINUTE + 30}
    assert all(isinstance(entry["schedule"], crontab) for entry in schedule.values())


def test_turnover_request_dispatches_once_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.celery_app import celery_app
    from shared.services.retrieval.lexical import maintenance_signals
    from shared.services.retrieval.lexical.maintenance_signals import TURNOVER_TASK, LexicalMaintenanceSignals

    held: set[str] = set()

    @contextlib.contextmanager
    def windowLock(name: str, period_seconds: int, buffer_seconds: int = 0) -> Iterator[bool]:
        acquired: bool = name not in held
        held.add(name)
        yield acquired

    sent: list[tuple[str, object, object]] = []
    monkeypatch.setattr(maintenance_signals, "periodic_task_lock", windowLock)
    monkeypatch.setattr(celery_app, "send_task",
        lambda name, **options: sent.append((name, options.get("countdown"), options.get("args"))))
    monkeypatch.setattr(LexicalMaintenanceSignals, "_dispatchPool", _InlinePool())
    assert LexicalMaintenanceSignals.requestTurnover()
    assert LexicalMaintenanceSignals.requestTurnover()
    # Delayed past the indexing deadline so it cannot close an index mid-write;
    # explicit empty args, because the task router reads len(args).
    assert sent == [(TURNOVER_TASK, 300, [])]


def test_turnover_request_never_raises_into_index_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.celery_app import celery_app
    from shared.services.retrieval.lexical.maintenance_signals import LexicalMaintenanceSignals

    def brokerDown(name: str, **options: object) -> None:
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(celery_app, "send_task", brokerDown)
    monkeypatch.setattr(LexicalMaintenanceSignals, "_dispatchPool", _InlinePool())
    assert LexicalMaintenanceSignals.requestTurnover()
    assert LexicalMaintenanceSignals.dispatch("task", "identity")


def test_saturated_dispatch_leaves_work_for_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.lexical.maintenance_signals import LexicalMaintenanceSignals

    monkeypatch.setattr(LexicalMaintenanceSignals, "_dispatchPool",
        SimpleNamespace(trySubmit=lambda operation: False, close=lambda: None))
    assert not LexicalMaintenanceSignals.dispatch("task", "identity")


def test_gevent_dispatch_sends_inline_without_a_native_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.celery_app import celery_app
    from shared.services.retrieval.lexical import maintenance_signals
    from shared.services.retrieval.lexical.maintenance_signals import LexicalMaintenanceSignals

    def refuseNativeThread(operation: object) -> bool:
        raise AssertionError("gevent workers must not send from a native thread")

    sent: list[tuple[str, object]] = []
    monkeypatch.setattr(maintenance_signals, "is_module_patched", lambda name: True)
    monkeypatch.setattr(LexicalMaintenanceSignals, "_dispatchPool",
        SimpleNamespace(trySubmit=refuseNativeThread, close=lambda: None))
    monkeypatch.setattr(celery_app, "send_task", lambda name, **options: sent.append((name, options.get("args"))))
    assert LexicalMaintenanceSignals.dispatch("task", "identity", hop=1)
    assert sent == [("task", ["identity", 1])]
