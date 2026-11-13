# OpenSearch lexical v1

Status: accepted product decisions, reconciled with the current engineering
baseline on 2026-11-05. Isolated candidate/component evidence exists; formal
service integration, target-environment qualification and production delivery
are not accepted as complete. This plan does not authorize application
implementation, new resources or release. The owner separately authorized
bounded checks on the existing Oracle server; see the
[verification receipt](../validation/opensearch-handoff-oracle-check-2026-11-04.md).
Search stays on PostgreSQL until the migration check passes and the single
global switch is turned on.

The [implementation design](opensearch-engineering-v1.md), selected on
2026-11-03, consolidates concrete persistence/publication, cross-process
coordination, conservative open/close timeout recovery, wire/SDK examples,
fault cases, and ordered operational contracts. Use the current
[development plan](opensearch-development-plan.md) for tasks and acceptance,
the [integration constraints](opensearch-integration-constraints.md) for the
reader-free runtime contract, and the [progress ledger](opensearch-implementation-progress.md)
for measured scope. Historical measurements keep their original limits; they
do not establish target capacity or formal delivery.

Formal integration/release acceptance takes place in **staging**. Oracle is
used for isolated development checks and early measurements; production
deployment and observation come after staging signoff. The developer handoff
materials comprise the fixed candidate source, existing Oracle setup/private
fixtures, reproduction commands and scoped results, with later task evidence
appended as it becomes available. They are not another environment or a
requirement to assemble a ZIP.

Validation environment update, 2026-11-03: reuse the owner-provided
[Oracle comparison service](http://100.77.144.81:18765/). Its six-case live
replay matched the frozen reference; see the [reuse report](../validation/opensearch-lab-reuse-2026-11-03.md).
Preserve shared fixtures and isolate mutation cases. Its current host/resources
and provisional judgments do not establish the target-node or business gates.

Owner clarification, 2026-11-02: the old PostgreSQL lexical writes and data
exist only for temporary migration evaluation and rollback. After stability
review, stop those writes and clean up obsolete lexical data only through a
separately approved retirement change. PostgreSQL
must not supply OpenSearch's permanent backup, disaster-recovery rebuild,
or post-snapshot lexical catch-up. Earlier statements to that effect are
withdrawn. The placement of body text, vectors, citations, assets, and normal
application metadata remains as originally agreed; it does not authorize
using them as an OpenSearch disaster-recovery source.

Durability decision, 2026-11-02: the owner accepted remote segments and
request-durable translogs on S3, remote cluster state, and a distinct full
snapshot repository for the first-version validation baseline. Retain the
accepted node/heap/EBS/private-network specification and reuse the free S3
Gateway route. S3 is the principal additional cloud-service expense;
engineering time, optional paid services, or required capacity expansion
must be assessed separately. Actual monthly usage and performance are not
accepted results, and no numeric cost ceiling has been chosen.

Use explicit `index.translog.durability=request`, enable remote cluster
state, and disable shallow copying for the independent full snapshots.
Retain hourly snapshots for seven days, preserving at least three complete
successful coverage-verified snapshots. Recovery does not use PostgreSQL
lexical data or regeneration. This is validation-design acceptance, not
resource provisioning, implementation, or production capacity acceptance.
See the [accepted baseline](opensearch-publication-readiness.md#accepted-opensearch-durability-validation-baseline),
[official evidence](../validation/opensearch-durability-2026-11-02.md), and
[cost assessment](../validation/opensearch-durability-cost-2026-11-02.md).

Completion-contract update (2026-11-01): the owner subsequently accepted
[ADR 0025](../adr/0025-separate-parsing-completion-from-search-readiness.md).
Parsing completion must be observable independently, while terminal job
success requires search readiness for the processed revision. Statements
below that leave a job complete after an unsuccessful or unconfirmed index
write are superseded for the planned implementation. The current visibility
basis and its verification are recorded in
[publication readiness](opensearch-publication-readiness.md).

Execution amendment: [ADR 0026](../adr/0026-publish-search-readiness-from-background-tasks.md)
now selects one synchronous indexing execution at the end of the existing parse
task, after committing the readable parse result and dispatching subscribed
`job.parsed`. Manual retry runs synchronously in its HTTP request. Neither path
creates an indexing task or performs automatic retry. Each execution has a
fixed configurable ten-second limit when OpenSearch is primary, including connection/opening, preparation,
writes, visibility and publication. Failure preserves parsing/result/charge
and emits subscribed `job.failed` with indexing context.

Owner amendments (2026-11-05): use one `refresh=wait_for` bulk when the revision
fits a configured byte budget; split oversized revisions by serialized bytes,
preserving whole-revision activation and the single execution deadline. No
explicit-refresh replacement or fixed 200-record batch rule is selected.
During manual retry, a client disconnect alone does not cancel the original
running handler: it may complete the current execution within its deadline.
Handler cancellation/process exit uses existing failure settlement/recovery;
never transfer it to another task or leave detached native work. The client
can query the original job after losing its response.

Reuse the existing API and Celery/gevent deployment. An indexing failure is a
recorded phase outcome, not a parser exception that triggers reparse/refund.
Search-ready SDK waiting raises on it; parsed-only waiting succeeds. The server
flow and overall phase states do not depend on the client's waiting target.
Historical separate-queue measurements below do not validate this amendment;
the selected mixed-load and worker/request-occupancy gates remain required.

## Serving

Owner clarification: if an agent retrieval encounters an unrecovered OpenSearch
service failure, fail the whole request as temporarily unavailable. Do not let
the model continue with other tools and return a successful final result or
cache partial evidence as success. Both supported agent harnesses obey this
rule; ordinary zero hits and correctable tool arguments still permit continued
exploration. Existing cache/small-corpus paths that require no failing native
operation remain valid without a new health probe. See the
[agent failure contract](opensearch-development-plan.md#64-agent-retrieval-and-native-failure-propagation).

Lexical backend selection uses one global switch. Individual documents and
revisions do not select different backends. Separate per-revision indexing
state records search readiness and indexing failure, as required by ADRs 0025
and 0026.

Before the switch, search reads the PostgreSQL token table. New publications
also write OpenSearch, so behavior for readers does not change.

Each authorization-scoped namespace has its own OpenSearch index, keyed by
corpus kind, resolved owner and namespace, so IDF stays inside that scope.
The indexes live on one cluster that can close an index.
Knowhere maps a namespace to its index. Callers still pass only the
namespace. Knowhere checks namespace access in PostgreSQL before it queries
OpenSearch. OpenSearch accepts only Knowhere service roles and is not a
second authorization system. An index is created on the namespace's first
publication, not in advance.

Proactive LRU turnover was accepted for validation on 2026-11-03, replacing
the earlier capacity-only eviction trigger. Keep recently used indexes open
and reclaim idle indexes in the background to retain opening headroom.
Use 360/320 as configurable starting validation candidates; final production
watermarks require mixed-load qualification. A closed index keeps
segments on disk and releases indexing/search runtime structures; cluster
metadata remains a resource cost. A writer ensures its index is open before
writing. A hot search uses captured index/build bindings directly without an
open/UUID/registry preflight. A recognized closed-index response allows one
bounded lifecycle recovery and replay of the original search bindings;
unknown failures and known missing indexes retain their error handling.
OpenSearch does not choose which indexes
stay open. Knowhere closes the least recently used open indexes so the
number that stay open fits the configured cap. The owner selected 400
simultaneously open business namespace indexes for the first version;
opening reservations count toward admission. System/plugin indexes are not
namespace eviction candidates, but their resource use must be included in
target-node capacity validation. One machine can therefore keep
many namespaces on disk. The open indexes are the ones recently written or
searched.

Opening an index waits until its primary shard is active. The Open Index API
exposes 30-second default timeout parameters; these are not a typical open
time or an end-to-end guarantee. An already open namespace does not pay the
reopen cost. Indexing must override native defaults with its remaining
whole-execution budget; a 30-second native default cannot extend the selected
ten-second indexing deadline.

Current amendment (2026-11-04): LRU may close during native reads or writes.
Active requests and retained writer `index_use` credentials are not close
fences; request completion is not eviction eligibility or slot release.
Preserve writer ownership/expiry and stale-writer fencing. Recover recognized
closure with one bounded read replay. A write failure terminates the indexing
execution; only a subsequent explicit retry may try again. Do not add reader
close exclusion. Only confirmed closure with no outstanding
lifecycle issuer releases the reservation; uncertainty still counts.
The open-namespace cap is configured at 400, with a default
five-second configurable search admission wait when capacity is unavailable.
Honor a shorter caller deadline. At expiry return a retryable busy outcome;
already open namespaces need no new opening slot. This budget does not set
OpenSearch operation timeouts or an end-to-end request SLA. Capacity
acceptance requires populated target-node qualification; current component
proofs and limits are recorded in the development plan.

The [2026-11-02 local measurement](../validation/opensearch-open-close-2026-11-02.md)
used 370 synthetic namespace indexes and ten swaps per target. Opening to
primary readiness took median 83.7 ms for 32 units and 71.5 ms for 45,000
units. Closing an idle victim, opening the target, and completing both first
channel searches took median 116.9/104.3 ms and largest samples 263.2/144.7 ms.
This was an isolated 2-vCPU VM, 1-GiB heap, mostly closed indexes, warm
filesystem cache, and no S3 remote durability/state or production mixed load.
It does not establish production timing, an open-index cap, or a waiting
budget. The historical Elasticsearch 6.4.2 report at 12,000/43,000 indexes
is a separate 2019 observation, not a timing estimate for Knowhere. See the
[lifecycle policy and timing limits](opensearch-publication-readiness.md#index-slot-coordination-and-measured-local-cost).
The owner accepted that local operation overhead as sufficient to retain
on-demand opening/closing in the first-version design. The owner selected
400 simultaneously open business namespace indexes as the configuration cap.
Validate the current 370-namespace set and the 400-namespace boundary,
including system-index overhead, on the target node under the agreed mixed
load. This is not a verified production capacity. The owner-selected
five-second wait is a capacity-admission policy, not a production timing
measurement. Actual production timing remains pending. Proactive turnover
and its proposed watermarks are recorded in the
[LRU design](opensearch-publication-readiness.md#proactive-lru-turnover).
No separate fixed idle timeout is selected. Preserve the final reader-free
lifecycle protocol in the [integration constraints](opensearch-integration-constraints.md),
including reservation reconciliation; target-node qualification remains open.

Official support covers the close/open primitives, including retained disk
data and shard recovery on reopening. It does not endorse the proposed
360/320 LRU watermarks or certify the node's capacity. Qualify recovery/first-
query overhead, reopen frequency, active-operation safety, and explicit
closed-index snapshot coverage on the target configuration before accepting
these candidate settings. See the
[support boundary](opensearch-publication-readiness.md#official-support-boundary-and-costs).

After the switch, a lexical query reads that namespace's index and
is limited to the document's published revision. A replacement that has only
finished parsing does not become the published revision until its index is
ready and activation is complete, as recorded in
[ADR 0027](../adr/0027-activate-document-revisions-only-after-search-readiness.md).
Path and content are queried
separately and fused with the current reciprocal rank fusion:
path weight 1, content weight 2, k = 60. Canonical text, vectors, page/asset
metadata and citation bindings keep their existing PostgreSQL placement.
Image/table/page-asset bytes keep their existing object-storage/artifact
placement; this migration does not move those bytes into PostgreSQL or
OpenSearch. Retrieval hydrates through the existing canonical references.

OpenSearch stores the tokens already produced by the current jieba tokenizer.
Path tokens and content tokens are written as separate whitespace-separated
fields. The index analyzer is whitespace. BM25 uses k1 = 1.5 and b = 0.75.
The Lucene IDF formula is accepted for this version. The current negative-IDF
floor is not ported.

Original-text indexing or replacing jieba with smartcn is a separate future
decision, not a scheduled consequence of token-table retirement or a v1
deliverable. Any tokenizer change needs versioned inputs/mappings, migration
planning and affected quality/performance checks before authorization.

Retrieval keeps using the published OpenSearch revision while a replacement
is pending or has failed indexing. A new document without a published
revision contributes no candidates. Document allowlists narrow this same
published scope; an empty eligible scope returns the ordinary empty result.
Current v1/v2 ingestion rejects an explicitly different namespace for an
existing `document_id`; omitting it retains that document's scope. Preserve
this existing behavior, including demo/private boundaries and migration
rollback. A namespace-changing replacement is not a new feature in this plan.
The owner clarified on 2026-11-02 that no explicit-version search mode or
special readiness error is needed. The proposed `REVISION_NOT_READY` HTTP
409 branch is withdrawn. Observe readiness through job states, SDK waits,
and webhook events, as recorded in the
[retrieval scope contract](opensearch-publication-readiness.md#accepted-retrieval-scope-and-readiness-observation).

Default namespace queries search published revisions only. A newly added
document with no published revision is excluded until it is ready and
activated, without blocking other searchable documents. Do not add notices
or warnings about unready documents to normal retrieval responses. Client
documentation explains the two phase states: result-only clients can proceed
at parsing completion; search clients wait for overall completion, which
always requires search readiness.

Once the index is open, the synchronous indexing execution uses
`refresh=wait_for`, checks every bulk item, and confirms visibility before
terminal job completion. Parsing completion and result delivery happen at an
earlier, independently observable milestone. The refresh interval is
configurable; thirty seconds is only a reference, not a hard deadline.
The synchronous indexing execution ensures the index is open after the durable
parsing-to-indexing handoff and before its writes. Speculative opening during
parsing is not a prerequisite or a selected optimization.
After launch, the time to open a closed namespace index is recorded. That
record checks the plan. It is not the way the delay is first discovered.

## Publication

On the OpenSearch serving path, the initial database transaction persists
candidate revision data and parsing results without switching the revision
selected for retrieval. The published revision pointer changes only after
complete OpenSearch indexing, search-visibility acknowledgment, and activation
checks. PostgreSQL hydration must use the selected published revision as well.
This publication selection is specific to retrieval. Default document-content
reads select the latest successfully parsed revision independently of indexing
readiness. Both views share immutable revision data; preserve the selected
pointer/storage representation in the engineering design and verify it during
integration. Retrieval references keep their hit's
revision when opening text, page assets, or citations.
After formal OpenSearch cutover, preserve the existing token-table writer
in a background thread during migration evaluation. The same create/update-index
entry point persists durable copy intent with publication and submits it to a
bounded thread pool after commit; no per-copy job/task. Run COPY on its own
connection/session/transaction using retained immutable revision data. Pool
saturation or restart leaves pending work for existing maintenance to resume.
Keep long COPY outside shared publication locks so it does not block another
OpenSearch activation; validate both lock and resource contention. Track and
repair failed or missed copies idempotently. OpenSearch publication determines
job completion; a failed legacy copy does not downgrade a completed job or
change billing. While PostgreSQL remains the live backend before cutover,
its own index must still be ready before overall completion; continue accepting
new documents under a separately qualified longer PG execution budget.

The token table remains a temporary migration rollback copy until OpenSearch
is accepted as stable. Before deliberately switching back, verify that all
required current published revisions and archival state are covered,
including pending COPYs and failed copies being repaired. Retention alone
does not imply that an asynchronous copy is current.

After OpenSearch is accepted as stable, a separately approved retirement
change can retire this legacy COPY/dual-write path and clean up obsolete
PostgreSQL lexical data; stability or elapsed observation days do not trigger
that deletion automatically. Neither this copy nor
other PostgreSQL document data is an assumed OpenSearch recovery source.

Tokens are frozen for the immutable revision/input and sent by a claimed
indexing execution. A physical native `_id` is a stable hash of
`(build_id, unit_id)`, with document/revision identity retained in fields.
Replaying a bulk within the same still-owned execution overwrites the same
IDs with identical content. A different indexing execution gets a fresh build
and different native IDs even for the same revision, so a late old writer
cannot overwrite the new execution's records. Revision/unit alone is not the
native identity. This version does not tokenize page by page during parsing.

After a replacement is ready and activated, clean up the old revision's
lexical records in the background. Accepted amendment on 2026-11-04: do not
wait for old readers. Existing queries retain their captured build bindings
and return actual hits with normal HTTP 200, even if cleanup removes the
updating document's hits or leaves an empty list. Do not retry the version
race, substitute the replacement, or emit an updating error. Returned bodies,
assets and citations still bind the hit revision; real native failures retain
their error/recovery handling, including separate LRU reopening. Later queries
use the new publication. Keep the namespace index and new revision's records;
delete only the exact retired build, never the current published build. Do not
clean up the published old revision while its replacement is pending or failed.
The earlier reader-drain requirement is superseded. The isolated implementation
passed targeted checks; production acceptance is pending. See the
[validation report](../validation/opensearch-unlocked-reads-2026-11-04.md).
See the [cleanup policy](opensearch-publication-readiness.md#accepted-old-revision-lexical-cleanup).

Old parsing results, body data, page assets, and references follow existing
retention rules. A returned reference keeps reading its original parsed
revision after lexical cleanup. Temporary links keep their existing expiry
behavior, with the SDK/reference entry point obtaining a valid link for that
revision. Cleanup does not establish indefinite data retention or undo an
already completed publication.

Archiving a document removes its records from retrieval. The owner selected
**close and retain empty namespace indexes for v1**. Unneeded document/build
records are still cleaned normally. After eligible cleanup confirms emptiness,
close through existing maintenance/lifecycle coordination and retain the
registry/native identity; a later upload reopens the same index. Whole-index
deletion/recreation is a future feature, not a v1 prerequisite.

An empty published scope may still contain a first-publication candidate,
active/uncertain writers or retained records; do not confuse it with confirmed
native emptiness or bypass writer/cleanup eligibility. No new reader/use close
fence is introduced. A missing established index remains a recovery fault.
Track retained empty/closed catalog counts and metadata/disk growth. See the
[development-plan decision register](opensearch-development-plan.md#15-remaining-decisions-and-configuration-work).

Accepted on 2026-11-02: allow archival after parsing completion while
indexing is pending or running. Archival excludes the document from subsequent
retrieval and prevents later workers from reactivating or publishing it.
If the job is unfinished, settle overall/indexing failure with parsing still
completed and an error explaining archival. Preserve the parsing result,
do not refund its charge, and use the existing subscribed `job.failed` event.
Do not admit indexing retries for archived documents. A job already completed
before archival retains `done` and its completed phase states. Coordinate
archival and activation atomically; physical lexical cleanup can follow in
the background. See the
[archival policy](opensearch-publication-readiness.md#accepted-archiving-during-indexing).

## Revision publication

Accepted on 2026-11-02 for the planned migration; isolated candidate evidence
is distinct from formal service acceptance. Prepare
the immutable candidate in OpenSearch and confirm complete item success and
search visibility before publishing it. In one PostgreSQL transaction,
recheck document/job/attempt eligibility, switch the published revision,
advance the authoritative internal retrieval-cache generation, mark indexing
and overall completion, and persist the subscribed completion outbox event.
Deliver notifications after commit using the retained event identity.

Retrieval selects a coherent published-revision/generation snapshot before
cache-hit acceptance and uses it for OpenSearch filtering and body/reference
hydration. Delayed Redis invalidation cannot authorize an obsolete cache hit.
Keep OpenSearch calls outside PostgreSQL transactions. After an interrupted
or ambiguous commit, inspect committed metadata: an unpublished candidate
remains outside scope; a committed publication keeps its new selection and
continues delivery of the original outbox event. Coordinate activation with
archival and supersession, and recover under the existing indexing retry
policy without reparsing or another charge. See the
[publication protocol](opensearch-publication-readiness.md#accepted-revision-publication-protocol).

## Business retrieval-quality acceptance

Accepted on 2026-11-02: freeze an initial set of 20 real business queries with
known expected supporting evidence. Cover common terms, rare terms, multiple
terms, path matches, and different namespace sizes. Compare final evidence
under matching corpus revisions and request settings, exercising the lexical
adapter rather than relying on cache or small-corpus bypasses.

Use native Lucene BM25 in the first release. Preserve the marked evidence
that PostgreSQL returns for the same corpus revision and request options.
Resolve clear relevance regressions or incorrect citations, permissions,
and revision selection before cutover. Record evidence both backends miss as
an existing quality gap. The result order and membership can change while
the PostgreSQL evidence remains available. The
[isolated business replay](../validation/opensearch-business-ranking-2026-11-03.md)
preserved all 13 PostgreSQL evidence hits on the frozen 20-query sample. See the
[quality gate](opensearch-publication-readiness.md#accepted-business-retrieval-quality-gate).
That historical run failed its final absolute-evidence assertion on seven
shared misses; it is not a green test. KW-01 still needs baseline packaging
and access verification. KW-09 corrects the relative evaluator and reruns
the unchanged original set plus a separate reviewed extended suite; do not
replace the original twenty queries or relabel their historical results.

## Migration

Accepted on 2026-11-03: use two releases. Release 1 deploys preparation,
resumable backfill, and reconciliation while PostgreSQL continues serving
retrieval. Copy only non-archived documents' current published revisions
using existing token rows without reparsing or retokenizing. Reconcile
ongoing publication/replacement/archival changes.

Release 2 formally enables OpenSearch after final reconciliation and coverage
checks, using a nighttime maintenance window with up to two hours of total
downtime, accepted on 2026-11-03. Count final drain/reconciliation, deployment,
switching, readiness verification, and any necessary restoration within that
same window. Reserve a rehearsed restoration margin and finish initial
backfill before the window. The five-minute normal indexing catch-up target
remains unchanged. Verify current revision and
unit identities/counts and input consistency, alongside the accepted quality,
focused capacity, publication, and recovery gates. Then activate the single
global switch and resume publication. Keep the old PostgreSQL lexical writer
as an asynchronous observation-period rollback copy, as described above.
See the [rollout plan](opensearch-publication-readiness.md#accepted-two-release-rollout-and-asynchronous-postgresql-lexical-copy).

## First observation period

Accepted on 2026-11-03: observe the formally switched service for an initial
fourteen days, starting after successful cutover and service restoration.
Maintain the asynchronous PostgreSQL lexical rollback copy and monitor its
lag/failures. Review real retrieval quality and latency, indexing backlog,
LRU reopening, version/namespace correctness, and backup coverage together
with prior acceptance evidence.

At the review point, assess whether actual traffic/evidence supports stability
or observation should continue. Legacy-write retirement and lexical-data
cleanup require separate later decisions after stability is confirmed.
Elapsed time alone triggers neither action. A deliberate migration rollback
can be evaluated during observation after verifying PostgreSQL coverage;
ordinary outages still follow the accepted OpenSearch-native recovery routes.
See the [observation policy](opensearch-publication-readiness.md#accepted-first-observation-period).

## Migration rollback compatibility

The owner confirmed that PG-primary operation continues accepting new uploads
and updates. Parsing results can be returned first, while search readiness and
completion wait for the PG index. Use the same create/update-index entry point
and one-execution policy, with a separately qualified longer PG deadline and
compatible HTTP/gateway/SDK timeouts. The OpenSearch ten-second default does not
apply here. The non-gating background thread is the temporary PG shadow copy
while OpenSearch serves, not a reason to report readiness before PG is ready.

Accepted on 2026-11-03: deliberate rollback switches the global lexical
backend and authoritative indexing target to PostgreSQL while keeping the
published client contract. Preserve phase states, SDK waiting options,
webhook subscriptions, revision-bound results, and free eligible indexing-only
retry. Initial indexing runs inline in the parse task and manual retry runs
synchronously in its HTTP request; overall `done` waits for committed,
search-ready PostgreSQL lexical publication. Completed jobs remain completed;
failed jobs require explicit retry, with successful parsing results retained.

Verify current published coverage and archival consistency, settle in-flight
publication, fence late OpenSearch activation, and establish coherent caches
before service resumes. Keep a compatible application release rather than
reverting to the old single-stage client behavior or removing state metadata.
This is a controlled migration rollback, not automatic fallback or disaster
recovery. See the [rollback scope](opensearch-publication-readiness.md#accepted-migration-rollback-scope-and-client-compatibility).

Later owner clarification: planned switching may interrupt unfinished indexing
rather than wait for every accepted job to end. Preserve the usable parse and
charge, fence activation and record indexing-stage failure with its subscribed
event and transition explanation. After service resumes, the user may explicitly
retry the original eligible job; do not automatically resume interrupted work,
including after an aborted switch. Already completed jobs remain completed.
A publication gate cannot become a parsing failure; a first attempt created
only after service resumes can use the selected backend normally. Apply the
[transition state matrix](opensearch-development-plan.md#1431-in-flight-jobs-during-a-backend-transition).

## Operational alert rules

Accepted on 2026-11-03: alert on service unavailability, final non-archival
indexing failure, and unexpected version/namespace correctness failures;
indexing publication age above five minutes; final legacy-copy failure or
oldest outstanding copy age above thirty minutes; and snapshot failure,
incomplete coverage, or a complete-success age above two hours. Alerts do
not alter job states or automatically switch retrieval backends.

The owner already has Logfire error-log alerts. Official OpenSearch Alerting
supports cluster metrics, but application work-state checks and off-node
outage detection require Knowhere-side observation. The accepted delivery
approach uses periodic checks from the existing Knowhere runtime and structured
errors through the existing Loguru/Logfire bridge, with repeated-condition
throttling and controlled verification of actual notification delivery.
Historical alert-design acceptance is not evidence of live rule installation
or verified notification delivery. Track candidate and formal acceptance in
the progress/development plan.
See the [alert rules and delivery contract](opensearch-publication-readiness.md#operational-alerts-and-existing-logfire-delivery).

## Write failure

The existing worker/broker may redeliver a parse task. Preserve that recovery
mechanism while checking durable phase/ownership before any parsing, billing
or upload work. Reuse completed parsing; do not restart an already-started
indexing execution, create another attempt from delivery count or let an old
message execute a newer manual retry. An eligible original initial attempt
without a start can claim its first execution once. Resolve committed success
or settle expired failure; lock expiry alone cannot authorize stale publication.
Stable namespace and build/unit record identities complement these guards;
they do not authorize automatic write replay. See the
[duplicate-delivery contract](opensearch-development-plan.md#911-duplicate-delivery-and-processing-ownership).

Each attempt has **one complete indexing execution**. Initial execution runs
in the parse task; an eligible explicit retry runs in its HTTP request, using
the same original job/revision and retained input. Record start/owner before
connection/opening and use a fresh build if this attempt writes. No automatic
execution retry, failed-item/build replay, indexing queue or detached task.

When OpenSearch is primary, use the fixed configurable **ten-second
whole-execution deadline** for both paths, including input preparation, connection/opening, writes, visibility
and guarded publication. Heartbeat/progress cannot extend it; large inputs
are subject to the same limit. Bound real native/database waits and task/request
occupancy, then bound and measure failure-state settlement overhead separately.
A ten-second limit is not evidence that every input can finish successfully.

Any unsuccessful outcome marks indexing/overall failed and emits subscribed
`job.failed` with `failure_stage=indexing`; retain parsing completed, its result
and charge. The parse task ends normally after recording this failure rather
than invoking parser autoretry/refund hooks. A search-ready wait raises, while
a parsed-only wait/load still succeeds. Worker/API disappearance, including
between parsed commit and execution start, is settled by extending the existing
stale-job sweep task on its unchanged 30-minute schedule. It repairs recorded
current-attempt failure or expired indexing using the indexing deadline or
bounded unstarted threshold, with completed parsing/result/charge preserved.
The original initial intent uses its own creation time for that threshold;
manual admission persists the new request's operation deadline even before
execution starts. The original parse's age cannot expire a new manual attempt.
Do not add a watchdog or launch indexing; remove the obsolete redispatch
schedule. Ordinary errors/timeouts remain immediate, and manual retry can
resolve expired state without waiting for the sweep. Verify the unstarted
threshold, bounded scans and actual sweep lag in KW-03.
Resolve ambiguous committed publication before failure; a timeout does not
cancel remote writes or release unresolved native reservations.

[ADR 0029](../adr/0029-keep-parsing-charges-and-allow-free-indexing-retries.md)
keeps explicit retry free without reparsing in every waiting mode. There is no
separate fixed retry expiry while the revision remains eligible and its
canonical parsing result is available. Retain needed indexing input for that
eligibility, regardless of age; reclaim after no eligible retry or other
consumer needs it. Existing result lifecycle, admission and rate limits still
apply; signed-URL expiry is not result removal. Preserve prior attempts and
failures; duplicate delivery/concurrent manual admission cannot restart parsing
or create another execution. Parsing failure retains its existing refund rules.
No completion-mode billing flag is needed.

A failed/unactivated candidate cannot serve lexical hits. The previous
published revision stays selected until a new attempt successfully publishes.
See [current execution and acceptance](opensearch-development-plan.md#93-finite-failures-and-review-gaps).
Body text and vectors retain their original PostgreSQL placement; this does
not authorize PostgreSQL lexical disaster recovery.

## Webhook subscriptions

Use one callback URL with an optional `events` list, as recorded in
[ADR 0028](../adr/0028-select-webhook-events-on-a-single-callback-url.md).
Omitting `events` preserves the existing terminal notifications. An explicit
list selects the callbacks delivered to that URL: parsing completion alone,
overall completion alone, or both as separate events. Parsing and indexing
failures use `job.failed` with `failure_stage` set to `parsing` or `indexing`
and both phase states. An indexing failure preserves the successful parsing
state and result. Accepted on 2026-11-02: name the parsing-completion event
`job.parsed`, emitted when the immutable parsing result is durable and usable.
It does not mark the overall job completed. If both success milestones are
subscribed, retain distinct `job.parsed` and `job.completed` events even if
delivery is delayed; delivery order is not guaranteed. Indexing-only retry
does not create another parsing-completion event. Payloads use the accepted
`parsing_status` and `indexing_status` fields below. Event selection does not
change the SDK's default
wait-for-overall-completion behavior. SDK `parse()`
also gains a separate, explicit parsing-only waiting target: it returns the
usable parsing result earlier while indexing continues. The default remains
overall search-ready completion, and webhook subscriptions do not select the
function's waiting target. The accepted waiting option is Node `waitFor`
or Python `wait_for`, with values `"search_ready"` (default) and `"parsed"`.

Accepted on 2026-11-02: an explicit `webhook.events` must be a nonempty list
containing only `job.parsed`, `job.completed`, and `job.failed`. Reject an
empty list, non-list value, or unknown name through the ordinary parameter
error before job creation or parsing charges. Mixed valid/unknown lists are
invalid as a whole. Deduplicate repeated event names; this does not change
webhook redelivery or ordering semantics. Omitted `events` preserves the
existing terminal subscriptions when a callback URL is configured. See the
[events validation contract](opensearch-publication-readiness.md#accepted-events-validation).

## Resource priority

Prioritize online retrieval when it competes with indexing on the OpenSearch
node. Bound write admission/rate within the execution's remaining ten-second
budget; if capacity is unavailable, fail that execution instead of queueing an
automatic retry. Parsing still uses its existing task queue. Exact admission
limits and node sizing require workload validation; no hardware isolation is
implied and normal-load capacity failures cannot satisfy acceptance.

## Shared parsing and indexing workers

Use the existing ECS gevent parse task for parsing and the initial inline
indexing execution. It retains its slot until that execution ends, subject to
the ten-second indexing budget and bounded outcome settlement. Manual retry
instead runs synchronously in the API request. Both paths use the same indexing
service and publication checks. Retain QStash for webhook delivery.

The shared pool does not provide fixed per-type slot reservations or hardware
isolation. No numeric concurrency increase or global indexing limit has been
accepted. Validate appropriate gevent concurrency against the real daily
indexing burst shape scaled by ten and the accepted retrieval burst shape
with a 50-QPS reference peak. Check inline and manual indexing duration,
parse-task backlog drain, lexical retrieval, and resource/connection-pool pressure under
the focused scope below. Full parsing throughput and simulated parsing
occupancy are outside this validation. Keep backfill separately
rate-limited and online retrieval prioritized. Additional concurrency
controls, dedicated workers, or larger resources require workload evidence
and, where relevant, a separate cost assessment.

The local small-node visibility observations do not establish a one-to-two
second production document-indexing time or justify a global cap of two.

## Reliable parsing-to-indexing handoff

Commit parsing completion, immutable result/revision reference, latest-parsed
selection, indexing intent and subscribed `job.parsed` outbox event together.
After commit, dispatch the parsed event without waiting for webhook delivery,
then invoke indexing once in the same parse task. Do not wait for native I/O
inside that PG transaction or queue a separate indexing task.

Durable phase/owner state prevents duplicate execution and permits failure
settlement after interruption; it does not authorize automatic restart.
Preserve parsed result/charge and free explicit synchronous retry. Existing
webhook delivery, LRU and cleanup tasks retain their own policies.

The state is ordinary application metadata, not an OpenSearch lexical backup
or disaster-recovery source. The [current handoff](opensearch-development-plan.md#91-durable-handoff-and-selected-backend)
and [failure contract](opensearch-development-plan.md#93-finite-failures-and-review-gaps)
replace the historical separate-task dispatch/reconciliation protocol.

## One active job per document

Accepted on 2026-11-02: retain one active processing job for the same
`document_id` through parsing and indexing, including an active explicit retry. Another update receives HTTP 409 with the active job identified.
Overall search-ready completion or terminal failure releases the restriction;
parsing completion and an early parsing-only SDK return do not.

The owner explicitly confirmed that an accepted new job still parsing blocks
indexing retry of an older failed revision with HTTP 409. Do not queue that
retry for later. If an eligible old retry wins admission first, the concurrent
new job conflicts instead. This is the same document-level admission rule;
manual indexing retry still belongs to the original job and creates no new job.

For simplicity, the owner also selected 409 for new-version uploads whenever
the old job remains nonterminal, including an expired indexing deadline whose
failure has not yet been settled. Upload does not repair that state. Processing,
redelivery, sweep or explicit indexing retry must commit the old job's terminal
outcome before a new version can be admitted under normal eligibility rules.

Other documents, including peers in the same namespace, remain eligible for
concurrent processing. Default retrieval keeps the published OpenSearch
revision while its replacement is indexing. This document-level rule does
not set a global indexing concurrency cap or reserve worker slots.
Indexing-only retry must respect document activity and the supersession
policy below. See the
[admission policy](opensearch-publication-readiness.md#accepted-same-document-admission-through-indexing).

## Retry eligibility after a later update

Accepted on 2026-11-02: when a later update for the same document is durably
accepted, reject indexing-only retries for an older failed candidate with
HTTP 409 and explain that it has been superseded. Keep its retained parsing
result accessible. The later update need not be parsed or published yet;
its subsequent failure does not automatically revive the older candidate.
A rejected update does not supersede it.

Only an eligible failed candidate can use the free indexing-only retry path.
Fence stale tasks at activation so that a late completion cannot restore a
superseded candidate. Preserve the selected writer/admission fencing; service
integration and its concurrency acceptance remain required.
The published OpenSearch revision continues serving until an eligible
replacement is ready and activated. See the
[supersession policy](opensearch-publication-readiness.md#accepted-superseded-candidate-retry-policy).

## Indexing retry on the original job

Expose `POST /v1/jobs/{job_id}/indexing/retry` and its v2 equivalent, with Node
`jobs.retryIndexing(jobId)` and Python `jobs.retry_indexing(job_id)`. The request
executes once synchronously, with the configurable ten-second whole-execution
deadline when OpenSearch is primary; PG-primary work uses its separately
qualified longer budget. It does not create a new job, queue a task or continue indexing in
the background after the request. Reuse the original immutable parsed revision,
result, webhook URL and subscriptions, without upload/reparse or another charge.

Return 200 with normal job state for published success or historical completion.
A currently active execution returns 409 / `INDEXING_ALREADY_RUNNING`; it does
not admit a duplicate. An admitted failure is persisted and returns a canonical
non-2xx error with original job/phase/retained-result context: timeout 504 /
`DEADLINE_EXCEEDED`, native unavailable 503 / `UNAVAILABLE`, corrupt internal
input/mapping 500 / `INTERNAL_ERROR`. There is no 202 queued-attempt response.
Preserve the other eligibility/conflict mappings in
[Section 5.4](opensearch-development-plan.md#54-free-indexing-only-retry-and-admission).

Admission remains atomic with document/job activity and archival/supersession
checks. A newer accepted update permanently supersedes an older failed
candidate even if that update fails. Previous attempts/events are retained;
late work cannot change a newer outcome. An HTTP response loss does not prove
rollback: inspect committed state before admitting a subsequent explicit retry.
No cross-attempt public idempotency key or client attempt selector is added.

## Webhook event time and terminal states

Accepted on 2026-11-02: use the original server-side event time and existing
event ID, preserving them and the event-time payload on redelivery. Do not
add a client-facing state-version counter or promise delivery order. A late
failure event cannot undo overall completion; completed jobs cannot return
to failure or active processing. Final indexing failure ends the current attempt,
while an eligible explicit retry can start a new indexing attempt under the
same job. Old attempts cannot change a newer attempt's outcome.

Retry outcomes reuse `job.failed` and `job.completed`; indexing-only retry
does not generate a new parsing-completion event. Timestamps provide context
without a strict ordering guarantee, and the job query remains authoritative
for current state. Preserve historical failure notifications and the selected
`event_id`, `parsed_at`, `completed_at` and `failed_at` projections; see the
[current webhook contract](opensearch-development-plan.md#55-webhooks). See the
[notification policy](opensearch-publication-readiness.md#accepted-webhook-timestamps-and-terminal-state-behavior).

## Phase states

Accepted on 2026-11-02 for the planned migration; isolated candidate evidence
is distinct from formal service acceptance. Preserve
the existing overall `status` values and add these job-revision-scoped fields
to API and webhook payloads:

| Field | Allowed values |
| --- | --- |
| `parsing_status` | `pending`, `running`, `completed`, `failed` |
| `indexing_status` | `not_started`, `pending`, `running`, `completed`, `failed` |

Once parsing succeeds, its phase remains completed despite indexing failure
or explicit indexing-only retry. Indexing completion requires successful
required writes, visibility acknowledgment, revision activation, and cache
coherence. Explicit indexing retry may pass through pending/running internally,
but its HTTP response reports the execution's final success/error, not queued work. Overall `done` requires both phases
completed and cannot regress; later closure, transient unavailability, or
replacement does not erase historical completion. SDK properties use existing
language conventions with identical values and meaning. See the
[phase-state contract](opensearch-publication-readiness.md#accepted-phase-state-contract)
for lifecycle combinations and client integration scenarios.

Completion terminology is explicit: overall `done` is successful completion of
both phases; overall `done` or `failed` means the current job execution is
terminal. A caller's parsed-only workflow can finish before overall success,
without changing API status or SDK `isDone`/`isFailed`. Event subscriptions and
wait targets are independent and do not change sweep/admission behavior.
The initial parse task still executes both phases; its normal return is not
job success. The existing sweep skips terminal jobs and checks active jobs by
phase, using indexing's own deadline after parsing completion rather than
skipping the job or applying parsing's timeout. See the
[completion definitions and state table](opensearch-development-plan.md#52-phase-states-and-historical-completion)
and [sweep classification](opensearch-development-plan.md#93-finite-failures-and-review-gaps).

## Default document reads and SDK behavior

Accepted on 2026-11-02: unqualified document-content reads select the latest
successfully parsed revision, including while its indexing is pending or
failed. Job-result reads retain their job-specific result. Default search
uses published revisions; content and citation reads from a search result
stay on the revision actually retrieved. All views share immutable revision
data, and response metadata/readiness must match the version returned.

Minimize caller responsibility. Reading document content requires no revision
parameter by default. The SDK keeps its accepted default wait for overall
search-ready completion and the single explicit parsing-only waiting option.
Accepted on 2026-11-02: Node `waitFor` and Python `wait_for` both accept
`"search_ready"` (default) or `"parsed"`, on both `parse()` and `jobs.wait()`.
The option selects only the current call's waiting target. Clients can wait
for parsing and later for search readiness on the same job; background work,
billing, webhook subscriptions, and overall job status retain their existing
accepted semantics. See the
[SDK waiting contract](opensearch-publication-readiness.md#accepted-sdk-waiting-behavior).

Accepted on 2026-11-02: if indexing ultimately fails during the default
`parse()` wait, raise the SDK job-failure exception with the indexing failure
stage, original job identifier, and access to the retained parsing result.
The ordinary result loader must support that completed parsing result even
though the overall job failed, retaining the original immutable revision.
The parsing-only wait succeeds once parsing completes, even if indexing has
already failed by the first poll. Eligible indexing-only retry stays free
without reparsing or another parsing charge. Exact exception properties
must be finalized in KW-05 using the existing SDK exception conventions; see the
[exception contract](opensearch-publication-readiness.md#accepted-sdk-exception-behavior-for-indexing-failure).

Accepted on 2026-11-02: preserve the configurable SDK polling timeout, whose
current local Node and Python defaults are 30 minutes, as one budget per
wait invocation without restarting it after parsing. Expiry raises a waiting
timeout with the original job identifier and latest known state when
available. Background work continues; callers can query or resume waiting
on that same job without re-uploading. Client wait expiry must not fail the
server job, automatically resubmit parsing, or reset the indexing execution deadline.
See the
[waiting-timeout contract](opensearch-publication-readiness.md#accepted-sdk-waiting-timeout-behavior).

References or links returned by retrieval bind their revision automatically;
the SDK propagates that binding, and direct API users can use those returned
references/links without building revision parameters or coordinating two
publication pointers. Preserve the selected revision-bound shapes/storage and
existing asset/link retention with same-revision URL renewal; verify SDK/public
projection during integration. See the
[read contract](opensearch-publication-readiness.md#accepted-document-reads-and-client-responsibility).

## Retrieval throughput baseline

The [2026-11-01 production QPS report](../validation/retrieval-qps-2026-11-01.md)
measured 354 production retrieval requests over a 14-day window. The observed
rolling-one-second arrival peak is five requests. Apply the owner's accepted
ten-times planning rule: the first-version external retrieval peak target
is 50 QPS. The owner accepted the real production arrival rhythm with ten
times the request volume and a fifty-QPS peak as the first-version acceptance
profile. Continuous 50 QPS is not required by this decision. The overlapping
seven-day access logs and telemetry match exactly at 234 requests. Lexical
fan-out, indexing throughput and target-node capacity still require measured
qualification under the current [performance plan](opensearch-development-plan.md#7-performance-plan).
The target topology and acceptance gates below are already selected.

## Warm-index mixed-load performance gate

Accepted on 2026-11-02 as an internal first-version validation target; not
implemented or measured on the target node. Under the accepted ten-times
real retrieval burst profile with its 50-QPS peak, concurrently with
ten-times daily indexing arrivals, require lexical discovery P95 at or
below one second for already-open namespace indexes. Include published-scope
lookup, both BM25 channels, RRF, and required service round trips. Count
actual lexical executions separately from cache hits and other bypasses.

The normal target workload must not encounter capacity-caused timeouts or
busy rejections. Deliberately saturated/fault scenarios are separate checks.
Cold-index opening and capacity waiting are measured separately under the
accepted gate below. The workload multiplier was raised to ten on 2026-11-03.
This gate does not set an end-to-end agentic
retrieval or LLM latency target. See the
[performance gate](opensearch-publication-readiness.md#accepted-warm-index-mixed-load-performance-gate).

## Cold-index and capacity-wait performance gates

Accepted on 2026-11-02 as internal validation targets, alongside the request
to revise turnover to proactive LRU. With capacity immediately available,
closing an idle victim if needed, opening a closed target index, and first
lexical discovery together must have P95 at or below three seconds. Preserve
the five-second configurable capacity-wait budget, shortened by an earlier
caller deadline. Successful calls that waited for capacity must directly
measure P95 at or below eight seconds from starting that wait through first
lexical discovery. On budget expiry without admission, return the existing
retryable HTTP 503 busy outcome.

Amended on 2026-11-04: LRU may close an index during a read or write. Search
permits one bounded close recovery; a failed write terminates indexing without
automatic replay. No per-request close exclusion is added. Keep reservations
within the 400-business-index hard cap and coalesce concurrent opens. Measure SDK retry-inclusive time
separately. Deliberate saturation validates the busy branch; normal target
mixed load still requires no capacity-caused timeout/rejection. These targets
are not measured production results. See the
[cold-index performance gates](opensearch-publication-readiness.md#accepted-cold-index-and-capacity-wait-performance-gates).

## Ten-times workload and indexing catch-up

Accepted on 2026-11-03: replace the earlier three-times validation load with
ten times the real production arrival profile for both retrieval and ordinary
indexing. The frozen five-QPS peak gives a new 50-QPS external retrieval peak
test target while retaining the real burst shape; continuous 50 QPS is not
required. Keep the accepted one/three/eight-second latency gates and the
five-second capacity-wait budget at this increased load. Namespace count,
the 400-index hard cap, and the 32-GiB validation-node baseline are unchanged.

Indexing backlog must not grow persistently across repeated mixed-load
bursts. After a burst returns to the test's ten-times-scaled normal arrivals,
the eligible unfinished jobs admitted by then must all reach committed
publication/search readiness within five minutes, including already executing
work. Normal target load must not produce capacity-caused final indexing
failure. Backfill and rebuilds stay separately rate-limited. Historical
validation reports keep their original observations and earlier calculations.
These are unverified internal targets; see the
[workload and catch-up gate](opensearch-publication-readiness.md#accepted-ten-times-workload-and-indexing-catch-up-gate).

## Mixed-load validation duration

Accepted on 2026-11-03: continuously replay the ten-times real peak/trough
profile for one measured hour, then maintain the test's ten-times-scaled
ordinary arrival rate for five minutes while verifying publication catch-up.
Setup/warmup are reported separately. Track heap, GC, LRU reopening,
parse-task backlog, inline/manual indexing duration/completion, errors, and actual lexical-path
execution counts. Cache hits and small-corpus bypasses are separate metrics.
Deliberate overload, fault injection, and recovery drills are separate cases.
The run has not been executed and does not prove indefinite stability. See the
[duration contract](opensearch-publication-readiness.md#accepted-mixed-load-validation-duration).

## Staged feasibility validation plan

Accepted on 2026-11-03: validate underlying compatibility first, integration
correctness second, and target-node capacity/recovery third. First pin an
official ARM64 version and verify remote segments/request-durable translogs,
remote cluster state, close/open, and independent full snapshots together,
including node/disk loss and restore with live repositories unavailable.
Use an isolated synthetic fixture and identify the limits of S3 emulation
explicitly; this is not AWS or production qualification. The owner subsequently
requested the existing Oracle-host comparison environment instead of this
Mac. Coordinate with the other experiment before starting workloads.

After separately authorized business integration, verify publication/client
contracts and the twenty-query evidence gate. Then qualify the accepted
32-GiB configuration under the ten-times mixed workload, LRU/cold-open gates,
and end-to-end recovery objectives. Do not substitute full parsing runs for
the focused indexing/retrieval workload.

The initial staged authorization was for compatibility; later isolated
component work is recorded in the progress ledger. Historical authorizations
do not grant new execution scope. The current review is document-only and
does not authorize packaging, implementation, tests, paid cloud resources,
migration or production changes. See the [staged validation plan](opensearch-publication-readiness.md#accepted-staged-feasibility-validation-plan).

The 2026-11-03 Oracle functional PoC passed all seven cases under S3 emulation,
including recovery of acknowledged unrefreshed/unflushed changes after local
data loss and full-snapshot recovery with source live storage unavailable.
Whitespace/BM25 settings, namespace records, aliases, and post-recovery writes
were verified. Generated runtime resources were removed and all eighteen
pre-existing containers were preserved. Actual AWS behavior, business
integration, and target-node capacity/recovery still require their planned
qualification. Full-snapshot recovery must apply the accepted batch/LRU
policy because restored indexes open for service. See the [completed
compatibility report](../validation/opensearch-compatibility-2026-11-03.md).

## Focused validation scope

Accepted on 2026-11-03: use prepared parsing results and existing jieba token
payloads to generate indexing load directly. Exercise the real indexing
queue/worker, publication metadata, OpenSearch writes/visibility, cache
coherence, lexical retrieval, and index lifecycle. Keep the ten-times workload,
one-hour run, and five-minute catch-up window for these changed paths.

Use fixed parser-output fixtures at the completion/handoff boundary for
targeted state, SDK, retry, webhook, activation, archival/supersession, and
reference-consistency checks. No full parsing-throughput run, simulated
parsing occupancy, or external LLM load test is required. Shared-worker
deployment remains unchanged as the design choice. Report actual lexical
call/fan-out volume and distinguish it from the external-QPS reference;
capacity conclusions cover only the exercised indexing and lexical paths.
See the [focused scope](opensearch-publication-readiness.md#accepted-focused-validation-scope).

## Remaining preparation and validation

The [2026-11-03 completeness review](opensearch-publication-readiness.md#design-completeness-review-before-integration)
distinguishes product/architecture acceptance, concrete engineering design,
and implementation evidence. The engineering reference now selects physical
persistence/publication, writer/lifecycle coordination and reader-free queries, wire
contracts, the acceptance manifest/fault matrix, and operational runbook
contracts. The original reviewed twenty-query fixture/evidence exists with its
recorded final assertion failure. Baseline packaging/access verification,
extended reviewed evidence and corrected-gate reruns, target/AWS configuration,
formal integration and source-matched execution/runbooks remain work under
the development plan; do not recreate the historical query set.
The owner has accepted native ranking
statistics while candidate and obsolete revisions coexist in the namespace
index, with quality acceptance covering partial writes, failures, retries,
activation, and cleanup. The seven underlying compatibility cases do not
validate those application mechanisms.

One item is open at a time. The owner accepted the exact phase-state fields,
values, and invariants after discussing lifecycle, throughput, infrastructure,
and client behavior. Online retrieval
priority is accepted, together with the ten-times production burst profile
and its 50-QPS peak. Daily indexing at ten times the real production burst
shape is also accepted, with migration backfill and full rebuilds separately
rate-limited. The mixed-load infrastructure baseline below is accepted;
the retained-volume and S3 snapshot routes and snapshot policy remain accepted.
The healthy-volume and S3 snapshot internal recovery objectives below are
accepted at 15 minutes and one hour respectively. The PostgreSQL full-rebuild
route and its proposed four-hour target are withdrawn. The remote data/state
and independent full-snapshot combination is accepted for validation under
the stated cost boundary. Single-execution indexing and unified
billing are accepted in ADRs 0026/0029: indexing failure after successful parsing
causes no refund, and indexing-only retry is free in every mode. No extra
billing-mode parameter is needed. Initial indexing executes once in the existing parse task after the parsed
commit/event dispatch; manual retry executes once inside its HTTP request.
There is no indexing queue or automatic retry. The existing 30-minute stale-job
sweep settles interrupted indexing without restarting it. One active job per document
covers parsing and indexing. Superseded candidates cannot be retried or
activated. Explicit retries reuse the original job and parsed revision with
a new indexing attempt; active or ready calls do not start another attempt.
Notifications use original timestamps and event IDs; completion is
irreversible without an added client-facing status-version counter or a
delivery-order guarantee. Retry API mappings and writer/execution identities
are selected in the engineering/development plan. Formal integration/fault
validation, ten-second deadline enforcement and interruption-settlement bounds, numeric concurrency and
indexing limits, capacity, recovery timing, and scaling thresholds remain
implementation/validation work. The phase-state contract is accepted.
Retrieval uses published scope and job-based readiness observation; the
explicit-version search/not-ready error branch is withdrawn.

## Indexing throughput scope

The owner accepted daily indexing at ten times the real production burst
shape, covering new documents, revision replacements, and indexing retries.
Migration backfill and full rebuilds have separate rate limits rather than
setting the daily workload target. Preserve online retrieval priority while
bounding write admission by the remaining ten-second execution budget rather
than queueing automatic indexing work.
Exact concurrency, rate limits, and bulk-rebuild completion budgets remain
open. Ordinary indexing catch-up follows the accepted five-minute gate.

## Accepted infrastructure validation baseline

Use one always-on Linux ARM64 `r7g.xlarge` in the existing production VPC's
private subnet in `us-east-1`: 4 vCPU, 32 GiB RAM, and a 16-GiB JVM heap.
Use a separate encrypted 100-GiB gp3 data volume at 3,000 IOPS and
125 MiB/s, plus a 20-GiB gp3 system volume. The host is dedicated to
OpenSearch; API remains on ECS, and parsing and indexing use the existing
ECS parse task for initial execution; manual retry executes in the API. Each namespace index has
one primary shard and zero replicas, accessible only through authorized
Knowhere services on a private endpoint.
Use a configurable cap of 400 simultaneously open namespace indexes,
including opening reservations. Include system-index overhead when validating
the node rather than treating the namespace cap as a total memory guarantee.

Include S3 remote segments/translogs with explicit request durability, remote
cluster state, and independent full snapshots in this validation baseline.
Keep the stated cost boundary; separately assess resource increases or
optional paid components. Exact software release and measured capacity are
not yet accepted.

This is an accepted mixed-load validation baseline, not a production capacity
result or a resource-provisioning instruction. The cost basis and remaining
storage/capacity qualifications are recorded in
[publication readiness](opensearch-publication-readiness.md#accepted-infrastructure-validation-baseline).
The accepted failure recovery routes are recorded below.

## Accepted first-version capacity expansion policy

Accepted on 2026-11-03: monitor and alert, then expand manually. Diagnose
sustained retrieval-latency/indexing-backlog problems alongside CPU, heap/GC,
and disk pressure. Set numeric warning thresholds using focused capacity
validation, rather than using namespace count alone as an expansion trigger.

Prefer adjusting the single node's resources to address the measured
bottleneck. If one node cannot meet the targets at an acceptable cost,
evaluate a multi-node design and expense separately. Revalidate affected
performance and recovery gates after a material configuration change.
No automatic scaling or larger-resource provisioning is authorized by this
decision. See the [capacity expansion policy](opensearch-publication-readiness.md#accepted-first-version-capacity-expansion-policy).

## Accepted failure recovery routes

1. For process or host failure with healthy storage, restart OpenSearch or
   replace the instance in the same availability zone and reuse the retained
   EBS data volume. Preserve that volume on instance termination.
2. For lost/corrupt storage or an unavailable original availability zone,
   restore the latest successful OpenSearch snapshot from S3 to a replacement
   node and volume. Recover remote cluster state and the latest durable
   index state through remote store, and verify the complete replacement path.

The previously recorded third route, rebuilding OpenSearch from PostgreSQL
after loss of all usable OpenSearch data/backups, is withdrawn by the owner's
2026-11-02 correction. Recovery must verify namespace completeness, revision
changes, deletions, and visibility using independently protected OpenSearch
data. Ordinary application metadata can be checked for consistency; it does
not supply a lexical payload backup or regeneration source.

The snapshot policy, healthy-volume recovery objective, and S3 snapshot
recovery objective below remain accepted, along with the native durability
combination. Recovery coverage requires validation; there is no accepted recovery path if
all independently protected OpenSearch copies are unusable.

## Accepted first-version recovery automation scope

Accepted on 2026-11-03: automatically restart a crashed OpenSearch process.
After an alert, an operator triggers rehearsed scripts for host replacement,
retained-volume attachment, and S3 remote-store or snapshot recovery. Scripts
preserve the single-writer boundary and automate the final checks for lexical
retrieval, published revisions, namespace coverage, and result hydration.
Use the existing off-node checks and Logfire notification path.

The 15-minute healthy-volume and one-hour S3 targets still include detection
and all human response time, including off-hours delay. This design does not
establish unattended achievement of those targets. Supervision, scripts,
response arrangements, and recovery drills remain implementation/operational
work. Recovery preserves completed jobs and does not automatically revive
failed indexing attempts. See the
[automation scope](opensearch-publication-readiness.md#accepted-first-version-recovery-automation-scope).

## Accepted snapshot frequency and retention

Schedule an OpenSearch snapshot to S3 every hour. Retain routine snapshots
for seven days, while preserving at least the three most recent complete
successful snapshots during cleanup. If no newer usable snapshots exist,
retain those three even beyond seven days. Failed and partial snapshots do
not satisfy the minimum.

Use OpenSearch's snapshot deletion API for retention cleanup. Coverage must
include closed namespace indexes and be verified before a snapshot counts as
usable. The precise scheduler and cleanup implementation remain open;
`min_count` alone has not been verified as enforcing the complete-success
requirement. Additional pre-maintenance snapshots remain a recommendation.

Hourly scheduling does not guarantee hourly completion. Recovery starts from
the latest usable OpenSearch snapshot. The previous PostgreSQL catch-up
assumption is withdrawn. Remote segments/translogs and cluster state provide
the accepted candidate for latest-state durability; snapshots provide a
historical state. Recovery time objectives follow below.

## Accepted healthy-volume recovery time objective

For process or host failure with a healthy original EBS data volume, design
for an internal recovery objective of 15 minutes. Count from retrieval
disruption through detection, response (including human delay), restart or
host replacement, volume attachment, endpoint reconnection, and verification
that normal API retrieval correctly serves the required published corpus.

Preserve ordinary cold-namespace opening behavior. Measure the drain time
of candidate indexing work accumulated during the outage separately from
restoring the already published corpus. The target includes off-hours
response and requires a recovery drill; it is not measured capacity or a
customer SLA. Automation follows the accepted scope above; implementation
and actual response coverage remain to be established.

The S3 snapshot recovery objective remains accepted below. The PostgreSQL
full-rebuild path and proposed duration are withdrawn.

## Accepted S3 snapshot recovery time objective

For recovery from an S3 snapshot, design for an internal objective of one hour.
Count from the original retrieval disruption through detection, response,
replacement node/volume provisioning, snapshot restoration, independently
protected remote-state/data recovery, correctness and search-visibility
checks, and normal API verification for the
required published corpus. Count any earlier unsuccessful original-volume
recovery attempt toward the same hour.

The target assumes the S3 repository and normal application dependencies
remain available. PostgreSQL does not supply lexical recovery data. Preserve
ordinary cold-index opening and measure new candidate
indexing backlog drain separately. The objective is a design requirement
requiring a representative drill, not a measured capability or customer SLA.

The proposed four-hour PostgreSQL full-rebuild target is withdrawn. Revision
activation and index lifecycle use the selected engineering/development
contracts; remaining integration and acceptance are tracked as tasks.
Billing is uniform across all waiting and delivery choices.
The one-hour target must be validated against the accepted native
recovery design; it cannot be justified through PostgreSQL reconstruction.
