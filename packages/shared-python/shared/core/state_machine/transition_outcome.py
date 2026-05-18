from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TransitionOutcomeReason = Literal[
    "transitioned",
    "job_not_found",
    "invalid_transition",
    "cas_conflict",
    "transition_exception",
    "rollback_exception",
]


@dataclass(frozen=True)
class JobTransitionOutcome:
    job_id: str
    to_state: str
    reason: TransitionOutcomeReason
    attempts: int
    from_state: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.reason == "transitioned"

    def as_bool(self) -> bool:
        return self.succeeded

    @classmethod
    def transitioned(
        cls,
        *,
        job_id: str,
        from_state: str,
        to_state: str,
        attempts: int,
    ) -> JobTransitionOutcome:
        return cls(
            job_id=job_id,
            from_state=from_state,
            to_state=to_state,
            reason="transitioned",
            attempts=attempts,
        )

    @classmethod
    def rejected(
        cls,
        *,
        job_id: str,
        to_state: str,
        reason: TransitionOutcomeReason,
        attempts: int,
        from_state: str | None = None,
        error_message: str | None = None,
    ) -> JobTransitionOutcome:
        return cls(
            job_id=job_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            attempts=attempts,
            error_message=error_message,
        )
