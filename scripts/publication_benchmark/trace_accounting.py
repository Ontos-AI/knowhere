"""Terminal publication-trace accounting contract.

Phase 0 freezes the input/output contract used by the 100 percent
terminal-trace accounting check: publication attempts in, one terminal event
per attempt out, with missing and duplicate terminal events reported instead of
silently ignored.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence


TERMINAL_TRACE_ACCOUNTING_CONTRACT_VERSION: str = (
    "publication-trace-accounting/1"
)
TERMINAL_TRACE_ACCOUNTING_INPUT_FIELDS: tuple[str, ...] = (
    "publication_attempt_refs",
    "terminal_event_attempt_refs",
)
TERMINAL_TRACE_ACCOUNTING_OUTPUT_FIELDS: tuple[str, ...] = (
    "publication_attempts",
    "terminal_events",
    "missing_attempt_refs",
    "duplicate_attempt_refs",
    "unexpected_attempt_refs",
    "is_accounted",
    "coverage_ratio",
)


class TraceAccountingError(ValueError):
    """Raised when terminal publication traces do not reconcile."""


def terminal_trace_accounting_contract() -> dict[str, object]:
    """Describe the Phase 0 terminal-trace accounting boundary.

    Phase 0 does not emit publication traces. It freezes the input and output
    shape so Phase 1 telemetry can be admitted against one stable contract.
    """
    return {
        "schema_version": TERMINAL_TRACE_ACCOUNTING_CONTRACT_VERSION,
        "inputs": list(TERMINAL_TRACE_ACCOUNTING_INPUT_FIELDS),
        "outputs": list(TERMINAL_TRACE_ACCOUNTING_OUTPUT_FIELDS),
        "rule": "every publication attempt has exactly one terminal event",
    }


@dataclass(frozen=True)
class TerminalTraceAccounting:
    """Accounting result for one batch of publication attempts."""

    publication_attempts: int
    terminal_events: int
    missing_attempt_refs: tuple[str, ...]
    duplicate_attempt_refs: tuple[str, ...]
    unexpected_attempt_refs: tuple[str, ...]

    @property
    def is_accounted(self) -> bool:
        """Return whether every attempt has exactly one terminal event."""
        return (
            not self.missing_attempt_refs
            and not self.duplicate_attempt_refs
            and not self.unexpected_attempt_refs
        )

    @property
    def coverage_ratio(self) -> float:
        """Return the share of attempts with exactly one terminal event."""
        if self.publication_attempts == 0:
            return 1.0
        accounted = self.publication_attempts - len(self.missing_attempt_refs)
        return accounted / self.publication_attempts

    def to_dict(self) -> dict[str, object]:
        return {
            "publication_attempts": self.publication_attempts,
            "terminal_events": self.terminal_events,
            "missing_attempt_refs": list(self.missing_attempt_refs),
            "duplicate_attempt_refs": list(self.duplicate_attempt_refs),
            "unexpected_attempt_refs": list(self.unexpected_attempt_refs),
            "is_accounted": self.is_accounted,
            "coverage_ratio": self.coverage_ratio,
        }


def account_terminal_traces(
    *,
    publication_attempt_refs: Sequence[str],
    terminal_event_attempt_refs: Iterable[str],
) -> TerminalTraceAccounting:
    """Reconcile publication attempts with terminal trace events."""
    attempts = tuple(str(ref) for ref in publication_attempt_refs)
    if len(set(attempts)) != len(attempts):
        raise TraceAccountingError(
            "publication attempt references must be unique within one batch"
        )
    attempt_set = set(attempts)
    counts = Counter(str(ref) for ref in terminal_event_attempt_refs)
    missing = tuple(sorted(ref for ref in attempt_set if counts[ref] == 0))
    duplicates = tuple(
        sorted(ref for ref in attempt_set if counts[ref] > 1)
    )
    unexpected = tuple(sorted(ref for ref in counts if ref not in attempt_set))
    return TerminalTraceAccounting(
        publication_attempts=len(attempts),
        terminal_events=sum(counts.values()),
        missing_attempt_refs=missing,
        duplicate_attempt_refs=duplicates,
        unexpected_attempt_refs=unexpected,
    )


def assert_terminal_trace_accounting(accounting: TerminalTraceAccounting) -> None:
    """Raise unless every publication attempt has exactly one terminal event."""
    if accounting.is_accounted:
        return
    raise TraceAccountingError(
        "terminal publication trace accounting failed: "
        f"attempts={accounting.publication_attempts} "
        f"terminal_events={accounting.terminal_events} "
        f"missing={list(accounting.missing_attempt_refs)} "
        f"duplicate={list(accounting.duplicate_attempt_refs)} "
        f"unexpected={list(accounting.unexpected_attempt_refs)}"
    )
