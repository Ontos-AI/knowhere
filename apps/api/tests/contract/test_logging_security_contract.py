from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _FakeLogfireExceptionHelper:
    exception: BaseException
    is_recording_exception: bool = True

    def no_record_exception(self) -> None:
        self.is_recording_exception = False


def test_redact_sensitive_text_masks_postgresql_url_credentials() -> None:
    from shared.core.logging import redact_sensitive_text

    message = (
        "invalid dsn after "
        "postgresql+psycopg2://postgres:super-secret@database.example:5432/knowhere"
    )

    redacted = redact_sensitive_text(message)

    assert "super-secret" not in redacted
    assert "postgresql+psycopg2://[REDACTED]@database.example:5432/knowhere" in redacted


def test_logfire_callback_does_not_export_exception_with_database_credentials() -> None:
    from shared.core.logging import _downgrade_expected_logfire_exception

    helper = _FakeLogfireExceptionHelper(
        exception=RuntimeError(
            "invalid dsn: postgresql://postgres:super-secret@database.example/knowhere"
        )
    )

    _downgrade_expected_logfire_exception(helper)  # pyright: ignore[reportArgumentType]

    assert helper.is_recording_exception is False
