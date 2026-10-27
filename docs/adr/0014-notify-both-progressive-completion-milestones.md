# Notify both progressive completion milestones

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

The same progressive Job first publishes a viewable baseline and later makes
the fully enriched result available. Hipohipo needs both milestones to start
viewing early and refresh its final snapshot. Knowhere and Hipohipo already
have a webhook delivery and reception integration.

## Decision

Reuse the Job's existing webhook callback URL, authentication, delivery retries,
and durable event mechanism. A successful progressive Job produces two distinct
business events:

- `job.completed`: the initial baseline and required resources are available.
- `job.enrichment.completed`: full enrichment and the final package are available.

The second event schedules result synchronization, not another parse or usage
charge. Ordinary full ingestion retains its existing single successful
completion event. Extend both event construction and reception to recognize
the new event explicitly.

## Consequences

Two business events do not imply exactly two HTTP requests: deliveries may be
retried or arrive out of order. Receivers must handle each milestone
idempotently and ignore obsolete source/parse identities. Publish each event
only after its represented content is durable, using the existing transactional
outbox and post-commit dispatch pattern. ADR 0019 selects ten-minute server
reconciliation, and ADR 0020 selects fifteen-second visible-client status checks.
Final snapshot addressing remains implementation work.
