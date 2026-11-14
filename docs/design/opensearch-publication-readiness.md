# OpenSearch publication and search readiness

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Status: historical decision/rationale and evidence ledger, reconciled on
2026-11-05. Accepted product contracts remain recorded at their named anchors, subject
to the current single-execution amendment below;
historical measurements retain their original configurations and limits.
Isolated candidate/component evidence exists; formal service integration,
target-environment qualification and production delivery remain unaccepted.
Dated statements of "not implemented" describe their original decision stage,
not a claim that no isolated candidate exists now.

For current tasks/acceptance use the [development plan](opensearch-development-plan.md);
for reader-free query and lifecycle rules use the
[integration constraints](opensearch-integration-constraints.md) and
[engineering design](opensearch-engineering-v1.md). Current product decisions
are consolidated in [v1](opensearch-lexical-v1.md) and the ADRs. This ledger is
not an independent implementation checklist or execution authorization. The
current review is document-only; it does not start packaging, implementation,
new native/paid testing or deployment. Separately authorized completed Oracle
checks are recorded in the [handoff verification](../validation/opensearch-handoff-oracle-check-2026-11-04.md),
including its same-data single-bulk follow-up; they do not implement the revised
API/worker contract or replace formal staging acceptance.

Engineering update, 2026-11-03: the concrete
[implementation design](opensearch-engineering-v1.md) now selects persistence,
claims/lock order, publication and reader-free queries, native index lifecycle
coordination, wire/SDK examples, acceptance cases, and operational runbook
contracts. Earlier statements below that leave these design choices open
are superseded by that reference. Formal integration/qualification remain open.
The original twenty-query business evidence exists with a failed historical
final assertion; baseline packaging/access verification, corrected-gate reruns,
extended reviewed cases and target/AWS inputs remain work in the development
plan. Do not replace the original query set. The accepted native ranking boundary includes
candidate/obsolete coexistence, failed retries, and cleanup.

Lab reuse update, 2026-11-03: the owner supplied the existing
[Oracle lab](http://100.77.144.81:18765/) for validation. A bounded check
replayed six existing comparisons and a native explanation successfully,
with all six matching frozen results. Reuse that deployment and fixture;
keep mutation cases in separate indexes. This does not replace reviewed
business evidence, application protocol tests, or target-node qualification.
See the [reuse report and captured evidence](../validation/opensearch-lab-reuse-2026-11-03.md).

Owner clarification, 2026-11-02: the PostgreSQL lexical index and its derived
data are temporary migration rollback resources. Stop their writes and
retire that data after OpenSearch is accepted as stable. PostgreSQL must not
be a runtime fallback, permanent lexical backup, post-snapshot lexical replay
source, or full disaster-recovery source for OpenSearch. The previously
recorded third recovery route and proposed four-hour PostgreSQL rebuild are
withdrawn. References to PostgreSQL-based snapshot catch-up are superseded.
The original placement of body text, vectors, assets, citations, and ordinary
application metadata in PostgreSQL remains as agreed; it does not make that
data an authorized OpenSearch disaster-recovery source.

## Current single-execution amendment

The owner's latest decision supersedes earlier separate indexing tasks/queues,
automatic retries, automatic failed-build replay, three-round budgets, the
30-minute execution ceiling and immediate-return manual retry below.

- Initial indexing runs once synchronously at the end of the existing parse
  task, after durable parsed state and subscribed `job.parsed` dispatch.
- Explicit retry runs once synchronously inside its HTTP request. It reuses
  the original job/revision and does not create a task or detached continuation.
- Owner-confirmed on 2026-11-05: use one `refresh=wait_for` bulk when the
  revision fits its serialized-byte budget; oversized revisions use multiple
  byte-bounded requests at record boundaries. All requests share one deadline
  and one whole-revision publication outcome. No fixed 200-record rule,
  explicit-refresh replacement, automatic batch retry or partial activation.
- A manual-retry client disconnect alone does not cancel the original running
  handler. Continue its current execution within the existing deadline and
  let the client query the original job. Actual handler cancellation/process
  exit stops forward work and uses bounded settlement or existing recovery;
  preserve committed completion and native uncertainty, with no detached writes.
- Each OpenSearch-primary execution has a fixed configurable ten-second budget covering
  connection/opening, preparation, writes, visibility and guarded publication.
- Failure retains parsing/result/charge and emits subscribed `job.failed`
  with indexing context. The parse task returns normally; search-ready waiting
  raises and parsed-only waiting succeeds against the same server state.
- Reuse the existing stale-job sweep task and its 30-minute schedule to settle
  indexing failures/timeouts left unfinished by a disappeared worker/API.
  Extend the parsing-completed exclusion with phase-aware indexing handling;
  remove obsolete indexing redispatch rather than adding a watchdog. Ordinary
  errors/timeouts remain immediate, and manual retry checks expired state
  without waiting for a sweep. Preserve completed parsing/result/charge and
  settle the current attempt/job/subscribed failure outbox idempotently using
  the indexing deadline or bounded unstarted threshold. This never launches
  indexing or fails an already completed job because of a PG shadow-copy error.
  The original initial intent uses its own creation time; each manual admission
  persists its new request deadline even before execution starts. An old parse
  or prior attempt cannot make the new attempt immediately expire.
  Verify the unstarted threshold and sweep lag in KW-03. Native uncertainty
  and publication fencing remain.
- One shared create/update-index entry point submits the non-gating PG rollback
  copy to a bounded background thread pool after durable intent commits.
  Copies own their sessions and must not hold shared publication locks during
  long COPY; existing maintenance recovers pending intent after interruption.
  No per-copy job/task is required.
- PG-primary preparation/rollback continues accepting new documents and updates.
  Return parsed results first where requested; search readiness waits for PG
  publication under a separately qualified longer budget and matching HTTP
  timeouts. The ten-second OpenSearch default does not apply to that path.
- The SDK's existing configurable 30-minute waiting timeout is independent.
  A ten-second server limit does not prove all documents finish successfully.
- A planned backend transition may interrupt unfinished indexing without waiting
  for every accepted parse to finish. Preserve parsing/result/charge, settle
  indexing-stage failure with its subscribed event and require explicit retry
  after service resumes. This also applies after an aborted switch. Completed
  jobs stay completed; parsed-result persistence cannot depend on the publication
  gate being open. Earlier instructions to resume interrupted indexing after a
  switch are superseded by the
  [transition state matrix](opensearch-development-plan.md#1431-in-flight-jobs-during-a-backend-transition).

Current work, response codes and acceptance are in the
[development plan](opensearch-development-plan.md#9-worker-publication-and-failure-integration)
and [ADR 0026](../adr/0026-publish-search-readiness-from-background-tasks.md).
Historical observations and rationale below keep their original context;
they do not validate this amendment. Other product, quality, lifecycle and
recovery contracts remain unchanged unless explicitly superseded.

## Current agent-retrieval failure clarification

The owner also confirmed request-level native failure semantics: an unrecovered
OpenSearch failure in an agent tool ends the whole retrieval as temporarily
unavailable. It cannot become a successful answer through another tool or
populate the normal result cache. Ordinary zero-hit/tool-argument recovery and
valid native-bypass paths remain unchanged. Both harnesses and their exception
wrappers require verification under the
[agent failure contract](opensearch-development-plan.md#64-agent-retrieval-and-native-failure-propagation).

## Current duplicate-delivery clarification

The owner confirmed this duplicate-delivery mechanism; one indexing round
does not imply one message delivery. Keep the existing independent broker
visibility-recovery process and parsing recovery. Remove only the obsolete
automatic lexical indexing redispatch policy. Gate repeated task entry on
durable phase/ownership before expensive side effects, reuse committed parsing
and allow only one initial indexing claim. A recorded execution start cannot
be reset by redelivery or lock expiry; resolve committed success or settle
failure, with another execution requiring an eligible explicit client request.
An original parse message cannot execute a later manual attempt. Stable
namespace/build/unit IDs and guarded publication/outbox updates prevent duplicate
logical results; remote calls already sent may still finish and need fencing
and cleanup. See the [current redelivery matrix and code findings](opensearch-development-plan.md#911-duplicate-delivery-and-processing-ownership).
The current source defaults are not proof of deployed time limits, and lock
expiry does not prove that old code has stopped.

## Current completion-meaning clarification

The owner confirmed these definitions for the planned integration.

The same job has two sequential phases within the initial parse task. Parsing
completion means a readable durable parsing result; indexing completion means
the selected primary backend's publication is ready. Overall `status=done`
requires both phases. Terminal job state means `done` or `failed`, so terminal
does not mean successful. The existing `job.completed` webhook's
`status=completed` remains the wire equivalent of overall success, not parsing
alone. Celery task return is execution bookkeeping, not a business milestone.

Client wait satisfaction and event subscriptions do not change these facts.
A parsed-only caller may finish its own workflow while the same job is still
running or has failed indexing. Preserve the actual API status and overall SDK
helpers; do not manufacture early job success. Selecting `job.parsed` callbacks
does not change the SDK's default search-ready wait. Without explicit selection,
the SDK waits for both phases and a configured webhook URL retains terminal
callbacks. Omitted webhook configuration still means no callback.

The existing sweep keeps its overall terminal-state guard and branches on
persisted phase state for active jobs: existing parsing/upload expiry before
parsing completion, indexing's deadline/unstarted threshold afterward. Merely
removing the parsing-completed exclusion and applying generic job age is
insufficient. Never filter jobs based on a client return, event subscription,
notification delivery or task `SUCCESS`. Apply the authoritative
[completion/state definitions](opensearch-development-plan.md#52-phase-states-and-historical-completion),
[client observation matrix](opensearch-development-plan.md#53-sdk-behavior)
and [sweep classification](opensearch-development-plan.md#93-finite-failures-and-review-gaps).

## Current empty-namespace lifecycle amendment

The owner selected close-and-retain for empty namespace indexes in v1.
Eligible archived, obsolete and failed document/build records are still cleaned
normally. After eligible cleanup confirms emptiness, close through existing
maintenance/lifecycle coordination; keep registry/native identity and reopen
the same index on later use. Whole-index deletion/recreation is outside v1.
An empty published selection alone does not establish native emptiness, and
retaining the index does not retain otherwise unnecessary document records.
Preserve writer/cleanup fencing and ordinary LRU rules without adding reader
close exclusion. Measure retained catalog counts and metadata/disk growth.
See [current cleanup and acceptance](opensearch-development-plan.md#62-replacement-cache-and-cleanup).

## Current retry eligibility and input-retention amendment

The owner selected no separate age-based expiry for manual indexing retry.
An otherwise eligible failed version remains retryable while the document is
active, the candidate has not been superseded and its canonical parsing result
remains available. Retain required temporary indexing input for that eligibility,
even for an old failure. Reclaim it after no eligible retry, PG copy, active
writer or other consumer needs it, with the required fencing and reference
checks. No age-only bucket TTL may remove still-needed input.

Existing canonical-result lifecycle, authorization, admission and rate limits
still apply. A renewable download URL expiring is not result removal;
unexpected missing/corrupt assigned input is an operational error rather than
an expired retry window. This is the selected rule, not evidence of an existing
collector or verified bucket configuration. See
[current input lifecycle and acceptance](opensearch-development-plan.md#94-temporary-indexing-input-storage-and-reclamation).

## Design discussion background

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

The owner requested that lifecycle, throughput, and infrastructure decisions
precede exact phase-state enums. After that discussion and the client scenario
review, the phase fields, enum values, and transition invariants were accepted
on 2026-11-02 in the phase-state contract below. Implementation and the
remaining request/error details still need to follow that accepted contract.

The design discussion followed these dependencies:

1. Define parsing, durable result persistence, reliable indexing handoff,
   indexing, visibility acknowledgment, revision activation, retry, and
   cleanup lifecycles. Define the corresponding retrieval read lifetime and
   index open/close coordination.
2. Define mixed-workload targets and overload policy. Measure parsing by file
   or page rate with format/size distribution, indexing by retrieval-unit and
   pre-tokenized payload rates, and retrieval by user-request rate and actual
   lexical-search fan-out. Define acceptable queueing and recovery behavior.
3. Select worker-pool and queue boundaries, admission limits, node resources,
   storage, index lifecycle limits, recovery approach, and scaling triggers.
   An async HTTP wait alone does not isolate parser capacity from indexing.
4. Use official product limits and mechanisms as the basis, then perform a
   bounded representative mixed-load check against explicit targets. The
   small visibility probe does not establish production throughput or validate
   the accepted 32-GiB validation node's production capacity.
5. Derive phase-state fields and enums from the agreed lifecycle.

The owner accepted online retrieval priority when background indexing and
search contend on the OpenSearch node. Limit and queue indexing to leave
capacity for search, while retaining an indexing processing budget to avoid
indefinite starvation. Exact limits and budgets are not yet established.
The first capacity baseline is now measured: five user-facing retrieval
arrivals in a rolling second, giving a fifty-QPS peak target under the
owner's accepted ten-times rule. The owner selected the real production
burst shape scaled to ten times the volume, with a 50-QPS peak, as the
first-version acceptance workload. Continuous 50 QPS is not required by that
decision. The owner also accepted daily indexing demand at ten times the
real production burst shape. Migration backfill and full rebuilds have
separate rate limits. The owner accepted the node and storage baseline below
for mixed-load validation. The retained-volume and S3 snapshot routes and hourly/seven-day
snapshot policy below are accepted. The owner accepted a 15-minute internal
recovery objective for process/host failure with a healthy original data
volume and a one-hour internal objective for S3 snapshot recovery. The owner
also accepted remote data, remote cluster state, and independent full
snapshots under the stated cost boundary for first-version validation.
The owner selected simple external-service retries and unified billing:
successful parsing remains charged if indexing fails, and indexing-only
retry is free in every delivery mode. Parsing and indexing use separate tasks
and queues on the existing shared ECS gevent workers. Numeric concurrency is
chosen through mixed-load validation; neither a fixed two-task global indexing
cap nor an additional distributed concurrency controller is a prerequisite.
The parsing-completion state and indexing intent are committed in one
PostgreSQL transaction, then dispatched to Celery after commit with
reconciliation and idempotent execution. The owner accepted one active job
per document through the indexing phase; competing updates receive HTTP 409
with the active job identified. A later accepted update supersedes an older
failed candidate, preventing its indexing retry or activation. Explicit retry
reuses the original job and parsed revision, records a new indexing attempt,
and coalesces calls while indexing is active. Notifications use original event
timestamps and existing event IDs without a client-facing status-version
counter or delivery-order guarantee. Overall completion is irreversible;
indexing failure permits only an eligible explicit retry to restart work.
The first-version configured limit is now 400 simultaneously open namespace
indexes, subject to target-node capacity validation; system-index overhead
must also be included in that validation. Search slot admission waits up to
five seconds by default, configurable, before a retryable busy outcome.
Proactive LRU turnover was accepted for validation on 2026-11-03 to preserve
opening headroom, superseding the earlier capacity-only eviction trigger.
The 360/320 starting watermarks remain configurable validation candidates;
production settings require target-node qualification.
The owner accepted a fixed initial set of 20 real business queries with known
expected evidence as a retrieval-quality gate before switching to OpenSearch.
Ranking differences are allowed; missing key evidence and incorrect
citations, permissions, or revision selection must be resolved before cutover.

Read-model decision, 2026-11-02: default document-content reads select the
latest successfully parsed revision, even while indexing is pending or failed.
Default retrieval selects published revisions, and citation/body/asset reads
from a retrieval result remain bound to the revision actually retrieved.
They share immutable revision data. Minimize client burden: the service and
SDK own ordinary version selection and reference propagation; callers do not
have to manage two revision pointers. The PostgreSQL publication protocol
is now accepted below; concrete pointer storage and coordination are selected
in the engineering reference, and their implementation remains open.

## Confirmed requirements

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

- Keep indexing delay controlled and observable. The owner suggested roughly
  thirty seconds under normal conditions as an example, not a finalized
  percentile target, timeout, or guarantee during an outage.
- Let callers determine whether the document revision they just processed is
  ready for retrieval. Customers may chain parsing and immediate search in a
  continuous business workflow.
- Retain the previous PostgreSQL lexical index for deliberate migration
  rollback only, as recorded in ADR 0024. Stop the legacy lexical writes and
  retire obsolete data after OpenSearch is stable. Neither the temporary
  lexical copy nor other PostgreSQL document data is an assumed source for
  OpenSearch disaster recovery or snapshot catch-up.
- Preserve terminal job completion as confirmation that the processed revision
  is ready for retrieval, while exposing parsing completion independently so
  result-only clients can display or download results earlier. See ADR 0025.
- Ground visibility and timing claims in official documentation. Do not make
  an expensive custom percentile benchmark campaign the basis of the readiness
  contract, and do not present a configured refresh interval as an end-to-end
  completion guarantee.
- The owner accepts the officially documented visibility acknowledgment.
  Thirty seconds is a reference only; an official end-to-end time guarantee
  is not required.
- The owner selected background indexing with asynchronous notification,
  as recorded in ADR 0026. Parsing results can be delivered before indexing;
  terminal job completion follows confirmed search readiness.
- When automatic indexing retries are exhausted, mark indexing failed and
  notify the client. Retain successful parsing and its result, and support
  explicit indexing-only retry through the accepted job-scoped retry API below.
- Keep the published OpenSearch revision selected for default retrieval until
  a replacement is fully searchable and activated. Retrieval searches published
  scope; observe readiness through jobs rather than a new explicit-version
  search selector or not-ready error. See ADR 0027.
- Default namespace retrieval searches published revisions and does not block
  on a newly added document with no published revision. Do not add notices or
  warnings about unready documents to the retrieval response. Explain this
  scope in client documentation; readiness is observed through the two phase
  states, and overall completion always requires search readiness.
- Webhook configuration keeps a single `url` and gains an optional `events`
  list. Explicit subscriptions select which events reach that URL; omitting
  the list preserves existing terminal notifications. See ADR 0028.
- Parsing and indexing failures share the `job.failed` event. Its payload
  carries `failure_stage` (`parsing` or `indexing`) and both phase states;
  indexing failure preserves successful parsing and result access.
- SDK `parse()` keeps its default wait for overall search-ready completion
  and gains an explicit parsing-only waiting target. Early return delivers
  the parsing result while indexing continues. This option is independent
  of webhook event subscriptions, as recorded in ADR 0025.
- Online retrieval has priority over background indexing during resource
  contention on the OpenSearch node. Limit indexing concurrency or rate and
  allow its work to queue. This is an application scheduling requirement,
  not a claim that separate namespace indexes isolate physical resources.
- The first-version user-facing peak retrieval target is 50 QPS, derived
  from the observed rolling-one-second peak of 5 QPS and the owner's accepted
  multiplier of ten. Acceptance preserves the real production arrival
  rhythm at ten times the volume, reaching a peak of 50 QPS; it does not
  require continuous 50 QPS.
- Daily indexing capacity covers new documents, revision replacements, and
  indexing retries under the real production burst shape scaled by ten.
  Migration backfill and full rebuilds have separate rate limits and do not
  define the daily workload target. Exact rate limits and completion budgets
  remain open.
- Use the accepted infrastructure baseline below for mixed-load validation:
  one `r7g.xlarge`, 16-GiB heap, separate 100-GiB gp3 data and 20-GiB gp3
  system volumes, private networking, and one primary shard with no replicas
  per namespace. Acceptance of this baseline is not production capacity
  acceptance or authorization to provision resources.
- Recover first by restarting OpenSearch or replacing the host with the
  retained same-zone data volume. For lost storage or an unavailable original
  availability zone, restore remote cluster state and the latest durable
  index state on a replacement node using remote store. Independent full
  snapshots provide historical recovery points. The earlier PostgreSQL
  reconstruction route is
  withdrawn; recovery when all OpenSearch copies are unusable is unresolved.
- Schedule an S3 snapshot every hour, retain routine snapshots for seven days,
  and preserve at least the three most recent complete successful snapshots
  during cleanup. Backup frequency is a schedule, not a recovery-time or
  data-loss guarantee.
- For process or host failure with a healthy original data volume, design
  for a 15-minute internal recovery objective, including detection, response,
  restart or replacement, and end-to-end retrieval verification. This target
  has not yet been demonstrated by a recovery drill.

- Include S3 remote segments and request-durable translogs, remote cluster
  state, and independent full snapshots in the first-version validation
  baseline under the accepted cost boundary. Keep the PostgreSQL lexical
  retirement constraint. Verify actual cost, performance, namespace coverage,
  and recovery before any production capacity claim.
- For S3 snapshot recovery, design for a one-hour internal recovery objective
  from the original retrieval disruption through replacement provisioning,
  snapshot restoration, recovery-state verification, and API verification.
  The earlier PostgreSQL catch-up mechanism is withdrawn; validate the
  accepted OpenSearch-native mechanisms before claiming recovery completeness.
  Earlier failed recovery attempts count toward that same clock. This target
  has not yet been demonstrated by a recovery drill.

## Capacity targets still to establish

- Namespace-size distribution and the internal demand generated by the
  accepted external retrieval arrival profile. Translate this into lexical
  searches, including the two channels and any agent recall fan-out.
- Cold-index opening and capacity-wait acceptance criteria; the warm-index
  mixed-load performance gate is now accepted below.
- Complete indexing arrivals in retrieval units and pre-tokenized payload
  bytes, plus measured queue stability and drain results against the accepted
  five-minute catch-up target below.
- Node CPU, heap, filesystem-cache and disk budgets; capacity validation of
  the selected 400-open-namespace limit; recovery behavior; and conditions
  for increasing capacity. The expansion policy below is accepted; numeric
  warning thresholds and measured capacity remain validation work.

The peak target is 15 external retrieval QPS under the accepted ten-times
production burst profile. Production node capacity is not accepted yet.
The existing small-node visibility verification is not capacity evidence.

## Accepted warm-index mixed-load performance gate

Status: accepted on 2026-11-02 as an internal first-version acceptance target;
not an observed result, production SLA, or node-capacity acceptance.

Run the agreed ten-times real production retrieval burst profile, reaching
50 external retrieval QPS at its peak, concurrently with ten-times daily
indexing arrivals under the accepted representative production burst shape.
Retain the separate limits for migration backfill and bulk maintenance.

For requests whose namespace index is already open with an active primary,
require lexical discovery P95 at or below one second. Count elapsed time for
published-version scope lookup, both BM25 channels, RRF, and application /
service round trips needed for that discovery. Measure actual lexical-path
executions; application cache hits and small-corpus bypasses are separate
samples and cannot satisfy the lexical latency gate. Report sample counts,
corpus sizes, namespace distribution, and lexical invocation/fan-out volume.

Under this normal target workload, require no timeouts or busy rejections
caused by insufficient node/index-slot capacity. Report failures alongside
latency so dropping slow requests cannot appear to satisfy the gate.
Deliberate overload and fault scenarios have their own expected-error checks
and do not replace this normal-load requirement.

Measure cold-index opening and capacity waiting separately using the accepted
cold-index gate below. The workload multiplier was raised to ten on
2026-11-03; this one-second threshold does
not describe complete agentic retrieval or LLM time. The local close/open
probe and earlier Serverless ranking probe do not establish this target's
achievement. Validate it on the accepted node and durability baseline before
production capacity acceptance.

## Accepted cold-index and capacity-wait performance gates

Status: numerical acceptance targets accepted on 2026-11-02 while the owner
requested revision of index turnover to proactive LRU. These are internal
targets, not measured production results or a vendor SLA. The selected 360/320
turnover settings are validation candidates, not qualified production values.

Cold means a closed namespace index that must be opened for this request,
not full-node startup or disaster recovery from S3. Under the accepted node,
durability settings, and representative mixed load, validate these cases:

| Situation | Acceptance target |
| --- | --- |
| An opening slot is free or an idle victim can be closed immediately | Closing the victim if needed, opening the target to an active primary, and completing first lexical discovery together have P95 at or below three seconds. |
| All 400 business slots are charged and no lifecycle-safe slot is available | Apply the existing five-second configurable capacity-wait budget, shortened by an earlier caller deadline. Active readers/writers alone do not exclude LRU closure. |
| Capacity waiting ends successfully | Directly measure elapsed time from starting the capacity wait through first lexical discovery; require P95 at or below eight seconds. |
| The capacity-wait budget expires without admission | Return the ordinary retryable HTTP 503 busy outcome. |

These are single server-side lexical-call measurements, including the
accepted discovery scope; record SDK retry-inclusive total duration separately.
Measure the eight-second criterion directly rather than reporting a sum of
component percentiles. Current amendment (2026-11-04): LRU may close during
native reads/writes; request activity and writer `index_use` records are not
close fences. Preserve writer ownership, one bounded read recovery and bounded
same-owned-build write recovery. Count all reservations/uncertainty within 400,
coalesce opens and release a slot only after confirmed close with no outstanding
issuer. See the [current lifecycle contract](opensearch-development-plan.md#63-lru-and-cold-recovery).

Use deliberate saturation cases to validate waiting/503 behavior. The normal
ten-times target mixed workload must still pass the accepted no-capacity-
rejection gate. Include both representative small namespaces and the largest
approximately 45,000-unit namespace. Target-node tests have not been run.

## Accepted ten-times workload and indexing catch-up gate

Status: accepted on 2026-11-03 for first-version validation; not measured.
This supersedes the former three-times/15-QPS validation workload.

Scale both external retrieval arrivals and ordinary post-parse indexing
arrivals to ten times the observed production burst shape. The frozen
retrieval baseline is five arrivals in a rolling second, so the new peak
test target is 50 external retrieval QPS. Preserve the real peak/trough
shape; continuous 50 QPS is not required. Include new-document publication,
revision replacement, and eligible indexing retries. The arrival multiplier
does not multiply the configured open-index cap or imply ten times the
namespace inventory. Preserve representative namespace sizes and payload
distribution, and measure actual internal lexical fan-out.

Keep the accepted latency targets: already-open lexical discovery P95 at
or below one second, cold opening through first discovery P95 at or below
three seconds when capacity is immediately available, five seconds for the
capacity-wait budget, and directly measured P95 at or below eight seconds
for successful discovery calls that waited for capacity. Normal target load
must not produce capacity-caused timeouts, busy rejections, or final indexing
failures. Deliberate overload/fault cases remain separate checks.

During repeated representative mixed-load bursts, indexing backlog must not
grow persistently. When a burst ends and indexing arrivals return to this
test's normal level, also scaled ten times, start the catch-up clock. Every
still-eligible unfinished indexing job already admitted at that point must
reach committed publication/search readiness within five minutes. Include
both queued and executing work. Completion is the accepted published/done
outcome, not task dispatch, queue removal, or discarding failed work.
Report any deliberate archival/supersession exclusions with their reasons.

The five-minute value is an internal workload-recovery acceptance target.
Migration backfill and full rebuilds retain their separate rate limits and
do not define this ordinary indexing target. The existing 32-GiB node remains
the validation baseline; its ability to meet the ten-times target has not
been established. Keep candidate LRU watermarks subject to the same test.

Historical validation reports and raw measurements retain their original
values and the earlier three-times planning calculation. The ten-times
revision changes current design targets, not the frozen observed traffic.

## Accepted mixed-load validation duration

Status: accepted on 2026-11-03 for first-version validation; not executed.

Continuously replay the accepted ten-times representative retrieval/indexing
peak-and-trough profile for one measured hour. Keep the 50 external retrieval
QPS peak as a burst target rather than sustained traffic. Report fixture
setup and warmup separately from the measured hour; normal cold opening and
LRU turnover remain part of the relevant measured scenarios.

After the final representative burst, continue the test's ordinary arrival
profile, still scaled ten times, for five minutes. Use the accepted catch-up
cohort and completion definition to verify that the remaining eligible
indexing work reaches committed publication/search readiness. Do not stop
all arrivals merely to make the backlog disappear.

Record the full-hour and follow-up trends for JVM heap, GC, LRU reopening
frequency, indexing backlog, and completion age, together with the accepted
latency/error metrics and node resource observations. Preserve actual
lexical execution counts, cache hits, and small-corpus bypass counts as
separate observations.

This duration applies to normal mixed-load capacity validation. Deliberate
saturation, publication fault injection, and recovery drills remain separate
cases. A passing one-hour run is first-version acceptance evidence; it does
not establish indefinite stability or remove the need for operational
observation after cutover.

## Accepted staged feasibility validation plan

Accepted on 2026-11-03: proceed from the design baseline to a feasibility PoC
in the following order. Acceptance of the plan is not production acceptance,
authorization to implement the business integration, or permission to
provision paid cloud resources.

1. **Underlying compatibility.** Pin an official ARM64 OpenSearch version
   and image digest. On an isolated synthetic fixture, enable remote segments,
   request-durable remote translogs, remote cluster state, namespace index
   close/open, and a distinct full-snapshot repository with both shallow-copy
   options disabled. Verify visibility, acknowledged writes/updates/deletes,
   metadata and namespace isolation, closed-index backup coverage, recovery
   after loss of the only node's local data, and independent snapshot restore
   without access to live remote repositories. Do not use PostgreSQL as a
   lexical recovery source. Record any S3-emulation limitations; actual
   AWS IAM/TLS/S3 behavior remains a
   separate qualification on the selected deployment configuration.
2. **Integration correctness.** After separately authorized implementation,
   exercise the real indexing/publication/retrieval boundary using prepared
   parser outputs. Verify the accepted phase states, finite retries,
   subscriptions, archival/supersession, revision-bound references, activation
   and cache consistency. Execute the accepted twenty-query business-evidence
   gate. These are focused integration cases, not a parsing throughput test.
3. **Target-node acceptance.** On the accepted 32-GiB node and real durability
   configuration, exercise the agreed ten-times retrieval/indexing burst
   profile for one measured hour plus the five-minute catch-up window. Verify
   warm/cold/wait gates, open-index capacity, LRU turnover, and representative
   end-to-end recovery drills against the accepted internal objectives.
   Record measured resource use and cloud-cost inputs.

Resolve underlying incompatibilities before treating later integration or
capacity results as evidence for the intended architecture. A local functional
pass does not establish AWS compatibility, Knowhere integration correctness,
target-node capacity, or the 15-minute/one-hour end-to-end recovery objectives.
Report each stage independently. Any discovered hard constraint can require
revisiting the design rather than silently weakening an accepted gate.

Only stage 1 is authorized for execution now. Execution-location clarification
on 2026-11-03 supersedes the initial local-machine preparation: the owner
requested reuse of an existing Oracle-host comparison experiment and asked
not to run validation on this Mac. Coordinate with that experiment before
starting workloads, reuse its verified environment where suitable, and
preserve its data and running comparisons. Use synthetic tokens and
disposable isolated test storage. The temporary local VM and copied images
were removed before any test containers started; existing local services
remained running. See the cleanup record (`../validation/opensearch-compatibility-2026-11-03/local-cleanup.json`).
Application implementation, new paid cloud provisioning, formal migration,
and production switching remain outside this execution. No runtime
compatibility pass has yet been established.

Stage-1 progress on 2026-11-03: the [official compatibility review](../validation/opensearch-compatibility-official-2026-11-03.md)
records version-specific configuration and recovery constraints for the
3.9.0 candidate. Read-only inspection found an existing retrieval-quality
lab at `<lab-home>/knowhere-retrieval-lab` through the historical SSH alias
the Oracle lab host. Its README describes an ARM64 OpenSearch 3.9.0 comparison
with six namespaces and forty-eight queries; this is not yet an independently
verified durability configuration or a compatibility pass. The owner confirmed
the Oracle lab host as the intended host. The sidechat is not present in the
currently visible chat list. Its shared runtime protocol identifies an active
comparison. Source inspection established that the initial protocol is a frozen
checkpoint; the live result is `data/performance-evidence.json`. That result
reported `measured` and no active benchmark process remained before starting
the compatibility probe. Preserve its indexes, data, timing runs, and
other services. The standalone compatibility probe uses a separate directory,
network, loopback ports, containers, and data volumes. It reuses the verified
official image already on Oracle; the Mac runs no test backend. The probe
passed remote import, argument-parser, and lint checks using the existing lab's
Python 3.14 interpreter rather than the host's Python 3.8 default. All seven
underlying functional cases then passed with S3 emulation: remote-backed
writes/visibility, close/open, full snapshot coverage of a closed namespace,
latest acknowledged-state recovery after discarding the only node's local
data, source-independent full-snapshot recovery, and writes after both
recovery routes. Cleanup preserved all eighteen pre-existing containers and
removed the generated runtime resources. See the [completed compatibility
PoC](../validation/opensearch-compatibility-2026-11-03.md).

This permits progression to the planned integration work; actual AWS behavior,
application correctness, and target-node capacity/recovery remain unaccepted.
Ordinary full-snapshot restore opens restored indexes, so recovery scripts
must apply the existing batch/admission/LRU policy within the 400-open-index
limit rather than assume historical closed state is automatically retained.

## Design completeness review before integration

Review date: 2026-11-03. Product behavior, architecture boundaries, rollout
policy, and acceptance targets have an accepted baseline. The seven-case
underlying PoC does not make the implementation design complete. No
application integration is authorized by this review.

The review originally identified the following missing outputs. The linked
engineering design now supplies the selected mechanisms and contracts;
the table distinguishes them from preparation or execution still required:

| Item | Selected design and remaining evidence |
| --- | --- |
| Persistence and publication | [Persistence](opensearch-engineering-v1.md#persistence-and-identities) and [transactions](opensearch-engineering-v1.md#transactions-claims-and-publication) select fields, immutable input, authorization-scoped identities, claims, and activation. Additive migrations and application integration are not written. |
| Cross-process lifecycle coordination | [Read/cleanup](opensearch-engineering-v1.md#read-lifetime-cache-and-cleanup) and [index lifecycle](opensearch-engineering-v1.md#index-admission-turnover-and-uncertain-outcomes) select short SQL coordination and explicit native uncertainty handling. Fault tests must verify those protocols, including late RPCs. |
| Wire contracts and compatibility | [Contracts](opensearch-engineering-v1.md#public-contracts-and-sdk-projections) select phase projections, retry codes, webhook snapshots, and existing SDK load/error access. Implement and exercise them against released and modified SDKs. |
| Frozen acceptance materials | [Manifest and fault matrix](opensearch-engineering-v1.md#acceptance-manifest-and-fault-cases) define required inputs and observable outcomes, including transitional ranking. The twenty actual business queries/evidence and frozen authorized inputs still need assembly and review. Demo/synthetic data do not close that gate. |
| Executable operational plan | [Runbook contracts](opensearch-engineering-v1.md#installation-rollout-and-recovery-runbook-contracts) define installation inputs and ordered cutover/abort/rollback/recovery checks. Actual AWS identifiers/policies, executable scripts, restoration timing, and drills remain future work. |

Numeric worker/indexing concurrency, bulk sizes, qualified production LRU
watermarks, measured capacity, real-S3 performance/cost, and recovery duration
remain validation-derived results. Do not invent final values to make the
design document appear complete. The engineering reference consolidates these
choices without claiming implementation or measured production capacity.

### Ranking statistics during revision coexistence

Status: accepted on 2026-11-03. Native Lucene statistics include physical
candidate and obsolete records while they coexist in the namespace index.

The official [BM25 explanation](https://docs.opensearch.org/latest/api-reference/search-apis/explain/#bm25-scoring-components)
describes index-wide term/document and average-length statistics, while
[query filters](https://docs.opensearch.org/latest/query-dsl/query-filter-context/#combining-query-and-filter-contexts)
restrict eligible hits without recomputing their scores from a new filtered
corpus. With one primary per namespace, the index is the native statistics
scope. Therefore, under the planned same-index revision staging design,
refreshed candidate records and retained obsolete records can influence
scores of published records even when publication filters prevent those
other records from being returned. This is an inference from the documented
scoring/filter semantics, not an observation from the seven recovery cases.

Accepted first-version boundary: retain native statistics rather than
implementing published-only BM25 statistics. Guarantee correct eligible
revisions, bodies, and references; do not promise unchanged ranking while
physical revision records are added or cleaned up. Add quality cases for
partial candidate writes, failed/retried candidates, activation, and cleanup
against the reviewed business-evidence gate. Do not accept a clean frozen
index result as proof of quality throughout those transitions. This is an
accepted ranking boundary and acceptance requirement, not implementation or
evidence that transitional quality has passed.

## Accepted focused validation scope

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: owner clarification accepted on 2026-11-03; not executed. This
supersedes the proposed broader parsing/LLM end-to-end layer and simulated
parsing-worker occupancy. Validate the lexical replacement and related
publication changes within their own boundaries.

Use prepared parsing results and existing jieba token payloads as inputs.
Generate post-parse indexing arrivals directly from the accepted unit/byte
and burst profile. Do not derive this load by repeatedly parsing PDFs or
running external parsing/LLM services. The one-hour ten-times mixed-load run
and five-minute catch-up window remain accepted for indexing and lexical
retrieval only.

For capacity tests, start from retained parsed revisions and their indexing
intent, using real indexing queue/worker execution, PostgreSQL publication
metadata, and the target OpenSearch/durability configuration. Exercise bulk
item handling, visibility acknowledgment, publication commit, cache coherence,
warm/cold lexical discovery, and LRU admission/turnover. Generate actual
BM25/RRF work and report cache/small-corpus bypasses separately. Translate the
50-external-QPS reference profile into measured internal call volume/fan-out;
do not equate that reference with OpenSearch HTTP QPS or claim unrelated
agent/LLM throughput from this component test.

Use deterministic fixtures for targeted integration and fault cases. Supply
prepared parser output to the real completion/handoff boundary to test
durable result/indexing-intent commit, dispatch, and recovery without running
the parser. Cover phase-state projection, SDK waiting/loading, indexing-only
retry, subscribed event creation/delivery, archival/supersession races,
activation interruption, and version-bound result/citation reads. Existing
quality and OpenSearch-native backup/recovery gates retain their own scoped
cases; none require a complete parsing-throughput benchmark.

The existing shared-worker architecture remains the implementation choice.
This validation does not include full parsing throughput, synthetic parsing
occupancy, or external LLM capacity. Limit reported capacity conclusions to
the indexing and lexical paths exercised under the stated worker resources.
Do not represent them as acceptance of whole-system parsing/agent throughput.

## Measured production retrieval baseline

The [2026-11-01 production QPS report](../validation/retrieval-qps-2026-11-01.md)
records 354 production retrieval requests over the frozen 14-day window.
The maximum rolling-second arrival count is five, so the agreed ten-times
planning rule gives a 50-QPS peak target. Rolling-minute traffic peaks at
13 requests, giving a scaled reference of 130/minute (about 2.17 QPS).

The overlapping seven-day CloudWatch access-log and Logfire counts match at
234 requests, including identical status-code counts. Completion-time peak
counts are lower than arrival counts and are not used as the planning base.
The report retains sanitized timings, query definitions, and evidence limits.

The owner accepted the real production burst shape at ten times the volume,
with a fifty-QPS peak. Internal lexical fan-out, indexing throughput,
cold-index demand, and node sizing remain
capacity-design work; the measured API peak does not establish those values.

## Indexing throughput and infrastructure discussion

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Indexing capacity must be expressed as retrieval units and pre-tokenized
payload bytes per time window, together with burst size and queue drain time.
PDF pages, source-file bytes, PostgreSQL term-row count, and parser concurrency
are not interchangeable with OpenSearch indexing throughput.

The owner accepted normal ingestion, revision replacement, and indexing
retries under the real production arrival shape scaled by ten as the daily
indexing workload. Initial migration backfill and bulk rebuild operations
have separate rate limits. This scheduling decision does not authorize a
permanent PostgreSQL disaster-recovery source. They do not determine the daily workload
target. Exact concurrency, rate limits, and bulk-rebuild completion budgets
remain open; ordinary indexing catch-up follows the five-minute gate above.

The existing production publication telemetry did not return usable unit-count
events in the inspected window. A read-only database aggregate over
2026-10-19 05:50 UTC through 2026-11-02 05:50 UTC found 244 successful
document-ingestion results, of which 217 still have a personal-document
binding. All 217 have persisted map-unit statistics:

| Measure | Observed personal-document subset |
| --- | ---: |
| Total retrieval units | 5,327 |
| Median units per result | 1 |
| 95th percentile units per result | 147.2 |
| Maximum units per result | 482 |
| Maximum units arriving within one second | 549 |
| Maximum units arriving within sixty seconds | 991 |
| Maximum units arriving within five minutes | 991 |

Arrival windows use `job_results.created_at`, which is a publication-entry
proxy, not the future post-commit indexing-queue timestamp. The rolling
database windows include both endpoints. The other 27 successful results
belong to the shared demo corpus. Their separate map-unit statistics have not
yet been included, so these peaks must not be treated as the complete
production input.
Historical deletions or in-place retries may also be absent from this ledger.
The maximum task size is distinct from the maximum existing namespace size.
No source text or customer identifiers were exported, and the global term
table was not scanned.

The subset contains 553,301 PostgreSQL term rows and 1,236,584 path/content
token occurrences. Neither is a byte count or an OpenSearch operation count.
An OpenSearch write-capacity number has not been measured or inferred from
these statistics or parser concurrency. Ten times the observed subset
would be 5,490 units per one-second burst and 9,910 per minute; these are
illustrative input volumes, not accepted sustained-throughput requirements.
The sanitized aggregate (`../validation/indexing-load-2026-11-01/summary.json`)
and read-only queries (`../validation/indexing-load-2026-11-01/queries.sql`)
retain the evidence and its coverage limits.

Official [Bulk API guidance](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/#optimal-batch-size)
says batch size depends on document size, mappings, hardware, and networking.
Its suggested batch ranges are tuning starting points, not minimum batch sizes
or a throughput guarantee; small completed jobs need not wait to fill a batch.
Official [memory guidance](https://docs.opensearch.org/latest/install-and-configure/configuring-opensearch/configuration-system/#memory-and-storage-settings)
recommends about half of system memory for heap. The owner accepted the
32-GiB node with 16-GiB heap and the CPU/disk settings below as the validation
baseline. The shared-worker execution baseline below is also accepted.
Active-index limits, numeric concurrency, and mixed-load capacity acceptance
remain open.

## Accepted shared-worker execution baseline

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: accepted on 2026-11-02 for the planned migration; not implemented or
capacity-validated.

Reuse the existing Celery/Redis infrastructure and ECS worker deployment.
Separate parsing and indexing into different task types and queues, consumed
by the same gevent worker pool. Parsing finishes after its result is durable
and the indexing intent is committed through the accepted handoff below.
Indexing performs OpenSearch writes,
all-item checks, search-visibility acknowledgment, revision activation, and
the remaining completion steps. Indexing-only retries reuse the parsed result.
Webhook delivery continues through the existing QStash integration.

Increase total gevent concurrency as appropriate for overlapping I/O waits.
The current inspected production deployment has two worker tasks, each with
2 vCPU, 4 GiB memory, and `WORKER_CONCURRENCY=10`; these are the existing
resource and configuration baseline, not measured task throughput. No new
concurrency number has been accepted. Select it using the real daily indexing
burst profile scaled by ten alongside the accepted retrieval burst profile
with a 50-QPS peak.

The previously suggested global indexing concurrency of two has no measured
capacity basis and is not selected. An additional always-on indexing ECS
service and its illustrative compute charge are not part of this baseline.
An additional distributed concurrency controller is not a prerequisite.
Introduce further indexing concurrency or rate controls only as justified by
mixed-load results, while retaining the accepted priority for online search.
Migration backfill and bulk maintenance remain separately rate-limited.

Distinct queues do not reserve per-type execution slots or isolate CPU,
memory, database connections, or OpenSearch resources. This version accepts
sharing the worker pool rather than requiring fixed parsing/indexing slot
allocations. Validate indexing readiness including queueing, backlog drain,
lexical retrieval, and resource/connection-pool pressure under the accepted
focused scope. Full parsing throughput and simulated parsing occupancy are
outside this validation.
Larger gevent concurrency alone does not establish capacity. A later worker
split or resource increase requires workload evidence and a separate cost
assessment; retaining current resource sizes does not add a permanent worker
compute charge.

The earlier local probe observed three bulk-plus-refresh requests of 352
synthetic units at about 0.80-1.00 seconds. Neither that observation nor a
one-second refresh interval establishes a production document-indexing time
or a suitable concurrency limit. See the
[visibility report](../validation/opensearch-visibility-2026-11-01.md).

## Accepted parsing-to-indexing handoff

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Once the parsing result is durably available, commit its parsing-completion
state, result/revision reference, and the corresponding indexing intent in
one PostgreSQL transaction. The intent identifies the job and immutable
document revision. Do not publish to Celery or wait for OpenSearch inside
that transaction.

After commit, promptly dispatch the indexing task to its Celery queue.
The durable intent permits the parsing task to release its execution slot
without requiring successful indexing or an indefinitely available broker.
A failure to enqueue does not erase successful parsing. Enqueue acknowledgment
alone does not establish indexing success or search readiness.

Reconcile missed dispatches and abnormally interrupted work in the background
using the retained task state. Allow duplicate delivery and make indexing
execution idempotent for the same job and revision. Preserve the indexing
attempt history across dispatch reconciliation. Reconciliation must not reset
the finite failure-retry budget, automatically restart an exhausted indexing
failure, or rerun parsing. Exhausted indexing work remains failed with the
accepted notification and free explicit indexing-only retry policy.

PostgreSQL holds ordinary task state and version identifiers for this handoff.
This decision does not retain the old lexical table or authorize PostgreSQL
as an OpenSearch lexical backup, replay source, or disaster-recovery source.
OpenSearch disaster recovery retains the accepted original-volume and S3
mechanisms.

The existing webhook implementation already persists event intent within its
lifecycle transaction, publishes after commit, and reconciles orphaned events.
Reuse that architectural pattern for Celery indexing dispatch; this does not
route indexing work through QStash or reuse webhook event records as indexing
records. Exact storage representation, claim/lease mechanism, reconciliation
frequency, and revision-activation fencing remain implementation details to
resolve. Public phase fields follow the accepted phase-state contract below.

Integration validation must cover interruption after database commit but
before enqueue, enqueue with a lost acknowledgment, duplicate delivery,
interruption during indexing, and reconciliation after retry exhaustion.
These cases are planned verification requirements, not completed tests.

## Accepted same-document admission through indexing

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Retain the existing single-active-job rule for a document and cover the full
parsing-plus-indexing lifecycle. For the same owner's `document_id`, reject
another update while its current job is queued, parsing, waiting for indexing,
indexing, or executing an admitted explicit retry. Return HTTP 409 and identify
the authorized active job using `active_job_id`. Release this
admission restriction after overall search-ready completion or terminal
failure. A separate parsing-completion event, available parsing result, or
parsing-only SDK return does not release it.

Current owner simplification: this upload/admission rule also rejects an old
job that still has a nonterminal status after its indexing deadline. Return
409 without settling the old job or creating a replacement. Wait for normal
processing or a recovery path to commit done/failed. Explicit indexing retry
keeps its immediate expired-state checks; upload does not gain that behavior.

For example, with revision 1 published and revision 2 still indexing, an
attempt to submit revision 3 for the same document receives HTTP 409. Default
retrieval continues to use revision 1 in OpenSearch. After revision 2 reaches
overall completion or terminal failure, another update can be admitted.
Different documents can continue processing concurrently, including documents
in the same namespace. This rule does not impose a namespace-wide or global
indexing concurrency limit, reserve gevent slots, or keep an idle worker busy
waiting for a future retry.

Indexing-only retry must coordinate with this document-level activity rule
and the accepted supersession and retry-identity policies below. Use the
[current retry response contract](opensearch-development-plan.md#54-free-indexing-only-retry-and-admission)
and preserve free retry and the parsing-charge policy. The owner explicitly
confirmed that a new job still parsing blocks retry of an older failed index
with HTTP 409, without queuing it for later. If an eligible old retry wins
admission first, the concurrent new job conflicts instead. A retry stays on
its original job; this rule does not create an additional retry job.

Current API inspection found the active-job check and conflict response in
`apps/api/app/services/document_ingestion/scope_service.py` and its callers.
The planned integration must preserve the rule through indexing, including
concurrent admission and delayed task delivery; the existing check alone is
not evidence that this new lifecycle is implemented. The admission-enforcement
mechanism remains unspecified; public phase fields are accepted below.

## Accepted superseded-candidate retry policy

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Once a later update request for the same document is durably accepted, an
older failed candidate is superseded. Reject an indexing-only retry for that
candidate with HTTP 409 and explain that a later update has been accepted.
Retain access to its parsing result under the existing result-retention
policy. A rejected or rolled-back update request does not supersede the
candidate. Acceptance of the later update is the boundary; it need not finish
parsing or become searchable first. A later failure does not automatically
restore the old candidate's retry eligibility.

Free indexing-only retry remains available to a failed candidate that has
not been superseded and satisfies the document-activity rule. This changes
retry eligibility, not the accepted parsing-charge or free-retry policy.

Fence delayed deliveries and old in-flight attempts at revision activation.
The activation-eligibility check and publication decision must be coordinated
with acceptance of subsequent updates so that a late task cannot switch the
document back to a superseded candidate. Concrete fencing must implement the
accepted publication protocol below. This policy does not introduce
a phase-state enum or a historical-version rollback API.

For example, revision 2 fails indexing, then an update for revision 3 is
accepted. Retrying revision 2 receives HTTP 409, including after revision 3
has been published. The previously published revision remains selected until
an eligible replacement is ready and activated. Superseding a failed
candidate does not retire the currently published revision at request time.

Planned integration checks cover a retry racing a new update, a rejected
new update, a new update that later fails, and a stale task arriving before
or after publication of the newer revision. These checks have not been run.

## Accepted indexing-retry job identity

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: accepted on 2026-11-02 for the planned migration; not implemented.

An eligible explicit indexing retry reuses the original `job_id`, immutable
parsed revision, and retained parsing result. Record a new indexing attempt
under that job instead of creating another parsing job or result. Each
attempt has bounded automatic retries. Broker redelivery and automatic retry
remain associated with their attempt; neither creates a new explicit retry
or resets its budget.

The retry operation behaves as follows:

- If indexing for the requested revision is already active, return its current
  state without starting another indexing attempt.
- If the requested revision is already search-ready, return that state
  without indexing again.
- If indexing failed and the revision remains eligible, start one new
  indexing attempt asynchronously without reparsing or another parsing charge.
- If a later accepted update superseded the failed candidate, return HTTP
  409 as required by the supersession policy.
- If the document is archived, reject indexing retry without admitting
  another attempt, as required by the archival policy below. Retained job
  results and historical completion do not reactivate an archived document.

Admission of a new attempt must be atomic with the job's current-attempt and
document-activity checks so concurrent retry calls cannot create duplicate
active attempts. These rules coalesce calls while work is active or already
ready; no cross-attempt HTTP request-idempotency key contract has been chosen.
The endpoint and SDK access pattern are accepted below. Detailed response
fields/status codes outside the accepted conflicts and attempt-identifier
representation remain unspecified.

Explicit retry may move the original job from indexing failure back into
active indexing and later overall completion. Keep its parsing phase
successful, retain attempt/failure history, and preserve previously emitted
failure notifications. Old deliveries must not complete or fail a newer
attempt. Overall completion is irreversible. Notification timing follows the
accepted timestamp/terminal-state and phase-state contracts below.

Planned checks cover concurrent duplicate retry calls, retry of an already
ready revision, failure followed by a successful retry on the same job,
superseded candidates, and redelivery from a previous indexing attempt.
These are integration requirements, not completed verification.

## Accepted indexing-retry API and SDK access

Current owner amendment: `POST /v1/jobs/{job_id}/indexing/retry` and the v2
equivalent run one indexing execution synchronously inside the HTTP request.
They reuse the original job/revision, require normal ownership/write permission
and no upload/reparse/charge, and do not create a task or detached continuation.
Node `jobs.retryIndexing(jobId)` and Python `jobs.retry_indexing(job_id)` await
that final response. Preserve webhook URL and subscriptions.

Use the same selected-backend budget as initial indexing: a fixed configurable
ten-second limit for OpenSearch, and a separately qualified longer PG-primary
budget during preparation/rollback. Return 200 for published success or
historical completion; an active execution returns 409 /
`INDEXING_ALREADY_RUNNING`. Persist admitted failure before returning its
canonical non-2xx error with original job/phase/retained-result context:
timeout 504 / `DEADLINE_EXCEEDED`, unavailable backend 503 / `UNAVAILABLE`,
corrupt internal input/mapping 500 / `INTERNAL_ERROR`. No 202 queued work.

Admission remains atomic and rejects superseded/archived candidates or required
canonical parsing results lawfully removed under their existing lifecycle.
There is no separate age-based retry expiry; signed-URL expiry does not end
eligibility. Missing/corrupt assigned input is an operational error. Preserve
prior outcomes and resolve an ambiguous commit after a
lost response; concurrent calls cannot start duplicate work. See
[current response and eligibility rules](opensearch-development-plan.md#54-free-indexing-only-retry-and-admission).
This supersedes the earlier immediate-return/background-retry contract.

## Accepted webhook timestamps and terminal-state behavior

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Keep the webhook contract simple. Do not introduce a client-facing status
version number, require clients to maintain a version counter, or promise
delivery order. Reuse the existing event ID for duplicate recognition and
the original server-side UTC occurrence time for event timing. Redelivery
preserves the event ID, occurrence time, and event-time state snapshot;
do not replace them with the delivery time or the job's later state.
Existing completion/failure timestamps can express this timing; exact field
names for the combined phase contract remain to be finalized.

Overall completion is irreversible for that job. Reject late worker actions
that would move a completed job to failure or active processing, and document
that an older failure notification does not undo completion. Indexing failure
ends the current execution and does not restart automatically after its
retry budget is exhausted. An eligible explicit retry can start another
indexing attempt under the original job, as already accepted above. Old
attempts cannot change a newer attempt's outcome.

Reuse `job.failed` and `job.completed` for retry outcomes; parsing is not
rerun, so no new parsing-completion event is generated by indexing-only
retry. Previously emitted failure events remain historical records.
Timestamps provide event-time context, not a strict ordering protocol. The
job query is authoritative for current state when notification timing is
ambiguous. This decision does not replace server-side state-transition or
attempt-ownership checks with timestamp comparisons.

The inspected state machine already rejects `done` to `failed` and permits
an explicit retry transition from failure. The existing webhook outbox also
stores completion/failure occurrence timestamps. These are foundations for
the planned integration, not evidence that its phase contract is implemented.

## Accepted document reads and client responsibility

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Default document-content reads by `document_id`, including parsed chunks,
select the latest successfully parsed revision. Its parsing result becomes
readable independently of indexing progress or failure. Job-result reads
continue to identify that job's immutable parsing result. Default retrieval
continues to select published revisions, while retrieval bodies, page assets,
and citations use the same revision as the corresponding hit.

For a document with revision 1 published and revision 2 parsed but not yet
search-ready, ordinary document-content reads return revision 2, default
search uses revision 1, and a reference returned by that search opens revision
1. A newly parsed document can expose its content before it has any published
retrieval revision. A failed parse does not replace the latest successfully
parsed revision. Indexing success later makes the parsed and published
selections converge after activation.

Use the existing immutable revision dataset for these reads. Separate the
logical parsed-content and retrieval-publication selections; do not assign
both meanings to a single ambiguous current-revision selector. The exact
storage representation and field names remain to be designed. Content,
filename, page information, assets, and readiness in a response must describe
the revision actually returned. An older published revision's readiness must
not make a newer parsed revision appear search-ready or overall complete.

Keep ordinary client use simple:

- Reading a document's parsed content requires only its document identifier;
  explicit revision selection is optional.
- Keep the SDK's default parsing call waiting for overall search readiness.
  The already accepted parsing-only option remains the single opt-in for
  callers that want an earlier result.
- Retrieval returns a reference or link bound to the retrieved revision.
  The SDK carries that binding when opening content or citations, and direct
  API users can use the returned reference/link rather than reconstructing
  revision parameters or managing publication pointers themselves.
- Expose the returned revision and its phase states for observation without
  requiring clients to coordinate parsing and indexing, choose a lexical
  backend, or operate namespace indexes.

Current code already binds job results to jobs, supports revision-scoped
document chunks, and propagates `job_result_id` and asset URLs in resolved
references. Default document chunks currently follow `current_job_result_id`,
and private document detail projection does not yet implement the new read
contract. Reuse and extend these foundations; do not claim that existing SDKs
or all citation paths already hide version selection. Exact public reference
shape, lifetime/retention, cache behavior, and publication protocol remain
implementation topics.

## Accepted two-release rollout and asynchronous PostgreSQL lexical copy

Status: rollout sequence and preservation of the legacy write path accepted
on 2026-11-03; not implemented or deployed. The owner permits up to two
hours of total nighttime downtime for the formal cutover, as defined below.

### Release 1: prepare and migrate existing data

Keep PostgreSQL as the live lexical backend while deploying the preparation,
backfill, and reconciliation capabilities. Backfill only non-archived
documents' current published revisions, using existing jieba token data
without reparsing or retokenizing. Use resumable revision/unit coverage
records; physical native IDs still bind `(build_id, unit_id)` with a distinct
build for each claimed execution, including migration executions.
Reconcile publications, replacements, and archival that occur during the
copy so final coverage reflects the current published corpus, not just an
earlier snapshot. Existing namespace generation and revision metadata can
identify changes; concrete reconciliation mechanics remain implementation
work.

While PostgreSQL still serves retrieval, preserve its completion boundary:
the live PostgreSQL lexical data must be ready before overall job success.
Do not enable a detached rollback-copy mode while that copy is still the
live search index. Complete long-running backfill and the agreed quality,
focused capacity, publication, and recovery verification before the final
maintenance window.

### Release 2: formally enable OpenSearch

Use the agreed nighttime maintenance opportunity for the final publication
barrier, draining in-flight publication, final delta reconciliation, and
coverage checks. Check published revision identity, unit identity/counts,
and input consistency; aggregate count equality alone is insufficient.
Only after the checks pass, activate OpenSearch globally and resume normal
publication under the accepted background-indexing protocol. Service
readiness and scoped retrieval smoke checks are part of completing the
maintenance window. This plan does not authorize deployment now.

### Preserve the PostgreSQL lexical writer asynchronously

Once OpenSearch serves retrieval, keep the existing PostgreSQL term-table
write/COPY implementation in a background thread during the migration
observation/rollback period. One shared create/update-index service records
durable copy intent with publication and submits it to a bounded thread pool
after commit. Do not wait for that copy or create a per-copy Celery job/task. Use retained revision-bound
prepared data, idempotent execution, a separate database connection and
transaction, tracked failures, and recovery of pending intent after thread-pool
saturation or process restart through existing maintenance. An untracked
process-local thread alone does not retain a reliable rollback copy. Long token
COPY must not hold the shared namespace/control/publication locks needed by
OpenSearch activation; confirm this under paused/slow-copy integration tests.

This applies to the legacy lexical table, not the PostgreSQL document/job
metadata, body data, or atomic publication transaction. Overall completion
waits for the authoritative OpenSearch publication, independently of the
asynchronous legacy copy. A legacy-copy failure must not downgrade a
completed job, emit a new client `job.failed`, or alter parsing billing.
Track and repair copy lag/failures internally.

A deliberate migration rollback to PostgreSQL requires verified coverage
of the current published corpus, including completion of pending/failed copy
work and consistent archival state. Keep the accepted boundary: this is a
temporary migration rollback path. After OpenSearch is accepted as stable,
stop those lexical writes and clean up obsolete lexical data as previously
agreed. It is not an OpenSearch recovery source.

Include this related asynchronous-copy path in focused integration/resource
checks while it is enabled: measure its queue lag and database/worker
pressure separately. The five-minute indexing catch-up gate measures
OpenSearch publication, not completion of the legacy rollback copy.

### Accepted first observation period

Accepted on 2026-11-03: use fourteen days as the first internal observation
period after successful formal cutover and restoration of normal service.
Keep the temporary PostgreSQL lexical copy writing asynchronously during
this period, and track its backlog, lag, and failed-copy recovery.

Review actual business retrieval quality, latency, indexing failures/backlog,
LRU reopening frequency, and backup coverage. Include version/reference and
namespace correctness. Accepted native Lucene ranking differences remain
part of the design; ranking-order parity alone is not a rollback criterion.
Assess observed traffic and coverage together with the prior focused quality,
capacity, publication, and recovery evidence.

The first fourteen days are a review period, not an automatic stability
declaration. At its end, explicitly assess whether evidence is sufficient
or observation should continue. Stopping legacy writes and cleaning up the
PostgreSQL lexical data remain separate later decisions after stability is
confirmed; do not trigger either action solely because the period elapsed.

If the solution is judged unsuitable during observation, a deliberate
global migration rollback may be evaluated at that point, subject to verified
PostgreSQL-copy coverage and the accepted controlled switch procedure.
Ordinary OpenSearch failures continue to use its native recovery routes;
this observation period does not add an automatic PostgreSQL fallback.

### Accepted migration rollback scope and client compatibility

Accepted on 2026-11-03: a deliberate migration rollback changes the global
lexical retrieval backend and authoritative indexing target to PostgreSQL
while retaining the published client contract. It does not roll the
application back to the earlier synchronous, single-stage client behavior.

- Keep separate parsing and indexing states, the default search-ready wait
  and optional parsing-only wait, subscribed webhook events, revision-bound
  result access, and free eligible indexing-only retry.
- Under the current execution amendment, initial indexing runs inside the parse
  task and manual retry runs synchronously inside its HTTP request. With PostgreSQL primary, overall
  `done` and `job.completed` require that revision's PostgreSQL lexical data
  and publication are committed and ready for retrieval. Parsing completion
  can still be delivered earlier. The detached migration-copy role no longer
  describes the authoritative PostgreSQL indexing work after rollback.
- Preserve completed jobs as completed without reissuing completion solely
  because the backend changed. Preserve failed jobs as failed until an
  eligible explicit retry; do not automatically revive them during rollback.
  Retained parsed results remain available without reupload or reparsing.
- Verify current published-revision coverage and archival consistency before
  switching. Use the controlled publication barrier to settle in-flight work
  and prevent late OpenSearch work from activating revisions after the switch.
  The later owner-selected interruption rule supersedes automatic resumption:
  retain parsing/result/charge and settle interrupted indexing as failed.
  Only an eligible explicit retry starts a fresh attempt against the selected
  backend after service resumes; preserve original job identity and ownership.
  An aborted switch does not revive the interrupted attempt either.
- Preserve publication selection and reference consistency, and establish
  coherent cache behavior before resuming service. Use a compatible application
  release that supports the accepted client contract and PostgreSQL target;
  reverting to an old binary or removing the new state metadata is not the
  rollback procedure.

This remains an explicit, temporary migration decision after coverage
verification. It adds no per-request fallback, automatic backend switch, or
PostgreSQL disaster-recovery dependency. The future retirement decision is
unchanged. See ADR 0024.

### Accepted nighttime downtime budget

Accepted on 2026-11-03: allow up to two hours of total customer-impacting
downtime during the second release's nighttime cutover. This replaces the
earlier proposed five-minute maintenance limit. Measure from the first
service unavailability/publication barrier through restored API and retrieval
service. Include drain, final reconciliation, deployment/switching, readiness
checks, and any required rollback/service restoration in the same clock.
Do not reset the clock for another attempt.

Complete initial full backfill and long-running verification during release
1. Use the nighttime window for final cutover work. Restore service as soon
as it is ready; two hours is the outer budget, not the intended duration.
Measure restoration time during rehearsal and reserve enough of the window
for it when setting the abort point. Do not consume the entire budget on
preparation and begin restoration only at its expiry.

If preparation/coverage cannot finish with the required restoration margin,
leave PostgreSQL serving, lift the barrier, and reschedule rather than
switching an incomplete corpus. If a commit result is ambiguous, inspect
actual committed state under the accepted publication protocol; do not
assume the backend never switched. Any deliberate migration rollback still
requires verified PostgreSQL-copy coverage and the controlled global switch.
The no-runtime-fallback rule remains unchanged.

The five-minute indexing catch-up gate is a separate normal-load validation
criterion and remains unchanged. Existing SDK waiting-timeout/resumption
semantics also remain unchanged: a client polling timeout or rejection of a
new request during maintenance does not itself fail an existing server job.
An admitted indexing phase actually interrupted by the planned transition does
become failed under the current owner-selected rule, while parsing/result/charge
remain available for explicit retry after service resumes.

## Operational alerts and existing Logfire delivery

Status: alert rules and the existing Logfire delivery approach accepted on
2026-11-03 after official-source review. The owner confirms that an error-log
alert is already configured. Reuse that notification path with off-node
Knowhere checks. No monitors, code, or live notification rules have been
configured here.

| Condition | Operational response |
| --- | --- |
| Service unavailable, final indexing failure, or unexpected revision/namespace correctness failure | Alert on detection; exclude intentional archival termination and ordinary client errors. |
| An admitted indexing task has not reached publication after five minutes | Alert on indexing age/backlog; this does not itself fail the task. |
| Legacy PostgreSQL copy exhausts recovery attempts, or the oldest outstanding copy exceeds thirty minutes | Alert while that temporary copy is enabled; keep public job completion unchanged. |
| Snapshot creation/deletion fails, coverage is incomplete, or no new complete successful snapshot exists for over two hours | Alert; failed/partial or coverage-incomplete snapshots do not reset successful-backup age. |

Age checks start at the relevant durable indexing/copy intent, not the
original upload/parsing start time. Query retained work state so a missed
broker dispatch is visible; queue length alone is insufficient. None of
these operational alerts selects a backend, changes job states, or refunds
charges automatically.

### Native capabilities and accepted delivery

The official [Alerting monitors](https://docs.opensearch.org/latest/observing-your-data/alerting/monitors/)
support scheduled checks and notification actions. Official
[cluster-metrics monitors](https://docs.opensearch.org/latest/observing-your-data/alerting/per-cluster-metrics-monitors/)
cover cluster health, CPU, JVM memory, and disk-related metrics. Monitors and
notification channels require configuration; these capabilities do not
include Knowhere's application-specific indexing/copy rules by themselves.
Because these monitors execute within OpenSearch, the single-node design
implies they cannot be the sole detector of that node's own outage.

Use one notification path through the existing Logfire error-log
alert. Run periodic health/work/snapshot checks from the existing Knowhere
runtime outside the OpenSearch host, initially every minute with bounded
request timeouts. Use official health/statistics/snapshot APIs and durable
application task metadata as inputs. Detect service failures even without
user traffic. Emit immediate terminal/correctness failures where detected;
periodic checks detect age and missing-progress conditions.

The existing `shared/core/logging.py` already configures Logfire and adds a
Loguru sink at INFO and above. The official
[Loguru integration](https://github.com/pydantic/logfire/blob/main/docs/integrations/loguru.md)
preserves severity and structured fields. Reuse that bridge: qualifying
operational conditions emit structured `error` records so the existing
error-log alert can handle them. Expected retries/client actions retain
ordinary non-error handling; record recovery as `info`. Include stable event
names, resource/job/revision context where relevant, observed values,
thresholds, and observation time without document contents or credentials.

Coalesce/throttle repeated observations of the same ongoing condition on
the producer side, accounting for multiple API/worker processes. Verify
the actual notification rate under the existing alert rule rather than
assuming every repeated error is automatically grouped. Keep any planned
maintenance suppression bounded to the accepted maintenance window.
Do not add a parallel OpenSearch notification channel as a prerequisite.

Focused implementation acceptance must verify that a controlled error reaches
the intended Logfire project/environment and triggers the existing alert,
that background checks detect a stopped OpenSearch node and stale work,
and that intentional archival/normal retries do not generate those errors.
The user's existing alert configuration has not been changed or tested here.

## Accepted revision-publication protocol

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Use PostgreSQL's document-publication metadata as the authority for which
revision retrieval selects. This is ordinary document/job metadata, not a
PostgreSQL lexical copy. Keep latest-parsed selection independent as agreed.

1. Write the immutable candidate revision to OpenSearch using idempotent
   `(build_id, unit_id)` native identities, check every required bulk item, and confirm
   search visibility. Keep OpenSearch network calls outside PostgreSQL
   transactions. OpenSearch visibility alone does not publish the candidate.
   A replay within one owned execution uses the same IDs; a different claimed
   execution uses a fresh build, even for the same revision. Revision/unit
   coverage identifiers alone are not safe physical native IDs.
2. In one short PostgreSQL transaction, recheck the current job/attempt,
   document activity, archival state, and supersession eligibility. Then
   switch the published-revision selection, advance the authoritative
   internal retrieval-cache generation, set indexing and overall completion,
   and persist the subscribed `job.completed` outbox event. These changes
   commit together. Its event identifier, occurrence time, and state snapshot
   remain stable under subsequent delivery retries.
3. After commit, deliver the retained outbox event through the existing
   notification mechanism. A failed delivery does not undo publication or
   job completion. Do not create a second logical completion event to recover
   an already committed publication.

Retrieval captures a coherent PostgreSQL published-revision/generation
snapshot before accepting a cached result. Bind cache entries, OpenSearch
candidate filters, and body/reference hydration to that snapshot. Apply the
published-revision filter before per-channel top-K truncation and RRF.
Subsequent snapshots after publication use the new generation and revision;
requests that already selected the old snapshot may finish with it under
the existing read-lifetime policy.

The current Redis cache version and invalidation are not part of the
PostgreSQL transaction. The future integration must make the committed
PostgreSQL generation authoritative for cache-hit acceptance; a delayed Redis
invalidation must not allow a new query to accept an obsolete cache entry.
Concrete cache-key/projection changes remain implementation work. This
internal cache generation does not add a client-facing job-state version.

If OpenSearch preparation succeeds but the publication transaction has not
committed, the candidate remains outside retrieval scope and the old
published revision remains selected. For a new document, it remains outside
retrieval until publication commits. Recover through the accepted retained
indexing intent and bounded retry/reconciliation policy.

After an interrupted or ambiguous commit, read PostgreSQL's actual committed
job/publication state before deciding what to retry. If committed, retain
the new publication and continue delivery of the existing outbox event. If
not committed, preserve old selection and resume eligible work on the same
job/revision without reparsing or another parsing charge. Never assume a
commit failed merely because its acknowledgment was lost.

Serialize activation with archival and subsequent-update admission through
the same guarded metadata transition. A document archived or superseded
first cannot be published by a late worker. Detailed row/namespace lock
ordering, attempt claims, and storage fields remain to be implemented.

Planned fault checks cover interruption before and after OpenSearch
visibility, before the PostgreSQL commit, loss of the commit acknowledgment,
interruption after commit but before notification dispatch, delayed Redis
invalidation, stale cache writers, and archival/supersession races. Verify
version-consistent search and citations, irreversible completion, and a
single logical completion event. This is a protocol acceptance, not a
passing implementation or fault-injection result.

## Accepted archiving during indexing

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: accepted on 2026-11-02 for the planned migration; not implemented.

After parsing completes, allow the document to be archived while indexing
is queued or executing. The single-active-job update restriction must not
block this archival operation. On archival commit, exclude the document
from subsequent retrieval scope. Background work cannot reactivate it or
publish a candidate after archival.

If the job has not reached overall completion, settle it as `status=failed`,
`failure_stage=indexing`, `parsing_status=completed`, and
`indexing_status=failed`, explaining that the document was archived.
Preserve the immutable parsing result under existing retention rules.
Successful parsing stays charged, with no refund. Send `job.failed` under
the original event subscriptions; no additional event type or cancellation
status is introduced. Archival ends this execution without waiting to
exhaust the automatic indexing retry budget.

Reject indexing retry for an archived document without starting another
attempt. Reconciliation and delayed/duplicate workers must respect the same
archival boundary. If the job was already `done` before archival, retain its
successful historical job and phase states; archival does not produce a new
job failure or undo an earlier completion event.

Coordinate archival with activation using atomic document/job checks. If
archival commits first, activation cannot succeed. If full publication and
job completion commit first, later archival hides the document while its
completed job remains unchanged. An in-flight OpenSearch write may finish,
but it cannot authorize publication. Logical exclusion must not wait for
physical lexical cleanup; use the existing safe background cleanup policy
and leave other documents in the namespace searchable.

Read-only source inspection found that the current archive service has no
active-job rejection and advances namespace generation. Ordinary publication
can write the document back to `active`; its explicit archived-document
guard currently applies only to the shared demo corpus. The new integration
must guard ordinary documents too. This is an identified implementation
requirement, not proof that the archival race has been fixed.

Planned checks cover archival while queued, archival during a bulk request,
archival racing activation, duplicate task delivery, rejection of retry for
an archived document, preserved result access/billing, and archival after
completed publication. These checks have not been implemented or run.

## Accepted old-revision lexical cleanup

Status: accepted on 2026-11-02; reader-drain requirement superseded by the
2026-11-04 amendment below. The amendment is implemented on the isolated working
branch and passed targeted checks; production acceptance remains open.

Clean up obsolete revision records in OpenSearch after the replacement is
fully search-ready and activated. A namespace has one index, so revision
cleanup removes that revision's lexical records inside the namespace index;
it does not delete the namespace index or the new revision's records.
If replacement indexing is pending or failed, retain the published revision
that still serves retrieval.

An in-progress retrieval request keeps its selected revision set for its
remaining search, hydration, and reference work. Requests selecting revisions
after activation use the newly published set. Accepted on 2026-11-04: cleanup
does not wait for requests using an old build. If its removal causes fewer or
no hits for the updating document, return the actual results with normal HTTP
200, including an empty result list. Do not retry because of replacement,
switch the request to the new revision to fill gaps, or return an updating
error/warning. No final publication-drift check is required solely to restart
this race. Every returned body, asset and citation remains bound to its hit's
selected immutable revision/build. Actual backend failures are not normal
empty results; LRU reopening remains a separate recovery policy. Knowhere owns
cleanup; callers do not track readers or schedule it. Delete only exact retired
build IDs, never a current published build. Retired builds cannot be republished.
The prior requirement to drain readers is withdrawn; crash recovery and cleanup
scheduling still require implementation and validation.

Old parsing results, body data, page assets, and citations follow the existing
result-retention and permission rules. A previously returned reference reads
its bound parsing revision independently of whether its lexical records
remain in OpenSearch. Temporary resource URLs retain their existing expiry
behavior; the SDK/reference entry point obtains a valid link for the same
revision. This decision does not promise indefinite data or URL retention.

A cleanup failure does not undo the new revision's activation or downgrade an
already completed job. Verification must cover queries spanning activation,
cleanup while an old query is active with fewer/empty hits and no version retry,
correct body/citation binding, references opened after lexical cleanup, genuine
native errors, and interrupted cleanup. Targeted contract/native checks now
pass; see the [validation report](../validation/opensearch-unlocked-reads-2026-11-04.md)
for evidence and remaining acceptance limits. See the
[revised coordination proposal](opensearch-concurrent-read-lock-proposal-2026-11-04.md#accepted-replacement-behavior-and-proposed-smaller-coordination-protocol).

## Index-slot coordination and measured local cost

Status: the owner accepted the coordination principle and selected a
first-version configured cap of 400 simultaneously open namespace indexes
on 2026-11-02. This is a configuration decision. Isolated controller/component
proofs now exist, but production capacity is not accepted. The owner selected a default
five-second, configurable search admission-wait budget. It does not set the
OpenSearch open/search timeouts or an end-to-end request latency guarantee.
The owner accepted the observed local operation cost as sufficient to retain
on-demand opening/closing in the first-version design. Production timing
still requires the selected node, S3 configuration, and mixed-load validation.

Current amendment (2026-11-04): hot search uses its ordinary captured scope
without open/UUID/lifecycle preflight, leases or shared-row read admission.
Writers ensure an open index before claim/writes; a recognized closed-index
read failure allows one bounded recovery of the original bindings. LRU may
close during native search/write/visibility work. Writer `index_use` credentials
remain ownership/fencing records and do not exclude closure. Coalesce guarded
opens of the same closed namespace; do not recreate a known missing index.
See the [current query/lifecycle rules](opensearch-development-plan.md#6-query-consistency-locks-and-lifecycle-rules-to-preserve).

The earlier active-use close exclusion and request-completion eligibility rules
are withdrawn. Proactive turnover uses best-effort recency and guarded lifecycle
eligibility, not an active-request close fence. The 360/320 validation candidates
are recorded below; production qualification and numeric resource warnings are
pending. A fixed inactivity timeout has not been selected.

The 400-index cap applies to business namespace indexes managed by Knowhere,
with one primary shard and zero replicas per namespace. Include opening
reservations in admission accounting so concurrent open requests do not
over-admit. Plugin/system indexes are outside the namespace eviction set;
their actual shard and memory overhead must be measured and reserved in
node capacity validation. The setting does not claim that 400 namespaces
plus system overhead have already been demonstrated safe.

When a namespace needs an opening slot, close a lifecycle-eligible LRU victim
and confirm closure with no outstanding issuer before releasing its slot.
Request completion and deletion of old build records do not release a slot.
Active native requests alone do not prevent eviction. If all slots remain
charged and no safe release is available, wait for admission for
up to five seconds by default, or a shorter caller deadline, then return a
retryable busy outcome if admission remains unavailable. This configurable
budget covers capacity waiting rather than the full search request or its
subsequent OpenSearch operations. It is an owner-selected experience budget,
not a duration inferred from the local timing samples. Background indexing
keeps the accepted queueing and finite retry policy. Do not bypass the cap
or add a PostgreSQL lexical fallback. Already open namespaces do not need a
new opening slot.

The existing `UnavailableException` provides the repository's HTTP 503,
retryable service-error shape. Use the normal error contract rather than
requiring callers to operate index lifecycle endpoints. Exact retry hints,
cancellation/coordination behavior and SDK deadline handling follow the current
engineering/development contract. Proactive LRU turnover is accepted for validation; final production
watermarks remain subject to representative mixed-load qualification.

This is distinct from worker/task concurrency. Knowhere coordinates lifecycle
state across API and worker processes; callers do not open or close indexes.
Preserve the selected reservation/uncertainty reconciliation and writer fencing
without restoring active-use close exclusion. Formal service/target acceptance
remains required. No additional always-on service is selected here.

A bounded [local measurement](../validation/opensearch-open-close-2026-11-02.md)
now provides observations rather than an assumed typical duration:

| 370 synthetic namespace indexes; ten swaps per target | 32-unit target | 45,000-unit target |
| --- | ---: | ---: |
| Close idle 32-unit victim, median | 26.4 ms | 22.5 ms |
| Open target until primary active, median | 83.7 ms | 71.5 ms |
| First path and content searches, median combined | 6.3 ms | 8.9 ms |
| Close victim through completed first search, median | 116.9 ms | 104.3 ms |
| Complete swap, largest observed sample | 263.2 ms | 144.7 ms |

The node was a temporary local 2-vCPU/4-GiB VM with a 2-GiB container and
1-GiB heap. There were 370 namespace indexes, plus three plugin/telemetry
indexes; one namespace stayed open between measured swaps. The large target
had 45,000 synthetic units and about 88.3 MiB of primary store at setup.
Filesystem caches were not dropped, no production mixed load was replayed,
and S3 remote durability/state was absent. The operation sequence did not
implement Knowhere's lifecycle controller or simulate waiting for busy users.
These timings do not set production capacity, an open-index cap, a wait
budget, a percentile target, or a latency guarantee on the accepted 32-GiB
node. Closed namespace metadata remains part of the cluster-size workload.

Total cold-namespace request overhead comprises waiting for an idle victim,
closing it, opening the target, and the first search. The table measures the
last three terms with an already idle victim. Validate the accepted node,
S3 configuration, and ten-times real mixed-load profile before choosing
the remaining numeric budgets. The official
[Open Index API](https://docs.opensearch.org/latest/api-reference/index-apis/open-index/)
and [Close Index API](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/)
describe behavior and 30-second default timeout parameters; these are not
typical operation durations or an end-to-end response guarantee.

Capacity acceptance of the configured 400-open-namespace cap must not be
derived from the close/open timings above. Official
[AWS OpenSearch Service heap guidance](https://docs.aws.amazon.com/wellarchitected/latest/amazon-opensearch-service-lens/aosperf01-bp03.html)
uses approximately 25 shards per GiB of JVM heap as a planning guideline.
For the selected 16-GiB heap this is roughly 400 shards, including system
shards. This is managed-service guidance, not a capacity guarantee for the
self-hosted mixed-role node; its approximate total-shard guideline does not
certify the owner-selected namespace cap plus system overhead. Validate the
current approximately 370 namespace indexes simultaneously open, then the
configured 400-namespace boundary with representative data. Increase open
counts gradually on the selected node while checking the agreed mixed load,
heap/GC behavior, and queue drain. The previous local probe kept almost all
namespace indexes closed and does not satisfy this capacity requirement.
Revise the configured cap before production acceptance if validation fails.

## Proactive LRU turnover

Status: proactive turnover accepted for validation on 2026-11-03. Use the
high/low settings below as configurable candidates during validation;
production watermarks are not accepted. Isolated controller proofs are recorded
in the progress ledger; target-node mixed-load qualification remains open.

Open describes whether OpenSearch can serve the index. Current amendment
(2026-11-04): native request activity is not a close fence, and request completion
is not a prerequisite for reclamation. LRU uses best-effort recency; reads have
no use leases and retained writer credentials only protect writer authority.
The earlier in-use close-protection rule is withdrawn; use the
[reader-free integration constraints](opensearch-integration-constraints.md).

Preserve the 400-business-index hard limit. Trial a 360-index high watermark
to trigger background reclamation and a 320-index low-watermark target.
Reclaim lifecycle-eligible least recently used indexes in bounded batches,
even if native work races closure. Preserve ownership/epoch/UUID fencing,
coalesced lifecycle operations and bounded closed-index recovery. Recheck
lifecycle eligibility, count opening/closing/uncertain reservations and release
a slot only after confirmed closure with no outstanding issuer. If reservations
cannot be safely released, retain them and reconcile rather than bypassing the
cap. The low watermark is a target, not permission to bypass those fences.

Use Knowhere's background lifecycle coordination to prepare spare slots
before a new request needs one. New documents in an already open namespace
reuse that index. A new or evicted namespace still pays creation/opening
cost; proactive turnover removes avoidable foreground victim-reclamation
work, not the intrinsic opening operation. No caller-operated lifecycle API
or additional always-on service is proposed.

Normal cluster restarts preserve existing cluster state, as documented in
the [bootstrapping reference](https://docs.opensearch.org/latest/tuning-your-cluster/discovery-cluster-formation/bootstrapping/).
Do not treat restart as an eviction policy or reset admission accounting to
zero without checking actual state. The
[Close Index API](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/)
retains disk data while reducing indexing/search runtime structures; closed
index metadata still contributes to cluster overhead.

Validate open-index reuse, reopen frequency, reclamation batch cost, normal
admission waits, and mixed-load latency before tuning the proposed watermarks.
Also exercise the 370/400-index resource boundary independently of normal
background reclamation so turnover does not mask hard-limit resource costs.
The proposed settings do not establish that the node can sustain them.

### Official support boundary and costs

Reviewed on 2026-11-03 and accepted as the support/validation boundary.
This is documentation evidence, not target-node validation or production
acceptance of the candidate watermarks.

OpenSearch officially supports closing and reopening indexes, and ISM also
offers close/open actions. The
[Close Index API](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/)
retains disk data, blocks reads/writes while closed, reduces indexing/search
runtime structures, and explicitly states that opening/closing restarts
shards through normal recovery. The
[ISM operation reference](https://docs.opensearch.org/latest/im-plugin/ism/policies-operations/)
documents these actions. These primitives do not certify Knowhere's
cross-process LRU coordinator, the proposed 360/320 watermarks, the 400-index
hard limit on this node, or the one/three/eight-second acceptance targets.

Do not claim zero side effects. Inference from the documented recovery and
closed-index behavior identifies these costs and correctness requirements:

- Frequent eviction/reopening can increase recovery work and first-query
  latency. An aggressively low retention target can evict namespaces that
  will soon be used again. Validate reopen frequency, warm reuse, CPU/I/O,
  cluster-manager work, and any additional S3 requests on the chosen remote
  store baseline. Their magnitude is not established by the documentation.
- Closing an index still needed by another API/worker operation can fail that
  operation. The activity guard must cover all replicas/processes and be
  rechecked atomically when claiming closure. Official API availability is
  not proof that this application coordination is correct.
- The [Create Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/create-snapshot/)
  states that snapshots include open indexes by default; closed indexes are
  included only when specified in `indices`. Preserve the accepted explicit
  closed-index coverage requirement and verify actual complete backup and
  restore coverage. Coordinate turnover with backup/recovery activity.

Keep the 360/320 settings as configurable candidates until representative
mixed-load tests qualify them. Verify the chosen OpenSearch release together
with remote durability/state and full snapshots; do not describe the complete
combination as officially endorsed or proven harmless.

## Accepted business retrieval-quality gate

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: first-release scoring and relative acceptance agreed after the
isolated business replay. Use native Lucene BM25 with the existing jieba
tokens, BM25 parameters, and path/content RRF. Retain custom scoring as a
separate option if the business requires stable rankings during staged
writes.

Start with 20 real business queries whose expected supporting evidence is
known. Cover common terms, rare terms, multiple terms, path matches, and
different namespace sizes. Freeze the corpus revisions, query inputs,
request options, and expected evidence before comparing the backends. Keep
the existing jieba tokens, BM25 parameters, and path/content RRF settings;
the accepted Lucene IDF change remains part of the planned implementation.

Judge the final user-facing evidence relative to PostgreSQL on the same
inputs. Preserve the marked evidence groups that PostgreSQL finds in the
requested final results. Investigate lost evidence and clear business
relevance regressions before cutover. Record shared misses separately as
existing quality gaps. Require correct citation content, namespace and
permission isolation, and revision identity. Accept reordered results and
changes to the result set when the marked evidence remains available.

Exercise the lexical adapter in the regression set and distinguish cached or
small-corpus bypass results from actual lexical-path checks. Keep request
parameters and selected revisions comparable so backend differences are not
confused with a corpus update or a different retrieval route. Expected
evidence is a business judgment, not automatically whatever PostgreSQL ranked
first. Record results and any accepted relevance changes for review.

Ranking-transition clarification accepted on 2026-11-03: apply this evidence
gate before staging, during partial and complete unactivated writes, after
candidate failure/automatic or explicit retry, at activation, and before/
during/after obsolete-build cleanup. Physical candidate and retained old
records may influence native BM25 statistics while remaining outside the
eligible result set. Correct publication and citation selection and the
relative evidence gate apply throughout. See the
[ranking boundary](#ranking-statistics-during-revision-coexistence) and
[fault matrix](opensearch-engineering-v1.md#acceptance-manifest-and-fault-cases).

The [20-query business replay](../validation/opensearch-business-ranking-2026-11-03.md)
found that native BM25 preserved all 13 PostgreSQL evidence hits through each
stage. Both backends missed the same seven marked evidence sets. The replay
used a frozen 18-document subset of one namespace. Treat it as evidence for
that sample and keep throughput, mixed-load, and recovery qualification
separate.

## Accepted infrastructure validation baseline

Status: accepted mixed-load validation baseline. Production capacity is not
accepted and resources have not been provisioned. The owner accepted the
machine, heap, disk, network, shard, and durability baseline; exact software
version and shared-worker numeric concurrency remain open. The first-version
recovery automation scope is accepted below; scripts and supervision are not
yet implemented.

| Component | Accepted validation starting configuration |
| --- | --- |
| Placement | Existing production VPC in `us-east-1`, private subnet |
| Compute | One always-on Linux ARM64 `r7g.xlarge`: 4 vCPU, 32 GiB RAM |
| Process ownership | Dedicated OpenSearch host; API remains on ECS; parsing and indexing share the existing ECS worker deployment |
| JVM heap | Equal minimum and maximum, 16 GiB; remaining RAM shared by native allocations, the OS, and filesystem cache |
| Persistent data volume | Separate encrypted 100-GiB EBS gp3 volume, initially 3,000 IOPS and 125 MiB/s |
| System volume | Separate 20-GiB gp3 volume |
| Shards | One primary shard per namespace index, zero replicas |
| Open namespace indexes | Configured cap 400, including opening reservations; validate system-index overhead separately within the node's resource budget |
| Connectivity | Private service endpoint; allow only authorized Knowhere services; no public OpenSearch endpoint |
| Remote durability | S3 segments/translogs with request durability and remote cluster state |
| Historical backup | Distinct full-snapshot repository with the accepted hourly/seven-day/minimum-three-complete-successful policy |
| Cost boundary | Reuse the node/EBS/network baseline; S3 is the principal additional cloud-service expense; separately assess paid optional services or capacity increases |

Software selection remains open: pin an official ARM64 OpenSearch release and
image digest; the earlier visibility probe used 3.9.0.

The hardware specification is verified against the
[AWS memory-optimized instance specifications](https://aws.amazon.com/ec2/instance-types/memory-optimized/).
The heap starting point follows the official half-memory recommendation above.
The gp3 starting performance follows the
[EBS volume guide](https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html#gp3-volume-performance).
These facts do not establish Knowhere workload capacity. The 100-GiB data
volume is an accepted validation starting size, not a footprint estimate
derived from the 14.5-GiB PostgreSQL table. Production storage sizing must
cover measured OpenSearch data, overlapping revisions, translogs, merge
workspace, and free-space watermarks.

Read-only inspection confirmed that the existing API and worker tasks already
use private subnets in the same production VPC across `us-east-1a` and
`us-east-1b`. This baseline deploys one node in one availability zone; those
two available subnets do not make it highly available.

The AWS Price List API was queried on 2026-11-01 local time for Linux,
shared-tenancy, on-demand pricing in `us-east-1`:

| Item | Calculation | Monthly planning amount |
| --- | --- | ---: |
| EC2 | $0.2142/hour * 730 hours; SKU `4JC5TDVR4MFWANVE` | $156.37 |
| Data volume | 100 GiB * $0.08/GB-month; gp3 SKU `JG3KUJMBRGHV3N8G` | $8.00 |
| System volume | 20 GiB * $0.08/GB-month | $1.60 |
| Base subtotal | Compute and provisioned gp3 capacity | $165.97 |

See [EC2 on-demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/)
and [EBS pricing](https://aws.amazon.com/ebs/pricing/). This subtotal excludes
snapshots, S3, monitoring/logs, data transfer, taxes, any additional networking,
and any future worker resource expansion. The accepted shared-worker baseline
does not add an always-on indexing service. The subtotal assumes reuse of
existing VPC connectivity and no newly provisioned NAT gateway or load balancer.

No runtime PostgreSQL lexical fallback is added. A single-node outage or
restart makes the OpenSearch lexical path unavailable until recovery. Indexing
work must remain recoverable and parsing results remain available. The three
retained-volume/S3 routes and the hourly/seven-day snapshot policy below are accepted.
The healthy-original-volume and S3 snapshot recovery objectives are accepted
at 15 minutes and one hour respectively. The PostgreSQL-based recovery route
is withdrawn. The native durability combination below is accepted for
validation. Process restart and operator-triggered recovery follow the
accepted automation scope below; implementation and recovery drills remain.

The publication protocol and index-lifecycle design are accepted. Active-index
limits, shared-worker numeric concurrency, and production
acceptance remain open; do not infer a guaranteed index count from heap size.

## Accepted first-version capacity expansion policy

Accepted on 2026-11-03: monitor and alert, then expand capacity manually.
The first version does not automatically resize instances, add nodes, or
increase storage in response to an alert.

Use sustained retrieval-latency or indexing-backlog problems together with
CPU, JVM heap/GC, and disk pressure to diagnose the bottleneck. Choose
numeric resource-warning thresholds from the focused capacity validation;
do not invent a universal utilization limit or treat namespace count alone
as proof that more hardware is needed. The open-index cap is a separate
admission control, not a total namespace limit or capacity guarantee.

Prefer an appropriate adjustment to the single node's compute, memory, or
storage resources after diagnosis. If a single-node configuration cannot
meet the required workload at an acceptable cost, separately evaluate a
multi-node design and its expense. Retest the affected performance and
recovery gates after a material resource change; prior capacity acceptance
does not automatically transfer to a different configuration.

Resource increases remain subject to the agreed cost assessment. This policy
does not provision or authorize a larger instance now, alter client
completion semantics, or add a PostgreSQL fallback.

The official [Nodes Stats API](https://docs.opensearch.org/latest/api-reference/nodes-apis/nodes-stats/)
provides CPU, JVM/GC, filesystem, thread-pool queue/rejection, and related
resource indicators. Knowhere's latency and durable indexing-backlog checks
remain application-level signals, using the accepted Logfire alert path.

## Accepted failure recovery routes

> Execution mechanics below are historical where they require separate indexing
> tasks/queues, automatic retry/replay or background manual retry. Apply the
> [current single-execution amendment](#current-single-execution-amendment);
> preserve the other scope, evidence and product rules in this section.

Status: the retained-volume and S3 snapshot routes remain accepted. The
owner withdrew the previously recorded PostgreSQL full-rebuild route on
2026-11-02. The accepted
validation baseline has one node and no replicas. The snapshot frequency
and retention policy below is accepted, as is the 15-minute internal recovery
objective for the healthy-original-volume route and one-hour objective for
S3 snapshot recovery. The remote data/state and independent full-snapshot
combination is accepted for validation. Recovery automation follows the
accepted scope below. Indexing during outages follows the accepted finite
external-service retry policy.

The accepted recovery routes are:

| Failure | Accepted recovery route |
| --- | --- |
| OpenSearch process or host failure, healthy data volume | Restart the process or replace the instance in the same availability zone, reuse the retained EBS data volume, then verify shard and publication readiness |
| Lost/corrupt data volume, or unavailable original availability zone | Create a replacement node and volume, recover remote cluster state, and restore latest durable index data through remote store; verify this complete path on the selected release |

Independent full snapshots remain the historical recovery layer. Their
restore workflow targets a captured state, distinct from latest-state
remote-store recovery; an arbitrary old snapshot plus live-log replay is
not an accepted recovery procedure.

If the original data and all usable OpenSearch backups are unavailable, the
current design has no accepted recovery path. Do not substitute PostgreSQL
reconstruction or claim a recovery duration for that unresolved case.

The [EBS volume guide](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes.html)
states that volumes persist independently of EC2 instances and can attach
only within the same availability zone. The original data volume must be
preserved on instance termination. Replacement must fence the previous
writer before attaching and mounting the data volume. An EBS volume is the
persistent store for restart recovery; it is not an independent OpenSearch
backup.

Use OpenSearch's supported snapshot API and `repository-s3`, following
[the official snapshot guide](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/).
Do not treat a copy of live node data directories as a consistent backup.
Snapshots are incremental and shards can reflect different times. A
snapshot alone cannot establish that all latest published revisions are
present. Protection of changes after the snapshot is a separate durability
requirement; the temporary PostgreSQL lexical copy cannot satisfy it.

The [Create Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/create-snapshot/)
documents that the default includes open indexes, while closed indexes must
be explicitly selected in `indices`. Backup coverage must enumerate the
expected namespace indexes, including closed indexes, and verify successful
completion and coverage. An accepted request or a partial snapshot does not
prove complete coverage. Confirm this behavior on the pinned release during
the bounded recovery verification.

The [Restore Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/restore-snapshot/)
reopens restored closed indexes. Restore namespaces in bounded batches and
coordinate with the active-index limit, rather than opening the entire
corpus at once.

Recovery verification must account for new revisions, revision switches,
archival/deletion, and outstanding candidate writes. Ordinary application
revision metadata can be checked for consistency, but it is not a lexical
payload backup or an authorization to regenerate index data from PostgreSQL.
An independent OpenSearch durability mechanism must protect any mutations
needed to complete recovery. Do not silently serve an incomplete or stale
published-revision set from an older snapshot.

Online lexical retrieval and its disaster-recovery data remain OpenSearch's
responsibility. Existing parsing results remain available for their original
application purposes. Indexing tasks during a shared outage follow the accepted
finite external-service retry and retry-exhaustion failure contract.

The snapshot schedule and retention policy below is accepted. Recovery time
objectives and automation scope are accepted below; namespace recovery
ordering and implementation remain open. No measured recovery duration or data-loss bound is
established by the machine specification or snapshot schedule alone.

## Accepted snapshot frequency and retention

Status: the owner accepted hourly S3 snapshots, routine seven-day retention,
and a floor of three complete successful snapshots. Backup resources and
jobs have not been created. Implementation remains open; accepted alert
thresholds and the accepted delivery approach are recorded above.

Accepted first-version policy:

- Schedule one snapshot every hour to the S3 repository, using UTC scheduling.
- Retain routine snapshots for seven days. In normal hourly operation this
  provides about 168 scheduled recovery points; successful points can be
  fewer because creation takes time and operations can fail or be skipped.
- Retention cleanup must preserve at least the three most recent complete,
  successful, coverage-verified snapshots, even if they are older than seven
  days because no newer usable snapshots exist. Failed or partial snapshots
  do not satisfy this safety floor.
- Delete expired snapshots through OpenSearch's snapshot API so shared
  snapshot data is preserved correctly. Do not expire repository objects
  directly through S3 object lifecycle rules.

The accepted one-hour interval and seven-day retention are Knowhere planning
choices. The official
[Snapshot Management guide](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-management/)
supports automated creation and deletion schedules, and the
[Snapshot Management API](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/sm-api/)
supports maximum age and minimum/maximum retained counts. The precise
scheduler, coverage checks, and cleanup implementation remain open. Do not
assume `min_count` alone enforces the desired floor of three verified
successful backups; confirm state/count semantics on the pinned release.

The official
[snapshot guide](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/)
explains that snapshots are incremental and share unchanged segment data.
Retaining 168 recovery points does not imply storing 168 complete index
copies. Actual repository size depends on changed/deleted data, segment
merges, and retained snapshots, and must be measured for the backup cost
estimate. Snapshots are not instantaneous or globally consistent point-in-time
copies of application state and OpenSearch together.

The interval primarily bounds intended backup spacing and influences the
amount of post-snapshot state that an independent durability mechanism must
protect. Recovery uses OpenSearch's own durable data and usable backups;
the earlier assumption of PostgreSQL catch-up is withdrawn. One-hour
scheduling alone does not establish an end-to-end data-loss or recovery-time
guarantee. Alerting for failed creation/deletion, incomplete coverage, and an
aging last successful backup follows the accepted operational alert rules
above, with the accepted existing Logfire error-log delivery path.

Additional snapshots before upgrades or destructive index maintenance,
with their own explicit retention rule, remain a recommendation. The owner
has explicitly accepted the hourly/seven-day/three-successful-snapshot policy;
this does not finalize every operational backup trigger.

## Accepted healthy-volume recovery time objective

Status: the owner accepted a 15-minute internal recovery objective for
process or host failure while the original EBS data volume remains healthy.
This is a design requirement, not a measured result or a customer SLA.

The internal objective for this route is **15 minutes**, to be verified in a
bounded recovery drill. It is not an OpenSearch/AWS duration guarantee or
a measured capability of the validation node. The S3 snapshot recovery
objective is accepted at one hour below. The PostgreSQL full-rebuild route
and its proposed duration have been withdrawn.

Accepted measurement boundary for the healthy-volume route:

- Start when the failure disrupts normal lexical retrieval. Count detection,
  notification/response, any human delay, restart or host replacement, volume
  attachment, endpoint reconnection, and readiness verification. Do not
  restart the clock when an operator begins the recovery steps.
- For a controlled drill, use the known fault-injection time. For production
  incidents, retain the last healthy and first failed probe timestamps as an
  interval around the onset, and report the detection/measurement uncertainty.
- Finish when normal Knowhere API requests can again retrieve the required
  published-revision corpus with working lexical search, correct revision
  selection, and hydration. Primary-shard recovery or a running process alone
  does not establish this endpoint.
- Verify serving completeness and consistency with application revision
  metadata, including deletions and revision changes. This check must not
  reconstruct lexical payloads from PostgreSQL. Preserve ordinary cold-index
  opening behavior; restoration does not require every namespace to stay
  open or warm simultaneously.
- Measure the drain time of new ingestion/retry work accumulated during the
  outage separately. That backlog is not silently included in, or omitted
  from, a claimed recovery time for the already published corpus.

The [AWS disaster recovery guidance](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/plan-for-disaster-recovery-dr.html)
says to choose recovery objectives based on business needs and validate the
implementation. The
[OpenSearch Restore Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/restore-snapshot/)
provides completion acknowledgment and recovery-progress monitoring for its
operation. Knowhere's reconciliation and API readiness endpoint are additional
application concerns; the API's completion does not by itself establish
this end-to-end objective.

Recovery follows the accepted automation scope below. Human response,
including off-hours delay, remains part of the accepted 15-minute clock.
Actual response arrangements and recovery drills must support the target
before it can be reported as achieved. A missed drill requires adjusting
the recovery design or explicitly revisiting the target.

## Accepted first-version recovery automation scope

Accepted on 2026-11-03: automatically restart a crashed OpenSearch process.
Host replacement, retained-volume attachment, and recovery from S3 remote
store or independent snapshots use rehearsed scripts triggered by an operator
after an alert. These operations are not automatically triggered by a failed
health probe in the first version.

The recovery scripts must preserve the accepted single-writer boundary,
reuse the selected independently protected OpenSearch data, and automate
post-recovery verification of lexical retrieval, published-revision selection,
namespace coverage, and result hydration before declaring service ready.
Ordinary PostgreSQL application metadata may verify consistency; it is not
a lexical recovery source.

The existing off-node checks and Logfire notification path provide detection
and alerts. The process supervisor, restart limits, script implementation,
and operator response arrangements remain engineering/operational work;
none has been implemented or exercised by accepting this scope.

Keep the 15-minute healthy-volume and one-hour S3 recovery objectives as
internal targets, not customer SLAs. Their clock includes detection and all
human response time, including at night. Operator-triggered scripts do not
establish unattended or round-the-clock achievement of those targets. Verify
the complete response and recovery path in bounded drills, and report misses.
Recovery does not downgrade completed jobs or automatically revive failed
indexing attempts. This scope adds no PostgreSQL fallback or separate
always-on recovery service.

## Accepted S3 snapshot recovery time objective

Status: the owner accepted an internal objective of **one hour** from the
original retrieval-disrupting failure until normal
Knowhere API retrieval is restored for the required published-revision
corpus. This is an accepted design requirement, not a measured capability
or a customer SLA.

Use the same end-to-end clock and correctness boundary as the accepted
healthy-volume objective. Include failure detection and response, new
instance/volume provisioning, service configuration and repository access,
selection and restoration of the protected S3 data/metadata, followed by
correctness and visibility
verification, and endpoint reconnection. If an
attempt to reuse the original volume precedes snapshot recovery, that time
counts from the same original failure; do not restart the clock at snapshot
selection or add a fresh hour after the 15-minute route.

The target assumes the S3 snapshot repository and ordinary application
dependencies remain available. PostgreSQL does not supply lexical recovery
data. If no usable OpenSearch copy remains, the current design has no accepted
recovery path. The one-hour objective remains an internal design target;
the former PostgreSQL catch-up assumption has been withdrawn. The accepted
remote-store mechanism and independent full snapshots must be verified for
their respective latest-state and historical-state recovery scopes before
claiming that the required corpus can be restored completely.

The completion scope is the required published corpus, including cold
namespaces under ordinary on-demand opening. Returning only a first restored
namespace is not completion of this objective. New candidate ingestion and
retry backlog drain remain separately measured. Namespace restoration may
be batched within the active-index limit without requiring all indexes to
stay open simultaneously.

One hour is the accepted internal objective for this deployment, not an estimate
derived from PostgreSQL's table size, the 100-GiB provisioned volume, or gp3's
nominal throughput. Actual OpenSearch footprint, usable snapshot age,
restore throughput, revision churn, and protected replay work must be covered
by a representative bounded drill before capacity acceptance. The official
Restore Snapshot API supports completion acknowledgment and monitoring; the
additional Knowhere steps above require application implementation.

## Withdrawn PostgreSQL disaster-recovery path

The owner clarified on 2026-11-02 that PostgreSQL's old lexical data is kept
temporarily to evaluate OpenSearch and permit migration rollback. Its writes
will stop and the related data will be cleaned up after OpenSearch is stable.
It must not become a permanent OpenSearch backup, disaster-recovery source,
or post-snapshot lexical replay source.

The proposed four-hour PostgreSQL full-rebuild recovery path is withdrawn.
It described disaster recovery after loss of OpenSearch data and usable
backups, not initial migration. Initial migration from existing PostgreSQL
lexical data remains a separate, temporary backfill operation with its own
rate limit; it does not establish a perpetual recovery dependency.

The owner subsequently accepted remote segments/translogs with request
durability, remote cluster state, and independent full snapshots for
validation. Those mechanisms protect OpenSearch data without PostgreSQL
reconstruction. The hourly/seven-day policy and 15-minute/one-hour internal
targets remain recorded; complete recovery coverage and duration still need
verification. No broad guarantee covering every logical error or loss of
all independent copies has been accepted.

If all independently protected OpenSearch data is unusable, recovery is
unresolved in the current design. Do not assign a rebuild duration or
silently assume that PostgreSQL can supply the missing data.

## Accepted OpenSearch durability validation baseline

Status: the owner accepted this combination for first-version validation on
2026-11-02, under the cost boundary below. This is not a production capacity
or recovery result; no infrastructure or paid validation has been run.

| Component | Accepted validation requirement |
| --- | --- |
| Remote data | Configure S3-backed segments and translogs from cluster creation, with segment replication and explicit request durability |
| Write acknowledgment | Set `index.translog.durability=request`; confirm each write item and search visibility before activating a revision and reporting overall job completion |
| Remote metadata | Enable remote cluster state, including `cluster.remote_store.state.enabled=true`, so replacing the only lost node does not depend on its local metadata |
| Historical backup | Use a distinct full-snapshot repository, with `remote_store_index_shallow_copy=false` and `shallow_snapshot_v2=false`; retain the accepted hourly/seven-day/minimum-three-complete-successful policy |
| Recovery source | Use OpenSearch's protected data and metadata; no PostgreSQL lexical backup, replay, or regeneration dependency |
| Cost boundary | Retain the accepted node, heap, EBS and private-network baseline; reuse the free regional S3 Gateway path, with S3 as the principal additional cloud-service expense |

Engineering/operations time remains part of the implementation cost. Optional
paid monitoring, SSE-KMS, extra network services, or required resource expansion
must be assessed separately rather than silently folded into this choice.
SSE-S3 provides the no-extra-encryption-fee cost basis. The actual S3 usage,
write-path delay, and mixed-load capacity remain unmeasured; no numeric
monthly-cost ceiling has been accepted. This design decision does not itself
instruct resource provisioning or application implementation.

The [durability research](../validation/opensearch-durability-2026-11-02.md)
records official semantics and pinned 3.9.0 source evidence. The
[cost assessment](../validation/opensearch-durability-cost-2026-11-02.md)
records verified unit prices, existing-network observations, and illustrative
scenarios, not a measured monthly bill. Exact release/image selection remains
open; research on 3.9.0 is not its production selection.

Remote-store recovery targets the latest durable state, including the
last acknowledged document operations under request durability while the
remote data remains intact. Independent full snapshots target historical
states for logical rollback. Do not assume an arbitrary old snapshot plus
live-translog replay is a proven recovery procedure. Protection of live data
and historical recovery are complementary, not interchangeable.

Validate single-node and whole-disk loss, remote-state bootstrap, restored
namespace coverage, cold-index behavior, independent full-snapshot recovery,
and remote upload failures. This official mechanism does not establish the
accepted 15-minute/one-hour end-to-end recovery durations or uninterrupted
availability of the single-node service.

The owner selected single-execution indexing and unified billing
below. Indexing-only retry is free after successful parsing in every mode.

## Accepted external indexing failure and billing policy

Status: unified by the owner on 2026-11-02 in
[ADR 0029](../adr/0029-keep-parsing-charges-and-allow-free-indexing-retries.md).
This supersedes the preceding mode-dependent refund rule and its proposed
extra billing-mode parameter.

Knowhere charges for parsing. Once parsing succeeds, indexing failure does
not refund that parsing charge, regardless of whether a client waits for
combined completion or receives the two milestones separately. Support free
indexing-only retry of the same parsed revision, using its retained result
without reparsing or another parsing charge.

Current owner amendment: execute indexing once synchronously at the end of the
parse task, after committing readable parsed state and dispatching subscribed
`job.parsed`. Any unsuccessful execution marks indexing failed and emits
subscribed `job.failed` with indexing context. The parse task then returns
normally without parser autoretry/refund hooks. A search-ready wait raises;
a parsed-only wait succeeds against the retained result even after failure.

Explicit retry also executes once synchronously inside its HTTP request, on the
same job/revision for free. No automatic replay, authoritative-indexing task,
new job or detached primary continuation is selected. Both OpenSearch-primary
paths have a fixed configurable ten-second deadline, beginning before connection/opening and
covering preparation, writing, visibility and guarded publication. Heartbeat/
progress cannot extend it. Verify actual task/request release, bound failure
settlement overhead, and settle interrupted work without restarting indexing.
Resolve committed completion and preserve unresolved native-operation fences.
This replaces the earlier three-round budget and 30-minute execution ceiling;
the separate SDK waiting timeout remains unchanged.

See [ADR 0026](../adr/0026-publish-search-readiness-from-background-tasks.md)
and [current execution acceptance](opensearch-development-plan.md#93-finite-failures-and-review-gaps).
The selected deadline is a limit, not measured ten-second success for every
input. Normal-load timeout failures remain part of performance acceptance.

Parsing failure keeps its existing normal refund behavior. SDK waiting
options and webhook subscriptions affect result delivery, not billing.
There is no separate completion-contract billing flag, combined-mode refund,
post-refund recharging question, or mode-specific retry price to design.
The earlier global dispatch-pause proposal is also not selected.

Search readiness and overall completion still require successful complete
indexing, visibility, revision activation, and cache coherence. A free retry
alone does not make a revision ready. No retrieval-query charge is introduced.

### Implementation boundary inspected

Current parsing work is charged by `WorkBillingService.charge_for_pages`.
`SyncJobFailureFinalizer` accepts a `should_refund` flag, while the current
`DocumentIngestionBaseTask.on_failure` passes `True`. The planned indexing
failure path must preserve the parsing charge uniformly rather than reuse
that parsing-failure refund choice unconditionally. The new integration is
not implemented yet; existing parsing-failure refund behavior remains the
baseline.

The current inline/manual execution boundary replaces the earlier shared-worker
task/queue handoff; see the single-execution amendment.
Retry identity and coalescing of active calls are accepted above. Remaining
work includes implementing and validating the publication protocol and
selected index lifecycle limits.
Detailed request/response properties remain open; phase fields and enum values
are accepted below, and no new billing-mode branch is needed.

## Existing behavior inspected

- The job success finalizer publishes the result before marking the job
  completed and creating the `job.completed` webhook event.
- Job status responses expose `done` and an immutable `job_result_id`, but no
  explicit per-revision search-readiness field.
- The existing retrieval readiness signal is a short-lived namespace-level
  Redis cache populated by retrieval. It does not establish the proposed
  per-revision client contract.

## Accepted completion semantics

- Parsing completion means the revision's durable parsing result is available
  for display or download, even while indexing is pending.
- Search readiness means the processed revision is available for retrieval.
- `done` and `job.completed` require both parsing completion and search
  readiness.
- Expose the accepted `parsing_status` and `indexing_status` fields for the
  relevant job revision. The parsing-completion event is `job.parsed`.

## Client usage contract

- Clients that only display parsing results can proceed at parsing completion.
- Clients that require immediate retrieval should wait for overall completion
  or confirm that the processed revision's retrieval state is ready.
- Overall completion (`done` and `job.completed`) always requires search
  readiness; parsing completion alone cannot produce overall completion.
- A default namespace query covers published revisions. A newly added document
  without a published revision is outside that scope until activated. Do not
  add pending-document notices, counts, or partial-result warnings to normal
  retrieval responses.
- Document filters narrow the same published-revision scope. A document
  without a published revision contributes no retrieval candidates; an empty
  eligible scope produces the ordinary empty result.

Phase-state fields, enum values, and milestone event names are accepted below.
These are requirements for the planned API, not claims about current behavior.

## Accepted retrieval scope and readiness observation

Status: clarified by the owner on 2026-11-02 for the planned migration;
not implemented.

Keep the existing retrieval query API and its namespace/document filters.
Retrieval searches published revisions selected by the service. A newly
parsed document without a published revision is outside that scope until
activation. It contributes no candidates, and an empty eligible scope
returns the ordinary empty result. While a replacement is pending or has
failed indexing, the previous published OpenSearch revision remains selected.

`include_document_ids` is a document allowlist over this same scope. It
does not require the newest uploaded revision, wait for indexing, or cause
a special readiness error. Apply published-revision scope consistently to
retrieval channels and shortcuts. Existing backend-failure handling remains
in force; publication-state exclusion does not redefine service failures.

Clients observe parsing/indexing progress through job state and webhook
events. The default SDK `parse()` and `jobs.wait()` target search readiness;
callers selecting the parsing-only target can later wait on the same job.
Retrieval does not become a processing-status endpoint or carry pending
document notices. Returned search references retain their selected revision
for content and citation reads under the accepted read contract.

The proposed explicit-version search branch and its `REVISION_NOT_READY`
HTTP 409 response are withdrawn. Do not add a public search revision
selector or a not-ready error as part of this migration. This supersedes
earlier wording about requests explicitly requiring an unready revision.

## Accepted SDK waiting behavior

Status: accepted for the planned migration; option names and values accepted
on 2026-11-02. Not implemented.

- The default `parse()` call returns after overall search-ready completion.
- An explicit parsing-only waiting target returns the persisted, usable
  parsing result as soon as parsing completes, while background indexing
  continues independently.
- Early return does not change overall job state to completed and does not
  generate `job.completed` before search readiness.
- The SDK waiting target controls function return, while `webhook.events`
  controls notification selection. Neither implicitly changes the other.
- Node uses `waitFor`; Python uses `wait_for`. Both accept the same values:
  `"search_ready"` (default) and `"parsed"`.
- The option is supported by both `parse()` and `jobs.wait()`. Selecting
  `"parsed"` waits for the immutable, usable parsing result; selecting
  `"search_ready"` waits for completed indexing and publication. Omission
  preserves the default wait for overall completion.
- This is an option for the current SDK wait, not a persisted job mode.
  Clients can first wait for parsing and later wait for search readiness
  on the same job. It does not change background indexing, billing, webhook
  subscriptions, or the meaning of the returned overall job status.
- Each wait invocation keeps its own accepted configurable timeout budget;
  parsing completion does not restart that budget. The parsing-only target
  succeeds even if indexing has already failed when the client observes
  completed parsing. Do not change overall `isDone` / `isFailed` semantics
  or require overall success to load a completed parsing result.
- Exact result/exception properties remain to be designed; the waiting
  parameter names, values, defaults, and supported entry points are accepted.

## Accepted SDK exception behavior for indexing failure

Status: accepted on 2026-11-02 for the planned migration; not implemented.

When a default `parse()` call waits for overall search readiness and indexing
ultimately fails, raise the SDK's job-failure exception. Its context must
identify `failure_stage=indexing`, retain the original `job_id`, and expose
the successful parsing result through the ordinary SDK result-loading path.
Bind that result to the failed job's immutable parsed revision; do not load
a different result from the document's latest revision instead.

Keeping result access means more than exposing a result URL that the SDK
then refuses to load because the overall job failed. Adapt the result-loading
eligibility check to recognize completed parsing independently of overall
job success. The exact exception property names and helper signatures remain
open. Reuse existing job-response context where practical rather than
requiring callers to reconstruct revision parameters or upload the file again.

The default wait still fails because the requested search-readiness target
was not achieved. Retaining a usable parsing result does not fabricate overall
success. The accepted parsing-only waiting target remains separate: successful
parsing satisfies that target even if indexing has already failed when the
client first observes the job. Eligible indexing-only retry uses the original
job and retained result for free; successful parsing remains charged under
ADR 0029.

Planned SDK/API acceptance cases include default-wait failure with stage and
job context, successful loading of the retained parsing result from that
exception context, parsing-only first observation after indexing failure,
and a subsequent eligible indexing-only retry without reparsing or another
charge. These checks have not been run against an implemented integration.

## Accepted SDK waiting-timeout behavior

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Preserve the existing configurable SDK polling timeout, with one budget per
wait invocation rather than resetting it when parsing completes and indexing
continues. Read-only local Node and Python SDK sources both default to
30 minutes. This is a client waiting budget, not a server processing deadline
or search-readiness guarantee.

On wait expiry, raise a distinct waiting-timeout exception carrying the
original `job_id` and the latest known job state when available. That snapshot
is an observation, not a claim about the job's state at exception delivery.
Keep background processing active; expiry does not fail or implicitly cancel
the server job. Callers can query the original job or resume waiting for it,
without another upload or automatic parsing resubmission. Resuming a wait
does not reset the server's indexing attempt or selected-backend execution deadline.

Planned checks cover timeout while parsing, timeout while indexing, a job
finishing just after the last observed state, and resumed waiting on the
same job after timeout. Verify no duplicate parsing or indexing submission
is caused by a client wait expiring. The stage-aware timeout integration
has not been implemented or validated.

## Accepted phase-state contract

Status: fields, enum values, and transition invariants accepted on 2026-11-02
after the client-perspective review; not implemented. Existing accepted
lifecycle, SDK waiting/exception, billing, and subscription decisions apply.
Explicitly open transport or payload details in the scenario review remain
open; this acceptance does not imply completed integration tests.

Preserve the existing overall `status` vocabulary. Add separate,
job-revision-scoped fields with these exact wire names and values:

| Field | Allowed values |
| --- | --- |
| `parsing_status` | `pending`, `running`, `completed`, `failed` |
| `indexing_status` | `not_started`, `pending`, `running`, `completed`, `failed` |

Use those field names in API and webhook payloads. SDK projections follow
their existing language conventions: Node exposes `parsingStatus` and
`indexingStatus`; Python retains the wire names. Values and semantics match.

The lifecycle combinations are:

| Situation | Overall `status` | `parsing_status` | `indexing_status` |
| --- | --- | --- | --- |
| Waiting for parsing or upload | `pending` / `waiting-file` | `pending` | `not_started` |
| Parsing or converting | `running` / `converting` | `running` | `not_started` |
| Parsed, inline indexing not yet started | `running` | `completed` | `pending` |
| Explicit indexing retry admitted within its HTTP request | `pending` | `completed` | `pending` |
| Indexing, visibility wait, or publication activation | `running` | `completed` | `running` |
| Published and search-ready at completion | `done` | `completed` | `completed` |
| Parsing failed | `failed` | `failed` | `not_started` |
| Single indexing execution failed | `failed` | `completed` | `failed` |

Once parsing completes for this job revision, `parsing_status` remains
`completed` through indexing failure and explicit indexing-only retry.
Indexing completion includes full publication readiness: successful required
writes, visibility acknowledgment, revision activation, and cache coherence.
A successful bulk HTTP response alone cannot complete that phase. There is
no automatic indexing retry or retry-backoff state. An eligible explicit retry restores active indexing on
the original job while preserving parsing completion. Current failure fields
must describe the current execution; historical errors stay in attempt/event
history rather than appearing as a current failure after retry admission.

Explicit retry may transition through pending/running internally, but its
HTTP request waits for final success/error. It does not return 202 queued work
or reset parsing. In-progress indexing remains nonterminal for a search-ready
wait; completed parsing continues to satisfy a parsed-only wait.

Overall `done` requires both phase fields to be `completed` and is
irreversible. Later index closure, a transient service outage, or replacement
of the published revision does not undo this historical job outcome or
downgrade its completed phases. Current retrieval availability is reported
by the retrieval operation rather than rewriting successful processing history.

This overall success differs from job termination (`done` or `failed`),
client wait satisfaction and Celery task completion. None of the latter
redefines the phase fields or sweep predicates; see the
[current completion clarification](#current-completion-meaning-clarification).

### Client scenario review

Use these scenarios for integration coverage, together with the already
accepted behavior contracts. Explicitly noted open API details remain open:

| Client situation | Required or recommended behavior |
| --- | --- |
| Default SDK parsing call, both phases succeed | Return the usable parsing result only after overall `done`; ordinary callers do not coordinate two stages. |
| Parsing-only SDK wait, indexing still pending | Return the durable parsing result as soon as parsing is completed, without changing overall job status. |
| Parsing-only wait first observes indexing failure | Parsing completion satisfies this wait target. Return its result even when overall `status=failed`; do not throw an indexing error for a satisfied parsing-only wait. |
| Default wait observes indexing failure | Fail the overall wait, identify `failure_stage=indexing`, and retain access to the successful parsing result and original `job_id`. Carry this context in the SDK exception instead of forcing another upload. Exact convenience methods remain open. |
| Parsing fails | Fail either waiting target, with no completed parsing result claimed. Preserve existing parsing-failure billing behavior. |
| Polling times out, connection drops, or the caller stops waiting | Do not mark the server job failed or cancel its background work implicitly. Retain the `job_id` so callers can query or resume waiting. A create request whose response was lost is a separate request-idempotency question. |
| Indexing execution fails | Fail indexing immediately after outcome settlement; no automatic retry. Search-ready waiting raises; parsed-only waiting retains success. |
| Client explicitly retries failed indexing | Run once synchronously in its HTTP request on the original job/revision for free. Return final success/error; active conflict is 409, historical done is 200, superseded candidate is rejected. |
| Replacement is parsed but not published | Default content reads show the latest parsed revision; search keeps the previous published revision; search references open that same previous revision. A failed replacement does not hide the published document. |
| A new document has no published revision yet | Its parse result can be readable, but retrieval omits it without a pending notice. Document allowlists use the same published scope; an empty eligible scope returns the ordinary empty result. |
| Another revision is accepted after a parsing result was returned | The SDK result remains bound to its original job/revision. Reading the document afresh may legitimately return newer parsed content; do not silently substitute it when loading the original result. |
| Webhooks arrive twice or out of order, including after explicit retry | Preserve event-time snapshots, event IDs, and timestamps. Treat each event as a milestone, not a replacement for all current job state. Query authoritative job state when timing is ambiguous; completion cannot regress. |
| Client explicitly subscribes only to parsing completion | Send only that subscribed event. Callers wanting failure notifications also subscribe to `job.failed`; `failure_stage` distinguishes parsing from indexing failure. Subscriptions do not change the SDK waiting target. |
| A completed job's index is later closed, the service is unavailable, or a later revision replaces it | Job completion remains a historical successful outcome. Opening an index or a transient query failure must not turn that completed job into failed. Report current request availability through the retrieval operation. |

For a parsing-only wait, check parsing completion before treating an overall
indexing failure as an error. The result loader must use the same parsing
completion rule. Do not redefine existing overall `isDone` / `isFailed`
helpers according to the caller's wait target, or fabricate `status=done`
on an early returned result.

Compatibility findings from read-only local source inspection:

- Node `src/lib/polling.ts` and Python `src/knowhere/lib/polling.py` currently
  finish on overall done/failed. The new parsing-only target needs explicit
  handling; adding response fields alone does not implement it.
- Node `src/resources/jobs.ts` currently rejects `load()` unless the job is
  overall done. That would block a usable parsing result while indexing is
  pending or failed and must change for the accepted contract.
- Node `JobFailedError` already retains the job response, providing a basis
  for stage-aware errors. Its polling timeout currently carries elapsed time
  without the job identifier; the convenience workflow needs resumable context.
- The existing webhook outbox uses `status=completed` on `job.completed`,
  whereas job-query responses use `status=done`. Preserve existing terminal
  payload vocabulary and add the new phase fields consistently rather than
  renaming an old webhook value as part of this migration. The exact new
  parsing-completion event payload remains to be designed.
- API `result_projection._resolve_credits_spent()` currently reports zero for
  every failed job. The future response must follow actual billing state so
  a charged parsing success with indexing failure does not falsely look free
  or refunded. SDK wait choice does not alter billing.

These are source-inspection findings and planned integration acceptance
cases, not passing SDK/API tests or a compatibility guarantee for all released
clients. Keep parsing progress separate from overall completion: a displayed
100 percent parsing progress can coexist with indexing still in progress.

## Webhook investigation and accepted subscription model

Status: the single-URL subscription shape and omitted-list compatibility are
accepted in ADR 0028, together with the unified failure event below. The
parsing-completion event name `job.parsed` was accepted on 2026-11-02.
The `parsing_status` and `indexing_status` fields and enum values are now
accepted in the phase-state contract above; implementation is not present.

Current code findings:

- API `WebhookConfig` contains only `url`. The current default-branch
  [Node SDK configuration](https://github.com/Ontos-AI/knowhere-node-sdk/blob/32b772af50332d384118a676e1eac9938231c824/src/types/params.ts)
  and [Python SDK configuration](https://github.com/Ontos-AI/knowhere-python-sdk/blob/3181528efb35b1b0637925b3a89e8083ecad9c40/src/knowhere/types/params.py)
  also contain only a webhook URL; neither defines an event-subscription list.
- Neither inspected SDK has a general webhook HTTP receiver or runtime
  callback-status validator. Customer callback endpoints process the inbound
  JSON. The Node SDK declares a static directory-ready event type; that type
  is not a runtime webhook receiver.
- Both SDKs' `parse()` methods poll job status to overall completion,
  independently of webhook delivery. Subscribing to an earlier event would
  not itself make `parse()` return earlier.
- Node `jobs.load()` rejects a job result unless it is done. Python
  `jobs.load()` can load a result URL without that terminal-state check.
  Early result delivery therefore requires checking the API and each SDK's
  result-access path, not just adding a notification.
- The current job webhook outbox maps `job.completed` to `status=completed`
  and every other supplied event type to `status=failed`. New phase events
  need explicit payload construction, not just a new subscription field.
- Current result-link enrichment runs only for `job.completed`; parsing-
  completion delivery must independently make the correct revision's result
  available.
- Outbound events already have independent IDs, an
  `X-Knowhere-Event-ID` header, per-event delivery state, and per-event QStash
  deduplication. Multiple milestone events for one job must remain distinct.

Accepted first-version shape: preserve the existing `webhook.url` and add an
optional `events` list. A single callback URL can receive several selected
event types and route them by the payload's `event` field. The accepted name
for parsing completion is `job.parsed`; `job.completed` retains its current
overall-completion meaning.

Accepted milestone event names on 2026-11-02:

| Event | Meaning |
| --- | --- |
| `job.parsed` | The job's immutable parsing result is durable and usable for reading or download; indexing/publication may still be pending. |
| `job.completed` | Parsing and indexing/publication completed, and the processed revision reached search readiness. |
| `job.failed` | The current execution failed; `failure_stage` identifies parsing or indexing. |

Create the parsing-completion event at the durable, usable-result milestone,
not merely when the parser stops running. Its result access must bind to
that job's parsed revision. It does not mark overall job success. If both
success milestones are subscribed, retain a distinct logical event for each
even when indexing finishes before notification delivery. Delivery may be
duplicated or reordered under the existing event-ID/timestamp contract.
An indexing-only retry does not create another `job.parsed` event because
it does not reparse the document; an existing event can still be redelivered.

```json
{
  "webhook": {
    "url": "https://client.example/knowhere",
    "events": ["job.parsed", "job.completed"]
  }
}
```

Accepted subscription semantics:

- Omitted `events` preserves the existing terminal-notification behavior:
  `job.completed` and `job.failed`, without automatically adding phase events.
- An explicit list selects exactly those event types. Selecting only parsing
  completion produces only that callback; selecting both milestones produces
  a distinct event for each milestone at the same URL.
- Event-specific URL mappings are not the first-version configuration.
  Different selected events can share one callback address.

### Accepted events validation

Status: accepted on 2026-11-02 for the planned migration; not implemented.

Apply these rules to `webhook.events` consistently across the jobs API and
SDK request models:

- Omitting `events` from a configured webhook preserves subscriptions to
  `job.completed` and `job.failed`; it does not add `job.parsed`.
- An explicitly supplied value must be a nonempty list containing only
  `job.parsed`, `job.completed`, and `job.failed`. Names match exactly;
  unknown names are not silently ignored or converted to defaults.
- Reject an empty list, a non-list value, or any unknown name through the
  existing invalid-argument response. A mixed list of known and unknown
  names is invalid as a whole. Identify `webhook.events` in field violations.
- Validate before job creation and dispatch or parsing charges. The server
  remains authoritative for requests from raw HTTP clients as well as SDKs.
- Deduplicate repeated names so each event type is selected once. This is
  subscription normalization, not a promise of exactly-once or ordered
  webhook delivery. Existing event IDs and redelivery behavior still apply.

Omitting webhook configuration entirely continues to mean no callback is
configured; event defaults do not create a callback URL. An explicit list
selects exactly those events and does not implicitly include failures.

Planned request checks cover omission, each allowed event, combinations,
duplicate names, an empty list, non-list values, unknown names, and a mixed
known/unknown list. Assert invalid configuration does not create a job or
start parsing. These are planned acceptance cases, not completed tests.

### Accepted failure-event contract

- Use `job.failed` for parsing failures and indexing failures. Do not add
  separate failure event types for the two phases.
- Include `failure_stage` with the value `parsing` or `indexing` and both
  parsing and retrieval phase states for the affected revision.
- An indexing failure leaves parsing successful and the parsing result
  available. A client can use these states to distinguish usable parsing
  output from a failed retrieval-publication attempt.
- Failure events follow the accepted subscription filter. An explicit list
  that omits `job.failed` does not implicitly subscribe to failures. The
  omitted-list legacy behavior continues to include `job.failed`.
- Use the accepted `parsing_status` and `indexing_status` fields and enum
  values, alongside `failure_stage` and its two accepted values.

Payload and delivery recommendations still being refined:

- Identify a milestone by `event`, independently of overall job status and
  the two phase states. A parsing-completion event must not label the overall
  job completed before search readiness.
- Keep each event's ID stable across delivery retries. Receivers deduplicate
  by event ID rather than job ID and allow duplicate or reordered delivery.
- Multi-destination subscriptions can be considered later if different events
  need different consumers.

Phase-state fields and values, event-list validation, event names, and SDK
waiting-option names, values, and
entry points are accepted above; detailed result/exception properties remain
open.

## Official evidence and its limits

- The [Refresh Index API documentation](https://docs.opensearch.org/latest/api-reference/index-apis/refresh/)
  explicitly recommends `refresh=wait_for` for workflows that index and then
  immediately search. It waits for visibility through a refresh before the
  indexing operation returns. A direct Refresh Index API call is synchronous
  across its targeted shards.
- The [Bulk API documentation](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/)
  states that bulk items are processed independently. Check each item's
  success; an HTTP success alone does not establish a complete revision.
  Its `timeout` parameter covers particular waits such as active shards, not
  a general end-to-end publication deadline.
- The [index settings documentation](https://docs.opensearch.org/latest/install-and-configure/configuring-opensearch/index-settings/)
  explains that explicitly setting `index.refresh_interval` to `1s` keeps
  refresh scheduling independent of search traffic. Leaving it implicit
  permits idle shards to stop background refreshes. The interval is a schedule,
  not a wall-clock guarantee covering queueing, recovery, indexing, and retries.
- These official mechanisms support an acknowledged visibility contract for
  successful writes. The reviewed documentation does not provide a thirty-
  second end-to-end SLA for Knowhere on a self-hosted single node.

## Accepted background execution

This heading is retained for existing links. The current owner amendment
runs initial indexing once in the existing parse task after parsing commits;
it does not create a separate indexing task. Dispatch subscribed `job.parsed`
before native work without waiting for delivery. Confirm every write and search
visibility outside PG transactions, then atomically publish and emit completion.

Manual retry executes synchronously in its HTTP request, not background work.
Both paths use the fixed configurable ten-second limit with OpenSearch as
primary; PG-primary work uses its separately qualified longer budget.
Webhook delivery, maintenance and temporary PG-copy work keep their separate
background policies; they do not authorize automatic indexing retry. See
[current execution amendment](#current-single-execution-amendment).

## Accepted failure and recovery behavior

- One failed/interrupted execution ends indexing as failed, with subscribed
  `job.failed`, `failure_stage=indexing` and completed parsing preserved.
- Preserve parsed result/charge in all waiting modes. Search-ready waiting
  raises; parsed-only wait/load succeeds. Existing parsing-failure billing
  remains unchanged.
- The parse task ends normally after recording indexing failure; it does not
  invoke parser retry/refund hooks, retry sleep or another indexing task.
- Only an eligible explicit client request starts another execution, once,
  synchronously in its HTTP request under the same job/revision. No new parse
  event or charge; retry outcomes reuse subscribed completion/failure events.
- Extend the existing 30-minute stale-job sweep for timeout/failure settlement
  after a worker/API disappears, without restarting work. Use indexing's own
  deadline/unstarted threshold, preserve parsing/result/charge, and protect
  newer attempts, committed completion and native uncertainty. Ordinary errors
  remain immediate; manual retry checks expired state without waiting for a sweep.
- Formal integration and timeout/fault acceptance remain open in KW-02/03/09.

## Readiness gate

For the complete processed revision, confirm every indexing item succeeded,
wait for visibility using the documented refresh mechanism, and complete
Knowhere's revision activation and cache-coherence steps before terminal job
success. Verify this integration with targeted correctness cases; do not use
elapsed time or a namespace-level cached signal as proof of readiness.

The publication protocol is accepted above but still requires implementation
and fault validation. OpenSearch's visibility contract alone does not
establish Knowhere's cross-system revision correctness or ongoing node
availability.

## Accepted revision switching

- A stable document identity can have a published revision and a candidate
  revision produced by replacement-file ingestion or another parse.
- Default retrieval continues to select the published revision from
  OpenSearch while the candidate is indexing or has failed indexing.
- Newly added documents without a published revision do not block default
  retrieval of other published revisions. Default responses do not carry
  pending-document notices; clients use the phase states and overall
  completion contract described above.
- Document allowlists use the same published-revision scope; no public
  explicit-version search mode or special not-ready response is added.
- Activate the replacement only after complete indexing, acknowledged search
  visibility, and coherent revision selection, result hydration, and cache
  handling. Persisting the candidate's parsing result alone does not switch
  default retrieval.
- Clean obsolete lexical revision records after activation and after queries
  using them have completed. Already started queries retain their selected
  revisions; subsequent queries select the new publication. Retain parsing
  results and reference data under existing retention rules. The exact
  request-lifetime and cleanup mechanisms remain to be designed.
- Only one job for a document is active through parsing and indexing. A
  competing update receives HTTP 409 with the active job identified; parsing
  completion alone does not release this restriction. See the accepted
  document-admission policy above.

This preserves continuity only while OpenSearch can serve the published
revision. It does not introduce a PostgreSQL lexical fallback or protect
against an OpenSearch node outage.

## Visibility verification

The official-guarantee question is resolved: visibility acknowledgment is
sufficient, and thirty seconds is not a hard deadline.

On 2026-11-01, a real, isolated, local OpenSearch 3.9.0 single node passed six
synthetic protocol cases. An intentionally unsafe negative control failed:
188 acknowledged writes were not yet visible to search. See the
[verification report](../validation/opensearch-visibility-2026-11-01.md).

This accepts the OpenSearch visibility mechanism for the design. It does not
accept Knowhere's not-yet-implemented phase-state, notification, or publication
integration, or establish production throughput and latency.

## Follow-up decisions

Lifecycle, mixed-load targets, overload policy, infrastructure validation
baseline, and the phase-state contract have been agreed for the planned
migration. Online retrieval priority and the 50-QPS peak target are accepted.
The ten-times real production burst profile, daily indexing multiplier,
separate migration/rebuild rate limits, and infrastructure validation
baseline, retained-volume/S3 snapshot routes, and the hourly/seven-day/
three-successful-snapshot policy remain accepted, as do the 15-minute
healthy-volume and one-hour S3 snapshot objectives. The PostgreSQL recovery
path and proposed four-hour objective are withdrawn. The remote data/state
and independent full-snapshot combination is accepted for validation under
the stated cost boundary. Shared dependency-outage waiting and retry-budget
handling follow the accepted external-service policy. Unified billing keeps
successful parsing charged and permits free indexing-only retry in every
mode. No billing-mode parameter or post-refund retry branch is needed. Initial
indexing runs once inside the parse task after the parsed commit/event dispatch;
manual retry runs once inside its HTTP request. OpenSearch-primary work has
a fixed configurable ten-second limit; PG-primary work has a separately
qualified longer budget while continuing new-document admission and requiring
PG readiness. The temporary PG shadow copy uses a non-gating background thread.
Neither primary path permits automatic replay or an indexing task. The existing
30-minute stale-job sweep settles interrupted indexing without re-execution.
The single-active-job rule covers
parsing and indexing for each document. A later accepted update prevents
retry or activation of an older failed candidate. Explicit retries reuse the
original job and parsed revision with a new indexing attempt; calls during
active indexing or after readiness do not start another attempt. Notifications
use occurrence timestamps and existing event IDs; completion is irreversible,
and no client-facing status-version counter or delivery-order guarantee is
added. Default document-content reads select the latest parsed revision;
retrieval references remain bound to the retrieved published revision, with
ordinary version handling owned by the service and SDK. The
[engineering reference](opensearch-engineering-v1.md) now selects exact retry
responses, fencing, reader lifetime, and coordination. Their implementation
and fault validation,
validation of the configured 400-open-namespace cap, implementation of the
five-second admission-wait budget, proactive LRU production-watermark
qualification and turnover implementation,
execution of the accepted business-quality gate, and translation into internal lexical
work remain open.
SDK waiting targets and the configurable timeout/resumption behavior are
accepted above. Retrieval uses published scope, and readiness is observed
through jobs rather than a new explicit-version search/error contract.
Search readiness
must not be represented as a guarantee of future node availability or of a
relevant hit for every query.
