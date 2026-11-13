# Native read coordination optimization

Date: 2026-11-03 (Pacific). The first optimization reduces PostgreSQL work and
improves the reproduced mixed-burst lexical P95 by 22.9%. The one-second
warm lexical gate still fails on the constrained Oracle node. Production
qualification remains open.

The subsequent [classic read simplification](opensearch-classic-read-simplification-2026-11-03.md)
records the next implementation and its fresh before/after comparison. The
measurements below remain the historical first optimization.

## Changes and safety boundaries

- `PublishedLexicalRead.start` captures document IDs, revisions and builds in
  one outer-joined query instead of loading each build separately. Missing,
  mismatched or retired builds still fail the entire read. The 24-document
  regression verifies one build-reading statement and at most six statements
  for capture, with every document/build/revision binding preserved.
- Backend control and existing private shared-generation gates are locked
  directly. Creation attempts are limited to missing rows, with the existing
  conflict-handling bootstrap retained. Two concurrent first readers of an
  empty namespace converge on one control row and one generation row.
- Liveness validation locks the backend control and its own lease without
  reacquiring the namespace generation gate. It does not capture or extend a
  generation. A valid pinned reader can therefore finish while another
  publication holds that gate. Version capture, renewal, cleanup and native
  index-use admission retain their existing gates and lock order.
- Lease ownership, state, backend epoch and expiration checks remain. Database
  time is sampled after the lease lock is acquired. A regression holds that
  lock until a previously live lease expires and verifies that the waiting
  validator rejects it. Row-lock wait behavior follows the
  [PostgreSQL locking rules](https://www.postgresql.org/docs/15/explicit-locking.html#LOCKING-ROWS).

No tokenization, BM25 parameters, RRF, index-use ownership, cold-index
opening/closing protocol, pool sizes or executor limits changed.

## Same-input before/after replay

Both variants used an owned OpenSearch 3.9.0 container capped at 0.55 CPU,
1,500 MiB RAM and a 640 MiB heap. The application and temporary PostgreSQL
shared another 0.55-CPU/1,700-MiB scope. The default executor remained at
16 threads. Sync and async connection budgets were unchanged.

The replay reused the prior frozen business-query manifest and prepared
18-document corpus, initially 1,727 lexical units. Each variant rebuilt its
fixture, warmed all 20 query strings, then replayed the same 75-second
initial-burst profile: 50 classic reads and 40 post-parse indexing jobs,
including a document replacement. No parsing or paid model calls ran.

The old and new source sets ran sequentially on the same node. Both achieved
50 arrivals in a rolling second; their actual arrival spans were 0.403 and
0.472 seconds respectively. Both completed all 50 reads and 40 indexing jobs
without errors and drained their pending indexing work. Results are one
diagnostic repetition per implementation, not a statistically qualified
capacity estimate. The source-restoration trap returned the isolated checkout
to the optimized implementation after the comparison.

| Measurement | Before | After |
| --- | ---: | ---: |
| Lexical P50 | 11.669 s | 9.202 s |
| Lexical P95 | 14.330 s | 11.045 s |
| Lexical maximum | 15.420 s | 12.104 s |
| Complete classic request P95 | 15.641 s | 12.892 s |
| Capture published revisions P95, excluding executor queue | 1.714 s | 0.591 s |
| SQL statements per complete read, excluding background renewals | 74 | 39 |
| Per-channel OpenSearch server `took` P95 | 0.952 s | 0.060 s |
| Sampled application/PG scope throttled time | 27.67 s | 25.69 s |

For context, the prior original 390-second preflight's all-read lexical P95
was 14.246 seconds. This table compares the newly rerun initial burst with
itself, rather than treating that older all-read P95 as the optimization's
baseline.

## Remaining latency

The table below partitions each lexical interval without double-counting
nested or parallel spans, then averages over the 50 requests. These are
means, not stage P95 values that can be added to the request P95.

| Interval category | Before mean | After mean |
| --- | ---: | ---: |
| Waiting for an executor thread | 5.121 s | 3.775 s |
| PG pool acquisition | 1.281 s | 1.202 s |
| Creating PG connections | 0.812 s | 0.543 s |
| PG statement round trips, including lock and scheduling waits | 2.386 s | 1.904 s |
| OpenSearch lifecycle/identity HTTP | 0.230 s | 0.092 s |
| Parallel BM25 HTTP interval union | 0.638 s | 0.184 s |
| Remaining application/transaction/scheduling time | 1.013 s | 1.338 s |

The structural N+1 issue and redundant liveness generation locks are removed.
The full read still has substantial executor, connection and coordination
cost. Hot native search admission still uses lifecycle observation, namespace
registry locking/touching and index-use leases. The no-filter selection step
also shares the default executor. PostgreSQL body hydration and final result
assembly remain part of the application path.

The next investigation should distinguish that remaining coordination cost
from executor admission and connection startup. Any further consolidation of
native-use admission must retain lifecycle observation fencing, cleanup
protection and close exclusion. Increasing concurrency alone is not an
established fix. The formal one-hour, cold-index, large-namespace and target
host qualification gates remain open.

## Verification and retained evidence

- Two new regression cases were first run against the old implementation:
  both failed for the intended reasons (24 build statements and validation
  blocked by an unrelated publication gate).
- The final targeted regression suite passed 10 tests in 13.45 seconds,
  covering bounded capture, invalid builds, concurrent initialization,
  publication independence, post-lock expiry and backend-epoch fencing.
- Existing publication and turnover contracts passed 29 tests in 66.27
  seconds, covering native failure propagation, demo runtime permissions,
  revision/cache consistency, old-reader cleanup protection, retries and
  lifecycle reservations. The full 400-index and real-Redis cases were
  deselected; they were not rerun for this optimization.
- An earlier integration attempt hit the new container's startup window and
  failed with connection errors. After actual node health was established,
  the complete selected integration suite was rerun successfully.
- Targeted Pyright reported zero errors/warnings. Ruff and whitespace checks
  passed. The mixed harness cases completed in 162.96 and 156.72 seconds,
  including setup and warmup; these passes do not override the latency gate.

The test reused the cached image, prepared inputs, filesystem/fake-Redis
fixtures and loopback connectivity, with external HTTP and telemetry export
disabled. No additional cloud resources or paid provider calls were used.
The owned container was removed and temporary test scopes exited. The
comparison chat's original ports and resources were not changed. Temporary
profiling code is archived outside the normal test directory. No production
migration, deployment or Git commit was performed.

Private inputs, scripts, source snapshots/hashes, raw timing events and
summaries are retained under `read-opt-6f21` in the isolated Oracle checkout.

- Before event SHA-256: `4dd990435c50f0562281a5aabe6d416f6822d3872786298c3f25e01ebb8cf44a`
- After event SHA-256: `865d1957a6723f60d900095042b11a7399f0f1a6194a24ac4a1970a74ea8ca92`
