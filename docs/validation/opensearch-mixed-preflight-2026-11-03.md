# Native BM25 mixed-load preflight on Oracle

Date 2026-11-03. The warm lexical latency gate failed in the resource-limited
Oracle preflight. The indexing recovery gate passed. The one-hour qualification
run remains pending.

The owner selected native Lucene BM25 for the first release and accepted the
relative PostgreSQL evidence gate. The accepted workload is ten times the
observed production burst shape with a 50-request rolling-second peak. The
warm lexical P95 limit remains one second and indexing catch-up remains five
minutes.

## Workload and isolation

- Used existing Logfire records from October 19 05:50 UTC through November 2
  05:50 UTC. The frozen query recovered 354 API retrieval request timestamps.
  Worker publication logs supplied 244 timestamps and chunk counts. Worker
  graph publication is a post-parse timing and size proxy.
- Preserved the selected streams' intra-burst timing and replicated each
  arrival ten times. The initial read burst and its preceding publication
  came from October 27 02:43:30 UTC. Independently overlaid the observed
  three-publication burst from October 27 17:32:53.794 UTC before that read
  peak. The next five minutes used a separate observed ordinary-flow segment.
  This deliberately aligns peak streams. It is a short mixed preflight,
  with unchanged intervals within each selected burst.
- Measured for 390 seconds after fixture setup and warmup. Sent 150 reads
  using the frozen 20 business query strings and 60 prepared post-parse
  indexing tasks. One indexing task replaced an existing document.
- Every read exercised classic lexical discovery with result caching and
  hit-stat updates disabled. The initial read namespace had 1,727 lexical
  units from the same 18-document fixture. A second warm namespace received
  most new documents, and some writes used the read namespace.
- Used real publication and indexing-worker code with a local application
  queue and two indexing executions. Template input sizes came from the
  publication chunk-count proxy. The test wrote 1,547 additional lexical
  units. Wire payload bytes were not measured.
- Oracle has two logical CPUs and about 11.66 GiB RAM. A dedicated OpenSearch
  3.9.0 container was capped at 0.55 CPU and 1,500 MiB RAM, with a 640 MiB
  JVM heap. A separate systemd scope capped the replay application and
  temporary PostgreSQL processes at 0.55 CPU and 1,700 MiB RAM.
- Reused the cached image and prepared data. Used loopback connectivity,
  fake Redis and filesystem storage. The comparison chat's service and
  indexes were outside the run. No cloud resources or paid provider calls
  were created.

## Measurements

| Measurement | Observed |
| --- | --- |
| Actual rolling-second retrieval arrivals | 50 |
| Initial 50-read arrival span | 0.4215 s |
| Peak indexing arrivals in a rolling second | 30 |
| Retrieval completions | 150 of 150 |
| Retrieval errors | 0 |
| Actual lexical executions | 150 |
| All-read lexical P50 / P95 / maximum | 7.12 / 14.25 / 15.41 s |
| Initial 50-read lexical P95 | 15.21 s |
| Later 30 / 40 / 30-read lexical P95 | 6.01 / 8.71 / 7.61 s |
| Complete classic request P95 | 15.51 s |
| Indexing completions with committed published revision | 60 of 60 |
| Final indexing errors | 0 |
| Indexing admission-to-readiness P95 / maximum | 29.59 / 30.70 s |
| Peak unfinished indexing jobs | 30 |
| Eligible cohort at the initial burst boundary | 30 jobs |
| Cohort recovery after that boundary | 28.594 s |
| OpenSearch search / write thread-pool rejections | 0 / 0 |
| Peak JVM heap | 73% |
| Recorded GC time increase | 659 ms |

The CPU quota throttled the application/PostgreSQL scope during the test.
OpenSearch reported no search or write queue rejections. These observations
bound this run and do not isolate one complete root cause for the latency.

A subsequent [stage-level diagnosis](opensearch-latency-breakdown-2026-11-03.md)
reproduced the initial burst and isolated executor waiting, PostgreSQL
connection acquisition and coordination round trips as the dominant delay.
Increasing only the application/PostgreSQL quota improved latency, while
direct-native OpenSearch on the limited node also remained above the target.

## Acceptance and remaining work

The one-second warm lexical gate failed, so the one-hour run was deferred.
All eligible indexing work at the burst boundary became search-ready within
five minutes while later arrivals continued.

This preflight covers two already-open namespace indexes. The 400-index LRU
mix, the largest approximately 45,000-unit namespace, indexing retries,
maintenance and remote durability settings need additional load coverage.
The agreed 4-vCPU/32-GiB host with its 16-GiB heap and remote durability
configuration remains the production qualification baseline.

Pytest reported one pass in 483.67 seconds, including setup, warmup and the
390-second run. This is completion of the test harness. The performance
acceptance failed as stated above. The temporary container and database were
cleaned up.

Private replay inputs, raw events, node snapshots, summary, and the test
script are retained under the isolated Oracle checkout in
`mixed-c661`.

- Event SHA-256 `0c227a4ab8ec8401a3dccf11bad73ca2226300342de422e48ff5172438d3e11e`
- Profile SHA-256 `fd1db2a7bfcc1de37c495a37971740ed8ed11fb3d30f4dd58cd9b51dc589db3b`
