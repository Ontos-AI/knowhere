# Retrieval Streaming over SSE

**Status:** Accepted design
**Related issue:** [#330](https://github.com/Ontos-AI/knowhere/issues/330)
**Related ADR:** [0005](../adr/0005-stream-retrieval-progress-over-sse.md)

## Purpose

Online Brain users currently wait for a complete retrieval response while
map-nav planning, searching, source review, and final hydration run. This
design makes that work visible without exposing chain-of-thought or changing
who owns answer generation.

## Ownership boundary

Knowhere owns retrieval, safe progress, evidence, and authoritative citations.
The downstream Online Brain client owns answer synthesis and answer-token
streaming. Knowhere does not begin generating the final answer as part of this
feature.

## Public API

Add `POST /v2/retrieval/query/stream` with `Content-Type: text/event-stream`.
The request body is the full existing
`RetrievalQueryRequest`; streaming changes delivery, not retrieval semantics.
The existing JSON endpoints remain unchanged as the fallback.

The stream is live-only. A disconnect cooperatively cancels the run; retrying
starts a new run. Event IDs are monotonic per connection but do not imply
replay or resumability.

The route remains behind the normal authenticated-user dependency and v2
route-admission policy. The guest-key allowlist, system-limit configuration,
OpenAPI registration, and any CORS policy must explicitly include the stream
route. v2 BYOK `llm_config` is accepted exactly as on the JSON route and is
never copied into an event payload or log message.

## Event contract

Each SSE frame has an event name (`progress`, `heartbeat`, or `terminal`) and a
JSON payload with `schema_version`, `stream_id`, `sequence`, and server
`elapsed_ms`. Progress payloads use the fixed, route-aware phases:

```text
started → planning? → searching → reviewing_sources → finalizing
```

Classic and small-corpus routes omit `planning`; they must not emit phases that
did not occur. In-progress events contain only safe aggregate counts such as
`candidate_source_count` and `reviewed_source_count`. They do not contain
document names, chunk content, query rewrites, raw planner output, citations,
or chain-of-thought.

The terminal event is a versioned envelope containing the existing retrieval
response as the authoritative result:

```json
{
  "schema_version": 1,
  "stream_id": "rst_...",
  "sequence": 7,
  "elapsed_ms": 2410,
  "status": "completed",
  "response": { "namespace": "default", "query": "...", "router_used": "mapnav", "evidence_text": "...", "referenced_chunks": [], "results": [] }
}
```

Failure terminals use `failed`, `cancelled`, or `no_results`. Failures expose a
stable user-safe `code` and `message`; detailed provider, database,
authentication, and planner errors remain in server logs.

Wire requirements are part of the contract: frames use UTF-8 SSE `id`,
`event`, and one `data` line followed by a blank line; a heartbeat is an SSE
comment or named heartbeat event and carries no retrieval data; exactly one
terminal event is sent before the connection closes; no `retry` directive is
promised because the stream is not resumable. Progress events may be
coalesced when a bounded queue is full, but terminal events must never be
dropped.

Cache hits still emit `started` and a terminal `completed` event, with a safe
`cache_hit: true` indicator. They omit phases for work that did not run.
`no_results` is reserved for a successful retrieval execution that produces no
evidence; provider, timeout, validation, and internal failures remain
`failed`.

HTTP authentication, request validation, and route-admission failures happen
before the stream starts and use ordinary HTTP error responses. Once a `200`
SSE response has been opened, execution failures must be represented by a
terminal SSE event because the HTTP status can no longer be changed.

## Internal implementation seam

Keep the synchronous map-nav implementation. Add an optional callback that
receives a sanitized progress projection after each completed planner,
search, or review step. The SSE route bridges this callback to an
`asyncio.Queue` using a thread-safe loop handoff while retrieval continues in
its existing worker thread.

The queue is bounded and owns cleanup of the worker task, callback, heartbeat
task, and database session. The callback must not touch an `AsyncSession` from
the worker thread. The route polls request disconnect state and propagates a
cancellation token; all producer tasks are joined or cancelled in a `finally`
block so abandoned streams cannot leak threads or connections.

Add cooperative cancellation checks between steps. An in-flight synchronous
provider call may finish before cancellation takes effect. Cancelled runs do
not perform final hydration when cancellation is observed in time.

Phase ownership is explicit: the route emits `started`; the map-nav adapter
emits `planning` before `plan_query` and `searching` before navigation or
classic discovery; the route emits `reviewing_sources` after retrieval
selection and before reference hydration; and it emits `finalizing` before
public projection. Counts are sourced from existing snapshot, reference, and
assembled-result counts and are omitted when not yet known.

## Correct duration accounting

The execution plan already starts a monotonic timer before cache lookup and
logs elapsed time after the route. However, `TraceRecorder` currently starts a
second timer in its constructor, and the map-nav route constructs it only
after navigation, reference resolution, and result assembly. Persisted
`retrieval_runs.latency_ms` and its aggregates therefore under-report retrieval
latency and mostly measure trace flush time.

The canonical `Retrieval Duration` is the execution-plan timer from retrieval
start through final public-result assembly. It includes cache lookup and
applies to cache hits and misses. It excludes authentication, network/SSE
delivery, and downstream answer generation.

Required changes:

- pass the execution start timestamp into `TraceRecorder`;
- set `retrieval_runs.latency_ms` from that timestamp;
- record cache-hit runs with the same definition;
- ensure classic, map-nav, small-corpus, cache-hit, failed, and cancelled
  retrievals all have an explicit timing/observability outcome;
- expose separate `time_to_first_event_ms`, `retrieval_latency_ms`, and
  downstream `time_to_first_token_ms` measurements;
- retain per-step `elapsed_ms` as step latency, not total request latency.

`retrieval_runs` is the ledger for every retrieval execution, not only
map-nav. Each row records the route type, `agentic_enabled`, `cache_hit`,
canonical latency, and terminal status for classic, map-nav, small-corpus,
cache-hit, failed, and cancelled runs. Add a backward-compatible status field
and migration rather than overloading free-form error text.

The execution timer must end after public response projection, not merely when
the internal route outcome is assembled. If trace persistence is best-effort,
its latency update must still use the captured execution timestamps and must
not extend the user-visible retrieval duration with an unbounded database
flush.

Because the map-nav route deliberately rolls back its request session before
the synchronous LLM episode, a trace row must not be created in that session
before the rollback. Capture the execution start immediately, then create or
update the trace record at the terminal persistence point with the captured
start and an explicit end/duration supplied by the execution plan (or use a
separate trace session). `TraceRecorder.complete()` must not silently choose a
later local constructor time or include its own flush duration.

## Operational requirements

- heartbeat every 15 seconds while active;
- `Cache-Control: no-cache` and `X-Accel-Buffering: no`;
- flush after every event;
- use `fetch`-style clients where POST authorization headers are required;
- propagate disconnects to the cancellation token.
- bound maximum stream lifetime and enforce the same request/rate-limit policy
  as the JSON endpoint;
- document worker-thread, database-connection, and concurrent-stream limits.
- count one stream request as one retrieval request under the existing user and
  system limits; retries count as new requests and cannot bypass quota.

## Delivery slices

1. **Knowhere contract and timing:** typed event models, stream route, callback
   bridge, cancellation, route admission, cache semantics, corrected
   `RetrievalRun` timing across all route types, and API contract tests.
2. **SDK adapters:** typed Python and Node stream consumers with fallback to
   the existing JSON query; parse named SSE events, expose abort/error
   handling, and preserve the full terminal response shape.
3. **Online Brain UX:** phase state model, progress view, safe error states,
   and integration with existing downstream answer-token streaming.
4. **Production verification:** proxy-path e2e tests and latency/buffering
   instrumentation before setting hard p50/p95 targets.

## Verification gates

- correct phase order for map-nav, classic, and small-corpus routes;
- cache-hit streams emit only applicable phases and identify the cache hit;
- no sensitive planner or evidence data before the terminal event;
- terminal citations match the existing JSON endpoint;
- cancellation, timeout, no-results, provider failure, and disconnect are
  distinguishable;
- persisted latency includes the full retrieval path for every supported route
  and matches API timing within an expected tolerance;
- cache-hit latency and route type are visible in observability data;
- heartbeats pass through the deployed proxy without buffering;
- first-event and first-token times are measured separately.

## Explicit non-goals

- v1 API streaming endpoint;
- resumable/replayed streams;
- partial authoritative evidence or provisional citation revision;
- moving answer generation into Knowhere;
- invented latency SLAs before a production baseline exists.
