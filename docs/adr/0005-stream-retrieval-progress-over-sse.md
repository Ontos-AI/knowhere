# Stream Retrieval Progress Over SSE

## Status

Accepted

## Context

Online Brain users currently wait for the complete Retrieval response while
map-nav planning, searching, and source review run. Knowhere owns retrieval and
evidence, while answer generation belongs to downstream clients. A streaming
contract must improve perceived latency without exposing chain-of-thought or
making partial citations authoritative.

## Decision

Add a v2-only `POST /v2/retrieval/query/stream` endpoint using Server-Sent
Events. The endpoint accepts the full `RetrievalQueryRequest` shape and is
live-only: disconnecting cooperatively cancels the retrieval run, and retries
start a new run. In-progress events use a fixed, route-aware phase vocabulary
(`started`, `planning`, `searching`, `reviewing_sources`, `finalizing`) and may
include only safe aggregate counts. The stream terminates with a versioned
envelope containing either the existing retrieval response as the authoritative
result or a typed, user-safe failure (`failed`, `cancelled`, or `no_results`).

The synchronous map-nav engine remains intact and publishes sanitized progress
through an optional step callback bridged to the SSE route by an async queue.
Answer-token streaming remains downstream. The endpoint sends heartbeats,
disables proxy buffering, and uses per-connection event IDs without promising
replay in the first version.

Retrieval duration is measured from the execution plan's start through final
public-result assembly. The same duration definition is used for persisted
`retrieval_runs.latency_ms` and latency aggregates; cache hits are recorded too.
Trace persistence must receive the execution start timestamp rather than
starting its own timer after navigation and hydration. Timing and terminal
outcomes must cover classic, map-nav, small-corpus, cache-hit, failed, and
cancelled routes. `retrieval_runs` is the ledger for all of those routes and
stores explicit route, cache, latency, and terminal-status fields.

## Consequences

The existing JSON retrieval endpoint remains backward compatible, while SDKs
and Online Brain clients need a new streaming adapter and UI state model. A
future resumable stream would require durable event replay and is deliberately
out of scope. Partial evidence and provisional citations are also deferred
until their grounding and revision semantics are defined.
