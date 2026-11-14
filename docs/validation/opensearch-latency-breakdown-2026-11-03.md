# Oracle lexical latency diagnosis

Follow-up: the [first read coordination optimization](opensearch-read-optimization-2026-11-03.md)
nearly halved the SQL statement count and improved a fresh same-input mixed-burst
lexical P95 from 14.33 to 11.05 seconds. The warm latency gate remains open.

Date: 2026-11-03 (Pacific). The dominant delay in the reproduced mixed burst
is application executor and PostgreSQL connection waiting. The shared
application/PostgreSQL CPU quota amplifies that delay. Native OpenSearch
execution also exceeds the one-second target on this small test node; this
run does not qualify production capacity.

## Reproduction and boundaries

The previous 390-second preflight recorded a 14.246-second lexical P95 across
150 reads, and 15.206 seconds for its initial 50-read burst. It did not record
per-request stage spans. This diagnosis replays that initial burst with
temporary instrumentation; it does not reconstruct historical request spans.
The instrumented mixed replay records a 16.792-second lexical P95.

Each variant uses the same frozen 20-query/18-document fixture, initially
1,727 lexical units, warm indexes, native BM25, and the initial production
burst profile multiplied ten times. The mixed cases include 40 prepared
post-parse indexing tasks, including one replacement. Measurement lasts
75 seconds after setup and warmup. Result caching and hit-stat writes are
disabled. The direct-native control requests the same two BM25 channels and
20 fused candidates, with published build IDs captured before measurement.
It omits per-request PostgreSQL coordination and body hydration.

OpenSearch stays at 0.55 CPU, 1,500 MiB RAM and a 640 MiB heap. The replay
application and temporary PostgreSQL share a separate 1,700 MiB scope with
either 0.55 or 0.9 CPU. The replay has 16 default executor threads; the sync
coordination pool has five persistent plus five overflow connections. The
request async engine has 12 persistent plus 12 overflow connections.

The node, database, index prefixes and scope belong only to this run on
the Oracle lab host. The comparison chat's service and indexes were untouched.
The test used a cached image, prepared data, fake Redis, filesystem storage
and loopback connections, with telemetry export and external HTTP disabled.
No paid API calls or additional cloud resources were used.

## Controlled replay results

Times are seconds. P95 uses the nearest-rank observation. Each case completed
50 reads with zero errors. Both mixed cases completed all 40 indexing jobs
without errors and drained their pending queues.

| Variant | App + PG CPU | Actual rolling-second read peak | Lexical P95 | Complete request P95 | Per-channel OpenSearch `took` P95 |
| --- | --- | --- | --- | --- | --- |
| Mixed | 0.55 | 50 | 16.792 | 17.530 | 0.896 |
| Reads only | 0.55 | 29 | 9.523 | 10.911 | 0.623 |
| Direct native, reads only | 0.55 | 50 | 5.013 | 5.013 | 1.021 |
| Mixed, higher app/PG quota | 0.90 | 50 | 9.296 | 9.758 | 0.618 |

The read-only application replay spread the scheduled arrivals across
1.778 seconds, compared with 0.423 seconds in the mixed baseline. It is
supporting evidence of substantial coordination cost without indexing, not
a clean numerical estimate of the indexing penalty. The two mixed runs both
achieved a 50-request rolling-second peak; their actual arrival spans were
0.423 seconds at 0.55 CPU and 0.736 seconds at 0.9 CPU. These are single
diagnostic repetitions with scheduler variation, not an exact causal
percentage estimate or a repeated capacity benchmark.

Changing only the mixed replay's application/PostgreSQL quota from 0.55 to
0.9 CPU reduced lexical P95 by 44.6%. Recorded quota-throttled time fell from
31.66 to 6.93 seconds across the measured scope-sampling windows. This is
evidence that the quota matters, not evidence that CPU alone explains every
wait or that increasing production CPU would achieve the target.

## One actual slow request, without adding percentiles

The mixed baseline's actual lexical-P95 request took 16.792 seconds through
lexical discovery and 17.531 seconds through complete classic retrieval.
Its lexical interval partitions as follows. Nested and parallel spans are
counted once; these are not sums of independent stage percentiles.

| Non-overlapping interval category | Seconds |
| --- | --- |
| Waiting for a default executor thread | 7.904 |
| Acquiring a PostgreSQL pool connection, excluding connection creation | 4.869 |
| PostgreSQL statement round trips, including lock and scheduling waits | 1.962 |
| OpenSearch lifecycle and index-identity HTTP calls | 0.558 |
| Two BM25 HTTP channels, union of their parallel intervals | 0.277 |
| Creating PostgreSQL connections | 0.015 |
| Remaining application, transaction and scheduling time | 1.208 |
| Total lexical interval | 16.792 |

The complete request's remaining approximately 0.739 seconds includes final
ranking, assembly, projection and read-session completion. PostgreSQL body
hydration is already inside the lexical interval; its inclusive span for this
request was 0.988 seconds and must not be added again to the table.

Across all 50 baseline lexical intervals, executor waiting accounts for
51.5%, connection acquisition/creation for 12.4%, PostgreSQL statement round
trips for 17.6%, BM25 HTTP intervals for 8.4%, lifecycle/identity HTTP for
1.6%, and remaining application time for 8.5%.

## Explanation supported by the trace and code

- A request enters the executor six times across the full classic path.
  Blocking coordination, pool acquisition and native HTTP work occupy the
  same 16-thread executor. Its backlog delays even the nearly empty filter
  step: this request waited 3.968 seconds before that step could run.
- The full instrumented path averages 74 PostgreSQL statements per read
  under mixed load, and 72 without writes. These counts exclude unassociated
  heartbeat work. `PublishedLexicalRead.start` selects the published document
  set and then calls `db.get(LexicalBuild, ...)` once per document
  (`lexical/reader.py`, lines 69-84).
- Read admission, index-use admission, liveness checks and release repeatedly
  enter PostgreSQL transactions. `ensureIndex` and `_lockLease` both lock the
  namespace registry row and update `lastUsedAt`
  (`lexical/coordination.py`, lines 93-111; `lexical/reader.py`, lines 165-183).
  Passive PostgreSQL samples observed transaction-ID and tuple-lock waits.
  The trace proves contention exists; it does not assign all lock-wait time
  to a particular statement or justify removing safety checks blindly.
- The direct-native P95 request still waited 3.714 seconds for an executor
  thread, followed by 1.277 seconds covered by BM25 channel HTTP. Its
  five-second client latency is not five seconds of Lucene computation.
  Across that control, the two-channel native function's P95 was 1.973
  seconds, while individual server-reported `took` P95 was 1.021 seconds.

The next optimization should reduce hot-read database round trips and shared
coordination contention, while preserving revision pins, cleanup safety and
index-use ownership. Executor/connection budgets also need to be aligned.
Merely increasing concurrency can move the backlog into PostgreSQL or
OpenSearch. Rerun this same burst after a separately authorized implementation
change, then return to mixed-load qualification on the agreed production
baseline. The present measurements cannot predict that node's latency.

## Evidence and cleanup

Pytest completed four diagnostic cases in 630.09 seconds, including setup and
warmup. This indicates diagnostic harness completion, not performance-gate
acceptance. No production implementation was changed for this diagnosis.
Instrumentation and the throwaway test were archived outside the checkout's
test directory. The owned container and temporary scope were removed/stopped;
the comparison service remained listening on its original ports.

Private scripts, profile, raw timing events, PostgreSQL aggregate wait samples
and `summary.json` are retained in the isolated checkout's
`latency-diag-e4a9` directory. No raw customer queries or SQL statements are
included in this report.

| Event stream | SHA-256 |
| --- | --- |
| Mixed 0.55 | `866369ab6c939994355b367421b000e989de854680cde20547a22bce43b0d90b` |
| Reads only | `bfd8c82d8bd08271ef290841c259459846268eb6a422af8cdea434daf5d58af8` |
| Direct native | `a6522e25fbf4d909b305487fee06fba39b484c0f81ec65e54924088c84367298` |
| Mixed app/PG 0.9 | `ab67ea568e0131b3e7277eabd64f276aba8c2611b4d3da59626ae5d202548c03` |
