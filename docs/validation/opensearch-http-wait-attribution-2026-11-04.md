# OpenSearch HTTP wait attribution — 2026-11-04

## Finding

The remaining multi-second delay is primarily outside OpenSearch's search
execution: logical HTTP connection-pool admission, caller scheduling, and
processing before/after the native calls. The application group is exhausting
its shared 0.55-CPU budget. That group contains the API retrieval pipeline,
indexing threads, the load driver, and temporary PostgreSQL processes; it is
not an API-only allocation.

This investigation changes no business source, lock protocol, production
configuration, or CPU/memory quota. Diagnostic monkeypatches run only in the
temporary test process. A pool-retention control removes connection churn
without materially improving latency in this pair. Increasing pool size alone
is therefore not supported as the primary remedy.

This extends the [unlocked-read validation](opensearch-unlocked-reads-2026-11-04.md).
Its earlier lighter-probe result was 7.08-second complete-pipeline P95. The
additional probes here produce 8.10-second P95; these are different diagnostic
runs, not evidence that unchanged business code regressed.

## Workload and measurement boundary

- Existing Oracle host, isolated owned native container on loopback port 32789.
  The other chat's application on 18765 and native node on 19203 were untouched.
- Same frozen 75-second 10× production-burst profile: 50 read arrivals within
  one second, 40 indexing jobs, including a changed-body replacement. Actual
  rolling-second peak is 50 in both valid runs; arrivals span 414 ms and 513 ms.
  This is a burst test, not sustained 50 QPS for 75 seconds.
- Prepared corpus: 18 documents / 1,727 initial queried units. No parsing,
  embeddings, paid model calls, or legacy PG frequency-copy task.
- Application/indexing/test-PG group: 55,000 CPU microseconds per 100,000
  microsecond period, 1,700 MiB memory. Separate native group: the same CPU
  quota, 1,500 MiB memory, 640 MiB JVM heap. Host: two CPUs.
- Async PG pool: 12 retained + 12 overflow; two indexing workers. Native HTTP
  pool: at most 32 connections; default retained idle connections: 20.
- Calls enter `run_retrieval_query` in process. Reported complete-pipeline
  latency includes arrival-to-handler scheduling and the retrieval pipeline;
  it does not include an external client's HTTP request to the API. Worker
  behavior is exercised through test indexing threads, not a deployed Celery
  service.

Each native path/content request carries a unique `X-Opaque-Id`. Temporary
HTTPcore probes record pool enqueue/assignment/resumption; HTTPX async trace
callbacks record connection, send, and receive boundaries. Additional light
probes record PG checkout/SQL, hydration, ranking, assembly, and JSON CPU time.
An independent observer samples CFS counters and socket queues outside the
application quota. Paired packet captures cover only the owned native traffic:
host loopback and the owned container's network namespace.

Both valid runs match all 100 channels across the application and both captures,
with zero kernel packet drops. Reassembled HTTP body lengths match HTTPX raw
download counts. The baseline clock-offset spread is 0.033 ms. Capture files
are mode 0600; reports contain timing/size metadata rather than corpus payloads.

## One complete slow request

The default run's nearest-rank P95 is request 28, at **8,100.09 ms**. Its content
channel finishes last. The following intervals belong to that same request and
close to its measured total; they are not a sum of unrelated stage percentiles.
The other channel overlaps this path and is not added separately.

| Critical-path interval | Time |
| --- | ---: |
| Arrival through submission of the last-finishing native channel | 2,068.10 ms |
| HTTP request preparation | 0.20 ms |
| Logical HTTP pool queue, until assignment that actually leads to sending | 3,318.03 ms |
| Assignment to coroutine resumption | 53.93 ms |
| Connection/setup through start of request headers | 615.95 ms |
| Request sending through last byte on client wire | 79.08 ms |
| Last request byte, client to native wire | 2.21 ms |
| Native receives full request to first response byte on native wire | 377.28 ms |
| Remaining response bytes emitted on native wire | 0.58 ms |
| Last response byte, native to client wire | 320.31 ms |
| Full body already on client wire to body-consumption callback | 591.52 ms |
| Body callback through native method return | 84.27 ms |
| Remaining caller work and scheduling through pipeline completion | 588.62 ms |
| **Total** | **8,100.09 ms** |

The response's native `took` is **184 ms**, nested inside the native interval.
It must not be added to the table. OpenSearch defines `took` at the coordinating
node, including search queueing and inter-node work but excluding request and
response transmission; it is not a pure Lucene CPU timer.
[Official Search API](https://docs.opensearch.org/latest/api-reference/search-apis/search/)

The 615.95-ms connection/setup interval is an application trace interval, not
a measured network round-trip time. Within it, the TCP-connect trace lasts
508.65 ms, but the caller's loopback TCP handshake takes only **0.021 ms**.
The SYN is emitted 121.24 ms after the connect trace starts, and the connect
completion callback runs 387.39 ms after the SYN-ACK already arrived. Across
100 default-run connections, handshake P95 is 0.032 ms while connect-trace P95
is 819.16 ms. This directly separates local socket handshake time from the
much slower caller continuation; it is not an external network-latency test.

Likewise, the 320.31-ms response tail
includes local forwarding/buffering and client receive backpressure. The client
advertised a zero receive window 31 times across the default run; its sampled
receive queue reached 285,005 bytes. The application often consumed data well
after the data reached the client kernel. These are observable caller-side
delays, although this instrumentation cannot divide each delay exactly among
CFS throttling, Python thread/GIL contention, runnable coroutine work, and
diagnostic overhead.

Across the 100 default-run channels:

| Independent distribution | Median | P95 |
| --- | ---: | ---: |
| Logical HTTP pool queue | 1,232.54 ms | 3,318.62 ms |
| Assignment to coroutine resumption | 28.00 ms | 128.94 ms |
| Native full-request receipt to first response byte | 237.07 ms | 981.01 ms |
| OpenSearch response `took` | 59.50 ms | 365.00 ms |
| Client full-body wire arrival to consumption callback | 271.52 ms | 553.29 ms |
| JSON decode, thread CPU | 1.79 ms | 3.62 ms |

These percentiles overlap and are **not additive**. The independent 50-ms
event-loop timer observed a maximum 457.85-ms wakeup overrun during the burst.
Node-side ingress, search queue/execution, serialization, and native event-loop
scheduling are bounded together by the native wire interval; they are not
individually measured. The evidence does not establish that OpenSearch has no
queueing or is ready for production capacity acceptance.

There are 100 channel requests and at most 32 HTTP connections. Under HTTP/1,
queued requests wait while earlier requests retain connections through response
consumption/closure. Slow caller continuations delay releasing those connections
and amplify the pool queue. Thus the measured pool wait is a location of delay,
not a separate additive estimate of CPU-throttling cost or proof that 32 is an
inherently insufficient connection limit.

## Connection reuse control

HTTPX 0.28.1 / HTTPcore 1.0.9 are the installed versions. The diagnostic control
changes only retained idle connections from 20 to 32, before the first native
send. Active connections remain capped at 32; CPU, memory, prepared inputs,
arrival profile, and business source remain fixed.

| Measurement | Default retention 20 | Diagnostic retention 32 |
| --- | ---: | ---: |
| Successful reads | 50/50 | 50/50 |
| Successful indexing jobs | 40/40 | 40/40 |
| Sync/async Top 20 parity | 20/20 queries | 20/20 queries |
| Observed native TCP connections for 100 channels | 100 | 32 |
| Complete-pipeline median | 6.701 s | 6.721 s |
| Complete-pipeline P95 | 8.100 s | 7.947 s |
| Lexical P95 | 7.634 s | 7.647 s |
| Client zero-window advertisements | 31 | 9 |
| Maximum event-loop timer overrun during the burst | 457.85 ms | 552.23 ms |

The installed pool's assignment/idle-eviction path was inspected directly.
Under this default saturation pattern, it closes idle connections rather than
reusing them for the entire cohort. Raising retention eliminates that churn in
the observed trace. It does not produce a substantial latency improvement in
this single pair. The reuse control also records 459 assignments for 100
channels, versus 100 in the default run: 63 channels are assigned more than
once before a connection can actually service them. Accordingly, pool queue
time is measured through the final useful assignment, not just the first wakeup.
The effect of those retries on CPU is not separately quantified.

The maximum unassigned-request counter is not directly comparable across these
two assignment behaviors. This control does not prove that pooling is irrelevant
or identify the optimal pool size. No dependency upgrade or permanent pool
configuration change is made.

[HTTPX resource limits](https://www.python-httpx.org/advanced/resource-limits/),
[HTTPcore 1.0.9 pool source](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/connection_pool.py)

## Effect of the 0.55-CPU quota

Counters cover the actual read-cohort window, approximately 8.33 seconds in the
default run. The configured quota is verified from live cgroup files for both
groups, not inferred from a launch command.

| Group / measurement | Default retention | Retention 32 |
| --- | ---: | ---: |
| Shared application group CPU consumed | 4.556 CPU seconds | 4.530 CPU seconds |
| Shared application group mean CPU cores | 0.547 | 0.543 |
| Application periods with throttling | 82/84 (97.6%) | 82/84 (97.6%) |
| Native CPU consumed | 2.701 CPU seconds | 2.672 CPU seconds |
| Native mean CPU cores | 0.324 | 0.321 |
| Native periods with throttling | 40/80 (50.0%) | 42/77 (54.5%) |

The application group's budget is effectively saturated. A quota of 0.55 means
all processes/threads in that group share 55 ms of CPU per 100-ms period.
When the budget is exhausted, runnable work waits for budget replenishment.
Async I/O avoids blocking a thread during I/O but does not create CPU budget.
`nr_throttled / nr_periods` counts periods that experienced throttling: 97.6%
does **not** mean 97.6% of elapsed time was spent waiting. Cumulative group
`throttled_time` is not assigned directly to an individual request.
[Linux CFS bandwidth documentation](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)

An additional approximate 5-Hz process/thread CPU sample in the reuse control
attributes the 8.31-second read window as follows:

| Work sharing the application quota | Sampled CPU time |
| --- | ---: |
| Python main thread: API event loop plus in-process test driver/probes | 1.96 CPU seconds |
| Two indexing threads | 1.54 CPU seconds |
| Admission/staging thread | 0.16 CPU seconds |
| Temporary PG and launcher processes | 0.56 CPU seconds |

These samples are coarse and omit some process-lifetime/boundary work; their
4.22-CPU-second sum need not equal the cgroup's 4.53 CPU seconds. In particular,
1.96 CPU seconds is not a measurement of API-only business CPU.

The node also experiences throttling, so attributing all remaining delay to
the caller would be incorrect. Nevertheless, the default request's 3.318-second
pool queue, data already waiting in the client kernel, event-loop overruns,
and saturated application budget show that search computation alone cannot
explain the delay. Both groups keep their existing limits in every valid run.
No new quota sweep was performed; this report cannot predict an exact latency
at one CPU or apportion an exact number of seconds solely to CFS.

## Limits and next engineering decision

Production separates API, worker, and PG resources, whereas this harness places
their work in one small CPU group and shares one Python process between API
and indexing. Thread/GIL contention and test-driver cost are additional
differences. These timings cannot be transferred directly to production.

The cohort also transfers 13,223,387 compressed bytes / 40,745,443 decoded bytes
across 100 native calls, with 100 hits returned per channel. JSON decoding uses
only 183.70 ms of total measured thread CPU, so it alone does not explain
multi-second waits. Response projection, decompression, result construction,
and other processing remain possible CPU-reduction targets; their individual
effects require a separate controlled experiment.

Before another implementation change, use these findings to choose between
restoring production-like resource/process separation within an explicitly
agreed budget and reducing measured caller CPU/payload work. Do not reopen
read locks, introduce more read threads, or enlarge pools on the assumption
that the remaining seconds represent PG locking or native BM25 execution.
The warm lexical P95 <= 1 second acceptance gate remains unmet.

## Evidence validity and cleanup

The initial `observed` setup stopped on a 403 before a measured workload;
it is excluded. `observed2` is also excluded: the temporary harness initially
omitted `extra_admission_h72b.py`, causing all 40 writes to fail. That mistake
was corrected before the two valid mixed runs. Their raw events independently
confirm 50 successful reads, 40 successful writes, and 20/20 parity each.
`observed2/invalid.json` records its exclusion. The prepared `identity32` option
was not run and has no results.

Before/after SHA-256 comparisons confirm all 37 monitored business source
files unchanged in both the local workspace and the Oracle checkout. Only
temporary diagnostic harnesses and documentation were added in this turn.
The load-test file is archived outside normal test discovery.

Oracle evidence is retained at
`<lab-home>/knowhere-readfree-r8k2/httpwait-m6q4`. It includes `audit.json`,
`source-before.json`, `source-after.json`, scripts, both valid phase directories,
`attribution.json`, `channel-metrics.json`, `wire.json`, `channels.json`,
CPU/socket samples, and private packet captures. Summaries and reproduction
files are copied locally under `/tmp/knowhere-httpwait-m6q4/oracle-artifacts`;
packet captures remain on Oracle. `handshake-attribution.json` records the
offline TCP/trace comparison, without another load run.

The owned `knowhere-httpwait-m6q4` container is stopped and removed, port 32789
is released, and its application scope is inactive. The other chat's native
container still has ID
`b45d654ecd836e28ded8e39f47b8cd6012049e426eebb5a9a8ebc7853c0401b0`
and start time `2026-11-03T07:36:19.527916941Z`; its 19203 listener and application
PID 1670349 on 18765 remain present. No other-chat resource was mutated or used
for the load. No new infrastructure or paid API service was provisioned; test
telemetry export was disabled. No production deployment was performed.

## Follow-up: independent application resource groups

The subsequent [process-isolation replay](opensearch-process-isolation-2026-11-04.md)
verifies independent API/indexing/PG CPU groups during the workload. Fresh
complete-pipeline P95 results are 7.324 seconds with the original shared 0.55
budget, 4.965 seconds with a shared 1.05 budget, and 6.089 seconds with independent
0.55 / 0.30 / 0.20 budgets. All primary runs complete 50 reads and 40 writes at
an actual peak of 50 arrivals per rolling second. Native CPU remains 0.55.
Caller full-body arrival to consumption P95 falls from 292 to 70 ms in the split
run, while pool and native-side waits remain substantial. These new measurements
do not support attributing all remaining latency to API CPU scheduling or treating
process isolation alone as a latency remedy. Business code remains unchanged.
