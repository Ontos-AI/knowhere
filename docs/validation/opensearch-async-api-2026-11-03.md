# Async API lexical retrieval

Date: 2026-11-03 (Pacific); paired runs completed on 2026-11-04 UTC.

The API async migration and safety regression checks are complete. The fresh
paired replay removed all read executor submissions, but did not pass the
performance gate: complete-pipeline P95 increased from 9.22 to 10.44 seconds,
and one of 50 async read attempts failed on a PostgreSQL namespace-row lock
timeout. This change is not qualified for production rollout.

## Implementation

API retrieval now awaits database coordination and native OpenSearch I/O.
`AsyncPublishedLexicalRead` uses the request's existing `AsyncEngine` and
asyncpg pool. Shared SQL-only decisions retain their short transaction
boundaries through SQLAlchemy's documented
[`AsyncSession.run_sync` adaptation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#running-synchronous-methods-and-functions-under-asyncio).
This runs on the event loop, without an executor or a second database engine.
The adapter's bridge session checks out no connection itself; the existing
decision's session uses the same engine/pool. Synchronous HTTP and sleeps are
kept outside these SQL callbacks.

Native calls use a loop/endpoint-scoped
[`httpx.AsyncClient`](https://www.python-httpx.org/async/), including lifecycle
observations, cold opening, capacity-victim closing and both BM25 channels.
API shutdown closes its pooled transport. Closing an individual index handle
does not close other requests' connections. The client caps native read HTTP
connections at 32, preserving the preceding channel-worker ceiling.

Renewal runs as an asyncio task with a separate short session from the same
pool. It does not share the body's active session. Consecutive hydration,
ranking, assembly and final validation still use the body's one transaction;
native HTTP runs outside SQL transactions. Both classic and agent API reads
use the async adapter. Explicitly bound legacy sync readers retain their
compatibility path outside normal API execution.

Worker execution remains gevent-compatible synchronous I/O. Worker and cleanup
implementation files are byte-identical to the pre-change snapshots. The
sync native client retains its transport; pure query construction, response
validation, RRF fusion and index settings are shared with the async client.
Jieba tokens, BM25 parameters, candidate limits, RRF and public responses are
unchanged. No schema, SDK, job-state, webhook or billing change was made.

## Preserved locking and failure behavior

The control, publication-generation, namespace-registry and owned-lease lock
rules remain unchanged. Namespace admission still takes its per-read row lock
and updates `lastUsedAt`; no LRU batching or lock weakening is included. SQL
allocation, reconciliation, reservation, expiry and final settlement reuse
the existing implementation. Core read lease/renewal/finalization helpers were
also checked against the prior source AST.

Classic reads retain index-use protection through body assembly; agent reads
retain it around native operations while keeping their revision lease across
reasoning gaps. Renewal remains 15 seconds with 60-second leases. Final
validation uses database time after the locks and only marks the local read
finished after commit.

Both channel tasks settle before use is released. Unknown transport outcomes
and independently cancelled channels take precedence over a known sibling
failure. Cancelling a request cannot establish that OpenSearch stopped, so
its native use remains uncertain when appropriate. Admission records its use
ID locally before the commit returns, allowing cancellation cleanup to find
a committed admission. Stopping renewal consumes the child's cancellation
without swallowing cancellation of the request itself.

## Safety verification

The final selected contract/native suite passed **67 tests, 2 deselected,
in 123.25 seconds**. Targeted Pyright reported zero errors/warnings; Ruff and
whitespace checks passed. The full 400-index and external real-Redis cases
were deselected, matching the previous diagnostic scope.

New tests first rejected the previous API's separate sync pool, executor
submission and sync HTTP. Further red/green cases caught cancellation after
admission commit, cancellation during renewal shutdown and a cancelled
channel masked by a known sibling failure. The final suite also verifies:

- Every API cursor uses exactly the provided async engine; async HTTP holds
  no request database checkout. The no-executor guard covers the real classic
  pipeline, rather than an isolated helper.
- Native HTTP streams are actually reused across handles. Renewal continues
  on the event-loop thread while native HTTP is delayed beyond a shortened
  revision lease's original expiration.
- Cancellation while blocked on a publication lock completes without leaving
  a queued thread; protection remains while channel cancellation is settling.
- Replacement during assembly preserves old body/citation identity and blocks
  cleanup; backend changes and expired protections reject success.
- Cold opening works, an idle victim can be closed at capacity, and an active
  victim remains open while the target request returns unavailable. The last
  two cases use a one-slot test override to exercise the actual async capacity
  branches without another full 400-index qualification run.

## Paired load protocol

The existing Oracle host uses an owned OpenSearch 3.9.0 container capped at
0.55 CPU, 1,500 MiB RAM and a 640 MiB heap. Application, indexing harness and
temporary PostgreSQL share a separate 0.55-CPU/1,700-MiB scope. Read executor
configuration stays at 16 and indexing executions at two. No new cloud
resources or paid providers are used.

The existing request async pool stays at 12 + 12 overflow. Before, API
coordination additionally used a separate 5 + 5 sync pool; after, coordination
shares the request async pool. Thus API connection ceilings go from 34 across
two pools to 24 in one pool. This comparison measures the requested async/pool
consolidation together; it does not increase database capacity to obtain a gain.
The indexing harness's separate sync engine is unchanged.

Each variant independently seeds the same prepared 18-document corpus,
initially 1,727 read-namespace units, and warms all 20 business query strings.
For every string it also compares sync and async native Top 20 hits, including
content, order, ranks and RRF scores, on the same stable published builds.
The measured workload is the unchanged 75-second production-derived burst
profile: 50 classic reads and 40 indexing tasks, including a changed-body
replacement. Cache and hit-stat writes are disabled consistently.

An independent producer records actual arrivals before event-loop dispatch;
latency includes that ingress waiting. Reads concentrate on one namespace and
writes use it plus a background namespace. This is a diagnostic hotspot at
50 rolling-second peak arrivals, not sustained 50 QPS or a complete production
customer/namespace distribution. There is no parsing or LLM execution.

The final comparison uses consecutive `before-final` and `after-final` runs.
The earlier `before` run was a pre-refactor/TDD preflight, not the final paired
baseline. The source restoration trap reinstates the async implementation.

## Results

Both runs recorded an actual rolling-second peak of 50 read arrivals, completed
all 40 indexing tasks successfully and had no pending work at the 75-second
end marker. The before run returned 50 successful reads; the after run returned
49 successes and one retryable `LEXICAL_BACKEND_UNAVAILABLE` error. The pytest
load wrapper's passing exit means the replay completed, not that its error or
latency acceptance gates passed.

| Measurement | Before: sync coordination | After: async coordination |
| --- | ---: | ---: |
| Complete pipeline P50 | 8.794 s | 9.160 s |
| Complete pipeline P95 | 9.218 s | 10.440 s |
| Lexical discovery P95 | 8.647 s | 10.204 s |
| Read errors / attempts | 0 / 50 | 1 / 50 |
| Read executor submissions / attempt | 2 | 0 |
| SQL driver executions / successful read | 21 | 21 |
| PG checkouts / read attempt | 4 | 4 |
| Observed read pool types | Sync and async | Async only |
| API pool connection ceilings | 10 sync + 24 async | 24 async |
| Namespace-row lock SQL P95 / completed execution | 1.458 s | 3.695 s |
| PG pool acquire P95 / checkout | 1.403 s | 2.222 s |
| Native BM25 channel `took` P95 / response | 84 ms | 289 ms |
| Indexing task end-to-end age P95 | 23.950 s | 24.584 s |

Request percentiles include all 50 attempts, including the failed after attempt;
after's successful-only complete P95 is also 10.440 seconds. The timed-out
namespace SQL execution does not produce an `after_cursor_execute` span, so
the SQL lock percentile only covers completed statements. Task age includes
arrival, queueing, publication and indexing; it is not native bulk duration.
All sampled native search/write rejection counters remained zero. The warm
one-second latency gate fails in both variants.

The representative after request at lexical P95 spent 4.38 seconds acquiring
PG connections and 4.42 seconds in SQL, including 3.70 seconds waiting on the
namespace row. These are intervals from that one request, not sums of stage
P95 values. Its two native BM25 channel RPCs overlapped for a 0.10-second wall
interval. Removing the thread queue did not remove database serialization.

The disjoint lexical-window partition, averaged across all 50 attempts, shows
where the removed queue time went:

| Mean time per attempt | Before | After |
| --- | ---: | ---: |
| Executor queue | 1.435 s | 0 s |
| PG pool acquisition, excluding new connection establishment | 1.166 s | 2.986 s |
| New PG connection establishment | 0.644 s | 0.148 s |
| SQL execution, including server lock waits | 1.890 s | 3.632 s |
| Native lifecycle RPC wall interval | 0.105 s | 0.671 s |
| Overlapping native BM25 channel wall interval | 0.283 s | 0.331 s |

These categories omit other application intervals and are not request P95.
The old executor supplied an implicit concurrency throttle. The async path
allows more work to reach the shared pool and unchanged namespace lock at
once; this replay observed larger waits there. The paired replay changes both
transport scheduling and pool consolidation, and does not isolate their
individual causal contributions. Host CPU throttling was present in both
runs. A single pair does not establish a precise regression percentage or
predict production latency.

### Failed request

At **2026-11-04 07:18:31.028 UTC**, PostgreSQL backend PID `3006907` logged
`canceling statement due to statement timeout` while locking a tuple in
`lexical_namespace_indexes` for `SELECT ... FOR UPDATE`. It matches the
failed read's recorded backend PID. This is a database admission failure,
not a native OpenSearch search rejection. The public error correctly did not
substitute empty evidence or the old PostgreSQL lexical backend.

Its timeline, relative to actual arrival, was:

1. Revision capture finished at approximately 1.84 seconds.
2. Prefetch finished and lifecycle HTTP began at 2.72 seconds.
3. Lifecycle HTTP finished at 4.10 seconds; a PG connection became available
   at 5.72 seconds.
4. Admission began its namespace-row lock around 5.74 seconds and failed
   around 9.44 seconds, before either BM25 search was sent.
5. Revision-read protection was cleaned up; the public failure completed at
   9.71 seconds.

The existing five-second admission budget and transaction timeout rules were
not changed. Their timeout is calculated before connection acquisition, so
pool waiting can extend the observed total beyond five seconds; this is not
a five-second API latency guarantee. Do not extend this timeout or weaken the
lock solely to hide this failure. The next performance investigation should
address admission scheduling and lock hold/wait time while preserving the
current ownership and cleanup guarantees.

### Ranking parity

Each variant independently recorded **20 / 20** matching query comparisons
between sync and async native Top 20 results, including content, order,
channel ranks and RRF scores. This establishes transport/scoring parity on
the stable prepared builds; it does not claim identical ranking between
concurrently changing publication snapshots. Revision coexistence, replacement,
cleanup and cancellation remain covered by the selected safety contracts.

### Reproduction identities

- Frozen arrival profile SHA-256:
  `4464e84a2ad2a509bd6f233b175c1f5a107510b97deae1bb1641ff58bc25a049`.
- Frozen business manifest SHA-256:
  `445f541447028a07ac39b82b4df578384fdfdfcde48a88f5bd541b2e98f4c421`.
- Before raw result SHA-256:
  `ebdb76c9c06c7ed36968afe43645a22af8ec7b2651470bdaaab066c8e043660e`.
- After raw result SHA-256:
  `c4e9da181c278eb9c53e7968204bc5e3c4712c491f00dafb78b28e6771b4d1c9`.

The final `after-hashes.json` source manifest matches both local and remote
implementation files after the baseline restoration trap. `probe-hashes.json`
identifies the shared load test, timing hooks and arrival profile.

## Limits and retained evidence

The latency gate is assessed on the complete pipeline as well as lexical
discovery. SQL/queue stage P95 values cannot be added to request P95. Driver
execution counts treat a batched update as one execution. The pipeline timing
excludes frontend HTTP routing/authentication and agent/LLM work.

This small, warm, single-hot-namespace replay does not qualify production
capacity. Production separates API, worker and Aurora resources; the fixture
shares their CPU. The existing one-hour, largest-namespace, cold-capacity and
recovery rollout gates remain separate. Worker gevent code is retained, while
the load harness executes the real indexing routine through its existing
two-thread proxy; it does not exercise the Celery broker/gevent dispatcher.

Private input/profile hashes, source snapshots, probe code, raw timing events,
contract/typecheck logs and summaries are retained under `async-e63a` in the
isolated Oracle integration checkout. No raw business queries or document
identifiers are copied into this report. No production migration, deployment
or Git commit was performed.

The owned `knowhere-async-e63a` container was stopped and its port released;
the temporary probe was archived inside the experiment directory. The other
chat's 18765/19203 listeners remained present. This used existing host capacity,
a cached image, temporary local PostgreSQL, prepared inputs and fake/local
storage. It created no paid cloud resources or paid API/LLM calls. Background
document parsing/upload was skipped under the no-new-fees constraint.
