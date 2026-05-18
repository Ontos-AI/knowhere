from __future__ import annotations

from dataclasses import dataclass

from shared.core.state_machine.states import is_valid_transition
from shared.core.state_machine.transition_outcome import JobTransitionOutcome

MAX_TRANSITION_ATTEMPTS = 3


@dataclass(frozen=True)
class TransitionJobSnapshot:
    status: str
    version: int


@dataclass(frozen=True)
class PreparedTransitionAttempt:
    outcome: JobTransitionOutcome | None
    from_state: str | None = None
    version: int | None = None

    @property
    def can_write(self) -> bool:
        return self.outcome is None and self.from_state is not None and self.version is not None


def prepare_transition_attempt(
    *,
    job_id: str,
    to_state: str,
    snapshot: TransitionJobSnapshot | None,
    attempts: int,
) -> PreparedTransitionAttempt:
    if snapshot is None:
        return PreparedTransitionAttempt(
            outcome=JobTransitionOutcome.rejected(
                job_id=job_id,
                to_state=to_state,
                reason="job_not_found",
                attempts=attempts,
            )
        )

    if not is_valid_transition(snapshot.status, to_state):
        return PreparedTransitionAttempt(
            outcome=JobTransitionOutcome.rejected(
                job_id=job_id,
                from_state=snapshot.status,
                to_state=to_state,
                reason="invalid_transition",
                attempts=attempts,
            )
        )

    return PreparedTransitionAttempt(
        outcome=None,
        from_state=snapshot.status,
        version=snapshot.version,
    )


def should_retry_cas_conflict(
    attempt_index: int,
    *,
    max_attempts: int = MAX_TRANSITION_ATTEMPTS,
) -> bool:
    return attempt_index < max_attempts - 1


def get_cas_retry_delay_seconds(attempt_index: int) -> float:
    return 0.1 * (2**attempt_index)


def build_cas_conflict_outcome(
    *,
    job_id: str,
    from_state: str | None,
    to_state: str,
    attempts: int,
) -> JobTransitionOutcome:
    return JobTransitionOutcome.rejected(
        job_id=job_id,
        from_state=from_state,
        to_state=to_state,
        reason="cas_conflict",
        attempts=attempts,
    )


def build_transition_exception_outcome(
    *,
    job_id: str,
    to_state: str,
    attempts: int,
    error: Exception | str,
) -> JobTransitionOutcome:
    return JobTransitionOutcome.rejected(
        job_id=job_id,
        to_state=to_state,
        reason="transition_exception",
        attempts=attempts,
        error_message=str(error),
    )


def build_rollback_exception_outcome(
    *,
    job_id: str,
    to_state: str,
    attempts: int,
    error: Exception | str,
) -> JobTransitionOutcome:
    return JobTransitionOutcome.rejected(
        job_id=job_id,
        to_state=to_state,
        reason="rollback_exception",
        attempts=attempts,
        error_message=str(error),
    )
