# Lock-free published retrieval: Oracle validation

Date: 2026-11-04 (Pacific). Branch: `feat/wangbinqi/opensearch-publication-slice`.
Status: the normal-read shared-row contention is removed and targeted safety
checks pass. The measured warm lexical latency gate still fails. No commit,
deployment, production migration, or cloud resource provisioning was performed.

## Change

Normal published retrieval captures backend mode/epoch, namespace cache
generation, index identity, and all eligible document/revision/build bindings in
one ordinary PostgreSQL SELECT. It does not initialize coordination rows, lock
shared rows, register readers/index uses, renew leases, synchronously update
last-used timestamps, or validate/release reader protection after hydration.
The empty-scope row preserves the actual cache generation without writes.

Native path/content BM25 and weighted RRF are unchanged. Hits are checked against
the captured document/revision/build mapping. Hydration and public source fields
continue to bind the hit revision. Cache keys retain the captured generation;
a late old response cannot populate the newer generation's key.

Retired-build collection no longer waits for readers. Replacement/cleanup can
produce fewer hits or an empty normal result, without a version retry or an
updating error. Canonical body/asset/reference retention is unchanged. Cleanup
still rejects current builds, targets exact non-reusable build IDs, and retains
late-writer cleanup records. Writer activation additionally rejects a build
that is no longer in its writing state.

A known closed-index response triggers at most one lifecycle recovery and
native replay using the original bindings. Missing indexes, other HTTP errors,
timeouts, and partial/shard-failed results remain unavailable errors; they are
not empty-success responses. LRU no longer treats index-use rows as close fences.
The lifecycle controller retains its create/open/close and 400-slot reservations.
Worker ownership tracking remains, and a closed-index write recovery replays the
same still-owned build with deterministic IDs before visibility/activation.

Native access hints are coalesced in a process buffer, bounded to 4,096 indexes.
The API lifespan runs a flush every 10 seconds through the existing async PG
pool; one SQL UPDATE persists a batch without changing in-progress lifecycle
timestamps or moving recency backwards. Cache-only requests need no native open.
Hints can be lost on a crash or failed flush; they affect eviction efficiency,
not publication identity. The flusher is cancelled on lifespan exit.

## Resource and isolation boundary

- Isolated checkout: `<lab-home>/knowhere-readfree-r8k2`.
- Owned native container: `knowhere-readfree-r8k2`, loopback port 32787, cached
  OpenSearch 3.9.0 image. No image pull or new VM/volume/bucket.
- Native quota: 0.55 CPU, 1,500 MiB memory, 640 MiB heap. App/temporary PG/indexing
  share a separate 0.55-CPU, 1,700-MiB scope. Async pool 12+12; indexing remains
  two executions. Neither connection nor CPU concurrency budget is increased.
- Only prepared chunks, real-query fixture strings, filesystem storage, dummy
  provider configuration, fake broker/provider boundaries, and local test PG
  are used. Outbound proxy settings reject unintended external provider calls.
  No parser/LLM, AWS/S3 write, paid provider, or new billed resource is invoked.
- The other chat's service on 18765 and native node on 19203 are separate and
  were not modified or restarted.

The host root disk was already above the default native percentage watermark.
Initial fixture setup could not allocate its index. Only this owned node uses
absolute free-space watermarks (low 2 GiB, high 1,500 MiB, flood 1 GiB), identical
for both variants. This is an isolated test setting, not a production proposal.
A host CPU preflight rejects a variant above 35%. Optimized starts were
deferred at 64.1% and 89.2%; a bounded quiet-window check then required five
consecutive samples below 35% before resuming. The quota was unchanged. Host
load still varied during measurement: detailed read-window means were 50.94%
before and 54.65% after, with 100% peaks. This is a shared-host comparison, not
a noiseless estimate of an exact speedup. No extra resources bypassed the guard.

## Safety and static checks

Two new tests were first run against the old implementation: they failed because
capture/finish emitted coordination locks/writes and capture waited for a held
shared row. After implementation the read suite passed, and its new recency test
failed until background coalescing was implemented.

The final selected native/contract suite passed **65 tests**, with the existing
full-400-index soak and separate real-Redis/gevent case deselected. Coverage
includes private/demo runtime-role behavior, held control/generation/registry
rows, empty scopes, complete generation snapshots, native scope mismatch checks,
cache generation, replacement cleanup before search and after hits, immutable
result/source revision identity, cold and racing LRU close/reopen, retry
exhaustion, missing-index refusal, native partial/unknown failures, same-build
write replay, stale-writer rejection, archival, supersession, terminal states,
cleanup, capacity reservations, async pool/HTTP reuse, and absence of API executor
submission. Retired reader-protection tests were replaced with the newly accepted
behavior rather than disabled.

An early writer recovery test used an unmanaged raw native close, which rightly
left the registry uncertain. The final test drives the actual LRU controller so
its recorded close state matches the native operation, then verifies replay of
the same partially visible build and one worker execution.

Targeted Ruff and Pyright passed. Tests and native load ran only on Oracle;
local work was source editing, syntax inspection, and artifact analysis.

## Frozen load protocol

Both variants run the same 75-second production-derived arrival profile: a
rolling-second peak of 50 reads against one namespace and 40 prepared indexing
tasks across the queried and background namespaces, including a changed-body
replacement. This is a real-burst-shape peak test, not sustained 50 QPS.

The prepared 18-document fixture and all 20 query strings are warmed before
measurement. Sync/async native Top 20 identities, order, and RRF scores must match
for all 20 strings. Cache and hit-stat writes are disabled consistently. A
separate producer records offered arrivals even if the API loop is delayed.
Both variants use the same pool, SQL, HTTP, commit, lock, and scheduler probes.
The optimized replay starts the same recency flusher as the API lifespan.

The baseline source is restored from the frozen pre-change snapshot; an exit
trap restores the optimized source. Native handles and temporary PG are fresh
per test. Measured source/probe hashes, retained input hashes/mtimes, and raw JSONL
remain under the owned `experiment` directory. Input hashes were retained after
the runs; the harness reads the same prepared paths and uses their checksums
when staging revisions. It does not export a separate input-hash ledger for
each run. The final source differs from the measured candidate only by a
reader comment; its production AST is unchanged. The comparison exercises the retrieval application
entry point, not external HTTP/auth/SDK networking or full parsing.

## Mixed-load results

The frozen detailed pair completed the same actual rolling-second peak of
50 arrivals. Percentiles below include all 50 attempts; the five baseline
failures never executed native BM25, so native-channel cohorts differ.

| Metric | Before | After |
| --- | --- | --- |
| Successful retrievals / attempts | 45 / 50 | 50 / 50 |
| Native lexical executions / channels | 45 / 90 | 50 / 100 |
| Successful indexing tasks | 40 / 40 | 40 / 40 |
| SQL statements per normal read, median / P95 | 19 / 19 | 5 / 5 |
| Explicit coordination locking SELECTs per read, median / P95 | 8 / 8 | 0 / 0 |
| Per-query lifecycle HTTP calls, total | 50 | 0 |
| Complete pipeline P50 / P95 | 11.326 / 11.884 s | 7.928 / 8.739 s |
| Lexical phase P50 / P95 | 9.881 / 11.553 s | 6.049 / 8.136 s |
| Sum of pool acquisition intervals per request, P95 | 5.223 s | 1.706 s |
| Indexing arrival-to-publication age, P95 | 27.673 s | 24.747 s |
| Native search / write rejection counters | 0 / 0 | 0 / 0 |
| Sync/async native Top 20 parity during warmup | 20 / 20 | 20 / 20 |

The five baseline failures were lexical-unavailable admission failures. The
after trace contains five ordinary SELECTs per measured read, no per-read DML,
no explicit coordination row locks, and no native lifecycle preflight calls.
Three background registry statements were observed separately, including the
coalesced recency writes. No pending indexing tasks remained at the end.

The old namespace/control/lease request gates are therefore absent rather than
merely faster. Locking the shared control, generation and registry rows in a
separate transaction no longer blocks normal retrieval in the regression test.
Mutation-side writer/lifecycle coordination remains.

### Remaining time and lighter-probe check

The detailed after run still showed start/capture P95 of 1.104 s, logical native
search P95 of 6.072 s, and channel HTTP P95 of 5.787 s. The native response's
`took` P95 was 348 ms. These are overlapping/different cohorts and their
percentiles cannot be added. `took` is not a measurement of the entire remote
round trip; the difference does not locate the wait exclusively in the caller.

A separate optimized-only replay removed SQL/pool/lock sampling and kept only
outer application/native timings. It retained the same corpus, 50-arrival peak,
40 indexing tasks, quotas, pools, and background recency flusher:

| Lighter-probe metric | Result |
| --- | --- |
| Successful reads / indexing tasks | 50 / 50 and 40 / 40 |
| Complete pipeline P50 / P95 / maximum | 5.635 / 7.076 / 7.401 s |
| Lexical phase P50 / P95 / maximum | 4.800 / 6.705 / 7.007 s |
| Channel HTTP P95 | 3.916 s |
| Native response `took` P95 | 330 ms |

Latency remains several seconds without the detailed SQL probes, so they are
not the whole explanation. This after-only repeat is not a matched lighter-probe
before/after comparison and does not isolate an exact instrumentation cost.
Further work must separate HTTP connection-pool waiting, client scheduling, and
native ingress/response scheduling under the CPU quotas. This change does not
establish which of those dominates.

The accepted **warm lexical P95 <= 1 second** gate is **not passed**, even in the
lighter-probe repeat. No normal after-run admission/rejection error was observed
in these 50 requests, but the sample is not a production error-rate guarantee.
Do not treat harness completion as performance acceptance.

## Limits and remaining acceptance

This validates the accepted best-effort replacement behavior and removes shared
coordination row competition from normal reads. It does not remove PostgreSQL's
ordinary MVCC/table/internal locks, connection-pool limits, CPU limits, or native
search queueing. Mutation and lifecycle transactions still coordinate writers.

The test does not certify the proposed production 32-GiB node, sustained 50 QPS,
all-namespace production distribution, 400-index turnover under this mixed load,
business relevance quality, SDK/browser asset downloads, or remote-store/disaster
recovery. The mixed profile does not run parsing, vector generation or the
temporary asynchronous PG legacy-frequency copy. Previously
selected production latency/error gates remain open until the corresponding
representative validation passes. No automatic PostgreSQL lexical fallback is
introduced.


## Retained evidence and cleanup

Raw records and reproduction scripts are in
`<lab-home>/knowhere-readfree-r8k2/experiment`, with a local private copy under
`/tmp/knowhere-readfree-r8k2/oracle-artifacts`. Key files are `comparison.json`,
`load-context.json`, `light-summary.json`, `contracts-final.log`,
`final-reference-check.log`, `final-code-check.log`, `baseline-hashes.json`,
`after-hashes.json`, `final-source-hashes.json`, `probe-hashes.json`, and the three
variant directories. The final reference check also asserts original body,
source filename and public source revision after replacement and lexical cleanup.
It passed independently after the 65-case suite.

The owned native container is stopped and removed, and port 32787 is released.
Both temporary load tests are archived outside normal test discovery. The other
chat's native container retains ID
`b45d654ecd836e28ded8e39f47b8cd6012049e426eebb5a9a8ebc7853c0401b0`
and start time `2026-11-03T07:36:19.527916941Z`; its 19203 port and the existing
18765 application process remain present. No deployment or production state was
changed.

The duplicated 408-MiB application data directory in this owned checkout was
replaced with a link to the existing original input directory after testing;
the original data was not modified. This releases the extra disk copy while
retaining the prepared-input reference for reproduction.

## Follow-up: HTTP and CPU attribution

The subsequent [read-only HTTP wait investigation](opensearch-http-wait-attribution-2026-11-04.md)
matches all 100 native channels to paired client/native packet captures under
the same quotas and actual 50-QPS peak mixed burst. In one 8.10-second request,
3.32 seconds are logical HTTP pool wait and 0.38 seconds are full-request receipt
to first response byte at the native node. A 509-ms TCP-connect trace includes
only a 0.021-ms loopback handshake; caller continuation accounts for the rest.
The application/indexing/test-PG group averages 0.547 CPU cores against its 0.55
limit and is throttled in 82 of 84 periods. A retention-only control reduces
native TCP connections from 100 to 32 but leaves P95 near 7.95 seconds. All
50 reads and 40 writes succeed in each valid run. Business code is unchanged;
these diagnostic timings do not qualify production capacity or isolate an
exact CPU-only latency penalty.
