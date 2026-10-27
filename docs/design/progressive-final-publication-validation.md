# Progressive final publication: consistency and capacity validation

Date: 2026-10-26 Pacific / 2026-10-27 UTC.

## Release decision

**Do not release against the currently observed production state.** Local
consistency validation passes after the fixes described below. Production has a
separate deployment failure and only one of two desired Worker tasks is running.
The measurements do not establish end-to-end progressive ingestion capacity on
the production deployment.

No production configuration, secret, database, deployment or workload was changed
by this investigation. The secret failure was observed on the already configured
ECS deployment; there is no evidence that these unshipped changes caused it.

## Production baseline

Read-only AWS inspection at `2026-10-27T02:39:55Z` found:

| Component | Configured resources | Observed running state |
| --- | --- | --- |
| API | 0.5 vCPU, 2 GiB per task; 20 GiB scratch; desired 2 | 2 old tasks, definition 62; replacement definition 63 has no running tasks |
| Worker | 2 vCPU, 4 GiB per task; 21 GiB scratch; gevent concurrency 10; desired 2 | 1 old task, definition 58; replacement definition 59 has no running tasks |
| Database | Aurora PostgreSQL 15.15; one Serverless v2 writer; 0–4 ACU; auto-pause 300 seconds | Available; capacity and CPU have reached their configured/metric ceilings |

Replacement API and Worker tasks fail during secret initialization because the
referenced secret JSON has no `VERCEL_KEY`. The running services therefore do
not represent the proposed progressive build. Repairing this requires a separate
authorized production change.

A final service inspection at `2026-10-27T03:48:16Z` found the same running counts:
replacement API/Worker definitions had zero running tasks and 74/84 failed-task
attempts respectively. These counters describe startup attempts, not failed jobs.

The API declares a 50+50 connection pool per process; the synchronous Worker
declares 5+5. These are upper bounds, not observed connection demand. CPU-based
Worker autoscaling is configured for 2–20 tasks, with a 35% CPU target; API
autoscaling is 2–4 tasks, with a 70% CPU target. Autoscaling neither repairs the
startup failure nor guarantees proportional capacity when database/provider
limits dominate.

CloudWatch window: `2026-10-26T02:39:55Z`–`2026-10-27T02:39:55Z`, 288 five-minute
buckets. Averages below are the mean of bucket averages; peaks are the maximum
of bucket maxima.

| Metric | Average | Peak |
| --- | --- | --- |
| API CPU / memory | 0.87% / 20.37% | 58.67% / 24.51% |
| Worker CPU / memory | 3.30% / 17.83% | 98.96% / 42.24% |
| Aurora CPU | 10.64% | 100% |
| Aurora capacity | 0.60 ACU | 4 ACU |
| Database connections | 19.66 | 25 |

Ten buckets reached 4 ACU, four had a database CPU maximum of at least 90%, and
three had a Worker CPU maximum of at least 90%. This is not evidence of sustained
saturation throughout those buckets, or proof that a particular parse caused it.
CloudWatch reported zero deadlocks. Low average utilization alone does not prove
enough headroom for a burst of final publications.

Production-only Logfire request/task spans for
`2026-10-26T02:40Z`–`2026-10-27T02:40Z` show 37 successful `/v2/jobs` admissions,
37 document-ingestion task spans (p95 317.03 seconds, maximum 2,497.60 seconds),
and 15 successful `/v1/retrieval/query` spans (p95 20.19 seconds). The last value
includes an unknown retrieval/provider mix and is not a BM25 benchmark. This is
observed demand, not measured saturation throughput. It contains no representative
deployed progressive campaign.

ECS Exec failed with `TargetNotConnected` on both running API tasks. Consequently
live pending/running job counts, database pool limits, lock waits and task-level
progressive resource attribution were not verified through SQL. No production
service was restarted to restore that access.

## Concurrent retrieval correctness

The starting implementation, commit `851da937`, failed a deterministic contract
using real PostgreSQL and the actual `run_retrieval_query` BM25 path. A final
publication committed between map-unit/token/hydration reads of the same
JobResult. A known matching query returned a successful empty result. An atomic
write transaction by itself did not protect a multi-statement read. PostgreSQL's
default READ COMMITTED isolation takes a fresh snapshot per statement; see the
[official transaction-isolation reference](https://www.postgresql.org/docs/15/sql-set-transaction.html).

The same-revision replacement is now published with the namespace serving
generation. Finalization acquires the namespace lock before the document lock,
patches the namespace map snapshot, and increments the generation in the same
transaction as content/index changes, final state and the webhook outbox event.
A rollback does not advance the generation.

Before returning an uncached retrieval result, execution validates its captured
generation after the entire route, including an Agent episode. A changed
generation discards the result, rolls back the read transaction, clears decoded
manifest state, and retries once. Another change produces the existing explicit
409 conflict (`ABORTED`) instead of partial success. A database validation error
fails closed. Required visual query-embedding failures still propagate.

This works across processes because the fence is persisted in PostgreSQL. It
does not depend on process-local mutexes or a long transaction spanning provider
waits. The generation check adds one SQL read per uncached attempt; a collision
can run the full route, including an Agent/provider episode, a second time.
Frequent publications in the same namespace can therefore increase latency,
provider cost and explicit conflicts. The retry count is bounded.

The contract matrix forces publication at three boundaries: BM25 unit reads,
hydration reads, and real `corpus.recall` followed by Agent final assembly.
For each boundary it verifies stable baseline, one mutation returning the final
version, and repeated mutation failing explicitly. External Agent generation is
mocked; storage, publication and retrieval reads are real. Additional contracts
cover strict database-validation failure and an empty namespace with an existing
generation. Existing Worker contracts verify one generation advance on successful
finalization and no extra advance after rollback/retry.

This change covers newly admitted revisions using final-only publication.
Compatibility for old in-flight incremental revisions preserves their existing
publication behavior; their scope writes were not converted to this generation
protocol here. Do not extend the new consistency guarantee to that legacy path.
Deployment acceptance must inventory remaining legacy work or handle it separately.

Redis response caching keeps its existing version invalidation and TTL behavior.
The forced-race and load campaigns bypass response-cache hits to exercise SQL.
They establish coherent uncached responses, not instantaneous cache freshness
during a Redis outage.

## Resource measurements

The local campaign uses an isolated PostgreSQL 15 database with pgvector, real
publication/map-unit construction and real classic BM25 retrieval. All writers
share one namespace, so they exercise namespace publication serialization.
The query searches that namespace for a protected canary document while other
documents are republished. It must return exactly its known evidence chunk.
This load campaign complements the forced same-document races above.

Fixtures were inspected from this checkout, rather than an older frozen corpus:

| Fixture | Physical source pages | Stored chunks | Stored content bytes |
| --- | --- | --- | --- |
| `stem-transformers-tutorial` | 187 | 301: 138 page, 161 image, 2 table | 56,575 |
| `spacex-s1` | 407 | 227: 1 page, 94 image, 132 table | 1,486,552 |

Replicas receive unique chunk identities and paths. Four technical replicas make
1,204 chunks; four dense replicas make 908 chunks, about 5.67 MiB of content.
Replicated fixtures are synthetic stress inputs, not independently parsed large
documents. Physical page counts must not be inferred from page-chunk counts.

### Native diagnostic run

The publisher and reader ran on an ARM64 Mac (10 CPUs, 16 GiB), without matching
production CPU quotas. PostgreSQL ran in Linux with 1.5 CPU and 1.5 GiB limits.
The publisher imported the Worker enrichment module but did not run OCR, models,
archive download/assembly/upload, or a full ingestion. Query cache, provider work
and hit-stat writes were bypassed. Results are diagnostic, not production capacity.

The six phases issued 394 queries at three requests/second: 394 successes, zero
conflicts and zero errors. Each phase lasted at least 15 seconds and until its
publication process ended; phase percentiles include idle tail time and warm
caches and must not be interpreted as a controlled interference ratio.

| Publication batch | Batch wall time | Publisher process peak RSS | Phase query p95 |
| --- | --- | --- | --- |
| No publication | — | — | 31 ms |
| 1 × 301 chunks | 0.82 s | 241 MiB | 85 ms |
| 5 × 301 chunks | 4.17 s | 250 MiB | 196 ms |
| 10 × 301 chunks | 6.09 s | 261 MiB | 18 ms |
| 10 × 1,204 chunks | 18.41 s | 367 MiB | 21 ms |
| 5 × dense 908 chunks | 47.58 s | 504 MiB | 50 ms |

The non-monotonic query percentiles reflect the campaign's cache/data/phase
conditions; they do not imply that more publication concurrency improves reads.
Namespace writers serialize. A dense final publication can hold the publication
lock for materially longer than a small fixture, delaying other writes in that
namespace. Readers use MVCC rather than taking that writer lock.

### Quota-limited Linux run

The second campaign runs on Linux ARM64 with cgroup limits of **2 CPUs / 4 GiB
for the publisher** and **0.5 CPU / 2 GiB for the reader**, matching the configured
per-task CPU/memory quotas. The local PostgreSQL container has 1.5 CPUs / 1.5 GiB;
an isolated Redis container handles publication invalidation. This is a publication
core probe, not the exact ECS image or Aurora: parser/OCR dependencies and Worker
startup are not loaded, external models and archive work are bypassed, and only
one publisher process plus one reader process is measured. Gevent concurrency
models publication slots, not separate deployed Celery instances.

The six phases issue 427 uncached queries at three requests/second: 281 exact
canary queries and 146 `replica canary` queries. The latter combines a path token
present in thousands of units with an invariant evidence token, exercising broad
postings without assuming an arbitrary query must match after content replacement.
All 427 returned success, with no missing canary, conflict or service error. Six
generation changes were detected and retried. Across phases, canary p95 is 366 ms;
broad-query p95 is 737 ms and maximum 1.96 seconds.

The overlap column includes requests started while the publisher process was
alive, including its startup; it excludes the minimum-duration idle tail. These
small, ordered, warm-cache phases do not locate the saturation threshold.

| Publication batch | Batch wall time | Publisher process peak RSS | Overlap queries / p95 |
| --- | --- | --- | --- |
| No publication | — | — | 46 total queries / 356 ms phase p95 |
| 1 × 301 chunks | 1.87 s | 175 MiB | 10 / 422 ms |
| 5 × 301 chunks | 8.87 s | 193 MiB | 30 / 1,149 ms |
| 10 × 301 chunks | 10.55 s | 184 MiB | 35 / 258 ms |
| 10 × 1,204 chunks | 25.37 s | 228 MiB | 79 / 630 ms |
| 5 × dense 908 chunks | 52.90 s | 443 MiB | 162 / 437 ms |

Reader cgroup peak usage was 137 MiB. Publisher-container peak usage was 1,611 MiB,
including dependency-install/file-cache history; it is not equivalent to the
443 MiB publication-process RSS. None of the four containers recorded an OOM or
OOM kill. The small database reached its memory limit and performed reclaim;
it recorded no deadlocks or temporary-file bytes. The campaign generated about
845 MiB of PostgreSQL WAL across 31 final replacements. Database size fell by
about 11 MiB while maintenance reclaimed earlier campaign data, so that size
delta must not be used as a per-document storage estimate.

The publication core fits these per-task quotas for these inputs. Index rebuilding
still has substantial database write cost. This does not prove that 4 GiB covers
parsing plus archive assembly, that two Workers fit Aurora's 4 ACU ceiling, or that
different namespaces publishing concurrently have the same serialized write load.

An initial extra-query probe incorrectly required `report` to match at every
stage. SQL inspection showed no path postings for that token and content postings
only in the dense fixture, which was temporarily absent. Its harness-generated
empty-result assertions are retained as an invalid probe, not counted as service
failures or as final acceptance evidence. The corrected campaign above was rerun
from the beginning.

## Automated checks and evidence

Selected regression checks after the fix: 38 API contracts, 60 Worker contracts,
and 47 shared lifecycle/provider/harness tests passed (145 total). `make check`
passed, as did explicit Pyright checking of the new concurrency contract. Existing
deprecation/configuration warnings remain. This is a focused validation set,
not a claim that the complete repository suite ran.

Retained local evidence is under `.benchmarks/progressive-final-20261027/` in this
worktree: `concurrency-before.txt`, `infrastructure.json`,
`infrastructure-refresh.json`, `infrastructure-final.json`, `production-workload.json`,
`measurements-native.json`, `measurements-linux.json`, per-batch publication JSON,
and `resources-before.json` / `resources-after.json`. The directory also retains
the isolated harnesses and the invalid initial probe. It is ignored by Git;
the evidence summaries in this report are tracked. Benchmark containers are
stopped after collection; their data and artifacts are retained.

## Acceptance still required

1. Restore healthy production replacement tasks and the two-Worker baseline;
   verify the intended image/configuration and required migrations/extensions.
   Inventory legacy incremental revisions before extending the new guarantee to
   traffic that may overlap those writes.
2. Run the exact deployable image with production-equivalent quotas, real
   archive/vector publication, representative native/scanned/PPTX inputs, and
   continued new admissions after baseline completion. Include multiple Worker
   processes, provider limits, queue delay, connections, lock waits, temporary
   disk, WAL/I/O and memory over a sustained campaign.
3. Verify final publication alongside deployed classic and Agent queries and
   the two webhook deliveries, then repeat Hipo acceptance with the published,
   pinned compatible SDK. Local contract tests do not replace this boundary.

These are validation gates, not a request for another ingestion parameter or a
new rollout mechanism. There is currently no evidence justifying an automatic
Worker resize, nor sufficient evidence to approve the current configuration for
the complete progressive workload.
