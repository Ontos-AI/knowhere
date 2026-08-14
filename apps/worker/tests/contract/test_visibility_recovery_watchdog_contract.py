from __future__ import annotations

from pytest import MonkeyPatch


def test_should_keep_watchdog_alive_after_a_recovery_error(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core import visibility_recovery_watchdog

    calls: list[tuple[str, float | None]] = []

    class StopAfterOneCycle:
        def clear(self) -> None:
            calls.append(("stop.clear", None))

        def set(self) -> None:
            calls.append(("stop.set", None))

        def wait(self, timeout: float) -> bool:
            calls.append(("stop.wait", timeout))
            return True

    def raise_recovery_error() -> dict[str, str]:
        calls.append(("recover", None))
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        visibility_recovery_watchdog,
        "restore_expired_reservations",
        raise_recovery_error,
    )
    monkeypatch.setattr(
        visibility_recovery_watchdog,
        "write_visibility_recovery_heartbeat",
        lambda: calls.append(("heartbeat.write", None)),
    )
    monkeypatch.setattr(
        visibility_recovery_watchdog,
        "remove_visibility_recovery_heartbeat",
        lambda: calls.append(("heartbeat.remove", None)),
    )
    monkeypatch.setattr(
        visibility_recovery_watchdog,
        "setup_logging",
        lambda *, service_name: calls.append(("logging", None)),
    )
    monkeypatch.setattr(
        visibility_recovery_watchdog.signal,
        "signal",
        lambda *args: None,
    )
    monkeypatch.setattr(
        visibility_recovery_watchdog,
        "_stop_event",
        StopAfterOneCycle(),
    )

    visibility_recovery_watchdog.run_visibility_recovery_watchdog()

    assert calls == [
        ("logging", None),
        ("stop.clear", None),
        ("heartbeat.write", None),
        ("recover", None),
        ("heartbeat.write", None),
        ("stop.wait", 30.0),
        ("heartbeat.remove", None),
    ]
