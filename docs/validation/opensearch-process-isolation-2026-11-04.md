# OpenSearch process and CPU isolation replay — 2026-11-04

## Result

API, indexing, and PostgreSQL resource isolation is now exercised successfully
on the existing Oracle host without modifying business source. The final clean
run completes all 50 retrievals and 40 indexing jobs at an actual peak of 50
read arrivals per rolling second. Complete-pipeline P95 is **6.089 seconds**;
lexical P95 is **5.961 seconds**. The warm lexical P95 <= 1 second gate remains
unmet.

Isolation does not by itself produce a faster result in this experiment. A fresh
shared 0.55-CPU baseline reaches 7.324-second complete P95. Raising the shared
application budget to 1.05 CPU reaches 4.965 seconds. Splitting that same 1.05
CPU into API 0.55, indexing 0.30, and PostgreSQL 0.20 reaches 6.089 seconds.
These are single burst observations, not stable production percentiles or a
measurement of an optimal CPU allocation.

The preceding [HTTP attribution investigation](opensearch-http-wait-attribution-2026-11-04.md)
correctly identified a saturated shared budget. This replay narrows that finding:
more shared budget helps, and caller response consumption improves with
isolation, but the remaining delay cannot all be called API scheduling delay.
HTTP pool admission and native-side request/response handling remain material.

## Production configuration and bounded test topology

Read-only ECS and RDS descriptions were refreshed during this task. No synthetic
retrieval or SQL was sent to production.

| Component | Observed production configuration |
| --- | --- |
| API | `knowhere-api-prod:75`, two tasks; 512 CPU units / 2,048 MiB each |
| API async pool | 50 retained + 50 overflow per process |
| Worker | `knowhere-worker-prod:71`, two tasks; 2,048 CPU units / 4,096 MiB each |
| Worker concurrency / configured sync pool | concurrency 10; pool 5 + 5 overflow |
| PostgreSQL | Independent Aurora PostgreSQL Serverless v2, version 15.17 |

Oracle has two CPUs. The experiment reproduces independent processes and CPU
budgets rather than provisioning production-equivalent capacity:

| Component | Final isolated allocation |
| --- | --- |
| API retrieval plus in-process test driver/probes | 0.55 CPU; 1,700 MiB limit |
| Indexing executor process | 0.30 CPU; 700 MiB limit |
| Owned PostgreSQL server and its backends | 0.20 CPU; 500 MiB limit |
| Owned OpenSearch 3.9.0 node | 0.55 CPU; 1,500 MiB limit; 640 MiB heap |

The combined configured CPU ceiling is 1.60, compared with 1.10 in the original
shared-baseline experiment. The shared-1.05 control has the same 1.60 total
ceiling, including OpenSearch. It prevents attributing all baseline improvement
to isolation when more aggregate budget is also available. CPU allocation among
components still differs: the shared group can use spare budget across roles;
the split group has independent hard ceilings.

PostgreSQL was already a separate operating-system process in the old harness,
but shared the application CPU budget. API and indexing previously shared a
Python process. The final run moves indexing to another Python process and has
systemd adopt worker/PG PIDs into separate scopes, including CPU, memory, and
systemd ownership. The observer verifies live quota files and checks process
membership during the captured workload, including new PostgreSQL backends.
The final capture contains **183 membership-check samples**, with nonzero CPU
accounting in every active component group.

The indexing proxy retains two active jobs and one staging executor. It invokes
the existing success-finalization and `IndexingWorker.run` implementations.
It does not start a Celery broker/gevent worker; their production bootstrap and
dispatch behavior are not qualified here. The API is called through the same
in-process `run_retrieval_query` seam as before, not an external HTTP client.

## Frozen workload and validity

All primary runs retain the same 75-second profile and prepared corpus: 18
queried documents / 1,727 initial units, a seeded background namespace, 50 reads,
40 writes, and one changed-body replacement. Read arrivals span 422, 429, and
422 ms respectively. The 50-QPS figure is a rolling-second **burst peak**, not
75 seconds of sustained 50-QPS traffic.

API async pooling remains 12 + 12 overflow. Native HTTP limits remain 32 active
connections / 20 retained idle connections. Query cache reads and hit-stat
side work remain disabled as in the original diagnostic. Ranking, candidate
limits, locking, version binding, and index-close/cleanup rules are unchanged.
Prepared inputs avoid parsing and embedding calls; the legacy PG frequency
copy is not part of the workload.

Each primary run verifies native sync/async Top 20 parity for all 20 fixture
queries before the timed burst. This is transport parity, not a new PG/native
ranking-quality acceptance. Each native path/content channel has a unique tag;
all **100 channels per run** match application traces and both private packet
captures, with zero kernel packet drops. All 40 writes finish before the
profile ends with an empty backlog. Normal reads execute five captured SQL
statements each.

The final clean split run starts after removal of four private indexes left by
interrupted experiments, restoring the test's two active fixture indexes.
Internal OpenSearch plugin indexes are retained. The earlier successful split
run with four extra idle fixture indexes is retained as `split-extra-idle`:
its complete P95 was 5.626 seconds. It is not the primary comparison result.

## Performance comparison

| Measurement | Shared 0.55 CPU | Shared 1.05 CPU | Split 0.55 / 0.30 / 0.20 CPU |
| --- | ---: | ---: | ---: |
| Successful reads | 50/50 | 50/50 | 50/50 |
| Successful writes | 40/40 | 40/40 | 40/40 |
| Complete-pipeline median | 5.528 s | 2.968 s | 4.537 s |
| Complete-pipeline P95 | 7.324 s | 4.965 s | 6.089 s |
| Lexical P95 | 7.190 s | 4.917 s | 5.961 s |
| HTTP pool queue P95, per channel | 3.393 s | 1.968 s | 3.261 s |
| Connection/setup trace P95 | 746 ms | 290 ms | 298 ms |
| Native full-request receipt to first response byte P95 | 1,942 ms | 1,562 ms | 2,000 ms |
| OpenSearch response `took` P95 | 1,161 ms | 393 ms | 840 ms |
| Client full-body wire arrival to consumption callback P95 | 292 ms | 75 ms | 70 ms |
| PG checkout P95, per checkout | 603 ms | 412 ms | 466 ms |
| PG SQL P95, per statement | 495 ms | 230 ms | 314 ms |
| Hydration P95 | 777 ms | 127 ms | 81 ms |
| Result assembly P95 | 332 ms | 91 ms | 70 ms |
| Write age P95, including queue/staging/indexing | 24.165 s | 16.085 s | 33.028 s |

These are independent distributions; **their percentiles cannot be added**.
The native `took` interval includes coordinator/search queueing and execution,
not just Lucene CPU, and excludes request/response transmission.
[Official Search API](https://docs.opensearch.org/latest/api-reference/search-apis/search/)

The split allocation improves several caller stages over the original small
shared group. However, it does not beat the shared-1.05 result, and its write
queue drains more slowly under the small worker/PG ceilings. This comparison
does not identify an optimal allocation or separately quantify GIL, CPU quota,
host scheduling, native queueing, compression, or socket backpressure effects.

## Remaining delay in one split-run request

Request 36 is the final split run's nearest-rank P95. Its content channel
finishes last. The following intervals belong to that same request and sum to
its 6,088.6-ms total; the parallel path channel is not added again.

| Critical-path interval | Time |
| --- | ---: |
| Arrival through submission of the last-finishing native channel | 1,338.0 ms |
| Logical HTTP pool queue | 3,000.2 ms |
| Request preparation, assignment resumption, connection/setup and sending | 109.3 ms |
| Native receives full request through final response byte on native wire | 1,439.4 ms |
| Response forwarding, caller body consumption and native method return | 43.6 ms |
| Remaining caller work and scheduling | 158.1 ms |
| **Total** | **6,088.6 ms** |

Within the native interval, first response byte takes 1,164.9 ms and the remaining
response stream takes 274.5 ms. The response's `took` is 707 ms, nested inside
this interval. Native ingress/queue/execution, response preparation, and native
response scheduling are not individually separated by these probes.

The 3.000-second pool queue is a measured waiting location. Earlier requests
retain connections through native handling and response consumption; it is not
an independent estimate of API CPU cost. The final run establishes neither that
32 connections are inherently insufficient nor that OpenSearch's uncapped
execution would take these seconds.

## CPU accounting

Counters cover each run's complete read-cohort window. Shared application
counters include API, indexing, test driver and PG; final API counters include
API plus its driver/probes. They are not API-only CPU measurements in all three
columns.

| Measurement | Shared 0.55 | Shared 1.05 | Final split |
| --- | ---: | ---: | ---: |
| Application/API group CPU consumed | 3.918 CPU s | 4.237 CPU s | 2.229 CPU s |
| Application/API mean cores | 0.504 | 0.815 | 0.354 |
| Application/API periods with throttling | 63/78 | 22/52 | 13/61 |
| Native mean cores | 0.417 | 0.413 | 0.348 |
| Native periods with throttling | 52/74 | 31/51 | 22/59 |

Final separate indexing accounting is 0.911 CPU seconds, mean 0.145 cores,
with throttling in 19/39 periods. PG accounting is 0.706 CPU seconds, mean
0.112 cores, with throttling in 15/57 periods. The API is no longer persistently
exhausting a budget shared with those components, but each small quota can still
limit bursts. Lower average CPU does not rule out burst throttling or waiting
on another component.

CFS quotas apply to all runnable work within their group. The fraction of
periods experiencing throttling is not the fraction of elapsed time spent
waiting. Cumulative group throttle counters are not assigned directly to
individual requests.
[Linux CFS bandwidth documentation](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)

## Harness corrections and limits

Three unsuccessful isolation attempts are explicitly excluded:

- `split-invalid`: a redundant pre-profile API quota reset reassigned manually
  moved processes; supposed worker/PG groups consumed zero CPU.
- `split-incomplete`: runtime verification stopped on a PG backend membership
  mismatch. The recorded assertion alone does not establish migration of an
  active backend into the API CPU group. The later
  [native wait investigation](opensearch-native-wait-attribution-2026-11-04.md)
  reproduces process-teardown reporting of root legacy cgroups and corrects the
  guard to exclude kernel-marked exiting tasks. This interrupted run remains
  excluded.
- `split-startup-failed`: systemd rejected process attachment without delegation;
  setup stopped before the timed profile. A small scope check confirmed the
  requirement before the final method was used.

The final helper uses systemd transient scopes with explicit delegation and
process adoption, rather than changing only CPU-controller membership. The
redundant quota reset is removed from the temporary split harness. It was
redundant in the two shared controls, where the intended shared quota remained
unchanged. No business source changed during these corrections.

The child resets inherited SQLAlchemy pools with `dispose(close=False)`.
[SQLAlchemy multiprocessing guidance](https://docs.sqlalchemy.org/en/20/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork)
Python still emits its multithreaded-parent fork warning. This temporary proxy
completed successfully, but is not a proposed production worker bootstrap.

The host is shared. Runs are serial, bounded by explicit quotas and idle checks;
the clean repeat waited for an idle window. No load targets the other chat's
services. Host noise, JVM history, different CPU allocation among roles, and
one burst per primary configuration limit causal precision. Memory pressure,
production API replicas/pools, full parsing, true Celery/gevent execution,
400-index turnover, sustained throughput and production target hardware remain
separate acceptance work. No production latency promise follows from this test.

## Evidence and cleanup

Raw timing records, private captures, excluded-run markers, source hashes,
resource-adoption records and scripts are retained at
`<lab-home>/knowhere-readfree-r8k2/isolate-v2d7`. Primary summaries are
`comparison.json` and each primary directory's `verified-attribution.json`.
Non-capture evidence is copied locally under
`/tmp/knowhere-isolate-v2d7/oracle-artifacts`; capture files remain private on
Oracle with mode 0600.

Before/after hashes confirm all 37 monitored business files unchanged locally
and on Oracle. The temporary test is archived outside normal test discovery.
The owned native container `knowhere-isolate-v2d7` is stopped and removed;
port 32791 is released and its scopes are inactive. The other chat's native
container retains ID
`b45d654ecd836e28ded8e39f47b8cd6012049e426eebb5a9a8ebc7853c0401b0`
and start time `2026-11-03T07:36:19.527916941Z`; its 19203 listener and application
PID 1670349 on 18765 remain present.

No new VM, volume, bucket, paid parsing/model request, or production deployment
was created. The cached OpenSearch image was reused with `--pull=never`, storage
was local, and telemetry export was disabled. Independent CPU/process isolation
is verified; the performance gate remains open.

## Follow-up: native queues and response cost

The [native wait attribution](opensearch-native-wait-attribution-2026-11-04.md)
reproduces 6.088-second P95 with tracked search-task CPU P95 of 9.31 ms. During
the representative pool wait, 85.65% of occupied connection-time waits for the
native first response byte. Disabling response compression at the same quotas
reduces P95 to 4.886 seconds but triples transmitted bytes. Lowering only native
CPU to 0.30 raises P95 to 8.292 seconds and mean executor queue time to 402 ms.
Offline response analysis finds 34.4 MB of returned indexing token strings;
omitting those two response fields is the next implementation candidate, not
an implemented or latency-qualified change. Business code remains unchanged.
