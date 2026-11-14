# OpenSearch application integration proof

Development status on 2026-11-05: the OpenSearch publication, retrieval,
SDK, lifecycle, migration and operator paths are integrated on
`feat/wangbinqi/opensearch-publication-slice`. The
[development validation ledger](opensearch-development-validation.md) records
the reviewed source, real component results, failed diagnostics, limits and
remaining staging acceptance. The
[development and release plan](opensearch-development-plan.md) remains the
accepted contract. The entries below preserve the earlier investigation and do
not override the final evidence ledger.

The final Oracle component campaign passed the real worker, execution fences,
native lifecycle, 400-slot admission and unchanged precompiled ranking baseline.
The automated filter extension still loses one PostgreSQL-found evidence group
after activation. Expanding candidate depth does not move that evidence into
the unchanged top 10. The strict raw replay also hit the ten-second fence on a
large input under the isolated CPU quota. These are recorded acceptance blockers;
there was no staging release or production cutover. Oracle comparison resources
and frozen inputs were preserved, and the temporary environment was removed.

KW-01 baseline closure (2026-11-05): the [Phase A handoff](opensearch-kw01-handoff.md)
records the frozen candidate, private fixture access/hashes, actual runtime and
staging/SDK inventory, isolated fresh-checkout reproduction, known ranking
failure and remaining broker skip. The original fixtures and 13 unrelated
containers were preserved; the owner authorized retirement of the old native
validation node and its exclusive volume.

Historical KW-02 starting point: development began with byte-bounded native bulk requests. The
candidate limit is 4 MiB of actual UTF-8 NDJSON, including actions/newlines;
complete oversized units fail explicitly. Local regression checks and the
owned native component cases pass. Inline execution, durable early ownership,
non-gating webhook dispatch, the shared deadline and real broker integration
remain open KW-02/KW-03 work. This change does not complete KW-02 or qualify
staging load/deadlines. The wider follow-up recorded 65 passed, one capacity
admission timing failure (6.90 seconds against six), and two skipped. The same
unchanged case passed separately on both frozen and new source; its timing
variability remains unqualified and is retained for KW-03, without weakening
the assertion. See the Phase A handoff for exact receipts.

Owner clarification (2026-11-04): formal integration/release acceptance runs in
**staging**; Oracle is the existing isolated development/component environment.
The handoff materials are the fixed candidate source, Oracle setup and private
fixture inventory, commands and results. There is no requirement for a second
environment or a ZIP. New implementation and its later acceptance evidence are
scheduled work, not unresolved product decisions. The separately authorized
[Oracle handoff check](../validation/opensearch-handoff-oracle-check-2026-11-04.md)
records fresh measurements and the limits of existing duplicate guards.

Same-data batching follow-up (2026-11-05): the 1,862-unit/103,025-token-occurrence
fixture is 1,644,744 actual bulk-request bytes. With the same native resource
limits, five single-bulk `refresh=wait_for` samples took 1.39–2.89 seconds and
passed all item/count/immediate-search-ID checks. Three fixed-200 controls took
10.75–11.38 seconds and failed the ten-second timing requirement. No explicit
refresh or application-source edit was used. KW-02 should implement byte-bounded
packing with one request for revisions that fit; KW-09 qualifies the general
limit under staging load. See the
[measurement and official basis](../validation/opensearch-handoff-oracle-check-2026-11-04.md#single-bulk-wait-for-follow-up-2026-11-05).

Owner confirmation and handoff review (2026-11-05): single bulk plus
`refresh=wait_for`, with byte-bounded splitting for oversized revisions, is
now selected. All batches share one deadline and one publication outcome;
KW-02/03 must include UTF-8/action-line sizing, oversized-record handling and
later-batch failure without partial activation. The owner also selected
continuing the original manual-retry handler after a client disconnect while
that handler remains running, within its unchanged deadline. Actual handler
cancellation/process exit still ends forward work and uses existing settlement/
recovery; no detached task or automatic replay is introduced. These are
accepted contracts, not implemented runtime behavior. KW-03 tests the distinct
disconnect, cancellation and response-after-commit cases; KW-05 documents
querying the original job after response loss.

Static handoff review (2026-11-04): preserve these open items rather than
interpreting component evidence as completed delivery:

- Agent-native failures are now explicitly fatal to the whole retrieval request.
  The inspected tool registry, dispatch wrapper and Cursor callback turn broad
  exceptions into tool errors; the outer plan's `LexicalIndexError` mapping alone
  does not enforce 503/no-success behavior. KW-04 must preserve typed fatal state
  through both harnesses, stop further agent work and prevent successful final/
  cache writes, including after partial evidence. KW-01 verifies supported
  transports: current v1/v2 routes are JSON; an accepted SSE ADR is not proof
  of a shipped route. See the
  [agent failure contract](opensearch-development-plan.md#64-agent-retrieval-and-native-failure-propagation).
- The read contract distinguishes missing persisted publication bindings,
  explicit native failures and confirmed recovery gaps from ordinary zero hits.
  Normal reads do not audit every physical build; scope capture and correct
  query errors are not proof that silent out-of-band record loss is detectable
  on every search. Preserve separate publication/recovery coverage checks and
  the no-hot-preflight rule.
- The owner selected interruption of unfinished indexing during a planned
  backend transition, retaining parsing/result/charge for explicit retry after
  service resumes. Completed jobs are unchanged and an aborted switch does not
  automatically revive interrupted attempts. The candidate parsed finalizer
  calls `LexicalCoordination.lockControl`, which rejects non-serving modes;
  KW-03/KW-10 must separate parse/failure persistence from publication admission.
  KW-11 rehearses parsing completion during the barrier, activation races and
  retry after switch/abort. See the
  [transition state matrix](opensearch-development-plan.md#1431-in-flight-jobs-during-a-backend-transition).
- Rollback repair has the same control-mode gap: `LegacyLexicalCopy.run` calls
  the serving-only guard, but required PG coverage must be repaired before
  reopening service. KW-10 must supply controlled maintenance repair without
  reopening primary indexing admission. Preserve the still-published revision's
  copy/input even when a newer candidate has been accepted but not published.
- Attempt timing must distinguish the original unstarted intent from a newly
  admitted manual retry. Persist the retry request's deadline at admission,
  even before its execution claim; the original parsing timestamp and previous
  attempt's deadline cannot expire it. KW-03 must cover old retained revisions,
  concurrent sweep and an API crash between retry admission and claim. This
  clarifies the accepted no-age-cutoff/whole-request-budget rules, not delivered code.
- Replacement admission is decided: new-version uploads return 409 until the
  previous job is durably done/failed, including an expired indexing execution
  not yet settled. Upload performs no timeout recovery. KW-02/03 must verify
  that rejection leaves job/attempt, billing and milestones unchanged, while
  explicit retry/redelivery/sweep retain their own recovery behavior.
- Redelivery review confirms existing late acknowledgments, an independent
  broker visibility-recovery process and a renewable Redis processing lock.
  These do not prove exactly-once work or cancellation on lock expiry. The
  parser entry still admits running jobs without an early parsing-completed
  check; the finalizer's later duplicate check does not prevent redundant work.
  KW-01 must verify deployed timing/configuration, and KW-02/03 must implement
  the [redelivery state matrix](opensearch-development-plan.md#911-duplicate-delivery-and-processing-ownership)
  and fault cases. Preserve broker recovery while removing automatic indexing
  redispatch. One stale engineering instruction to replay a closed-index write
  has been corrected to the selected fail-once/manual-retry rule.
- Owner-confirmed completion terminology is explicit in the
  [phase/wait contract](opensearch-development-plan.md#52-phase-states-and-historical-completion):
  phase completion, overall success, job termination, client wait satisfaction
  and Celery task return are distinct. KW-03 must retain the sweep's overall
  terminal guard and replace the parsing-completed exclusion with phase-aware
  checks. KW-05 must preserve one actual job state and overall SDK helpers
  across wait/subscription choices; client early return never means early
  `job.completed`. These are contract clarifications, not implementation proof.
- Empty-index scope is now decided: v1 closes and retains the index while
  cleaning eligible internal records normally. KW-06 must verify closure and
  identity-preserving reopening after later uploads; KW-09 measures retained
  catalog/resource growth. Whole-index deletion/recreation is not v1 work.
- The owner replaced the separate indexing task and three-round retry design
  with one synchronous execution at the end of the parse task, after the parsed
  commit/event dispatch. Explicit retry also executes synchronously in its HTTP
  request; no new job, indexing task or automatic replay. Both paths use a fixed
  configurable ten-second whole-execution limit when OpenSearch is primary.
  PG-primary preparation/rollback continues accepting uploads and waits for PG
  readiness under a separately qualified longer budget. Indexing failure preserves
  parsing/result/charge and does not invoke parser retry/refund hooks. Implement
  actual task/request bounds and indexing failure/timeout settlement in the
  existing stale-job sweep in KW-02/03, keeping its 30-minute schedule. The
  current sweep excludes completed parsing; add a phase-aware branch and remove
  obsolete indexing redispatch. Ordinary errors remain immediate and manual
  retry need not wait for a sweep. Verify the unstarted threshold and detection lag;
  KW-09 measures real input-size/mixed-load success within the limit.
  The historical candidate implementation and queue/retry results below are
  not acceptance of this new policy. See
  [current failure contract](opensearch-development-plan.md#93-finite-failures-and-review-gaps).
- The shared create/update-index entry point must submit the temporary PG copy
  to a bounded background thread pool after durable publication/copy intent;
  no per-copy task/job or waiting on that copy in the OpenSearch path. The
  inspected candidate still runs copies from maintenance and holds a namespace
  publication lock through COPY. KW-10 must change/validate that lock interval,
  thread/session ownership, bounded capacity and restart recovery. Threading
  alone does not prove absence of primary-path contention.
- The original business replay's absolute-evidence final assertion failed on
  seven shared misses. KW-09 must implement the accepted relative PG-evidence
  gate, preserve independent correctness blockers, retain the original 20-query
  baseline and add/run a separate extended suite. See
  [quality gate](opensearch-development-plan.md#81-align-the-regression-test-with-the-accepted-quality-gate).
- Temporary `indexing-input/v1` retention is now decided: no separate fixed
  retry expiry while the revision remains eligible and its canonical parsing
  result is available. Retain needed input regardless of age and reclaim it
  after the last eligible retry/PG-copy/other consumer no longer needs it.
  Bucket-policy verification, reference-aware collection and race checks remain
  KW-01/KW-06 work; save/load plus native-build cleanup are insufficient.
  See [input lifecycle](opensearch-development-plan.md#94-temporary-indexing-input-storage-and-reclamation).
- Existing branch/environment/fixtures are reusable handoff materials. Selected
  Oracle checks and fixture access have now been reverified. KW-01 still freezes
  the reviewed source and consolidates the complete baseline inventory and
  reproduction instructions; KW-09 supplies the future mixed-workload manifest.
  Do not require future implementation outputs to close KW-01. See
  [reproducibility gate](opensearch-development-plan.md#172-reproducible-handoff-package-required-not-yet-delivered)
  and [workload gate](opensearch-development-plan.md#workload-manifest-gate).
- Snapshot completion age, data boundary and duration must be distinct;
  historical restore success with missing current published builds is not a
  passing full-service/RTO result. See
  [backup policy](opensearch-development-plan.md#102-backup-policy).

The static review above changed documentation only. The separately recorded
Oracle handoff check executed existing-candidate probes and a temporary refresh
control, without changing application source; it does not close the listed
implementation tasks.

Bounded integration-entry checks (2026-11-04): the final reader-free protocol
passes all eight turnover contracts, including actual 360-to-320 turnover,
400 open indexes and admission of namespace 401 without exceeding 400 slots.
The largest retained real namespace (42,794 units / 644 documents) passes
native size, replacement, exact-build cleanup and close/reopen checks; adding
2,206 derived records separately verifies a 45,000-record boundary. On the
whole frozen scope, all native candidate depths (100, 1,000, all matches)
preserve the ten marked evidence successes of the offline PG scoring reference.
Candidate depth changes some Top-10 members/order; it is not exhaustive-RRF
equivalence. These are component functional/quality checks, not current
production inventory, whole API result assembly or latency qualification.
Eight additional canonical-reference, no-shared-row-wait and bounded LRU
recovery regressions also pass: 17 contracts total, with targeted lint/type
checks. Business source is unchanged and owned resources are cleaned up.
See the [completed preflight report](../validation/opensearch-namespace-and-slot-preflight-2026-11-04.md).

Response projection (2026-11-04): both native search adapters now omit
`pathTokens` and `contentTokens` from returned `_source`, keeping stored/indexed
tokens, BM25/RRF, candidate depth and revision/identity metadata unchanged.
The response/source, revision/cleanup, LRU, no-shared-row-wait and failure-path
checks passed 18 selected contract cases on the owned Oracle node. This was a
bounded functional check at reduced CPU quotas, not a new mixed-load latency
qualification. Targeted lint/type checking also passed. Host/configuration
and caller scheduling tuning is deferred to
integration with the real API and worker; the existing performance gate is
still open. The [integration constraints](opensearch-integration-constraints.md)
record the async-pool, lock-free read, exact-build cleanup, revision-bound
hydration and bounded LRU recovery rules that must survive that integration.

Latest implementation change (2026-11-04): old-build cleanup
may race with a query during replacement of the same document. Return actual
hits as normal success, including an empty list; do not retry the version race
or emit an updating error. Preserve each returned hit's body/citation revision
binding and separate handling of native errors and LRU reopening. The reader
leases and cleanup waiting described in the historical baseline below have
been removed from normal reads. The new path passed 65 targeted contract/native
checks, including held shared rows, replacement cleanup, revision-bound results,
bounded LRU reopening, and replay of a partially visible writer build. Lint and
targeted type checking passed. See the
[validation report](../validation/opensearch-unlocked-reads-2026-11-04.md),
[ADR 0027](../adr/0027-activate-document-revisions-only-after-search-readiness.md)
and the [revised protocol](opensearch-concurrent-read-lock-proposal-2026-11-04.md#accepted-replacement-behavior-and-proposed-smaller-coordination-protocol).

The final same-quota 50-peak mixed pair reduced per-read SQL from 19 to 5 and
explicit coordination locking SELECTs from 8 to 0. Successful reads were
45/50 before and 50/50 after; all 40 indexing tasks completed in both variants.
Detailed complete-pipeline P95 was 11.884 s before and 8.739 s after. A separate
lighter-probe optimized run still measured 7.076 s complete-pipeline P95 and
6.705 s lexical P95. The accepted 1-second warm lexical gate is not passed;
removing shared-row contention does not establish performance acceptance.

## Earlier verified baseline (2026-11-03)

The separate indexing queue and automatic redispatch described in this baseline
are historical candidate behavior, superseded by the inline/manual synchronous
policy above. Preserve the evidence without treating it as current acceptance.

- Durable parsed output advances the latest parsed revision and creates an
  indexing attempt. Existing job result and private document content reads
  can load that revision while indexing is pending.
- The existing worker deployment consumes a separate indexing queue. A
  committed attempt survives lost broker delivery, and the reconciler
  redispatches it without restoring its execution budget. A separate gevent
  process consumed a task from real Redis and completed native publication.
- Namespace indexes are scoped by corpus kind, owner, and namespace. They
  store the existing lexical tokens with whitespace analysis and native
  BM25; path and content results use weighted RRF. An acknowledged visible
  build becomes the published revision in one SQL transaction. The old
  published build remains eligible until this switch.
- The query reads one published revision set. A late cache writer or delayed
  Redis invalidation cannot override a newer publication. Native discovery
  errors propagate as unavailable. Lexical frequency lookup does not fall
  back to PostgreSQL.
- Failed indexing retains parsed output and the existing charge. The v1
  and v2 retry routes admit one free indexing attempt and coalesce duplicates.
  A newer admitted job fences a failed candidate; archival stops unfinished
  indexing without changing an already completed job. An expired execution
  cannot replace a successor's published build.
- Cleanup waits for active revision readers and retains a cleanup record when
  remote writes may still arrive. It leaves canonical content and citations
  in PostgreSQL. The temporary PostgreSQL frequency copy runs separately
  after publication, and blocked cleanup yields to later maintenance work.
- A single webhook URL accepts exact, deduplicated event subscriptions.
  Omission retains terminal notifications. The outbox preserves the event ID,
  occurrence time, revision, and phase snapshot; parsed and indexing-failed
  events can deliver the retained parse result. There is no delivery order
  guarantee.
- Shared demo publication, viewer reads, legacy copying, and cleanup were
  exercised with the non-superuser runtime role. The transaction-local
  maintainer setting is reapplied for authorized writes. Demo readers take a
  shared advisory lock instead of writing the generation row; writers take
  the corresponding exclusive lock. Both locks end with the SQL transaction.
  See [PostgreSQL advisory lock functions](https://www.postgresql.org/docs/15/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS).

## Evidence and reproduction

The isolated checkout on the Oracle lab host is
`<lab-home>/knowhere-opensearch-integration-20261103-c1a7`. It uses Python
3.12.13, pytest-managed PostgreSQL 14, filesystem test artifacts, OpenSearch
3.9.0, and Redis 7. The shared comparison node rejected index creation with
HTTP 403 and `cluster.blocks.create_index=true`. The final write tests used
an owned loopback node with a 512 MiB JVM heap and disposable storage. Both
owned OpenSearch and Redis containers were stopped after verification. The
shared node remained green, with nine active primary shards.

| Check | Result |
| --- | --- |
| New native publication, jobs, native bulk, subscriptions, and existing webhook contracts | 45 passed in 71.26 s |
| Existing PG jobs, documents, classic retrieval, revision races, evidence retention, and demo bundle contracts | 76 passed in 102.32 s |
| Existing shared demo lifecycle and catalog contracts | 12 passed in 21.53 s |
| Ruff and targeted Pyright | Passed; Pyright reported zero errors across 68 files |

Suite times are test wall times. The PG regression runs reported the existing
retrieval `data_type` deprecation warnings.

From `apps/api` in an isolated checkout, with the two test service URLs set:

```sh
PYTHONPATH=../../packages/shared-python:../.. \
  KNOWHERE_TEST_OPENSEARCH_URL="$OPENSEARCH_SLICE_TEST_URL" \
  KNOWHERE_TEST_REDIS_URL="$OPENSEARCH_SLICE_REDIS_URL" \
  ../../.venv/bin/python -m pytest -q --tb=short \
  tests/contract/test_opensearch_publication_contract.py \
  tests/contract/test_opensearch_jobs_contract.py \
  tests/contract/test_webhook_contract.py \
  tests/contract/test_webhook_secret_contract.py \
  ../../packages/shared-python/shared/tests/test_opensearch_lexical_index.py \
  ../../packages/shared-python/shared/tests/test_opensearch_webhook_subscription.py
```

The additive migrations extend `5e6f7a8b9c0d` through `0d1e2f3a4b5c`.
`KNOWHERE_LEXICAL_BACKEND` still defaults to `postgresql`. The switch to
`opensearch` is for the isolated proof until migration coverage and rollout
controls are ready. No production migration or deployment was performed.

## Remaining rollout gates

Index opening, closing, and LRU turnover now pass the
[isolated 400-index verification](../validation/opensearch-index-turnover-2026-11-03.md).
The [final-protocol size/slot preflight](../validation/opensearch-namespace-and-slot-preflight-2026-11-04.md)
repeats the complete slot boundary after reader protection was removed.
Target workload sizing and retained-volume recovery remain rollout gates.
The [Oracle mixed-load preflight](../validation/opensearch-mixed-preflight-2026-11-03.md)
reached 50 rolling-second retrieval arrivals while indexing concurrently.
The resource-limited run failed the warm lexical latency gate and passed
the five-minute indexing recovery gate. The one-hour qualification run
remains pending.

The [first read coordination optimization](../validation/opensearch-read-optimization-2026-11-03.md)
removes per-document build lookups and redundant liveness generation locks.
Under unchanged 0.55-CPU application/PG and OpenSearch limits, a fresh
same-input burst improved lexical P95 from 14.33 to 11.05 seconds and reduced
SQL statements per read from 74 to 39. Ten targeted regressions and 29 existing
publication/turnover contracts passed. The one-second warm gate still fails
on this constrained node; this is not production capacity qualification.

The [classic read simplification](../validation/opensearch-classic-read-simplification-2026-11-03.md)
then consolidated hot-index admission, reused HTTP/channel workers and kept
body processing plus final lease validation in one async transaction. A fresh
same-input comparison at actual 50 peak arrivals improved lexical P95 from
11.83 to 9.00 seconds and complete classic pipeline P95 from 12.71 to 9.81
seconds. SQL driver executions fell from 39 to 21 per read, executor
submissions from six to two and PG pool checkouts from twelve to four. Both
variants completed 50 reads and 40 indexing tasks without errors. The final
selected suite passed 56 tests, including cancellation, unknown outcomes,
revision consistency, cleanup and close exclusion. The warm latency gate
still fails under the unchanged 0.55-CPU quotas; production qualification is
still required.

The [remaining read wait diagnosis](../validation/opensearch-read-wait-diagnosis-2026-11-03.md)
reproduced 8.94-second lexical P95. Raising only the shared application/indexing/PG
quota from 0.55 to 0.9 CPU reduced it to 5.99 seconds; larger pools and more
threads did not remove the delay. Server lock logging located the primary
read lock hotspot at the namespace index registry, followed by the publication
generation row. The test concentrates 50 read arrivals on one namespace and
shares CPU across API/indexing/PG, unlike the current production resource
separation. The diagnosis preserves the implementation and calls for reducing
hot admission serialization before treating concurrency increases as a fix.

The [async API migration](../validation/opensearch-async-api-2026-11-03.md)
now awaits native HTTP and shared SQL coordination using the existing request
async pool. Worker gevent execution and the lock/lease protocol remain unchanged.
The final selected suite passed 67 tests, and sync/async native Top 20 results
matched for all 20 business queries in each load variant. A fresh paired replay
under the same quotas removed read executor submissions (two to zero), while
SQL executions stayed at 21 per successful read and checkouts at four. Pool
ceilings fell from 10 sync plus 24 async to one shared async pool of 24.
At actual 50 rolling-second peak arrivals, complete-pipeline P95 increased
from 9.22 to 10.44 seconds; lexical P95 increased from 8.65 to 10.20 seconds.
Both runs completed all 40 indexing tasks; reads changed from 50/50 successful
to 49/50. PostgreSQL logged the failed read's statement timeout while locking
the namespace registry row. The async migration is implemented but fails the
performance/error gate and is not qualified for production rollout. The next
investigation is admission scheduling and PG lock/pool waits; no lock weakening,
timeout extension or quota increase is included in this change.

The subsequent [PG lock ownership investigation](../validation/opensearch-pg-lock-ownership-2026-11-04.md)
changed no application code or lock rules. Read-only traffic reproduced
3.34-second namespace-lock SQL P95, proving that per-read exclusive admission
alone forms a queue. The valid mixed diagnostic recorded 4.16-second lock SQL
P95 and four namespace-row statement timeouts; all 40 indexing tasks succeeded.
Successful read admission held the registry for 93.7-ms median / 333.7-ms P95,
including four SQL round trips after lock acquisition. Queried-namespace
activation held its shared registry lock for 609-ms median / 698-ms maximum.
One snapshot showed 23 read lock waiters occupying a 24-connection API pool
while 22 further reads waited outside it. Indexing used its own sync pool;
checkout/checkin counts balanced. The next optimization target is the read
admission and publication critical sections, preserving their existing
ownership, lock and atomicity guarantees.

The [SQL round-trip optimization](../validation/opensearch-admission-round-trips-2026-11-04.md)
preserves lock modes/order and atomic publication. Read admission now uses two
SQL round trips after its namespace lock, down from four, and activation
acquires the generation row once, down from three. The selected suite passed
70 tests; final targeted type and lint checks passed. A fresh same-quota mixed
comparison confirmed 21 to 19 SQL executions per successful read and 38 to 34
per normal activation. All 40 indexing tasks succeeded in each variant, but
read success changed from 47/50 to 43/50 and complete P95 from 11.21 to 11.56
seconds. All failures were namespace-row statement timeouts. This single pair
demonstrates SQL reduction, not a latency improvement; the PoC still fails
the performance/error gate. Publication precomputation and any admission-lock
redesign remain separate work.

The [unlocked-read implementation](../validation/opensearch-unlocked-reads-2026-11-04.md)
subsequently removed shared-row admission from ordinary queries, preserving
immutable hit-revision binding, best-effort old-build cleanup and closed-index
recovery. The selected suite passed 65 tests; the lighter mixed-load repeat
completed 50/50 reads and 40/40 writes with complete-pipeline P95 7.08 seconds.

The [HTTP wait attribution](../validation/opensearch-http-wait-attribution-2026-11-04.md)
changes no business code. Both valid same-quota diagnostic runs achieve an
actual 50-QPS peak and complete 50 reads / 40 writes. Paired captures locate
3.32 seconds of one 8.10-second request in logical HTTP pool admission and
0.38 seconds on the native side before its first response byte. The caller's
TCP handshake is only 0.021 ms inside a 509-ms connect trace. Application,
indexing threads and temporary PG share the saturated 0.55-CPU group, which
experiences throttling in 82/84 periods. Retaining 32 rather than 20 idle
connections removes connection churn but leaves complete P95 near 7.95 seconds.
This is evidence of caller/resource contention in the harness, not an API-only
CPU measurement or production latency prediction. The warm lexical gate remains
open; changing lock rules again or increasing pools is not supported as the
next primary remedy by these measurements.

The [process-isolation replay](../validation/opensearch-process-isolation-2026-11-04.md)
then separates the API, indexing proxy and test PG CPU groups without changing
business code. Final runtime membership checks cover new PG backends; all 50
reads and 40 writes complete at an actual 50-QPS burst peak. Complete P95 is
7.324 seconds for a fresh shared-0.55 baseline, 4.965 seconds with a shared-1.05
budget, and 6.089 seconds with independent API 0.55 / indexing 0.30 / PG 0.20
budgets. Native CPU remains 0.55 throughout. The split run improves caller
response-consumption P95 to 70 ms from 292 ms, but logical HTTP pool wait remains
3.261 seconds and native request-to-first-byte P95 is 2.000 seconds. Its lexical
P95 is 5.961 seconds; the warm gate remains unmet. These measurements support
the impact of the original shared CPU limit, not a claim that isolation alone
is faster or that these small independent budgets represent production capacity.
All owned test resources are cleaned up; no production deployment is included.

The [native wait investigation](../validation/opensearch-native-wait-attribution-2026-11-04.md)
then reproduces 6.088-second P95 and matches 100 native channels to wire traces
and task CPU records. Tracked search CPU P95 is 9.31 ms; during one slow request's
pool wait, occupied connections spend 85.65% of their time awaiting the native
first byte. Mean search-executor wait is 141 ms per completed task in a bracket
that also contains three background searches. A diagnostic identity-encoding
control reduces complete P95 to 4.886 seconds, while lowering only native CPU
to 0.30 increases it to 8.292 seconds and mean executor wait to 402 ms. The
baseline returns 40.75 MB of decoded search responses, including 34.40 MB of
indexing token string values. Offline projection estimates 85.64% less canonical
response JSON after omitting those two fields. The later bounded implementation
adds request-level response projection and passes the 18 selected contract
checks described above; the earlier investigation itself changed no business
code. No permanent compression policy change or follow-up latency qualification
is included. The warm gate remains open.

1. The first-release scoring choice is native Lucene BM25. In the
   [isolated business replay](../validation/opensearch-business-ranking-2026-11-03.md),
   all 13 PostgreSQL evidence hits were preserved across build coexistence,
   retry, activation, and cleanup. Record shared misses as existing quality
   gaps. Apply the relative evidence gate on comparable frozen inputs and
   verify citations, scope, and version identity. Qualify the candidate
   window and complete the
   existing agent, SDK, webhook delivery, and authorization edge cases.
2. Replay the observed read and write bursts at 10× on the agreed host class
   (50 peak read QPS). Exercise the lexical path directly with prepared
   inputs, including cold opens, maintenance, backlog recovery, and separate
   migration throttling.
3. Verify native remote storage, remote cluster state, independent snapshots,
   and the recovery objectives on the target infrastructure. Rehearse the
   two release migration and deliberate rollback with complete copy coverage.
   The legacy PG frequency table remains a temporary rollback copy.
