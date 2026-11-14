# OpenSearch lexical replacement: implementation design

Date: 2026-11-05. Status: selected engineering baseline for the accepted
[first-version plan](opensearch-lexical-v1.md), with an isolated application
implementation described in the [progress report](opensearch-implementation-progress.md).
Production capacity, recovery and cutover acceptance remain open.
This document resolves the concrete design items in
the [completeness review](opensearch-publication-readiness.md#design-completeness-review-before-integration).
It does not authorize production migrations, paid resources, or cutover.
Product decisions remain in the linked plan and ADRs; configurable starting
values below are engineering choices to exercise, not measured guarantees.

For formal API/worker delivery, follow the [development and release plan](opensearch-development-plan.md).
It consolidates the current handoff state, work packages, acceptance gates,
operations and rollout responsibilities.

Formal integration and release acceptance run in **staging**. Oracle supplies
isolated component checks and early measurements; production deployment and
post-release observation follow staging signoff. Developer handoff materials
mean the fixed candidate source, existing Oracle setup/private-fixture inventory,
reproduction commands and scoped results, not another environment or a required
ZIP. Future implementation/acceptance outputs are appended by their owning tasks.
See the [handoff definition](opensearch-development-plan.md#172-reproducible-handoff-package-required-not-yet-delivered)
and [fresh Oracle check](../validation/opensearch-handoff-oracle-check-2026-11-04.md).

Accepted amendment (2026-11-04), implemented on the isolated working branch:
cleanup after document
replacement no longer waits for old readers. A query keeps its captured build
bindings and returns actual hits normally, including missing-document or empty
results, without version retry or an updating error. Returned bodies/assets/
citations still bind the selected revision. This supersedes the reader-drain
and in-flight protection requirements below. Normal reads now capture published
bindings and cache generation in one ordinary SELECT, without reader/use leases
or shared-row mutations. Recency hints flush in the background every 10 seconds;
closed-index recovery replays the original scope at most once. Genuine backend
failures remain unavailable errors. Production acceptance remains open; see the
[validation report](../validation/opensearch-unlocked-reads-2026-11-04.md).
See [ADR 0027](../adr/0027-activate-document-revisions-only-after-search-readiness.md)
and the [revised protocol](opensearch-concurrent-read-lock-proposal-2026-11-04.md#accepted-replacement-behavior-and-proposed-smaller-coordination-protocol)
for the current protocol. The [integration constraints](opensearch-integration-constraints.md)
are the current checklist for carrying the isolated changes into the real API
and worker. Host/configuration and scheduling tuning is deferred to that seam;
the existing production acceptance gates remain open.

## Invariants and module ownership

PostgreSQL owns ordinary job/revision metadata and publication selection.
OpenSearch owns lexical serving and its own remote durability. PostgreSQL's
old term-frequency table is only a temporary migration rollback copy. Neither
that table nor retained parsing artifacts are an OpenSearch disaster-recovery
source. No retrieval request falls back to PostgreSQL after OpenSearch cutover.

Keep the implementation behind three internal module interfaces. Callers do
not acquire locks, choose physical builds, or manage OpenSearch index state.
The names below describe planned interfaces, not existing implementations.

| Module | Interface and responsibility |
| --- | --- |
| Revision publication | `completeParsing(jobId, preparedRevision)`, `runIndexing(jobId)`, `retryIndexing(jobId)`. Own durable parse/index state, one-execution claims, synchronous manual retry, activation, archival/supersession checks, phase transitions, and subscribed outbox events. |
| Lexical index | `writeBuild(build, input)`, `search(readContext, query)`, `collectRetiredBuild(buildId)`, `reconcileIndexes()`. Own concrete index identity, lifecycle capacity, native requests, bounded closed-index recovery, visibility, and exact-build cleanup. |
| Retrieval read session | `runWithPublishedScope(request, operation)`. Own authorized single-statement scope capture, generation-keyed caching, revision-bound hydration, and native handle cleanup. No reader registration, renewal, or shared-row mutation. |

The OpenSearch adapter and temporarily retained PostgreSQL rollback adapter
share the lexical-serving seam. Adapter selection is an operator-controlled
backend transition, never a per-request fallback. Parsing, public routes,
agent tools, and SDKs use the same lifecycle contract with either selection.

At new-job admission, allocate a stable document identity/placeholder and
commit its accepted-job binding with job creation. An update uses its existing
document. An unparsed placeholder has neither parsed nor published selection;
it supplies no content or retrieval hits and is not presented as a completed
parsed document. Rejected admission changes neither binding nor charges.

## Persistence and identities

Use additive migrations later. The following are ordinary coordination rows,
not a replacement PostgreSQL word-frequency index. Apply the same selection
rules to the private and demo corpus models through `CorpusStorage`; never
identify an index using an unqualified namespace string.

| Record | Required representation and constraints |
| --- | --- |
| Existing document | Retain `current_job_result_id` as the published revision. Add nullable `latest_parsed_job_result_id`, `current_indexing_build_id`, and `accepted_job_id`. The last field identifies the most recently durably accepted update, including one still parsing or subsequently failed. All revision pointers must belong to this document and resolved owner. |
| Existing job | Add typed `parsing_status`, `indexing_status`, nullable `failure_stage`, nullable `current_indexing_attempt_id`, and nullable normalized `webhook_events`. Keep the existing internal CAS `version`; do not expose a public state version. Retain the existing active-document uniqueness rule through indexing. |
| Existing immutable revision | Keep one `JobResult` per job, its result/asset delivery data, revision-scoped chunks, sections, and serving manifest. Stamp the intended input format, map-unit layout version, and tokenizer/dictionary fingerprint at handoff. Add a nullable, single-assignment derived input reference/checksum and unit count when indexing preparation succeeds. Preparing a candidate must not update published namespace snapshots, graphs, or document selection. |
| `lexical_backend_control` | Singleton row with `backend` (`postgresql` or `opensearch`), `mode` (`serving`, `draining`, `maintenance`, `recovering`), internal `epoch`, expected cluster identity, and configuration revision. Epoch changes fence old application work during cutover, rollback, or recovery. |
| `lexical_namespace_indexes` | Unique `(corpus_kind, resolved_owner_id, namespace)`, opaque stable `index_name`, nullable native `index_uuid` until creation, state, current lifecycle operation identity/owner, last-used time, and last verified state. States: `uncreated`, `closed`, `opening`, `open`, `closing`, `uncertain`. Bind to the configured cluster identity. |
| `document_indexing_attempts` | Unique `(job_id, attempt_number)`, immutable revision/input binding, selected backend/epoch, state, initial-versus-manual origin, intent/admission time, operation-entry time and fixed deadline, durable execution claim, current build/owner token, claim expiry, and failure history. Manual admission persists its request's operation deadline even before the execution claim; an original unstarted intent has its own interruption threshold. At most one active attempt per job and one execution per attempt; admit atomically with the document/job check. If retained, the execution count is only 0 or 1. Legacy next-eligible/next-dispatch fields do not authorize automatic scheduling. |
| `document_indexing_builds` | Unique build identity for each execution that reaches build preparation, attempt/revision/scope/input bindings, purpose (`publication` or `migration`), owner token, visibility result, and optional activation/retirement generations. A retired build can never be selected again. An execution can consume budget before any build exists. Migration builds have an internal migration owner instead of a client indexing attempt. Persist cleanup state, conflicts, last observed count, and next sweep time on this row. |
| `lexical_operation_leases` | Internal writer/lifecycle operation ownership and admission-wait records. The schema can retain earlier `revision_read`/`index_use` kinds, but normal reads neither create nor renew them, and LRU/retired-build cleanup does not wait for reader leases. Preserve unresolved writer/lifecycle operations until completion or fencing is established. |
| Existing webhook outbox | Add a unique internal milestone key. Examples: parsing success per job, overall completion per job, parsing failure per job, indexing failure per indexing attempt. Snapshot phase states and original occurrence time; redelivery reuses the event identity. |

Add the missing operation/claim timing and origin representation through a new
additive migration; the candidate's existing dispatch-deadline migration is not
an execution deadline. Preserve each attempt's immutable time basis rather than
deriving a manual retry's expiry from the original parsing timestamp.
Keep old scheduling columns only for compatible transition/history until a
separately safe removal; do not modify an already existing migration.

Use integer counters, typed identifiers, and UTC timestamps; new lease/operation
timestamps use timezone-aware database types. Use explicit foreign keys where
the private/demo storage permits them and validate cross-corpus references at
the repository interface. Preserve existing authorization and row-security
conventions. Do not put native index names or build selectors in public input.

A physical OpenSearch record identifies `(build_id, unit_id)` with a stable
hash as `_id`; its fields include document/revision/build identifiers,
section/unit identity, ordering and type flags, and the prepared path/content
token strings. The build binds the tenant/index identity, input checksum,
and layout version. A duplicate bulk operation within that execution writes
identical content to identical IDs. A later indexing execution gets a new
build ID, so an old worker cannot overwrite records selected from a newer
execution. Existing randomly generated database row IDs are not native IDs.

### Durable indexing input

The immutable parsing result and revision-scoped canonical sections/chunks
are the durable handoff input. Indexing preparation produces the authoritative
leaf/interstitial map units once, preserving existing hierarchy and asset
ownership logic. Freeze their unit/section IDs, ordering, type flags, and
computed jieba path/content tokens in one compressed, checksummed private
object in the existing parsing artifact bucket, with a revision-specific key.
Reuse already prepared tokens where available; do not change tokenizer output
as part of this replacement. Persist the derived reference with a guarded
single-assignment update. Competing executions use the first committed object;
their losing uploads are ordinary orphan artifacts for existing cleanup.

This preparation belongs to indexing. Failure to prepare/upload this derived
object must not hide a completed usable parse or classify an indexing error
as parsing failure. A pending intent can initially have no object reference;
the inline worker or synchronous retry prepares one from the retained revision with the pinned
compiler/tokenizer version, without reparsing the source or calling an LLM.
All CPU-heavy work and object upload occur outside SQL transactions. Once
assigned, every subsequent execution uses the identical checksummed input.

This object is active-job retry input, not a journal or retained backup of
the published search corpus. Retain it while the parsed candidate is eligible
for indexing retry, with no separate fixed-day expiry. Age alone cannot end
eligibility while the document is active, the candidate has not been superseded
and its canonical parsing result is available. Long-unresolved eligible failures
may retain input long term. After successful publication, remove it asynchronously
once the temporary legacy-copy operation, if enabled, also no longer needs it.
Supersession, archival, or lawful canonical-result removal makes failed-candidate
input eligible for cleanup after active writers are fenced. Retain normal
parsing results under existing rules independently. Track pending input
bytes and S3 usage in validation; reclaim inputs after their final consumer,
not on a fixed retry age, and never silently reparse missing input. Pin the actual
result/bucket retention policy in KW-01/KW-06: the inspected model/adapter do
not define a numeric expiry, and renewable signed-URL expiration is not object
expiry. Exclude still-needed input from age-only bucket/prefix TTLs. Preserve
long-lived eligible input formats and pinned preparation behavior across
deployments; do not silently substitute a newer compiler.

Implementation status on 2026-11-04: `lexical/storage.py` currently provides
save/load and `lexical/maintenance.py` schedules legacy copies and native-build
cleanup. An explicit reference-aware input-object collector is still required;
the retention rules above do not establish that it exists. Implement and test
successful-publication/PG-copy dependencies, retry retention, writer fencing,
competing-upload orphans and idempotent deletion under
[development-plan Section 9.4 and KW-06](opensearch-development-plan.md#94-temporary-indexing-input-storage-and-reclamation).

A checksum/layout mismatch is a correctness failure, not a reason to silently
regenerate different tokens under a newer deployment. Backfill freezes input
from the existing published revision and token representation. No indexing
retry changes the parsed revision, document identity, or parsing charge.

### Native mapping and retrieval requests

Create one primary and zero replicas with a strict mapping. Path/content are
text fields using `whitespace` for both indexing and search and custom BM25
with `k1=1.5`, `b=0.75`. Identity/version fields are keywords, ordering/counts
are numeric, and image/table flags are booleans. Retain token repetitions;
do not lowercase, stem, or run a second jieba tokenizer in OpenSearch. Pin
the same query tokenizer/dictionary used for the input format. Reject an
incompatible format instead of mixing versions silently in one index.

Resolve one concrete authorized index, with no wildcard or cluster-wide
search. Compile the read session's selected build IDs and document/section/
type constraints into filters **before** each channel's candidate limit.
Use the existing query tokenizer and an OR match over its whitespace-joined
tokens, not caller-controlled query DSL. Empty tokens yield ordinary empty
lexical results. Validate returned document/revision/build/unit bindings
against the captured scope before hydration; a mismatch is a correctness
error/alert rather than a hit to expose or silently substitute.

Start with two concurrent single-index `_search` calls, with
`allow_partial_search_results=false` and `ignore_unavailable=false`. Inspect
timeouts and shard failures as well as HTTP status; fail the discovery if
either required channel fails. Do not fuse one successful channel with a
failed channel as an ordinary result. A later `_msearch` transport optimization
must inspect every child response: its searches fail independently. These
behaviors are documented in the official [Search API](https://docs.opensearch.org/latest/api-reference/search-apis/search/)
and [Multi-Search API](https://docs.opensearch.org/latest/api-reference/search-apis/multi-search/).

Use deterministic score/document/order/unit tie-breaking. Fuse unit identities
with zero-based channel rank `r` as `weight / (60 + r + 1)`, path weight 1
and content weight 2. Keep the existing downstream evidence ranking/assembly.
Native search responses exclude `pathTokens` and `contentTokens` with
request-level `_source` filtering. Stored/indexed tokens and all hit identity,
revision, ordering and hydration metadata remain intact; no reindex is needed.
Channel candidate depth is an explicit configurable test parameter: start
at `max(100, requested discovery top_k)` per channel and record it in every
quality/performance fixture. A finite candidate window can affect RRF results;
native-IDF acceptance is not proof that this window has passed. Qualify or
increase it against the required evidence and load gates before cutover;
do not call a truncated result equivalent to exhaustive full-list fusion.

## Transactions, claims, and publication

Use PostgreSQL for short coordination transactions. No OpenSearch RPC, S3
upload, parser, LLM call, or queue wait occurs while holding a SQL transaction
or connection for this protocol. Existing publication currently couples
revision preparation, pointer selection, token COPY, and final success;
implementation must split these responsibilities rather than append an
OpenSearch call to that transaction.

Participating mutation paths use this lock order, skipping objects not needed.
Normal retrieval does not acquire these explicit coordination row locks:

1. Backend-control row: shared for guarded mutations; exclusive for allocation,
   backend transitions, or recovery. Acquire the needed mode initially.
2. Namespace-generation rows, sorted by authorization-scoped identity.
   Exclusive for publication, archival, and update admission. Current v1/v2
   APIs reject a different namespace for an existing document. If a future
   separately approved move feature touches two namespaces, it must lock both
   in that order; this is not a feature to implement in the present migration.
3. Concrete namespace-index registry rows, sorted by identity, if needed.
4. Documents, jobs, indexing attempts, and builds, sorted within each class.
5. Lease, outbox, and legacy-copy intent rows.

Do not upgrade shared locks under contention. After acquiring locks, perform
fresh dependent reads and sample `clock_timestamp()` for lease checks; a
timestamp captured before a lock wait is insufficient. Bounded transaction
retries handle deadlocks/serialization failures without resetting indexing
attempt budgets. See the [primary-source coordination review](../validation/opensearch-coordination-semantics-2026-11-03.md).

Control-row locking and serving permission are separate concerns. During a
planned transition, parsed-result persistence and guarded indexing-failure/
outbox settlement must still work; native execution and activation remain
gated. The candidate `LexicalCoordination.lockControl` rejects every non-serving
mode, so reusing it unchanged for these persistence paths would violate the
retained-parse contract. Preserve ownership/current-attempt checks without
requiring failure settlement to publish against an obsolete backend epoch.

### Parsing completion

1. Prepare the immutable parsing result and revision-scoped canonical content.
2. In one guarded transaction, verify accepted-job identity and ownership,
   persist the revision and canonical candidate rows, set latest-parsed selection,
   settle parsing success/charge, set indexing pending, create the first
   attempt with its input-version binding, and create subscribed
   `job.parsed` exactly once. Derived native input may still need preparation.
   Do not switch the published pointer or published graph/snapshot here.
3. Commit, dispatch the subscribed parsed event through the existing webhook
   outbox, then call indexing once in the same parse task. Do not wait for webhook
   delivery, hold the PG transaction over native I/O, or dispatch indexing.
   If a planned transition currently gates primary publication, retain the
   parse and settle indexing-stage failure rather than starting native work or
   leaving an automatically resumable intent. Apply Section 14.3.1 of the plan.

Latest-parsed content/filename/namespace metadata comes from that revision;
the existing published selection and its metadata remain usable until
activation. Current v1/v2 ingestion shares `resolve_effective_document_scope`
in `apps/api/app/services/document_ingestion/scope_service.py`: an explicitly
different namespace for an existing document is rejected, while omission
retains its namespace. Demo/private boundaries remain enforced. This migration
does not add namespace moves. Only if a separately approved future API supports
a namespace-changing replacement would retrieval have to stay in the old scope
until activation and then change both generations/snapshots atomically. That
conditional rule is not a present integration task. Archival and permission
checks still apply in the supported same-namespace flow.

### Indexing execution

Run the initial execution directly in the existing parse task after the parsed
transaction commits and the parsed outbox event is dispatched. Manual retry
invokes the same service synchronously in its HTTP request. There is no
indexing queue, automatic redispatch, failed-build replay or detached task.
The candidate separate-task implementation must be adapted; its historical
tests do not establish the new contract.

Under a short guarded transaction, record one execution start/owner per
attempt before connection/opening. Verify backend/epoch, accepted job,
non-archived owner/document, immutable revision/input binding and current
attempt. A concurrent request or duplicate task delivery cannot start another
execution. Preserve history; only an eligible explicit retry creates another
attempt, on the same job/revision for free and without parsing.

Celery delivery can repeat independently of this execution rule. Apply the
[redelivery state matrix](opensearch-development-plan.md#911-duplicate-delivery-and-processing-ownership)
before parsing/billing/result upload and recheck durable state under ownership.
The current parser entry's terminal-state check plus Redis lock does not skip
already completed parsing; a finalizer-only check is too late. Keep the existing
broker visibility-recovery process, separate from removed indexing redispatch.
An eligible original initial attempt with no execution start can claim its
first execution once. A started attempt cannot restart on another delivery,
even after owner expiry; resolve committed success or settle failure instead.
An old parse message cannot claim a newer manually admitted indexing attempt.
Lock expiry or failed renewal does not prove the old worker stopped: validate
owner/attempt/build/deadline at effects and commit, retain late-writer cleanup,
stable native record IDs and one namespace lifecycle identity. Do not create
new attempts/builds/events merely because the queue delivered another message.

Each OpenSearch-primary execution has a fixed configurable **ten-second
deadline**, persisted with its start. Connection/opening, preparation, writes, visibility and
publication share it; heartbeat/progress do not extend it. Bound actual native
and database waits and input processing by the remaining time. Admission/pool/lock waits count from indexing-operation
entry; recording the claim cannot start a fresh budget after those waits.
On error or
expiry, fence publication, settle failure once and emit subscribed `job.failed`
with indexing context. Retain parsed data/charge. The parse task returns
normally without invoking parser retry/refund hooks; synchronous manual HTTP
returns the canonical error and retained-result context.

A worker/API crash, including between parsed commit and execution start,
uses the existing `app.core.tasks.stale_job_sweeper.expire_stale_jobs` task for
indexing failure/timeout settlement on its unchanged 30-minute schedule.
Extend its current parsing-completed exclusion with a separate indexing branch;
remove the obsolete indexing redispatch schedule, without adding a watchdog
or launching indexing. Ordinary errors/timeouts are still settled immediately
by the worker/request. The sweep repairs the current attempt's recorded failure
or expiry using its own backend-specific deadline, not parsing's two-/four-hour
thresholds. Parsing-expiry configuration must not disable this branch.
Pin the original initial intent's unstarted threshold and verify fair bounded
scans/detection lag in KW-03; manual retry need not wait for a sweep. Before an
execution claim exists, a manually admitted attempt already carries this HTTP
request's operation deadline. Only the original initial intent uses the
intent-creation-to-start threshold; an old parse or previous attempt cannot
prematurely expire a newly admitted retry. Apply the
[per-attempt timing table](opensearch-development-plan.md#93-finite-failures-and-review-gaps).
Preserve completed parsing,
result and charge, and settle the job/phase/subscribed outbox idempotently.
A prior failed attempt cannot overwrite a newer retry, and PG shadow-copy
failure cannot fail a completed OpenSearch job. See the
[settlement contract](opensearch-development-plan.md#93-finite-failures-and-review-gaps).
Retain the overall terminal-state guard (`done` or `failed`), then classify
active jobs by their persisted phase. Do not simply remove the parsing-completed
filter and apply generic job age to indexing. A client receiving `job.parsed`,
a satisfied SDK wait or a normally returned Celery task cannot exclude an
unfinished indexing phase from the sweep. Already-terminal failure needs no
second state transition; undelivered outbox events have independent recovery.
Resolve already committed completion before settling failure; never release unresolved native
reservations merely because the deadline expired. Failure settlement must
also have bounded task/request overhead. No timeout implies remote rollback.

Execution start does not authorize writes to an uncertain native index.
After guarded lifecycle admission/opening, bind verified cluster/index identity
to the same execution and its fresh build/owner before writing. Early failure
need not produce a build. A subsequent manual attempt uses a new build and
cannot overwrite the prior one. No network wait occurs inside the start or
binding transaction.

Ensure the immutable derived input exists as described above. Missing
retained compiler support is an indexing error requiring compatible worker
recovery, never a source reparse or hidden tokenizer change. Then, before
each bulk batch, verify the current unexpired writer claim, build/owner token,
backend epoch and concrete cluster/index identity. These checks provide writer
ownership and stale-writer fencing only. The existing worker may retain an
`index_use`-kind lease as its writer credential; that record does not exclude
LRU close. Normal reads still create no leases. This clarification does not
require renaming the lease kind, changing its schema or removing writer
credentials. Do not hold a SQL transaction over native I/O. Inspect every bulk
item, retain deterministic IDs and keep uncertain batch outcomes bound to
that build. A failed batch ends this execution; do not automatically replay
failed items or the whole build. Confirm visibility for all acknowledged
batches within the execution deadline. Empty-input
revisions follow the same publication checks without a fictitious bulk.
Never infer whole-revision success from HTTP 200 or a successful final batch.

Owner-confirmed policy: send one `refresh=wait_for` bulk when the revision fits a configurable request
byte budget. Replace the candidate's fixed 200-record split with bounded packing
of actual UTF-8 NDJSON bytes, including action lines and metadata; split larger
revisions only as needed, retaining all ownership and visibility checks. Do not
wait for other jobs to fill a batch or enable automatic helper retries. The
[same-data Oracle comparison](../validation/opensearch-handoff-oracle-check-2026-11-04.md#single-bulk-wait-for-follow-up-2026-11-05)
supports this choice for the 1.64-MB request: all five single-bulk samples passed
ten seconds, while three fixed-200 controls exceeded it. This is a planned
implementation change; the general byte limit and full deadline/runtime contract
still need staging qualification. No explicit-refresh policy is selected.

All batches consume the same remaining execution budget and activate only as
one complete revision. Failure of any later batch keeps earlier visible records
unpublished; retain the old selection and ordinary failed-build cleanup. Pack
whole retrieval records without changing their identities or truncating tokens.
A single encoded record exceeding configured hard request/memory limits fails
indexing with an explicit size reason in the canonical indexing error; do not
send an over-limit request, truncate/split that retrieval unit or automatically
retry the unchanged input. Preserve the parse and old publication. Test the
byte boundary with UTF-8 and action-line overhead; a target record count alone
does not define the request limit.

LRU may close the index during native writes, as specified under
[index admission](#index-admission-turnover-and-uncertain-outcomes). A recognized
closed-index write failure ends the indexing attempt under the one-execution
policy. A later explicit retry may open through normal guarded admission; it
cannot recreate a missing established index or bypass unresolved writer state.
This does not change the separately bounded closed-index query recovery path.

For the final check, confirm native cluster/index identity, the exact build's
expected visible unit count and payload bindings outside SQL transactions
after all item acknowledgments. Keep the writer claim valid through activation.
Inside the guarded activation transaction, check only the PG-recorded registry
and claim identities, writer ownership and recorded visibility proof against
the expected bindings; do not issue a native identity/visibility RPC there.
There is no requirement that the index remain continuously open and no
reader/index-use close fence. If the writer
claim expires/is lost or the cluster/index identity changes, that visibility
result cannot authorize publication. A later explicit manual execution uses
immutable input under its own build ID.

### Atomic activation

After native visibility is confirmed, one short transaction rechecks:

- Backend/epoch and normal serving eligibility; expected PG-recorded registry
  and claim index identity, using the native proof obtained outside SQL.
- Current accepted job, document active state, and immutable revision owner.
- Current attempt/build/owner nonce, unexpired writer claim, and
  complete input/count/visibility proof.
- The job has not already completed or been superseded/archived.

Then atomically select published revision/build, update published document
metadata/graph and namespace snapshots, advance affected namespace generations,
mark parsing/indexing completed and overall `done`, persist the subscribed
completion event, retire the previous physical build, and create temporary
asynchronous PostgreSQL-copy intent when enabled. No native RPC occurs here.
Graph/payload preparation happens beforehand; activation selects prepared
revision state rather than running expensive corpus preparation under locks.

If the commit acknowledgment is lost, inspect committed pointers/job/attempt
before deciding the outcome. A committed `done` is never downgraded or republished by an
old worker. Completion event deduplication is database-enforced. Redis cache
invalidation and outbox dispatch are post-commit accelerators, not authority.

Archival and accepted-update admission use the same gates. Archival wins if
committed first; a completed job remains successful if archival follows.
Acceptance of a later update changes `accepted_job_id` immediately, so an
older failed candidate never regains eligibility when the later update fails.

### Preparation and deliberate PostgreSQL rollback

Before cutover, the PostgreSQL adapter gates completion on its own materialized
index and serving manifest. OpenSearch migration builds run separately from
client attempts: their failure cannot change an already completed job, charge,
or webhook history. Bind a prepared native build only if the locked document
still selects the exact source revision/fingerprint. A newer publication or
archive makes an old migration build ineligible. Do not replay job completion
to attach this shadow binding. Cutover requires a verified native binding
for every required current published revision, including zero-unit cases.

After cutover, only OpenSearch success authorizes job completion. One shared
create/update-index service owns primary publication and the optional PG-copy
handoff. Submit the temporary PostgreSQL copy to a bounded process-owned
background thread pool after its durable intent commits. Do not await it or
create a per-copy Celery job/task. Each copy uses its own connection/session/
transaction and the same immutable input/revision. Saturation or process exit
leaves durable pending intent for existing maintenance to resubmit; do not
acknowledge copy completion merely because a thread was started. Copy failure alerts
operations without failing a completed client job. Preserve the per-word COPY
implementation for this temporary path rather than removing it prematurely.
Publication of a newer revision retires an older copy's eligibility to update
published namespace statistics; late copies cannot replace current selection.

The candidate `LegacyLexicalCopy.run` holds the shared namespace-generation
publication lock through token preparation/COPY; `_lockClaim` requires that
same lock for OpenSearch activation. Threading alone does not remove this
contention. Prepare/write revision-scoped copy data outside shared publication
locks, with exclusive copy ownership and a short guarded completion step.
Test a paused/slow copy against new primary publications and reads. Bound
thread/connection resources and per-copy I/O; background does not mean infinite
waits. Preserve pending intent, input retention, shutdown/restart recovery and
coverage checks before rollback.

Controlled rollback maintenance must be able to repair the required PG copy
while normal primary publication is gated. The candidate copy also calls the
serving-only `lockControl`; provide a guarded maintenance permission rather
than reopening ordinary indexing admission or selecting PG with incomplete
coverage. Copy eligibility follows the current published revision, not merely
`accepted_job_id`: a newer accepted/parsed but unpublished candidate does not
invalidate the still-published revision's required copy or input. Recheck
publication/archival and maintenance ownership at completion, and keep late
copies from replacing newer published coverage. Test barrier-time repair and
the accepted-but-unpublished replacement case in KW-10.

After deliberate rollback, the same inline/manual execution contract gates
completion on the PostgreSQL adapter again. Continue accepting uploads/updates:
parsed results are available first and search readiness waits for PG publication.
Use a separately qualified longer PG execution budget rather than OpenSearch's
ten-second default, with compatible HTTP/gateway/SDK limits for synchronous
manual retry. The background thread is only the non-gating shadow copy while
OpenSearch serves. Native shadow bindings may be absent while PG is
primary and must not be treated as ready for another OS cutover. Backend epoch
and coverage reconciliation govern each transition. The published revision,
two-phase client contract, and historical job outcomes remain authoritative;
no path interprets a request-local native error as permission to choose PG.

The owner permits a planned transition to interrupt pending/running indexing.
Apply the [transition state matrix](opensearch-development-plan.md#1431-in-flight-jobs-during-a-backend-transition):
retain parsing/result/charge, fence activation and settle indexing/overall
failure with the subscribed failure event and transition explanation. Resolve
committed success first. Already-failed or completed jobs are unchanged.
Incomplete parsing retains its existing recovery behavior; the publication
gate cannot become a parsing failure. A first attempt created only after
service resumes can select the current backend, but an interrupted attempt
never resumes automatically, including after an aborted switch. Its later
explicit retry creates a new attempt using the selected backend/epoch and
budget, preserving immutable input and history. No new task or event type.

## Read lifetime, cache, and cleanup

A retrieval resolves permissions and captures backend/epoch, native index
identity, namespace cache generation and published document/revision/build
bindings in one ordinary SELECT. This is a statement snapshot, without a
reader lease, shared-row admission lock, registry initialization or recency
write. Empty scope is an ordinary empty result; a published revision missing
its matching persisted build binding is a correctness/recovery error, not an
unready new parse. Native request errors and confirmed migration/recovery
coverage gaps also remain errors. Zero matches alone neither diagnose absent
physical records nor certify corpus integrity; do not add per-read full-build
counts/existence probes to infer either. Publication and migration/recovery
coverage validation remain separate from the ordinary query path.

All retrieval routes and shortcuts retain their captured scope. Validate native
hits against its document/revision/build bindings and hydrate canonical body,
assets and references from the hit revision. Activation does not silently
retarget an in-flight request to the new revision. Normal finalization closes
handles without a fresh coordination check or reader-lease settlement.

The API reuses the existing async database engine/pool and loop/endpoint-scoped
async HTTP client, with the existing 32-connection ceiling. Return database
connections before native HTTP waits. Do not wrap the normal async request in
`to_thread`, a synchronous connection pool or a per-request executor. Settle
both native channels on failure/cancellation and close HTTP runtimes at API
shutdown. Workers retain gevent-compatible I/O and worker-side execution claims.
Claimed writer executions still use bounded leases and ownership checks;
removing reader leases does not weaken stale-writer fencing.

Cache keys include the captured backend epoch, namespace generation and request
filters. A late old response cannot populate a newer generation's cache key.
Cache hits and routes requiring no native discovery do not open an index.
Physical staging/cleanup may change native statistics; captured publication
identity does not promise an immutable ranking.

An unrecovered native service failure inside agent retrieval is fatal to the
whole request under the owner-selected
[agent failure contract](opensearch-development-plan.md#64-agent-retrieval-and-native-failure-propagation).
Propagate a typed request-level failure through `agent_tools/registry.py`,
`agent_explore/dispatch.py` and both harnesses, including Cursor callback/thread
boundaries. Existing broad catches convert exceptions into recoverable tool
observations, so a native exception alone does not enforce this contract.
Stop further tool/LLM work, settle outstanding calls and prevent success/cache
writes even if earlier tools produced evidence or a harness attempts to finish.
JSON v1/v2 return 503/`UNAVAILABLE`; any already-supported stream uses its
terminal failure envelope. Verify the deployed transport baseline before
claiming SSE coverage or adding a new endpoint. Ordinary zero hits and tool
argument errors can still be handled by the agent, and valid cache/small-corpus
routes need no native health probe. Existing identity/authorization failures
must not become successful evidence either.

Normal native reads record best-effort recency in a bounded process buffer.
The API lifespan flushes batches every 10 seconds through its existing async
pool, independently of request completion. Lost hints affect eviction efficiency,
not revision identity. No per-read `index_use` row or hot-path lifecycle/UUID
preflight is required.

Retire a physical build before cleanup and never reuse its ID. Cleanup targets
that exact build, refuses the current selection and does not wait for readers.
If cleanup precedes search, return actual remaining hits, including empty
results, without a version retry or updating status. If hits already exist,
keep body/reference resolution on their original revision. Canonical content
and citation retention is unchanged. See the
[lock-free read validation](../validation/opensearch-unlocked-reads-2026-11-04.md)
and [integration regression checklist](opensearch-integration-constraints.md).

Delete-by-query is a snapshot operation. A late write or version conflict can
survive an earlier pass. Keep the retired build row as a durable tombstone,
repeat bounded sweeps with fresh visible counts, and persist conflicts/errors.
Do not claim permanent physical removal until old writers and their remote
operations are known to be finished or fenced. An empty pass alone is not
that proof. A stalled/uncertain writer blocks final cleanup acknowledgment;
it does not permit activation of its build. Cleanup failure never downgrades
`done`. Native statistics can change during every stage; do not force-merge
only the acceptance fixture to disguise this effect.

Parsing results, body rows, assets, and existing references follow their own
retention/permission rules. Lexical cleanup does not delete them. References
resolve their bound revision and renew expiring links through the existing
result/reference interfaces. No public revision selector is introduced.

## Index admission, turnover, and uncertain outcomes

The 400-open-index cap counts `opening`, `open`, `closing`, and `uncertain`
registry rows plus any discovered unmanaged open business indexes. System
index overhead has a separate resource budget. Confirmed `closed` is the
only ordinary slot-release state. Revision deletion does not release a slot.
Reserve an opening under the exclusive control-row allocation transaction
before sending the native request, including first-time index creation.
`uncreated` is only for a new registry identity that has never owned a native
index; a missing previously created index is a recovery fault. Creation uses
the same operation journal/uncertainty rules. Same-index callers share the
operation; duplicate creation is not a new slot.

V1 closes and retains empty namespace indexes; whole-index deletion/recreation
is out of scope. Eligible obsolete/archived/failed records still undergo normal
exact-build cleanup. Once cleanup confirms emptiness, use existing maintenance
and lifecycle coordination to close the index, retain registry/native identity,
and reopen that same identity on later use. No new delete/recreate state or
empty-index UUID rebinding is required. Empty published scope alone is not
proof of emptiness: candidates, consumers and unresolved writers may remain.
Preserve their fencing and existing LRU rules, without a new reader/use fence.
Measure retained empty/closed catalog count and metadata/disk growth; future
whole-index deletion requires a separate identity/backup/concurrency design.

Lifecycle mutations use short guarded transactions to reserve and reconcile
create/open/close operations; normal reads do not enter those gates. No native
RPC runs while holding a transaction. Only a confirmed completed close with no
outstanding lifecycle issuer releases the reservation. Reader/index-use leases
are not close fences.

Proactive turnover starts with configurable high/low watermarks 360/320 and
selects indexes by best-effort recency. A close may race a native search or write.
A recognized closed-index search failure allows one lifecycle recovery and
replay of the original captured scope. A second close or another backend failure
returns unavailable, not empty success or PG fallback. A closed-index write
failure ends the indexing execution without replay; deterministic IDs protect
record identity but do not authorize a second execution. Only a later eligible
explicit retry may write a fresh build. Cold callers can request a victim without a per-hot-query preflight.
At capacity, online lifecycle admission has priority over new backfill work.
Admission waiters carry caller deadlines and expire without consuming slots.
The existing five-second capacity-wait budget applies; exhausted admission
returns retryable HTTP 503, not an empty retrieval result. Capacity and bulk
settings remain qualified only after the agreed mixed-load run.

### RPC uncertainty and controller failure

Journal a lifecycle operation/owner before send. Disable automatic transport
replay of open/close commands; attach an opaque operation ID for diagnostics.
Native acknowledgment waits and client deadlines are not cancellation or
caller-epoch fencing. An open response with `shards_acknowledged=false` does
not prove closed state; a close response may report per-index failures.
Inspect the complete response and actual index/primary/block state.

If the issuer dies, the response is lost, or completion cannot be established,
mark that index `uncertain`. Keep its slot charged. Do not send an opposite
operation, admit a new writer through the lifecycle/claim path, or clear
uncertainty merely because a SQL lease expired or a state read currently looks
correct. Reconcile the unresolved issuer before new/conflicting lifecycle
operations or new writer admission. Existing writer claims still require all
ownership/expiry/epoch/index checks; this paragraph grants no mutation bypass
and cannot cancel an already dispatched native request.

Ordinary hot readers do not consult registry lifecycle state, register an
operation lease or wait for lifecycle transitions. An otherwise valid captured
published scope or generation-bound cache hit can proceed even when that
index's registry is uncertain; all authorization/identity checks still apply.
Do not add a hot read gate to enforce a blanket prohibition on native work.
If a search receives a recognized closed-index error, it must enter the
guarded lifecycle recovery path, which returns unavailable rather than issuing
open/replay while uncertainty is unresolved. Unknown native errors remain
errors. This admission distinction follows the current reader/claim protocol;
it still needs the final service-level uncertainty/race acceptance, not a new
claim of proven availability. Other healthy indexes can continue to serve.

An expired writer-use record does not prove a late write has completed. Keep
writer cleanup/fencing records until resolved. This is not a reader close fence:
LRU may close during native work, with the bounded recovery described above.
Uncertain create/open/close issuers retain their capacity reservations and must
be reconciled separately from ordinary search errors.

Reconciliation may use a known terminal native task/result and a fenced old
issuer. A missing task, elapsed quiet period, or opposite command is not
sufficient proof. If evidence cannot resolve the operation, alert and use the
existing operator-triggered retained-volume recovery route: gate native work,
terminate/fence old issuers and their connections, stop the old OpenSearch
process, and restart/reconcile before reopening admission. Do not automatically
restart the whole node on every timeout or promise bounded automatic recovery
from this ambiguity. This conservative failure behavior needs fault testing;
it requires no extra always-on coordinator service. See the [source audit](../validation/opensearch-coordination-semantics-2026-11-03.md).

On any restart, reconcile real cluster/index UUIDs, open/closed state, blocks,
primaries, reservations, and unresolved operations before normal admission.
Do not initialize the open count to zero. Unexpected indexes or identity
changes are correctness alerts, not implicit permission to recreate data.

## Public contracts and SDK projections

Keep existing job GET paths: `/v1/jobs/{job_id}` and `/v2/jobs/{job_id}`.
Use the existing direct job-response shape and canonical error envelope.
New phase fields are present on creation, get/list, retry, and logical webhook
snapshots. `failure_stage` is `null` outside failure. Indexing-only retries
clear the current embedded error while retaining internal attempt history.

API `status` and phase fields report the same authoritative state to every
caller. `completed` qualifies a phase; overall `done` requires both phases;
terminal means overall `done` or `failed`, not success alone. A satisfied
parsed-only wait is a client outcome, not a new job state. Celery task completion
is separate execution bookkeeping and never substitutes for either milestone.
Use the [completion vocabulary](opensearch-development-plan.md#52-phase-states-and-historical-completion)
and [client observation matrix](opensearch-development-plan.md#53-sdk-behavior)
for response/helper documentation and acceptance. In particular, describe a
job result as an immutable parsing result, not necessarily a completed
publication; it is available before overall success.

| Situation | Overall status | Parsing / indexing |
| --- | --- | --- |
| Waiting for upload or parse admission | Existing `waiting-file` or `pending` | `pending` / `not_started` |
| Parsing/conversion | Existing `running` or `converting` | `running` / `not_started` |
| Parsed, inline indexing not yet started | `running` | `completed` / `pending` |
| Indexing execution | `running` | `completed` / `running` |
| Explicit eligible retry admitted within its HTTP request | `pending` | `completed` / `pending` |
| Activated | `done` | `completed` / `completed` |
| Parsing failure | `failed` | `failed` / `not_started` |
| Final indexing failure or archive before activation | `failed` | `completed` / `failed` |

This example is the complete job response at parsing completion; existing
optional fields may be null. IDs/URLs and credits are illustrative fixtures.
Subsequent GETs can renew the ZIP URL for this same immutable revision.

```json
{
  "job_id": "job_example",
  "job_result_id": "rev_example",
  "namespace": "default",
  "document_id": "doc_example",
  "status": "running",
  "parsing_status": "completed",
  "indexing_status": "pending",
  "failure_stage": null,
  "source_type": "file",
  "data_id": null,
  "created_at": "2026-11-03T12:00:00Z",
  "progress": null,
  "error": null,
  "result": null,
  "result_url": "https://artifacts.example.com/rev_example.zip?signature=example",
  "result_url_expires_at": "2026-11-03T13:00:00Z",
  "file_name": "report.pdf",
  "file_extension": "PDF",
  "model": "base",
  "ocr_enabled": false,
  "duration_seconds": 30.0,
  "credits_spent": 1.0
}
```

On indexing failure this same response retains the result binding/URL and
actual charge, changes overall/indexing to `failed`, sets
`failure_stage=indexing`, and includes a standard embedded error. Use
`UNAVAILABLE` for native backend failures and `INTERNAL_ERROR`
for invalid internal input/mapping; `details.reason` distinguishes cases.
A charged parse must not report zero credits merely because overall status
is failed. This change does not alter existing parsing-failure billing rules.

### Indexing retry and errors

Both accepted POST retry paths have no request body. Keep normal job ownership
and write authorization; bypass parsing-credit reservation because no parsing
is started. Execute once synchronously in this request, with the same fixed
configurable ten-second deadline when OpenSearch is primary, or the separately
qualified PG-primary deadline during preparation/rollback. No new job/task or detached continuation;
use nonblocking API I/O and preserve the normal job/error response schemas.

A client socket disconnect alone does not cancel an already-running handler.
The owner selected finishing that handler's current execution within its
unchanged backend-specific deadline, with normal publication/failure events.
The client later queries the original job. Do not spawn or transfer work to
another handler/thread/task, extend the deadline or actively cancel solely
because the client disconnected. Actual handler cancellation, process exit or
deadline expiry stops forward indexing work: preserve a committed outcome,
attempt bounded settlement if possible, and otherwise leave existing guarded
recovery/sweep to settle abandoned state. Do not shield native writes/activation
past cancellation or create a detached continuation. Already-dispatched native
operations retain their uncertainty/fencing obligations. Failure to send the
HTTP response after successful commit never downgrades completion.

| Case | HTTP / canonical code / `details.reason` | Effect |
| --- | --- | --- |
| Eligible failed indexing, synchronous execution succeeds | 200 | Same job/revision, published done; no parsing or new charge. |
| Admitted execution fails | 504 / `DEADLINE_EXCEEDED` for timeout; 503 / `UNAVAILABLE` for backend unavailability; 500 / `INTERNAL_ERROR` for corrupt internal input | Persist indexing failure/outbox first; retain job/phase/result context in the canonical error. No automatic retry. |
| Already active indexing | 409 / `ABORTED` / `INDEXING_ALREADY_RUNNING` | No duplicate execution. |
| Historically `done` | 200 | Return existing completion; no restoration of an obsolete completed build. |
| Failed candidate superseded, including one whose successor failed | 409 / `ABORTED` / `INDEXING_SUPERSEDED` | No execution admitted. |
| Another active document job wins a concurrent admission | 409 / `ABORTED` / `DOCUMENT_JOB_ACTIVE` | Include its authorized `active_job_id`; no execution admitted. |
| Archived document, or parsing incomplete/failed | 400 / `FAILED_PRECONDITION` / `DOCUMENT_ARCHIVED` or `PARSING_NOT_COMPLETED` | No reactivation or parsing. A historically done job still returns its historical state. |
| Required canonical result lawfully expired under its existing lifecycle | 400 / `FAILED_PRECONDITION` / `INDEXING_INPUT_EXPIRED` | Use only with authoritative evidence of result expiry; no separate indexing-input age cutoff, signed-URL inference, silent reparse or charge. Unexpected missing/corrupt assigned input is an internal error/alert instead. |
| Missing or inaccessible job | Existing 404/403 authentication/authorization behavior | No existence disclosure beyond current conventions. |
| Service/admission unavailable | 503 / `UNAVAILABLE` / `INDEX_CAPACITY_BUSY`, `INDEX_STATE_UNCERTAIN`, or `LEXICAL_BACKEND_UNAVAILABLE` | Retryable; no empty search substitution. |

The new revision need not finish parsing before blocking old indexing retry.
Once its job wins durable acceptance, return 409 for retry of the older failed
candidate, without allocating another attempt or deferring execution. Reuse
document-level admission guards for both operations: if an eligible old retry
wins first, concurrent new-job admission conflicts while it remains active.
Retry remains on its original job/revision; no new retry job is introduced.

New-version upload has a simpler owner-selected rule: any existing nonterminal
job causes 409, even when its indexing deadline has elapsed. Do not settle that
job, emit a failure or create another job from the upload/admission path. Wait
for a committed terminal outcome from normal processing/recovery. The immediate
expired-state check below belongs to explicit indexing retry, not upload.

Before rejecting a stored pending/running attempt, check its actual committed
outcome and fixed deadline under the existing guards. An unexpired active owner
conflicts. An expired attempt is settled as completed or failed before this
explicit request can admit one new eligible execution; do not wait for the
periodic sweeper. Retain native-uncertainty/late-writer fencing and the total
request-operation budget. The original initial intent uses its creation time
and selected interruption threshold; a manually admitted attempt uses its own
persisted request deadline even before the execution claim. Expiry settlement
never launches work on its own.

No cross-attempt request idempotency key is added. Concurrent admission uses the
current-attempt guard; a competing active call receives 409, without joining
its wait or running another execution. A genuinely later call after another final failure
can intentionally admit another free attempt, subject to existing request
rate limits. An admission database failure is not reported as accepted.

Example supersession response, using the existing error envelope:

```json
{
  "success": false,
  "error": {
    "code": "ABORTED",
    "message": "A later update has been accepted for this document.",
    "request_id": "req_example",
    "details": {"reason": "INDEXING_SUPERSEDED", "job_id": "job_example"}
  }
}
```

### Webhook snapshots

Validate `webhook.url` and optional nonempty exact `events` before admitting or
charging a job. Omission retains `job.completed` and `job.failed`; an explicit
list selects exactly its members. Normalize duplicates. Keep the single URL,
existing signing/delivery mechanism, and event identity header; additionally
project that existing identity as `event_id` in the payload. Do not add public
state counters or delivery-order promises.

```json
{
  "event_id": "event_example",
  "event": "job.parsed",
  "job_id": "job_example",
  "job_result_id": "rev_example",
  "status": "running",
  "parsing_status": "completed",
  "indexing_status": "pending",
  "failure_stage": null,
  "parsed_at": "2026-11-03T12:00:30Z"
}
```

A completion snapshot uses `event=job.completed`, the existing webhook
`status=completed`, both phases completed, and `completed_at`. A failure
snapshot uses `event=job.failed`, `status=failed`, original `failed_at`, both
phase states, `failure_stage`, and the same standard embedded `error` object
as job GET. Parsing failure has no `job_result_id` unless a usable immutable
result exists. Parsing and terminal timestamps are their original milestone
times, not delivery time. Do not map every non-completion event to failure.

Retain existing completion result-delivery fields. Add the same bound result
delivery to `job.parsed` and to indexing-stage failure where available.
Delivery credentials/expiring URLs may be renewed for that bound revision;
renewal must not rebuild phase states or timestamps from the job's newer
state. Redelivery of an old failure after a successful retry remains the
original failure event. Consumers needing current state GET the same job;
ordinary SDK callers use get/wait/load. A distinct indexing attempt can emit
a new logical failure, while indexing-only retry never emits another parsed
event. Job completion is unique and irreversible.

Post-commit dispatch is a bounded notification attempt, not a prerequisite for
indexing or a committed HTTP success/failure response. Temporary submission
failure leaves the durable event for existing outbox recovery; customer delivery
does not gate the workflow. Bound submission work and test provider timeout at
parsed, completed and failed milestones. It must not trigger parser refund/
retry, rerun indexing or overwrite committed state.

### SDK behavior

Keep Node `JobFailedError.jobResult` and Python `JobFailedError.job_result` as
the existing response context, now containing phase/failure fields. Callers
load the retained parse with existing `jobs.load(error.jobResult)` or
`jobs.load(error.job_result)`. The loader checks parsing completion and the
bound result, not overall done. It may GET that same job to renew an expired
URL; it must never substitute the document's latest parsed revision.

Node `waitFor` and Python `wait_for` remain `search_ready` by default, with
`parsed` opt-in on parse/wait. Evaluate a satisfied parsed target before an
indexing-failure branch. Preserve overall `isDone`/`isFailed`; an early returned
response retains its actual running/failed status. Webhook subscriptions do
not select the wait target, and neither setting affects sweep eligibility.
Retry helpers
are `jobs.retryIndexing(jobId)` and `jobs.retry_indexing(job_id)` and await the
HTTP execution's final success/error, including retained-result context.
Disable automatic HTTP retries for these mutation helpers so one explicit
invocation cannot silently start another execution after a failed response.
Keep existing polling timeout types; carry `jobId`/`job_id` and
optional last `jobResult`/`job_result` in both SDKs. Each invocation has one
local timeout budget; resume by waiting on that same job, without a reupload.

Older responses lacking phase fields infer completed parsing only for overall
`done`; do not infer it for legacy `failed`. New server responses always expose
the phases. Existing clients omit events and retain terminal-only callbacks;
new events are opt-in. Exercise released SDK compatibility as well as modified
SDKs before cutover; additive fields alone are not compatibility evidence.

## Acceptance manifest and fault cases

The owner provided the existing [Oracle lab](http://100.77.144.81:18765/) for
reuse. Its [six-case replay and native explanation](../validation/opensearch-lab-reuse-2026-11-03.md)
passed without modifying the shared deployment/indexes. Use its frozen inputs
and an isolated mutation harness; the deployed compare API does not implement
revision publication or failure injection. Keep its provisional quality
labels and present host-resource limits explicit.

Freeze one manifest before the application comparison. It contains a fixture
ID/checksum, corpus kind/owner/namespace, document and revision identities,
unit count and input fingerprint, query text/options/route/top-k, expected
supporting evidence with revision/section identity, forbidden evidence, and
reviewer/date. Store result artifacts by fixture, transition, and backend.
Do not copy production credentials or private customer text into a public repo.

The original reviewed twenty-query business manifest and historical evidence
already exist. The [business replay](../validation/opensearch-business-ranking-2026-11-03.md)
preserved all 13 PG-found queries, with seven shared misses, but its final
absolute-evidence assertion failed; do not report it as a passing test or
select a replacement twenty-query set. KW-01 still needs to package the current
source/component fixtures and verify authorized private-package access; those
deliverables have not been asserted complete. KW-09 must correct the relative
PG-evidence gate, rerun the unchanged original baseline and add/run a separately
versioned reviewed extended set. See
[quality acceptance](opensearch-development-plan.md#81-align-the-regression-test-with-the-accepted-quality-gate)
and the [two-phase handoff](opensearch-development-plan.md#172-reproducible-handoff-package-required-not-yet-delivered).
Bundled/synthetic data can test mechanics but cannot supply invented business
judgments. Preserve existing review provenance and review added evidence before
running the extended set. Record actual lexical requests and separate cache
hits, small-corpus bypass and external agent fan-out.

| Case | Required observable result |
| --- | --- |
| Before candidate writes; partial candidate bulk visible; complete candidate unactivated | Only selected published hits/references. Review key evidence in all three ranking states, using actual BM25/RRF. |
| Candidate fails after partial writes; later explicit synchronous retry | Previous published revision remains selected; candidate/old-build records affect native statistics but never become eligible hits. Review evidence on every transition. |
| Activation followed by old/candidate cleanup, including partial/conflicting sweeps | New requests select new build; old pinned requests can finish; evidence and references remain correct before/during/after cleanup. Do not require identical rank order. |
| Duplicate delivery, owner expiry, paused worker resumes, older attempt returns late | No stale publication, downgrade, overwritten current build, or duplicate logical milestone. Orphans are revisited by cleanup. |
| Crash before/after parse commit or inline indexing start | Preserve result/charge and one parsed event; the existing 30-minute stale-job sweep settles interrupted indexing without automatic execution, using the indexing deadline/unstarted threshold and idempotent outcome/outbox updates. Manual retry can resolve expired state sooner. |
| Old parsed revision retried while sweep runs; API crash after retry admission before execution claim | Use the new request's durably recorded operation deadline, not the old parse/predecessor age. Leave its unexpired attempt alone; settle an expired abandoned attempt without execution. The original initial intent alone uses its own unstarted threshold. |
| Client disconnect with a live manual-retry handler; disconnect before write/during wait/after commit | Continue only that handler's current execution within its recorded deadline; persist normal outcome/events and let the client query the same job. No socket-triggered cancellation, task handoff, budget reset or new attempt. Preserve completion after response loss. |
| Slow synchronous retry, actual handler cancellation/process exit and unrelated API requests | Await native/database work without blocking the API event loop. Cancellation/expiry does not leave detached writes/activation; preserve committed outcomes and native uncertainty, with bounded settlement or existing abandoned-state recovery. |
| Visibility success then crash; activation acknowledgment lost; HTTP response lost after commit | Reconcile actual SQL commit; exactly one logical completion and correct publication/cache selection. |
| Retry vs new update; archive vs activation; later accepted update fails | Committed eligibility ordering wins; no old candidate revival; parsing result/charge and historical completion preserved. |
| Planned transition during parsing or pending/running indexing, including later abort | Preserve usable parsing and charge; settle interrupted indexing once without automatic resume. Publication-first remains done. A later eligible explicit retry uses a fresh attempt on the current backend/epoch. Exercise parse/failure persistence while control mode is non-serving. |
| Delayed Redis invalidation, old cache writer, empty published scope, body shortcut | Same captured published scope on all routes; real backend failures stay errors. |
| Long agent pause or old query spans activation/cleanup | Captured identity is retained; a collected lexical build may yield fewer/empty hits without retry; returned bodies/references remain revision-bound. |
| Agent recall fails natively after other evidence, through either harness | Whole request fails as unavailable; broad tool/callback catches cannot convert the failure into best-effort success. Stop further agent work and prevent successful cache/final-result writes. No-match and successfully recovered closed-index cases remain valid. |
| Concurrent cold opens, LRU close races native use, issuer crash or delayed close | One reservation/operation per identity; cap counts reservations/uncertainty; no release on timeout; conflicting transition blocked. |
| Read/write client dies but native operation continues; late write after empty cleanup | No reader close fence; lifecycle uncertainty retains reservations, writer tombstones catch late orphan records, and stale builds cannot activate. |
| SDK parsed/default waits; index failure; late failure webhook; result URL expiry | Existing load/get/wait flow suffices; no refund/reparse; original event snapshot and current job remain distinguishable. |
| API/worker rolling restart, backend epoch switch, OpenSearch node loss | Old claims cannot publish across epoch; historical done unchanged; no runtime PG lexical fallback. |

Run small deterministic lifecycle/fault cases before target-node mixed load.
Then apply the accepted one-hour ten-times real burst profile, 50 external
peak QPS, five-minute catch-up cohort, warm P95 <=1 second, immediate cold
P95 <=3 seconds, five-second capacity wait, and successful wait-to-first-lexical
P95 <=8 seconds. Include the real parse-task boundary and synchronous retry HTTP path, PG coordination, OpenSearch, remote
storage, and turnover. No full parser or simulated parser occupancy is needed.
Measure coordination overhead and unresolved-operation handling as part of
this path. The native seven-case PoC did not exercise these application gates.

## Installation, rollout, and recovery runbook contracts

These ordered steps describe scripts/configuration to implement and rehearse;
they are not executable scripts or claims that AWS configuration exists.

Installation input must pin the verified OpenSearch 3.9.0 Linux ARM64 image
and repository-s3 plugin, JVM heap, private endpoint/TLS/authentication,
EC2 instance profile and service access identities, EBS mount/encryption,
S3 region/buckets/prefixes, remote repositories, snapshot repository, cluster
name/identity, and the expected namespace catalog. Use the accepted single
r7g.xlarge/16-GiB-heap/100-GiB-gp3 baseline for capacity acceptance. The
owner-provided Oracle lab is available for reference replay and isolated
mechanism validation; its present resource configuration does not qualify
that target baseline. Formal qualification uses isolated staging resources;
record their actual configuration and any difference from the intended release
topology. Do not provision new resources during this task.

Use distinct remote segments, request-durable translogs, remote cluster state,
and independent full snapshots (`remote_store_index_shallow_copy=false` and
`shallow_snapshot_v2=false`). Installation must fail if required capabilities
or repository settings differ from the verified combination. Private service
networking and S3 gateway routing stay within the accepted cost baseline.
Instance-profile access scopes each repository prefix; services never receive
S3/OpenSearch admin credentials in public payloads. Real AWS IAM/TLS/KMS,
repository separation, request durability, and loss-of-access behavior require
validation beyond the completed S3 emulator cases. Reject S3 TTL deletion of
repository objects; use OpenSearch repository APIs and retention rules.

| Stage | Ordered operator/script behavior and completion evidence |
| --- | --- |
| Release 1: prepare | Add compatible schema/contracts behind backend control; keep PG primary; install and verify target/durability privately; freeze published revision/token coverage; backfill deterministic builds with separately limited throughput; reconcile updates/archives; finish quality/load/fault and recovery drills. No long backfill in the outage window. |
| Pre-window | Confirm PG rollback copy matches current published scope, all required assets/input and OS coverage are reconciled, old binaries cannot publish outside the new gates, snapshot/recovery path and alerts work, rollback duration is measured, and the operator is available. Set an abort time equal to first customer impact plus two hours minus measured restoration duration and margin. Missing restoration evidence blocks cutover. |
| Release 2: maintenance | Record first-impact time; stop new affected API/job admissions; fence/settle unfinished indexing under the selected interruption policy without waiting for every parse to finish. Reconcile final revision/namespace/checksum coverage, commit backend/epoch change and start compatible API/workers. Run published-scope search/citation/result/webhook smoke cases; reopen traffic only after checks. Interrupted jobs remain failed until explicit retry. |
| Abort or deliberate rollback | Before the abort time, stop/fence OS publication, preserve parsing and settle interrupted indexing, verify/repair asynchronous PG-copy coverage and archives, select PG with a new epoch and clear incompatible caches. Resume admission with compatible inline/manual synchronous behavior, not the interrupted attempts. End-to-end service restoration ends the outage clock. Do not regress completed jobs, automatically revive failures or roll back to a combined-only binary/schema. |
| Observation | Start the accepted 14-day initial observation after cutover; review quality, errors/backlog, copy lag, native usage, S3 cost, snapshots and recovery. Do not automatically stop PG writes or delete its table after fourteen days. Retirement is a separate stability decision. |
| Retained-volume recovery | Detect/alert from existing off-node runtime; operator gates native work and fences old issuers; confirm sole writer, original healthy volume and identity; attach/mount/start/reconcile; reopen indexes through admission; verify every required published selection and references through the API; resume work. Count from original incident, including detection and human delay, against the internal 15-minute objective. |
| S3 recovery | Gate/fence old writers, select the correct recovery route, verify repository access and corpus coverage, restore remote data/state into a replacement or restore an independent full snapshot as appropriate; reconcile PG publication metadata against actual recovered build coverage; batch opens/close restored indexes within the 400 cap; verify required published corpus through the API before serving. Count all prior failed recovery time against the internal one-hour objective. |

Remote data/state recovery targets the latest acknowledged native state. An
independent historical full snapshot may be older than PG's published pointer.
Do not synthesize the missing published records from PG, silently rewind done
jobs, or claim full restoration from a successful snapshot API response alone.
Keep affected scopes unavailable and escalate if native recovery cannot cover
the required published builds. The accepted RTO is an internal recovery goal,
not a guarantee against loss of every recovery copy. Record the actual
recovered boundary and any unrecoverable interval explicitly.

Hourly snapshots must include the expected closed and open business catalog.
Retention is seven days with at least three fully successful, complete-covered
snapshots; incomplete/partial snapshots do not count. Do not close/reopen an
index as part of retention without the same lifecycle module. Verify the
chosen native snapshot behavior with idle close, candidate writes, and
catalog changes; a backup operation does not independently publish a build.

Snapshot scheduling records the expected catalog and native snapshot identity,
then submits/polls outside SQL transactions. Do not open all closed indexes
for backup or impose an exclusive catalog-wide lock for the snapshot duration.
Route concurrent lifecycle decisions through the same index module, honor
native snapshot-related refusals, retain charged reservations until a close
is confirmed, and retry/alert incomplete coverage instead of counting a
partial snapshot as successful. Exercise snapshot/LRU overlap in the mixed
load; do not assume this interaction passed in the seven isolated PoC cases.

Full-snapshot restoration creates open indexes. Reserve the entire selected
restore batch **before** submitting it, including any already open indexes
on the target. Verify restored identities/coverage and close idle restored
indexes through the normal protocol before reserving another batch. Do not
restore the whole catalog first and enforce the cap afterward. Recovery is
the only path allowed to rebind known restored UUIDs, under its new backend
epoch and before normal traffic admission.

Run periodic checks outside the OpenSearch host through existing runtime and
Logfire error alerts. Preserve the accepted immediate failure/correctness
alerts, indexing intent older than five minutes, temporary PG-copy failure or
lag over thirty minutes, and failed/partial/incomplete or over-two-hour-old
complete snapshots. Add operation identity and unresolved-state age to the
same alert context; coalesce repeated reports. Metrics distinguish parse-task backlog,
claim, bulk, visibility, activation, admission, open, first search, native RPC
uncertainty, cleanup, snapshot coverage and recovery. Operational alerts do
not change historical successful job state.

## What remains before validation and implementation

This document selects storage ownership, internal identities, transaction and
claim protocols, revision-bound reads and exact-build cleanup, native-RPC recovery,
wire/SDK projections, acceptance cases, and ordered operational contracts.
The isolated implementation and validation state are recorded in the
[progress report](opensearch-implementation-progress.md); they do not establish
production readiness. Preserve the current [integration constraints](opensearch-integration-constraints.md)
and resolve remaining integration gaps before expanding capacity tests.

The original twenty-query manifest and historical evidence exist; preserve them
with their recorded failing final assertion. Remaining preparation is KW-01's
baseline packaging/access/reproduction work, KW-09's corrected-gate rerun and
reviewed extended set, actual target-node/AWS configuration inputs and the
source-matched integration/load/recovery harnesses and runbooks from their
respective tasks. These future supplements do not block KW-01's baseline
closure, and this document revision does not authorize packaging, implementation
or test execution. Final bulk sizes, shared-worker indexing budgets, qualified LRU
watermarks, measured latency/throughput, S3 cost, and actual RTO remain test
outputs. No additional parsing-throughput campaign or numeric latency promise
is needed to close the design.
