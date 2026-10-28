# Reuse ten-minute Hipo enrichment reconciliation

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

The final webhook normally starts Hipohipo synchronization promptly, but missed
or delayed notifications must not strand an already-published final result.
Hipohipo's production operations documentation records an existing ten-minute
durable task reconciliation schedule.

## Decision

Reuse the existing Hipo recovery workflow and its ten-minute cadence for bounded
checks of progressive results awaiting enrichment completion. Normal webhook
receipt continues to dispatch synchronization promptly. Recovery must operate
without an open browser and converge through the same idempotent synchronization
admission path.

## Consequences

Do not create a separate schedule solely for progressive results or poll every
historical document. Stop checking completed or terminally failed work until a
retry is admitted. Use bounded, fair batches so this recovery cannot starve
existing task classes. Ten minutes is the recovery check interval, not a promise
that result transfer or all queued recovery work finishes within ten minutes.
Verify the live schedule during deployment. ADR 0020 separately selects
fifteen-second visible-client status checks.
