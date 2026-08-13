from __future__ import annotations

from pytest import MonkeyPatch


def test_should_restore_expired_reservations_with_a_fresh_kombu_connection(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core.tasks import visibility_recovery

    calls: list[tuple[str, int | None]] = []

    class FakeQualityOfService:
        def restore_visible(self, *, interval: int) -> None:
            calls.append(("restore_visible", interval))

    class FakeChannel:
        qos = FakeQualityOfService()

        def close(self) -> None:
            calls.append(("channel.close", None))

    class FakeConnection:
        def ensure_connection(self, *, max_retries: int) -> None:
            calls.append(("ensure_connection", max_retries))

        def channel(self) -> FakeChannel:
            calls.append(("channel", None))
            return FakeChannel()

        def release(self) -> None:
            calls.append(("connection.release", None))

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

    assert result == {"status": "success"}
    assert calls == [
        ("ensure_connection", 1),
        ("channel", None),
        ("restore_visible", 1),
        ("channel.close", None),
        ("connection.release", None),
    ]


def test_should_report_recovery_connection_errors_without_raising(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core.tasks import visibility_recovery

    class FailingConnection:
        def ensure_connection(self, *, max_retries: int) -> None:
            raise RuntimeError("broker unavailable")

        def release(self) -> None:
            pass

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

    result = visibility_recovery.restore_expired_reservations()

    assert result == {"status": "error"}


def _acquired_lock():
    from contextlib import nullcontext

    return nullcontext(True)
