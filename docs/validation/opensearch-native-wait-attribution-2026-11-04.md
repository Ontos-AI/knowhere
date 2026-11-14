# OpenSearch native wait and connection occupancy — 2026-11-04

## Finding

The six-second result is reproduced. The long HTTP pool wait is primarily a
consequence of occupied connections waiting for the native service, rather
than requests simply waiting for an API thread to run. On the native side,
search tasks consume milliseconds of tracked CPU, while executor queueing,
shared network/HTTP work, JIT compilation, and a small CPU quota stretch elapsed
time. The new integration also returns large indexing-token fields that the
retrieval caller does not subsequently use.

Three process-isolated mixed-load runs complete 50/50 reads and 40/40 indexing
jobs each, at the same actual rolling-second peak of 50 read arrivals. No
business source is changed. The warm lexical P95 <= 1 second gate remains open.

## Controlled configurations

All runs use the existing two-CPU Oracle host and one owned OpenSearch 3.9.0
node on loopback port 32793. API/driver CPU remains 0.55, the indexing proxy
0.30, and the owned PG server/backends 0.20, in independent scopes. Native
memory remains 1,500 MiB with a 640 MiB heap. API async pooling remains 12 + 12;
native HTTP pooling remains 32 active / 20 retained idle connections. Two active
indexing jobs and one staging executor remain unchanged.

| Run | Native CPU ceiling | Timed search response encoding |
| --- | ---: | --- |
| `gzip055` | 0.55 | Existing gzip negotiation |
| `identity055` | 0.55 | Temporary `Accept-Encoding: identity` |
| `gzip030` | 0.30 | Existing gzip negotiation |

The first two cases change response encoding only. The last case restores the
original encoding and lowers only the native CPU quota. No quota is raised.
The same native JVM is retained across these ordered runs; allocated processors,
search executor size and the shared network worker count remain unchanged.
JIT history and host noise limit precise causal estimates from a single pair.

The frozen 75-second profile, prepared corpus, changed-body replacement, 50
reads and 40 writes match the previous
[process-isolation replay](opensearch-process-isolation-2026-11-04.md). This is
a burst peak, not sustained 50 QPS for 75 seconds. Parsing, embeddings, legacy
PG frequency copying and paid providers are not exercised. Production replicas,
Celery/gevent dispatch and the proposed production node are not reproduced.

## Measurement and validity

The existing client traces and paired packet captures are extended with:

- HTTPcore request-removal markers, connection identities and useful assignments,
  to measure which stages occupy connection capacity while another request waits.
- Native `phase_took` responses and Query Insights latency/CPU records joined
  using the existing unique `X-Opaque-Id` header.
- Native search executor queue/active/completed/cumulative-wait counters and
  shard query/fetch counters, sampled independently of the API quota.
- JVM thread names from a pre-profile thread dump, matched to host thread IDs;
  `/proc/.../schedstat` execution counters attribute CPU across search, shared
  network, JIT, GC and other threads.
- Live CPU quota/accounting and active-process membership verification, including
  new PG backends. The observer uses the same isolated-process protocol as the
  preceding replay.

All 100 path/content channels per case match both packet captures with zero
kernel packet drops. Raw body lengths match HTTPX download counts. Native CPU
records match 100/100, 99/100 and 100/100 channels respectively. The missing
identity-case record is an observability coverage limit; all business requests
and all wire captures succeed. CPU percentiles for that case use 99 records.
Top-N collection and node stats have overhead; these are diagnostic observations,
not production latency qualification. The expensive Search Profile API is not
used.

[Query Insights CPU and phase metrics](https://docs.opensearch.org/latest/tuning-your-cluster/monitoring/query-insights/top-n-queries/),
[OpenSearch 3.9 executor counter implementation](https://raw.githubusercontent.com/opensearch-project/OpenSearch/3.9.0/server/src/main/java/org/opensearch/threadpool/ThreadPoolStats.java)

## Results

| Measurement | Gzip / 0.55 CPU | Identity / 0.55 CPU | Gzip / 0.30 CPU |
| --- | ---: | ---: | ---: |
| Successful reads / writes | 50 / 40 | 50 / 40 | 50 / 40 |
| Complete-pipeline P95 | 6.088 s | 4.886 s | 8.292 s |
| Lexical P95 | 5.923 s | 4.656 s | 8.249 s |
| HTTP pool wait P95 per channel | 2.264 s | 1.232 s | 3.916 s |
| Native full-request receipt to last response byte P95 | 1.798 s | 0.281 s | 3.161 s |
| Reported search-task CPU median | 6.61 ms | 5.89 ms | 6.14 ms |
| Reported search-task CPU P95 | 9.31 ms | 9.07 ms | 9.91 ms |
| Native response `took` P95 | 906 ms | 245 ms | 1,927 ms |
| Search executor mean queued time per completed task | 141 ms | 24 ms | 402 ms |
| Observed peak search executor queue | 29 | 7 | 30 |
| Native periods experiencing throttling | 26/65 | 11/48 | 56/86 |
| Transmitted search body bytes | 13.22 MB | 40.75 MB | 13.22 MB |

MB means decimal MB. These are separate distributions and must not be added
to manufacture an end-to-end percentile. `took` includes coordinator/search
queueing and search work; it is not CPU time and excludes HTTP transmission.
[Official Search API](https://docs.opensearch.org/latest/api-reference/search-apis/search/)

The queue counters bracket the read cohort using a response completed before
the first arrival and a stats request started after the final read completed.
The brackets include 103/102/102 node searches, versus 100 traced business
channels, and 206/204/204 executor tasks. Thus the queue means include a small
amount of background work and are not exact per-business-request queue times.
There are no executor rejections. Wider snapshots after Query Insights reads
include additional internal searches and are deliberately not used for this
table.

Lowering only native CPU makes wall latency and executor queueing worse while
tracked search CPU remains similar. This establishes sensitivity to the native
quota and its queue, not just an API scheduling problem. The later run actually
has less sampled JIT CPU than the baseline, so increased JIT work does not explain
the degradation. It does not establish an exact unlimited-CPU counterfactual.

Disabling compression reduces native network-thread CPU and native wire latency,
but triples bytes delivered to the caller and moves some connection occupancy
into caller receiving/consumption. It is a diagnostic intervention, not an
accepted permanent configuration change or a complete performance fix.

## Why requests wait for an HTTP connection

In the gzip baseline, the same nearest-rank P95 request totals **6,087.9 ms**:

| Critical-path interval | Time |
| --- | ---: |
| Arrival through submission of its last-finishing channel | 1,958.1 ms |
| Logical HTTP pool wait | 2,203.4 ms |
| Preparation, assignment resumption, connection and request transmission | 213.8 ms |
| Native full-request receipt through final response byte | 1,503.7 ms |
| Response forwarding, consumption and native method return | 17.5 ms |
| Remaining caller processing and scheduling | 191.4 ms |
| **Total, rounded** | **6,087.9 ms** |

This is a new instance reproducing the previous 6.09-second P95, not the identical
request from the preceding experiment. Its last-finishing content channel has
50-ms reported `took` and 6.69-ms tracked search CPU. The wire span has substantial
time outside that reported search interval. The probes do not independently
time HTTP admission versus response construction/serialization/encoding within
that remainder.

During this request's 2.203-second pool wait, average occupied capacity is
**31.99 of 32 connections**. Integrating the other requests' connection occupancy
over that interval gives:

| Occupancy stage | Share of occupied connection-time |
| --- | ---: |
| Connection and request sending | 8.94% |
| Native request fully received, waiting for first response byte | 85.65% |
| Native response streaming | 2.29% |
| Response forwarding | 0.93% |
| Caller consumption and connection release | 2.18% |

These percentages are of cumulative connection-time, not percentages of API
CPU or of the request's six-second wall time. They show that earlier requests
retain capacity mainly waiting for native responses. The queued request cannot
reuse a connection until those operations release capacity. In the 0.30-CPU
case, native-before-first-byte occupancy rises to 92.81% and the representative
pool wait to 4.766 seconds.

In the identity case, the analogous occupancy shifts: native-before-first-byte
is 7.52%, while connection/sending and caller consumption/release are 42.96%
and 35.33%. Its much larger uncompressed traffic changes where the bottleneck
appears. Enlarging the pool alone is not established as a remedy.

## CPU actually consumed on the native node

Sampled JVM CPU during each read cohort is approximately:

| Thread role | Gzip / 0.55 | Identity / 0.55 | Gzip / 0.30 |
| --- | ---: | ---: | ---: |
| Search workers | 0.743 CPU s | 0.677 CPU s | 0.700 CPU s |
| Shared Netty transport/HTTP worker | 0.943 CPU s | 0.219 CPU s | 0.915 CPU s |
| JIT compiler threads | 0.973 CPU s | 0.569 CPU s | 0.438 CPU s |
| GC threads | 0.021 CPU s | 0.022 CPU s | 0.026 CPU s |

Thread samples use slightly wider boundaries than cgroup counters and may miss
threads that terminate between samples. Treat the totals as approximate. In the
baseline, about one third of sampled JVM CPU is JIT compilation, another third
the shared network worker, and about one quarter search workers. The node is not
spending its entire CPU budget on scoring. Network-worker CPU includes more than
compression alone; the encoding control is evidence of compression's impact,
not a direct per-request Deflater CPU timer.

The live node has two search executor threads and one shared network worker.
HTTP and transport sharing is supported by the pinned native implementation.
[OpenSearch 3.9 shared Netty group](https://raw.githubusercontent.com/opensearch-project/OpenSearch/3.9.0/modules/transport-netty4/src/main/java/org/opensearch/transport/SharedGroupFactory.java)

Reported CPU across all matched baseline search records totals 653 ms for 100
channels. That task accounting excludes some HTTP/response work; it cannot be
subtracted from whole-node CPU to create an exact serialization measurement.
Likewise, a throttled-period fraction is not a fraction of request wall time.
All native work, including JIT and network processing, competes for the same
small CPU allowance.
[CFS bandwidth semantics](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)

## Concrete response-size issue

The baseline transfers 100 native channel bodies with **40,752,661 decoded
bytes**, containing 9,064 hits. Offline packet reassembly finds **34,404,251
UTF-8 bytes of token string values** in `pathTokens` and `contentTokens`.

`NativeSearchPlan.buildPayload` requests at least 100 candidates per
channel and does not restrict returned `_source`. The native read model includes
the indexing token strings. After fusion, `discoverOpenSearch` uses document,
revision, section and unit identity plus score to hydrate results from PG; it
does not consume those returned token strings.

An offline canonical-JSON comparison removes only those two response fields:

| Offline serialization | Bytes |
| --- | ---: |
| Canonical full response bodies | 40,562,317 |
| Canonical bodies without the two token fields | 5,826,283 |
| Estimated canonical response reduction | **85.64%** |

Canonical reserialization differs slightly from the actual native bytes. This
is a response-size estimate, not a filtered-search benchmark or a latency claim.
The first implementation candidate is response projection retaining build,
revision, document/unit/section identity, sort order and required metadata while
leaving stored/indexed token fields available for matching. Its result ordering,
body/citation binding, and performance still require verification. No response
projection or candidate-limit change is implemented in this task.

## Timing and harness corrections

Query Insights' absolute timestamp is lazy-initialized separately from its
monotonic latency timer. Combining `timestamp + latency` to invent coordinator
start/end boundaries produced negative inferred post-search intervals. Those
timestamp-derived splits are rejected; the conclusions use wire spans, reported
durations, actual CPU, queue counters and controlled interventions.
[Pinned Query Insights listener](https://raw.githubusercontent.com/opensearch-project/query-insights/3.9/src/main/java/org/opensearch/plugin/insights/core/listener/QueryInsightsListener.java)

Early interrupted attempts are excluded. The PG membership guard initially
mistook process teardown for controller migration. A small local fork/exit probe
reproduces legacy cgroup paths becoming root for a zombie. A later captured
mismatch retained the owned PG unified scope while its legacy controllers were
root; a task can already be exiting before its displayed state becomes zombie.
The corrected guard checks kernel `PF_EXITING` as well as zombie/dead state before
and after membership reads, and still rejects a mismatched active process.
The successful runs retain runtime membership checks and nonzero independent
worker/PG CPU accounting. No conclusion is drawn from the interrupted cohorts.

This also corrects an earlier interpretation of `split-incomplete` in the
process-isolation report: that guard stop alone did not prove a live backend
had migrated into the API CPU group. Its exclusion remains appropriate.

Idle checks wait for five low-external-CPU samples before load starts, accounting
for the owned native node separately. Waiting for those windows is not counted
as request latency. Probe and payload-analysis jobs use existing resources;
offline payload analysis is limited to 0.10 CPU at low priority.

## Evidence, cleanup and limits

Oracle evidence is retained at
`<lab-home>/knowhere-readfree-r8k2/nativewait-n4h8`, including each run's
`native-attribution.json`, `native-request-metrics.json`, traces, private captures,
Query Insights records, native counters, JVM thread mapping, resource membership
records, input profile and reproduction scripts. `payload-size-estimate.json`
is retained in the baseline directory. Non-capture evidence is copied under
`/tmp/knowhere-nativewait-n4h8/oracle-artifacts`; captures remain mode 0600 on
Oracle. Documents contain no raw business query or corpus body.

Before/after hashes confirm all 37 monitored business files unchanged locally
and on Oracle. The temporary load test is archived outside normal discovery.
The owned node is stopped/removed, port 32793 is released, and its scopes are
inactive. The other chat's application PID 1670349 on 18765 and native container
`b45d654ecd836e28ded8e39f47b8cd6012049e426eebb5a9a8ebc7853c0401b0`
on 19203 retain their original process/container identities and start times.

No new VM, volume, bucket, model/parser call or paid API is provisioned. The
cached image is reused, storage is local and telemetry export is disabled.
These observations identify bottleneck mechanisms under this small isolated
configuration; they do not certify production latency, sustained throughput,
400-index turnover, full parser load, or a permanent compression policy.
