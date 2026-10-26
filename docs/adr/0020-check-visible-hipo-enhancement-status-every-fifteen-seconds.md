# Check visible Hipo enhancement status every fifteen seconds

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

Knowhere webhooks notify Hipo's backend, while a reader already displaying the
baseline also needs to discover final synchronization or terminal failure.
Hipo's current parse polling stops when initial parsing reaches ready.

## Decision

While the relevant page is visible and retrieval enhancement remains pending,
check Hipo-local enhancement and committed snapshot state every fifteen seconds.
Adopt the final snapshot after synchronization commits, preserving reading
position and citation pins. Stop polling on completion or terminal failure.
An admitted manual retry can make the operation pending and restart these checks.

## Consequences

Reuse the existing client query/polling infrastructure with a lightweight local
status projection. Coalesce checks for the active Project rather than creating
one independent timer per document or panel. Browser checks must not invoke
Knowhere, copy assets, or repeat parsing. Hidden pages pause; returning to the
page checks current state. This interval affects browser discovery, not backend
completion or transfer time. Server recovery remains independent at ten minutes
under ADR 0019; no browser WebSocket/SSE service is required.
