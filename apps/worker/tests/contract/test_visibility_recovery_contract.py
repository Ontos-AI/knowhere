from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext

import pytest
from pytest import MonkeyPatch


def test_should_restore_expired_reservations_with_a_fresh_kombu_connection(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core import visibility_recovery

    calls: list[tuple[str, int | None, int | None]] = []

    class FakeQualityOfService:
        def restore_visible(self, *, num: int, interval: int) -> None:
            calls.append(("restore_visible", num, interval))

    class FakeChannel:
        qos = FakeQualityOfService()

        def close(self) -> None:
            calls.append(("channel.close", None, None))

    class FakeConnection:
        def ensure_connection(self, *, max_retries: int) -> None:
            calls.append(("ensure_connection", max_retries, None))

        def channel(self) -> FakeChannel:
            calls.append(("channel", None, None))
            return FakeChannel()

        def release(self) -> None:
            calls.append(("connection.release", None, None))

    connection = FakeConnection()
    monkeypatch.setattr(
        visibility_recovery.celery_app,
        "connection_for_read",
        lambda: connection,
    )
    monkeypatch.setattr(
        visibility_recovery,
        "periodic_task_lock",
        lambda *args, **kwargs: _acquired_lock(),
    )

    result = visibility_recovery.restore_expired_reservations()

    assert result == {
        "status": "attempted",
        "batch_count": 10,
        "batch_size": 100,
        "recovery_limit": 1000,
    }
    assert calls[:2] == [
        ("ensure_connection", 1, None),
        ("channel", None, None),
    ]
    assert calls[2:12] == [("restore_visible", 100, 1)] * 10
    assert calls[12:] == [
        ("channel.close", None, None),
        ("connection.release", None, None),
    ]


def test_should_skip_recovery_when_another_invocation_holds_the_periodic_lock(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core import visibility_recovery

    def fail_if_connection_is_created() -> None:
        raise AssertionError("a skipped recovery must not open a broker connection")

    monkeypatch.setattr(
        visibility_recovery.celery_app,
        "connection_for_read",
        fail_if_connection_is_created,
    )
    monkeypatch.setattr(
        visibility_recovery,
        "periodic_task_lock",
        lambda *args, **kwargs: _skipped_lock(),
    )

    result = visibility_recovery.restore_expired_reservations()

    assert result == {"status": "skipped"}


def test_should_raise_recovery_connection_errors_for_celery_observability(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core import visibility_recovery

    connection_was_released: bool = False

    class FailingConnection:
        def ensure_connection(self, *, max_retries: int) -> None:
            raise RuntimeError("broker unavailable")

        def release(self) -> None:
            nonlocal connection_was_released
            connection_was_released = True

    monkeypatch.setattr(
        visibility_recovery.celery_app,
        "connection_for_read",
        lambda: FailingConnection(),
    )
    monkeypatch.setattr(
        visibility_recovery,
        "periodic_task_lock",
        lambda *args, **kwargs: _acquired_lock(),
    )

    with pytest.raises(RuntimeError, match="broker unavailable"):
        visibility_recovery.restore_expired_reservations()

    assert connection_was_released is True


def _acquired_lock() -> AbstractContextManager[bool]:
    return nullcontext(True)


def _skipped_lock() -> AbstractContextManager[bool]:
    return nullcontext(False)
