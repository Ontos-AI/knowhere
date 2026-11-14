# Remaining classic read wait diagnosis

Date: 2026-11-03 (Pacific); measurements and infrastructure reads occurred on
2026-11-04 UTC. This follows the
[classic read simplification](opensearch-classic-read-simplification-2026-11-03.md).
It diagnoses the remaining approximately nine-second lexical P95 without
changing application implementation or production configuration.

## Findings

The constrained mixed workload combines three interacting queues. A shared
0.55-CPU budget slows application/PG progress; publication and per-read index
admission contend on namespace rows; blocked operations occupy threads and
connections, making later requests queue outside the database as well.
CPU throttling is an upstream cause that can lengthen several intervals. It
cannot be added to pool/lock waits as an independent request-time component.

Increasing the shared CPU quota materially improved latency. Increasing
connection or thread counts alone did not establish an improvement. More
threads moved waiting into the sync pool and made request latency worse.
A warm, larger async pool also failed to remove the delay. These are isolated
diagnostic observations, not a production throughput qualification.

## Controlled interventions

All comparisons used the final application source from the preceding report,
the same prepared 18-document corpus (initially 1,727 units), 20 business query
strings and 75-second mixed profile. Each completed 50 reads at an actual
50-arrival rolling-second peak and 40 post-parse indexing tasks. This is a
burst, not sustained 50 QPS. One variant ran at a time on the existing Oracle
host, using owned loopback dependencies, fake Redis and filesystem storage.
Native OpenSearch stayed at 0.55 CPU, 1,500 MiB RAM and a 640 MiB heap.
Cache and hit-stat writes remained disabled, matching the preceding comparison;
all measured reads used the classic native-discovery route.
All 50 reads targeted one prepared namespace. Writers used that namespace
and a second background-write namespace. The arrival timing is derived from
production; this concentrated namespace distribution is a diagnostic hotspot
case, not a replay of production's complete namespace/customer distribution.

The application, indexing harness and temporary PostgreSQL shared a separate
scope, normally capped at 0.55 CPU and 1,700 MiB. Two indexing executions ran
concurrently. Each intervention changed the indicated variable; the CPU
variant raised only this shared scope's measurement quota to 0.9 CPU. The
other chat's services were not reconfigured. Host headroom was checked before
each variant, with a stricter check for the CPU increase; test processes kept
their existing lower scheduling priority.

| Intervention | Lexical P95 | Complete classic pipeline P95 |
| --- | ---: | ---: |
| Baseline: 16 threads, sync cap 10, async cap 24 | 8.936 s | 9.320 s |
| Shared application/PG quota 0.55 to 0.90 CPU | 5.990 s | 6.148 s |
| Sync connection cap 10 to 30 | 8.911 s | 9.805 s |
| Async connection cap 24 to 100, initially cold | 8.637 s | 10.239 s |
| Default executor 16 to 32 threads | 10.032 s | 11.089 s |
| Async cap 100, pre-established and retained 50 connections | 10.104 s | 11.136 s |

All six variants completed without read or indexing errors. Index-task
completion includes verification of committed publication. The last row
isolates pool warmness relative to the preceding async-cap-100 configuration;
it does not isolate warmness relative to the baseline's smaller pool. The
warmer explicitly verified 50 retained connections before measurement.
Connection creation remains included in other rows. Native indexes and query
strings were warmed before every variant using the original protocol.

These are single repetitions with concurrent indexing and a shared host.
Small differences, such as 8.936 versus 8.911 seconds, are not evidence of a
stable performance gain. Pipeline time includes ingress, hydration, ranking,
assembly and finalization, but excludes HTTP authentication/routing and LLMs.
The one-second lexical gate remains unmet in every variant.

## Threads and pools

The probe distinguishes actual SQLAlchemy queue blocking from connection
creation and the entire pool-acquisition call. Pool capacities were observed
at runtime, rather than inferred from configuration alone. Queue wait is
recorded at the sync/async queue's `get`, while connection creation has its
own span. SQLAlchemy documents that
[pool size plus overflow determines the connection limit](https://docs.sqlalchemy.org/en/20/core/pooling.html#sqlalchemy.pool.QueuePool).

At baseline, the mean per-request wait to start the capture thread was
0.570 seconds and to start native discovery was 1.225 seconds. All 16 threads
were occupied at a sampled peak. Capture used approximately 4 ms of actual
thread CPU on average despite 428 ms of thread occupancy; native discovery
used approximately 9 ms despite 1.872 seconds of occupancy. Those threads
spent much of their wall time waiting on downstream work.

Raising the sync cap eliminated its measured queue blocking, while namespace
lock-query mean wall time increased from 0.510 to 0.666 seconds and later async
queueing increased. With 32 threads, mean per-request sync queue time grew
substantially despite lower executor queueing. This is why extra threads or
connections cannot be treated as extra processing capacity.

The following are mean accumulated waits per request, including zero for
requests that never waited in a particular queue. They are not P95 components.

| Variant | Executor queues | Sync pool queue | Async pool queue |
| --- | ---: | ---: | ---: |
| Baseline | 1.795 s | 0.254 s | 0.614 s |
| CPU 0.90 | 1.053 s | 0.016 s | 0.253 s |
| Sync cap 30 | 1.652 s | 0.000 s | 0.873 s |
| Async cap 100, cold | 1.373 s | 0.250 s | 0.000 s |
| Threads 32 | 0.813 s | 1.859 s | 0.802 s |
| Async cap 100, warm | 2.956 s | 0.654 s | 0.000 s |

## Locks and CPU

The blocking sampler correlated backend PIDs with read ordinals or writer
threads and SQL categories. It found:

- Readers waiting for publication writers at the namespace generation row,
  used to capture a consistent set of published revisions.
- Readers queueing behind other readers at `lexical_namespace_indexes`, where
  hot admission takes `FOR UPDATE`, validates the index/read lease and writes
  `lastUsedAt` while inserting index-use protection.

Some returned blocking PIDs are earlier waiters, not current lock owners.
PostgreSQL explicitly distinguishes these
[hard and soft blockers](https://www.postgresql.org/docs/15/functions-info.html#FUNCTIONS-INFO-SESSION).
Sampling establishes the blocking chain but is not an exact lock-time ledger.
Its repeated lock-manager queries also have instrumentation overhead.

At baseline, the client-observed namespace admission critical interval after
the lock query averaged 92 ms, versus approximately 2.7 ms of thread CPU.
With 0.9 CPU it averaged 27 ms. Namespace lock-query P95 fell from 1.249 to
0.467 seconds, and generation lock-query P95 from 0.773 to 0.042 seconds.
These client SQL intervals include scheduling and round trips; they cannot
all be labeled database lock time. The short transaction spans contain no
native HTTP request.

The cgroup recorded repeated throttling. Raising only its quota reduced
lexical P95 by approximately one third, supporting a material causal role for
this resource cap. Sampled scope throttled time decreased from 23.49 to
4.80 seconds across the 75-second workload; it is a cgroup counter, not time
to add to each request. The
[Linux CFS documentation](https://docs.kernel.org/scheduler/sched-bwc.html)
explains that a group exhausting its quota cannot run until replenishment.
This affects the process that must finish a transaction as well as the
requests waiting for it. The inherited sampler's process-tree PG CPU field
missed daemonized PostgreSQL processes; its zero values are excluded from
analysis. No conclusion relies on that invalid per-process counter.

A separate baseline-quota probe enabled `log_lock_waits` with a 10-ms
`deadlock_timeout` on the owned temporary PostgreSQL instance. This diagnostic
setting was not applied to production and adds logging/deadlock-check overhead.
All 50 reads and 40 indexing tasks completed without errors, with actual peak
50 arrivals. Server acquisition timestamps were matched to client SQL spans
by backend PID and a monotonic/wall-clock anchor. Exact containment takes
precedence; only a 1-ms fallback handles log timestamp precision. Raw SQL and
business values were not copied into the derived timing artifact.

Of 419 logged acquisitions after waiting, 369 matched read SQL. Repeated
acquisitions within one statement are separate events, not separate queries.
Read wait time totaled 35.578 backend-seconds at the namespace registry,
9.878 at the generation row and 0.121 at lease insertion. Thus approximately
78% of recorded read lock waiting was at the registry and 22% at generation
capture. These are sums across concurrent backends, not elapsed wall-time
percentages. The recorded per-read lock wait mean was 0.912 seconds, P95
1.922 seconds and maximum 2.072 seconds. Waits below the logging threshold
remain outside this ledger.

One actual request at this instrumented run's lexical P95 took 9.904 seconds:

| Non-overlapping interval | Time |
| --- | ---: |
| Default-executor queues | 3.493 s |
| Actual sync/async pool queue blocking | 3.946 s |
| Logged server lock waits | 0.909 s |
| Remaining SQL round trips | 0.667 s |
| Application, transactions and remaining scheduling | 0.743 s |
| OpenSearch BM25 HTTP interval union | 0.129 s |
| Native metadata HTTP and ingress | 0.016 s |

These intervals describe the same request and therefore can be added. It
created no new PG connection itself; it could still wait for connections
occupied by other requests. The run's mean connection-creation interval was
0.479 seconds per read. Remaining SQL includes execution, delivery, scheduling
and any short unlogged locks. The approximately 9.9-second instrumented run
is separate from the 8.94-second comparison baseline. The example establishes
where time went in an observed request, not a fixed universal decomposition.

## Production comparison

Read-only ECS/RDS descriptions showed the following configuration, without
running synthetic requests or SQL against production:

| Component | Observed configuration |
| --- | --- |
| API | `knowhere-api-prod:75`, two running tasks, 512 CPU units / 2,048 MiB each |
| API async pool | `DB_POOL_SIZE=50`, `DB_MAX_OVERFLOW=50` per process |
| Worker | `knowhere-worker-prod:71`, two running tasks, 2,048 CPU units / 4,096 MiB each |
| Worker concurrency/pool | gevent concurrency 10; sync pool 5 + 5 overflow |
| PostgreSQL | Independent Aurora PostgreSQL Serverless v2 instance, version 15.15 |

The live API task had no explicit lexical-backend or sync-pool override among
the inspected environment/secret key names. The new implementation remains
unreleased. The source inspected does not set the event loop's default
executor size; its live thread count was not introspected. The test's explicit
16-thread setting is not a verified production count.

Production differs materially from the experiment: its API has approximately
0.5 CPU per task, while workers and PostgreSQL have separate resources. In
the experiment, API, indexing work and PostgreSQL compete for the same
0.55-CPU budget. Its async pool cap is also 24 instead of 100. The read burst
is concentrated on one namespace; production traffic spread across namespaces
would have a different row-contention pattern. The Oracle
nine-second result therefore cannot be projected directly onto production.

The risk still applies: a small API CPU quota can slow caller progress, and
the candidate's namespace admission row is shared across API processes.
Adding replicas does not distribute that row's lock. A larger OpenSearch node
also does not increase API CPU or remove this SQL serialization. Production's
larger async pool is not sufficient evidence of acceptable tail latency.

Existing Logfire data was queried for the preceding 24 hours ending
2026-11-04 05:10 UTC. It returned six observed retrieval request spans across
v1/v2, without a qualified classic/native-load breakdown. This is a telemetry
sample, not a verified total request count. No existing production CPU,
pool or thread metrics were returned, and no matching pool-pressure/timeout
events were found in that query. Absence of those events does not establish
absence of waits or throttling. Current telemetry cannot verify that the
new chain would meet its latency gate online.

## Engineering consequence

Keep the simplified read implementation; do not raise thread or connection
limits as a demonstrated fix. Prioritize reducing the namespace admission
critical section and allowing parallel reader admission while preserving
exclusive close checks and durable index-use protection. Investigate moving
or coalescing the per-query LRU timestamp write so that it does not serialize
every read. Also review the amount of publication work performed under the
generation lock. These are follow-up implementation directions, not changes
made by this diagnosis.

Release qualification must exercise the complete classic API path with the
intended resource separation and production-shaped bursts. Observe CPU
throttling, actual queue blocking and lock chains together. This diagnosis
does not qualify the production host class or authorize additional spending.

## Evidence and cleanup

All seven variants completed 50 reads and 40 indexing tasks, with zero recorded
errors and no pending indexing work. Runtime pool-capacity observations match
each intended intervention. The seven candidate source files were verified
byte-identical to the preceding implementation's final source manifest. No
application implementation, migration, production setting, deployment or Git
commit changed. The preceding safety regression suite was not rerun because
these source files did not change.

Private probe code/hashes, frozen profile, raw timing events, aggregate details,
server-derived lock timings and source verification are retained under
`waits-d49e` in the isolated Oracle integration checkout. Reproduction uses
`run.sh` for the baseline and four capacity interventions, and `run-extra.sh` for warm-pool
and lock-log probes. All use the manifest from the preceding report.

| Variant | Timing event SHA-256 |
| --- | --- |
| Baseline | `536374e3c5c6155125ec952864292baf0a2840059a66f810dae41750524711df` |
| CPU 0.90 | `465e787aaf9f31551d90e747c5c4729125f89a217a9b99f5e41c72ae4958b462` |
| Sync cap 30 | `ef0c2eb314b14cac2a5ddcfa4c6e833c7c69b7f68a4bbaeaf70bb8d19035f05c` |
| Async cap 100, cold | `1a77e6a23b9719de6c15f2d2d2dc7f8a1d6aa29296fcab085504372a9090e7d7` |
| Threads 32 | `25dfd0511136f5481aaaa566f30741e3b997d8af127ced12d43ef83c88406ea5` |
| Async cap 100, warm | `c7394610363e08084a7e01f7e54fd0e649817445a1dee96f5fa412a9ac54f276` |
| Lock log | `dc3a5cc2c6354dc8d047b0dff320b868ce276ca6f6c9e16bc5a8ea73dab78a12` |

The owned container was stopped, temporary replay tests were archived outside
the normal test directory and the owned scope exited. Its temporary port
32780 stopped listening. The other chat's ports 18765 and 19203 still listen;
its services and checkout were not modified. Only cached images and existing
host resources were used, without parsing, paid model calls, object-storage
operations or telemetry export. No cloud resource or paid provider call was
added. Production inspection used control-plane descriptions and existing
Logfire data, without production synthetic load or direct database queries.
