# OpenSearch integration constraints

Date: 2026-11-05. Scope: carry the accepted isolated implementation into the
real API and worker. This is the current implementation checklist; older
reader-lease and shared-row admission proposals are historical. No production
capacity, rollout, or recovery acceptance is implied.

The [development and release plan](opensearch-development-plan.md) is the
handoff/assignment entry point; preserve the constraints below throughout it.

Formal integration/release acceptance takes place in **staging**, through the
real API, worker and supported SDKs. Oracle is the existing isolated component
environment; its bounded checks do not qualify the final staging topology or
implement the revised indexing contract. See the
[2026-11-04 handoff check](../validation/opensearch-handoff-oracle-check-2026-11-04.md).
Production deployment and observation are later release steps.

## Indexing execution amendment

The owner replaced the earlier indexing-task/automatic-retry design with one
synchronous execution at the end of the existing parse task. Commit/readably
expose parsing and dispatch subscribed `job.parsed` first; index outside that
transaction. A failed execution preserves parsing/result/charge and emits
`job.failed` with indexing failure context, then ends the task normally without
parser autoretry/refund hooks. Both SDK waiting modes use the same server state.

Owner-confirmed batching: one `refresh=wait_for` bulk for a revision that fits
the serialized-byte budget; split oversized revisions at record boundaries,
with one shared execution deadline and complete-revision activation. Replace
the candidate's fixed 200-record split, preserve every item/visibility check,
and do not force refresh or add automatic helper retries. Test UTF-8/body
overhead, a single oversized record and later-batch failure without partial
publication. Numeric limits remain staging-qualified configuration.

Owner-confirmed manual-retry disconnect behavior: continue the same execution
while its original server handler remains running, up to the recorded deadline.
A closed client socket alone is not a cancellation request. Actual handler
cancellation, process exit or expiry stops forward work and uses existing
settlement/recovery; preserve completed outcomes and native uncertainty. Do not
transfer work to another task or leave detached writes/activation. Clients
query the original job after response loss and do not automatically resend.

Keep completion meanings separate. Parsing/indexing phase completion, overall
`done`, terminal `done`/`failed`, client wait satisfaction and Celery task return
are not interchangeable. Parsed-only clients can proceed without changing
the authoritative job status or existing overall SDK helpers. Event selection
does not select a wait target; neither choice changes sweep predicates.
The sweep retains its overall terminal guard and classifies active work by
phase; replacing the blanket parsing-completed exclusion with the generic
parsing-age predicate would incorrectly time indexing. Apply the
[completion and sweep contract](opensearch-development-plan.md#52-phase-states-and-historical-completion).

Manual retry also runs synchronously in its HTTP request, reusing the original
job/revision. No new job, indexing task, automatic replay or detached work.
There is no separate age-based retry expiry while that revision remains
eligible and its canonical parsing result is available. Retain needed temporary
indexing input, including for old eligible failures; reclaim only after no
eligible retry, PG copy or other consumer needs it. Exclude still-needed input
from age-only bucket cleanup. Signed-URL expiry does not remove the parsing
result; unexpected missing/corrupt assigned input is an operational error.
For OpenSearch-primary work, apply a fixed configurable ten-second whole-execution deadline, including
connection/opening, preparation, all writes, visibility and guarded publication.
Verify actual task/request release. Reuse the existing 30-minute stale-job
sweep for indexing failure/timeout settlement; ordinary errors remain immediate
and explicit retry checks expired state without waiting for a sweep. Use the
indexing deadline or bounded unstarted threshold, preserve parsing/result/charge,
and guard the current attempt and committed publication with idempotent outbox
updates. The unstarted threshold belongs to the original initial intent; manual
admission persists this retry request's deadline, even before its execution
claim. Never expire a new retry using the old parse or previous attempt's age.
Keep parsing expiry independent and remove the obsolete indexing
redispatch schedule; no new watchdog or automatic execution. Timeouts do not
prove remote cancellation or remove native uncertainty.
When PG is primary, keep accepting uploads/updates and require PG publication
for search readiness, using a separately qualified longer PG budget. When
OpenSearch is primary, submit the temporary PG copy through the same index
service to a bounded background thread pool after durable copy intent commits.
Use separate sessions and preserve restart recovery; long COPY cannot hold
shared publication locks or gate OpenSearch completion. No per-copy task/job.
Historical candidate queue/reconciler tests do not validate this amendment.
See [current work and acceptance](opensearch-development-plan.md#93-finite-failures-and-review-gaps).

During a planned backend switch/rollback, the owner permits unfinished indexing
to be interrupted. Preserve parsing/result/charge and settle indexing-stage
failure with a transition explanation; require explicit retry after service
resumes, including after an aborted switch. Completed jobs remain completed.
Do not wait for every parse to finish, turn publication gating into parsing
failure, or automatically resume an interrupted attempt on another backend.
Separate guarded parse/failure persistence from native execution/activation
permission in non-serving modes. Use the
[transition state matrix](opensearch-development-plan.md#1431-in-flight-jobs-during-a-backend-transition).

## Duplicate delivery and worker recovery

New-version admission preserves the owner-selected simple rule: return 409
while the old document job is nonterminal, even if indexing has expired.
Upload does not settle that state; explicit retry, redelivery and sweep keep
their separate guarded recovery behavior. Verify rejected upload has no
job/attempt, billing or milestone side effects.

Preserve the existing independent broker visibility-recovery process. It is
not the removed lexical indexing redispatch loop, and queue delivery can repeat.
Check durable phase and processing ownership before parsing, billing or result
upload; a completed-phase guard only in finalization is too late. Apply the
[redelivery state matrix](opensearch-development-plan.md#911-duplicate-delivery-and-processing-ownership):
an original initial attempt may claim its first execution once; repeated
delivery cannot restart it after a recorded start, reopen terminal failure or
execute a later manual attempt. It may resolve committed success/expired
failure sooner than sweep. Fence old owners after lock loss and use stable
namespace/build/unit identity and unique logical outbox milestones. Test lost
acknowledgment, partial writes, lock expiry and late old-worker resumption under
the actual gevent/bootstrap behavior; do not infer exactly-once execution or
guaranteed cancellation from a Redis TTL or task time-limit setting.

## Scope of the current optimization

Search requests exclude `pathTokens` and `contentTokens` from the returned
`_source`. Keep both fields in bulk input, stored source, index mappings, and
BM25 matching. Use request-level source filtering; do not disable source storage
or rebuild existing indexes. Both synchronous and asynchronous adapters share
`NativeSearchPlan.buildPayload`.

Keep build, document, revision, section and unit identity, ordering, section
path, and asset flags in search responses. Preserve native scores, deterministic
tie-breaking, candidate depth, path/content weighting, and RRF. Canonical body,
page, asset and citation data still come from the captured PostgreSQL revision.
The filter follows the official
[source-filtering API](https://docs.opensearch.org/latest/search-plugins/searching-data/retrieve-specific-fields/).
Filtering reduces returned bytes; OpenSearch still loads/parses stored source.
Do not treat it as eliminating source-read or server-side parsing cost.

Stop diagnostic tuning after this bounded change and its safety checks.
CPU allocation, process scheduling, connection sizing, JIT warmup and compression
policy need representative measurements after integration with the real API
and worker. Do not keep optimizing the temporary load driver to predict those
production values. The existing warm lexical latency gate remains unpassed;
deferring tuning does not waive it.

## Normal retrieval path to preserve

1. Resolve authorization and namespace ownership. Capture backend/epoch,
   cache generation, native index identity, and all eligible published
   document/revision/build bindings in one ordinary SQL SELECT. Empty scopes
   retain their actual generation without initializing rows.
2. Use the captured scope for cache lookup, query filters, native hit validation,
   body hydration and references. Cache-only requests need no native lifecycle
   operation. Any response cached after publication changes remains under its
   captured generation; never put it under the new generation.
3. For native discovery, await path and content searches using the shared async
   HTTP runtime. Return the database connection before waiting on native HTTP.
   Reject hits outside the captured document/revision/build bindings.
4. Hydrate canonical content and assemble public sources using the hit revision,
   including when activation occurs after hits were selected. Close read handles
   without registering, renewing, checking or releasing reader leases.

Normal reads do not acquire explicit shared/exclusive coordination row locks,
register `revision_read` or `index_use` rows, synchronously update recency, or
perform a hot-path lifecycle/UUID preflight. This means no shared backend,
namespace-generation or registry row is a per-query contention gate. Ordinary
PostgreSQL MVCC/internal locks and pool limits still exist.

The constrained replay reduced the complete normal path from 19 to 5 SQL
statements per read and from 8 to 0 explicit locking SELECTs. The complete path
still includes authorization/body reads; one statement describes publication
capture, not the entire API request. Do not reintroduce the removed checks
through a generic middleware, repository helper, or finalization hook.

The capture checks persisted bindings; it is not a physical inventory audit.
Native query errors and known migration/recovery coverage gaps remain errors,
but an ordinary zero-hit query does not prove missing records. Do not add
per-build counts/existence probes to the hot path. Keep explicit publication
and recovery coverage validation and the accepted retired-build empty-result rule.

An unrecovered OpenSearch failure inside an agent tool fails the whole retrieval
request as temporarily unavailable. Carry that typed fatal outcome through all
tool/dispatch/harness exception wrappers and both `openai`/`cursor_sdk` paths;
stop further agent work and forbid successful final/cache writes after failure.
Do not recover the request by having the model switch to read/grep or another
recall. Ordinary no-match/tool-argument errors and valid native-bypass routes
retain their existing behavior. See the
[agent failure contract](opensearch-development-plan.md#64-agent-retrieval-and-native-failure-propagation)
for transport handling and required acceptance.

## Mutation coordination to preserve

Publication, archival, backend transitions, execution claims and lifecycle
capacity changes still use guarded mutations and short transactions. Keep
document/job/attempt/build ownership checks, namespace generation changes,
terminal-state rules, outbox atomicity and lock ordering among those writers.
Normal reads do not join that writer lock order. No native RPC, parser, provider
call or queue wait belongs inside a coordination transaction.

Never reuse a physical build ID. Retired-build cleanup targets an exact build,
refuses the current selection, and retains late-writer/conflict cleanup records.
A stale writer cannot reactivate a retired or non-writing build. A document ID
or revision ID alone is not a safe native deletion selector.

Cleanup does not wait for readers. If an old build disappears before a search,
the request returns actual remaining hits, including an ordinary empty result.
There is no version retry or special updating status. If hits were already
selected, their bodies and references remain bound to that selected revision;
do not silently substitute the current document revision. This does not shorten
canonical body/asset/reference retention.

V1 closes and retains empty namespace indexes. Clean eligible archived,
obsolete and failed records normally, then close confirmed empty indexes via
existing maintenance/lifecycle coordination. Reuse registry/native identity
when later uploads reopen them. No whole-index deletion/recreation or new
reader/use fence; empty published scope alone is not proof of native emptiness.
Verify this lifecycle and monitor retained catalog/resource growth.

## LRU remains independent of normal-read locks

Retain 400 open/reserved/uncertain slots and the current 360/320 turnover
watermarks. Lifecycle mutations serialize create/open/close decisions and
reconcile uncertain operations; confirmed close releases capacity. Existing
writer ownership and lifecycle operation records remain relevant to recovery.
Reader/use leases are not close fences.

Recency is a best-effort process buffer, bounded to 4,096 indexes, flushed every
10 seconds by the API lifespan through the existing async pool. Flush in a
batch without moving recency backwards or changing active lifecycle operation
timestamps. Lost hints affect eviction efficiency, not publication correctness.

A recognized closed-index search failure permits at most one lifecycle recovery
and replay of the original captured scope. If closed again, missing, timed out,
partially failed or otherwise unavailable, return the existing unavailable
error rather than empty success. Do not broaden retries or switch to PG.
A closed-index write failure terminates the single indexing execution. Do not
reopen/replay it automatically; a later explicit retry uses a fresh build under
normal admission. Preserve uncertainty and stale-writer cleanup obligations.

## API and worker runtime boundaries

- API retrieval reuses its existing SQLAlchemy async engine/pool and a
  loop/endpoint-scoped async HTTP client. Do not wrap the whole retrieval path
  in `to_thread`, create per-request executors, use a sync database pool from
  the async route, or create HTTP clients per channel. Settle both channels on
  failure/cancellation. Close the runtime and recency flusher at API shutdown.
- Worker indexing retains gevent and its worker-side database/runtime lifecycle.
  Indexing concurrency is separately bounded; raising generic gevent concurrency
  alone is not a native throughput control. Do not introduce a new independent
  ECS service just to copy the temporary indexing proxy used in diagnostics.
- Keep the single-execution deadline (ten seconds for OpenSearch primary;
  the separately qualified longer budget for PG primary), synchronous manual retry,
  phase-aware stale failure settlement and native visibility-before-activation,
  latest-parsed versus published revision behavior, free indexing retry, and
  webhook terminal-state semantics. PG lexical data remains a temporary
  deliberate rollback copy, never a request fallback or native recovery source.

## Required regression checks during integration

| Scenario | Required behavior |
| --- | --- |
| Concurrent reads of one namespace; control/generation/registry rows locked elsewhere | Ordinary capture/search does not wait for those row locks or emit per-read DML. |
| Replacement/cleanup before native search | Return actual hits or empty; no version retry. |
| Replacement/cleanup after hits | Body, public source, revision ID and citations retain the hit revision. |
| Wrong build/revision, partial results, missing index or native timeout | Reject/unavailable; never PG fallback or empty success. |
| LRU closes during a read/write | One bounded search recovery; failed writes terminate indexing without automatic replay; no reader lock added. |
| Source projection in sync and async search | Token fields absent from response, present in storage/matching; ordered hits, native scores and identity metadata unchanged. |
| Repeated requests, cancellation, shutdown | Reuse existing pools; no API executor submission, leaked handles or unbounded retries. |

The existing contract cases are in
`apps/api/tests/contract/test_opensearch_publication_contract.py` and
`apps/api/tests/contract/test_opensearch_read_coordination.py`. Keep their real
native/PG seams when moving integration boundaries; do not replace concurrency
or revision checks with SQL-string-only assertions.

## Evidence and limits

- [Final size and slot preflight](../validation/opensearch-namespace-and-slot-preflight-2026-11-04.md):
  the retained 42,794-unit real namespace, a separately derived 45,000-record
  boundary, candidate-depth evidence preservation and the complete 400-slot
  protocol pass. This is not exhaustive-RRF equivalence or a mixed-load gate.
- [Async API](../validation/opensearch-async-api-2026-11-03.md): remove API
  executor handoff and reuse the existing async pools.
- [Lock-free reads](../validation/opensearch-unlocked-reads-2026-11-04.md):
  remove reader admission/shared-row mutation while preserving version identity,
  cleanup and bounded closed-index recovery.
- [Process isolation](../validation/opensearch-process-isolation-2026-11-04.md):
  separate API, worker and PG resource accounting instead of attributing their
  shared 0.55-CPU bottleneck to a particular service.
- [Native wait attribution](../validation/opensearch-native-wait-attribution-2026-11-04.md):
  6.09-second complete P95, search-task CPU P95 9.31 ms, occupied HTTP connections
  mainly awaiting native first bytes, and large unused token responses.

Those are isolated prepared-input tests at a rolling-second peak of 50 arrivals,
not sustained 50 QPS or end-to-end production qualification. They do not exercise
the whole parser, all SDK/agent paths, production replicas or target durability.
Separate per-stage wall time, CPU time, queue counters and wire bytes; do not
add unrelated percentiles or interpret native `took` as total request time.

### Source-projection verification

The shared query-builder change passed 18 selected native/PG contract cases:
15 projection, async API, revision/cleanup, no-shared-row-wait and LRU cases,
plus three unknown-outcome/cancellation cases. The two projection cases inspect
real synchronous/asynchronous HTTP responses, compare ordered hits and scores
against the same native query with full source, and require both token strings
to remain available in the unfiltered stored source. All other source metadata
must match exactly. Existing version tests assert original content, public
source revision and filename after replacement and lexical cleanup.

The failure-injection fixtures now identify channels from the query predicate,
not from the entire payload, whose source-exclusion list names both channels.
Only `searchplan.py` changes among the 37 monitored business source files.
Functional tests use the existing Oracle VM, a private loopback node on 32795,
0.30 native CPU / 1,500 MiB / 640-MiB heap and a separate 0.30-CPU test/PG scope.
No mixed-load benchmark, parser, LLM, new cloud resource or paid provider is
used. Host tuning and the previous performance acceptance gates remain deferred
and open respectively.

Targeted Ruff and Pyright pass. Pyright explicitly selects the same Python 3.12
interpreter as the tests; the host-default 3.8 interpretation was discarded.
Only a boolean test-parameter spelling was subsequently expanded, without
changing assertions or execution. Evidence is retained under
`<lab-home>/knowhere-readfree-r8k2/projection-p6k2` and privately copied to
`/tmp/knowhere-projection-p6k2/oracle-artifacts`. The owned node and test scopes
are removed/inactive after verification. The other chat's 18765 process and
19203 native node retain their original identity/start time.
