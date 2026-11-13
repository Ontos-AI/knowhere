# Classic native read simplification

Date: 2026-11-03 (Pacific). This follow-up simplifies the ordinary non-agentic
OpenSearch path after the [first read coordination optimization](opensearch-read-optimization-2026-11-03.md).
At the same actual 50-arrival peak, lexical P95 improved from 11.829 to
9.003 seconds (23.9%) and complete classic pipeline P95 from 12.706 to
9.809 seconds (22.8%). It retains publication consistency, retired-build
cleanup protection and index-close exclusion. The one-second warm lexical
gate still fails on this constrained node; production qualification remains open.

The subsequent [remaining wait diagnosis](opensearch-read-wait-diagnosis-2026-11-03.md)
separates CPU-quota effects, actual pool blocking and server-recorded lock waits
without changing this implementation.

## Request behavior

1. Capture published document revisions and build bindings once, with a
   renewable revision-read lease. Cache hits and the small-corpus route do
   not force an index open or native-use admission.
2. On native discovery, obtain one fresh combined lifecycle/identity
   observation. Under a short SQL admission transaction, validate the
   backend epoch, node incarnation, namespace registry and owned read lease;
   touch usage and create index-use protection together. A closed,
   restarted or uncertain index goes through the existing full reconciliation
   protocol. An in-memory open-state assertion does not replace admission.
3. Release SQL connections before native HTTP. Reuse a process/endpoint HTTP
   pool and bounded channel executor. Run the two existing BM25 channels,
   wait for both outcomes and retain uncertainty if either outcome is unknown.
4. Keep the classic request's index-use protection through immediate PG body
   hydration, ranking and result assembly. Those consecutive body operations
   and final validation use the same async session transaction and checked-out
   connection. Validate backend/lease authority using database time after
   acquiring the lease locks, then release both protections in that transaction.
   Mark the local context finished only after commit.

The request does not hold one PG connection throughout native I/O. Publication
capture and native admission remain separate short transactions. This avoids
occupying the body connection while OpenSearch works, while eliminating the
unnecessary connection return/reacquisition inside consecutive body processing.
Cancellation coordinates queued capture and in-flight native work before
releasing protection. A per-request lock provides that ordering; it is not a
global search lock. Agent retrieval keeps operation-scoped native protection
and its existing validation boundaries across reasoning/tool gaps.

The HTTP client is shared safely between threads as supported by
[HTTPX](https://www.python-httpx.org/api/#client). The bounded channel executor
has 32 workers, matching two channels for the replay's unchanged 16 execution
threads. It replaces per-query executors; it does not raise replay admission.
Final validation uses SQLAlchemy's documented
[async `run_sync` support](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#running-synchronous-methods-and-functions-under-asyncio)
and [transaction events](https://docs.sqlalchemy.org/en/20/orm/session_events.html#transaction-events).
Tokenization, native BM25, RRF, filters, citations and public contracts are unchanged.

## Comparison protocol

Both variants ran sequentially in an owned OpenSearch 3.9.0 container on the
existing Oracle host. Native limits were 0.55 CPU, 1,500 MiB RAM and a 640 MiB
heap. The application and temporary PostgreSQL shared another 0.55-CPU,
1,700-MiB scope. The default executor stayed at 16 threads; sync pool limits
stayed at 5 + 5 overflow and async limits at 12 + 12 overflow.

Each variant independently rebuilt the same prepared 18-document fixture,
initially 1,727 lexical units, warmed all 20 real business query strings and
replayed the same 75-second 10x burst profile. It contains 50 classic reads
and 40 post-parse indexing tasks, including a replacement with changed body
content. Two indexing executions run concurrently. Cache and hit-stat writes
were disabled consistently. No parsing, model, object-storage or external
telemetry calls ran.

An independent producer thread inside the same application quota records real
read arrivals before scheduling them onto the event loop. Both ingress
waiting and subsequent executor waiting are included in reported latency.
This prevents an overloaded event loop from silently reducing the offered
burst. It is a 50-arrival rolling-second peak, not sustained 50 requests per
second for 75 seconds. Connection startup remains included in both variants.

Both variants completed all 50 reads and 40 indexing tasks without errors and
ended with no pending indexing work. Their actual 50-read arrival spans were
0.421 and 0.408 seconds, respectively. The source-restoration trap returned
the isolated checkout to the final implementation; all seven source hashes
were checked against the local final manifest.

| Measurement | Before | After |
| --- | ---: | ---: |
| Lexical P50 | 9.708 s | 6.285 s |
| Lexical P95 | 11.829 s | 9.003 s |
| Lexical maximum | 12.056 s | 9.281 s |
| Complete classic pipeline P95 | 12.706 s | 9.809 s |
| SQL driver executions per complete read | 39 | 21 |
| Default-executor submissions per complete read | 6 | 2 |
| PG pool checkouts per complete read | 12 | 4 |
| Native lifecycle/identity HTTP calls per read | 2 | 1 |
| Per-channel OpenSearch server `took` P95 | 214 ms | 88 ms |
| Index-task arrival to committed readiness P95 | 28.572 s | 24.442 s |
| Index-task arrival to committed readiness maximum | 29.575 s | 25.390 s |

Counts exclude background renewals. SQL counts use SQLAlchemy's driver
execution events: a batched update of both lease rows counts as one driver
execution. Index readiness includes task queueing, not just native bulk time.
The complete classic pipeline includes ingress, coordination, hydration,
ranking, assembly and finalization; it does not include HTTP routing/authentication
or an agent/LLM. One diagnostic repetition per variant is not a qualified
capacity or tail-latency estimate.

## Remaining latency

These intervals partition the lexical window without double-counting nested
or parallel spans. They are means across 50 reads, not additive stage P95s.

| Interval category | Before mean | After mean |
| --- | ---: | ---: |
| Ingress/event-loop admission queue | 0.012 s | 0.063 s |
| Waiting for a default-executor thread | 3.981 s | 1.496 s |
| PG pool acquisition, excluding connection creation | 1.013 s | 1.079 s |
| Creating PG connections | 0.457 s | 0.547 s |
| PG statement round trips, including lock/scheduling waits | 1.701 s | 2.065 s |
| OpenSearch lifecycle/identity HTTP | 0.248 s | 0.054 s |
| Parallel BM25 HTTP interval union | 0.637 s | 0.237 s |
| Remaining application/transaction/scheduling time | 0.878 s | 0.638 s |

At the after variant's lexical P95 request, 4.098 seconds were executor
queueing, 1.725 seconds PG pool acquisition, 2.371 seconds PG round trips and
0.173 seconds the BM25 HTTP interval union. Simplification reduced repeated
admission and connection work; it did not eliminate application/PG contention.
PG round-trip wall time did not improve in every category. The application/PG
scope accumulated 23.62 and 25.40 seconds of sampled cgroup throttled time.
This evidence identifies substantial residual caller-side waits; it does not
isolate CPU quota from row-lock and connection contention sufficiently to
promise a target-host latency. Increasing concurrency alone is not established
as the next fix.

## Safety verification

The final selected contract/native suite passed **56 tests, 2 deselected, in
101.00 seconds**. Coverage includes:

- Pinned old revisions during concurrent replacement, with retired-build
  cleanup blocked until the reader completes; cache and retry isolation.
- Index-use and read protection retained during body assembly; idle trimming
  cannot close the active index. Closed indexes reopen through full admission.
- No synchronous PG connection held during native search; no extra synchronous
  finalization checkout; one body/final-validation async commit after search.
- Actual native HTTP connection reuse after closing an individual handle.
- Backend-epoch changes and either expired lease rejected before success.
- Cancellation during queued capture or native work, without orphaning a lease
  or releasing index use while the background call is still running.
- Unknown native outcomes preserved, including a known channel failure paired
  with an unknown sibling outcome.

New regression cases were observed failing before their corresponding fixes.
The full 400-index qualification case and external real-Redis/gevent case were
deselected; existing ordinary turnover contracts ran. Targeted Pyright for
Python 3.11 reported zero errors/warnings; Ruff and whitespace checks passed.
The tests establish these exercised boundaries, not every possible production
failure or client integration case.

## Scope and retained evidence

The main comparison is `before-final` versus `after-final`. An initial harness
allowed only 48 actual arrivals in one variant and was excluded. A subsequent
50-arrival pair tested an intermediate implementation before removal of the
last body-session commit; it is retained as diagnostic history, not presented
as the final implementation's result.

Private prepared inputs, source snapshots/hashes, scripts, raw timing events,
contract logs and summaries are retained under `classic-read-b82d` in the
isolated Oracle integration checkout. Raw business queries and document
identifiers are not copied into this report. Frozen manifest SHA-256:
`445f541447028a07ac39b82b4df578384fdfdfcde48a88f5bd541b2e98f4c421`.

- Before event SHA-256: `1d828ab46aba5a4ff12ca708f5249084a924031ce01ca1c90e6938a427c67e32`
- After event SHA-256: `c3840360d87a342c56dd2540e2f8d123d752ce24da564e7308397db850d5d3d3`

Only cached images and existing host resources were used, with loopback-only
test dependencies and external HTTP/telemetry disabled. No cloud resources
or paid provider calls were added. The owned final container was stopped and
its removal verified; its temporary port no longer listens. All owned scopes
exited and the temporary replay test was archived outside the normal test
directory. The comparison chat's original ports 18765 and 19203 still listen;
its checkout, container and services were not modified.

This uses a prepared subset on a constrained host, not the agreed production
4-vCPU/32-GiB/16-GiB-heap class. The one-hour qualification, largest namespace,
production-shaped cold-index capacity and target recovery tests remain
separate rollout gates. No production migration, deployment or Git commit
was performed.
