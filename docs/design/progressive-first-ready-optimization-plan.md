# Progressive first-ready optimization plan

Status: PG four-index delivery selected; implementation and local verification
complete, release and production acceptance pending. Updated 2026-10-29
America/Los_Angeles. The complete-dump Oracle comparisons and the recorded
production / rollback compatibility audit are complete. No production index,
deployment, database capacity policy, or business concurrency was changed.
Namespace-growth and compact-storage research is now a reduced screening PoC.
The user replaced the remaining 210+108 full matrix on 2026-10-29. Preserve
completed evidence and finish the already-started base0/S3 arm, then run at most
20 new screening publications. The completed comparisons above do not include
that new screening.

## Objective and accepted boundaries

Reduce the time between accepting a supported progressive document and making
its first snapshot readable and retrievable in Hipo. Preserve the baseline's
current text, original-page images, source PDF, page references, and retrieval
coverage. Keep the existing two Workers and all concurrency settings unchanged.
The user reconfirmed on 2026-10-28 that first-snapshot page-image generation must
remain enabled; neither removing it nor rendering images on demand is in scope.

The user accepted the following Hipo boundary on 2026-10-27: publish a readable
snapshot after its metadata, text, and original PDF are available and its images
can be read from that same immutable snapshot. Remaining image copies continue
in the background. This decision is also recorded in Hipo ADR 0053.

On 2026-10-29 the user accepted the current main orchestration: publish the
baseline, build coarse structure automatically, and run Fine only for scopes
whose coarse pages are actually read. Publish each completed scope. This
supersedes the earlier requirement to stage all Fine output and publish once
after automatic whole-document enhancement. On-demand Fine is separate from
on-demand image generation; eager baseline page images remain required.

Baseline completion still emits `job.completed`. Final packaging and
`job.enrichment.completed` remain conditional on all scopes becoming refined,
structure/vector repair completing, and the final package upload succeeding.
Unread scopes can remain coarse indefinitely. Preserve source-page citation
identity and the failure-only enhancement retry policy; do not invent a
whole-document enhancement action from the existing retry endpoint.

Excluded: on-demand image generation, archive-format/cache optimization,
historical-data cleanup, tokenizer or BM25 changes, new public ingestion
parameters, Worker scheduling changes, and concurrent-transfer tuning.

## Selected delivery and completion status

The user selected the existing PostgreSQL four-index solution on 2026-10-28.
Remove only `idx_document_map_unit_tokens_lookup` through the guarded maintenance
operator. Retain the primary key, binary token lookup, unit index and unit-leading
covering lookup, text unit IDs, current token rows, batching and readers.

Do not implement the S3 lexical-index candidate in this delivery. Compact-PG
frequency payloads, native C serving, Redis caching, BIGINT keys, new search
services and database replacement are also deferred. Existing S3 document
snapshots, page images and source PDFs continue to serve their existing purpose;
deferring the lexical-storage experiment does not remove those assets.

Keep both Workers, their concurrency settings, transfer admission/limits, API
resources and the current Aurora autoscaling/minimum-capacity policy unchanged.
The retained baseline page images are included in both implementation and the
production estimate below. Research-only adoption gates do not block this PG4
release. See [the four-index comparison](four-index-publication-validation.md)
and [the storage decision](publication-storage-alternatives.md).

| Workstream | Completed and verified | Remaining |
| --- | --- | --- |
| PG token index | Complete-dump Oracle SQL, real-reader parity, bounded read/write coexistence, guarded retirement/restoration procedures and recorded production/rollback source compatibility | Refresh live release/catalog checks; execute retirement separately; verify production publication and reads |
| Knowhere native first pass | Detailed asset probe deferred to enhancement; text scan and eager page images retained; full/scanned-path contracts passed | Review and release compatible readers before activating the new producer layout; same-input end-to-end timing |
| Knowhere asset transfer/access | Duplicate working image PUTs removed; immutable serving origins and cross-Worker recovery covered; batch capability API verified; latest main integrated with partial Fine image/table origins and final-snapshot handoff covered | Deploy every API and both Workers in the documented order; verify real origin reads and recovery |
| Node SDK | Readable-core preparation, pinned batch image access, full-copy integrity and cancellation tests; local commit `22d637a` | Review/merge and publish the minor release; a changeset is not a registry release |
| Hipo | Durable readable publication before image replication; browser/MCP access, original-page citations, pinned export waiting, details-only copy retry and final-pointer fencing covered locally; readable baseline no longer displays perpetual enhancement progress | Pin the published SDK and lockfile; refresh against Hipo main; rerun composed checks; resolve coarse navigation, missing-page references and activity-aware polling before claiming full on-demand UX acceptance |
| Full-dump storage comparison | Complete production-dump writable clones, same frozen 188-unit / 28,917-frequency parsed input, cold and warmed PG4/S3 owning-publication pairs | No S3 implementation or additional storage-replacement acceptance in this delivery |
| Namespace growth and compact-storage research | Original base0 evidence retained; reusable parsed-fixture, background-object seeds, prepared-cache and fixed 20-call screening contracts pass locally | Run the reduced complete-dump Oracle screening; compare publication, query latency, page references, memory, storage and WAL; directional findings and adoption remain pending |
| End-to-end performance/capacity | Historical 188-page production stage breakdown; local contracts and bounded component/resource measurements | Reacquire exact 188/407-page originals; full two-Worker, different-namespace mixed-flow acceptance and staged production measurements |

All implementation statuses in this table mean **local completion**, not release.
The 2026-10-28 repository check found no PR for this branch in Knowhere, the Node
SDK or Hipo. The SDK package and Hipo dependency still name published version
`2.7.1`; the new SDK path has been verified through local composition only.

### Main integration and on-demand Fine compatibility

Live `origin/main` was refreshed to `d1d72ff260e8` (v1.2.38, PRs 480,
482 and 484). Local merge `894c50c41680` preserves native-first profiling,
eager images, immutable baseline assets, Fine boundary fixes and original-page
visual matching. No production release or deployment is implied by this merge.

The integration exposed a real mixed-origin failure: a newly refined image or
file-backed table is uploaded to the working result prefix while the current
revision still points at the baseline snapshot. Readers previously looked for
the new file inside that immutable snapshot. Claim-owned chunks now retain
per-artifact origins bound to the baseline serving prefix. URL hydration,
image evidence and `corpus.query_table` use those origins during partial Fine;
the atomic final-prefix switch makes final snapshot bytes authoritative again.
Origins cannot escape the owning job's result tree. Baseline objects and
inventories are not mutated, and no baseline image is copied into working
storage for this fix. Roll out compatible origin readers before this producer.

Hipo local commit `d65f80ba` treats a ready, displayed snapshot as usable even
when upstream enhancement remains pending/running. It shows `Processed` and a
neutral explanation that search detail can improve as pages are read. Upstream
failure/retry remains visible. Reconciliation continues independently of the
visual pending state so later failure or final publication is still discovered.
No timeout is used to infer that coarse work is complete or the worker is idle.

The following boundaries remain explicit:

- Hipo copies immutable baseline/final snapshots, not every same-revision
  coarse or Fine update. Its local Tree/body can therefore remain the baseline
  page view indefinitely. Synchronizing coarse navigation requires a separate
  coherent read/synchronization contract; it is not implemented by the UI fix.
- Fine citations with genuine physical page numbers can open retained baseline
  source pages even when the refined chunk ID changes. Same-revision references
  without page coordinates must not be assumed to belong to that baseline;
  live result coverage and a safe missing-page reader path remain to verify.
- Exports pin the currently readable snapshot and wait for that snapshot's
  resource copies. They do not wait for every unread scope to run Fine.
- Public enhancement state still lacks an explicit on-demand-idle/coarse-ready
  signal. Existing polling is retained; activity-aware polling needs that
  signal and remains follow-up work.

Validation on the merged Knowhere tree: 24 PostgreSQL/ASGI API contracts,
65 Worker contracts/unit tests, 21 table/image tool tests, and the focused
46-test asset/evidence suite passed. Expected failing image/table regressions
preceded their fixes. Full Ruff and API/Worker/shared Pyright passed. Hipo's
focused 42 unit and 8 browser reader tests, whole-project TypeScript, and
changed-file lint/format checks passed. These checks establish local seams,
not production rollout, complete UI acceptance or capacity.

### Publication optimization after the orchestration change

Source review confirms that each Fine scope update still rebuilds all map units,
token rows and BM25 statistics for the document's current revision. This full
revision algorithm predates the new main changes. Main now invokes it for each
read-driven Fine publication and also patches the aggregate namespace MAP and
advances generation on each scope publication. On-demand Fine may process fewer
scopes, so source review alone does not establish higher total document cost.

The next bounded development candidates, before another large benchmark, are:

1. Retire redundant aggregate namespace MAP writes after checking release and
   rollback reader compatibility. Current outline reads live sections and no
   production aggregate reader was found in the refreshed source. Keep per-
   revision manifests, generation locking/advancement and after-commit cache
   invalidation. Setting the existing maximum byte limit to zero does not
   disable MAP encoding and is not a substitute for retiring those writes.
2. If token DML remains dominant, keep the complete authoritative map-unit
   constructor and BM25 statistics but reconcile logical units. Retain unchanged
   unit IDs/token rows; replace changed/removed units. Do not filter solely by
   scope ID: ancestors, interstitial units and shared boundary assets can change
   across scope boundaries. This reduces writes, not the whole-revision CPU pass.
3. Cover same-revision A-to-Fine/B-coarse updates against a full rebuild, equal-
   count content changes, rollback, sequential/concurrent sibling updates,
   generation/cache invalidation, shared assets and exact source-page parity.

These are concrete follow-up candidates, not implemented optimizations or
measured savings. They do not expand the current 20-publication screening.
Oracle continues using frozen source epoch `c61a48c78ea2`; local main integration
does not replace its source. Its revision-replacement component results cannot
establish the cost or correctness of new same-revision on-demand Fine updates.

### Current reduced Oracle screening

The remaining full matrix was replaced, rather than declared complete. The
90-sample continuation and queued 108-sample supplement were stopped. The four
original base0 groups completed 24 measured publications; their evidence and
takeover proof were retained. The reduced controller holds the same exclusive
database lease. At this integration checkpoint its first `other3m` cell had
completed growth/index preparation and was preparing background packed objects;
zero of its 20 new publisher calls had run. It does not require or fabricate an
original-210 completion certificate.

| Growth cell | Screening arms | New publisher calls |
| --- | --- | --- |
| Other namespaces, approximately 3M extra frequencies | PG4; compact PG and S3, each global/scoped/partitioned | 7 arms x cold1/warm1 = 14 |
| Current namespace, approximately 3M extra frequencies | PG4, global compact PG, global compact S3 | 3 arms x cold1/warm1 = 6 |

The budget is **20 actual new publisher calls**, with no extra warmup publishes.
The cold publication warms PostgreSQL for the following warm publication. Reuse
the existing historical base-reader fixture through validated binary COPY of
its small document/revision tables: no new publisher call is used for setup.
Keep ordinary foreign keys and verify 188 units, 28,917 frequency rows, schema,
pages and payload checksums. Restore the pointer only after its revision exists.

Prepare growth and all four token indexes once per cell. Build compatible packed
background artifacts once, retain immutable seed tables, and upload their S3
bodies once per cell. A prepared physical database cache contains the full dump,
growth, parsed fixture and seeds. Each arm restores that same cache, applies its
small metadata/object layout and copies the seed bytes without repacking or
reuploading. Keep only one cell cache at a time; preserve the protected original
archive, source files, PDFs and evidence. Report seed-table space separately
from production table footprints and preserve at least 1 GiB after cache creation.

Run the six frozen queries before and after each arm's two publications, with
one initial episode and one further episode in the same reader process. The
post-publication explicit pin resolves the new current revision. Retain cold
loads, overlay/cache rebuilds, exact results/page-reference comparisons, SQL/I/O,
WAL, memory and absolute storage counters. One cold/warm pair establishes
direction and obvious regressions, not percentiles, repeatability or production
capacity. Expand validation only after a candidate warrants it.

### Recorded full-matrix protocol (superseded)

The original campaign requested a combined comparison of the five-index layout,
PG4, scope-leading indexes, namespace partitioning, partitioning with redundant
aggregate-navigation writes retired, compact PG payloads, and the same compact
payloads in S3. S3 remains a research arm rather than an adopted delivery.

Every arm starts from the protected complete production-dump four-index archive
and the same frozen 188-page publication input. Add approximately one and three
million frequency rows from the same deterministic production-document mixture,
first in other namespaces and separately in the target namespace. Preserve
original rows. Record actual whole-document rounding rather than claiming exact
synthetic counts. Keep all container limits and business concurrency unchanged.
Untimed preparation temporarily retains only the map-unit token lookup while
cloning, then rebuilds every selected token index from the same canonical COPY
stream. Refresh token visibility/statistics and the 13 explicitly affected
metadata-table statistics before checkpointing. These setup operations are not
included in publication time; each measured writer has all of its selected
four or five indexes present.

Compare matched cold and warmed publication revision histories, initial-reader
cold/warm episodes, actual document outline/manifests and page citations. Compact
PG and S3 use the same binary representation and gzip level. Count initial BASE
construction separately; include object writes and the authoritative owner
commit in publication time. Measure namespace-generation cache misses as well
as cache hits, including fetch/decode/merge time and resident/workspace memory.
Both component readers capture and bind revisions using the public request
planner's existing function. Include that capture in the measured request and
retain explicit revision pins. Earlier direct-component smoke measurements
without this capture are diagnostic evidence, not public-request latency.
The current compact prototype merges namespace postings after a generation miss;
this cost is linear in the namespace postings and must not be hidden by warm
cache results. Old-revision replay, lifecycle compaction, deletion/moves,
multiwriter recovery and production rollback remain separate adoption gates.
The prototype changes one fixed document repeatedly and supports one latest
document overlay above a namespace BASE. It does not establish a production
policy for multiple documents changing at once. Its 128 MiB process cache and
separate transient-workspace budget also do not establish aggregate memory
safety for concurrent loads across many namespaces.
Warm native reads still perform document-authority work and dense unit
mask/scoring work. Neither compact storage nor S3 establishes constant-time
retrieval as a namespace grows.

The active document is replaced with matched new revisions while old revisions
are retained. Pair sample positions across layouts and report active versus
retained history counts. These samples are not independent pristine restores,
and this database experiment does not establish end-to-end two-Worker capacity.

The existing SQL no-match/unfiltered fallback counts current scoped units and
tokens to detect an incomplete index. Scope-leading indexes and partitioning
do not eliminate that linear integrity check. Removing it would weaken the
tested corruption contract unless a per-revision integrity certificate is
enforced by mutation fencing or every allowed mutation invalidates it
atomically. No such change is included in this experiment, and BM25 statistics,
eligibility, score normalization and citation rules remain unchanged.

Calibration found an existing instrumentation contract mismatch: the oversized
namespace-snapshot skip branch records `namespace_snapshot_skipped`, while the
publication trace rejects that counter name. The experiment records this one
counter through a private compatibility observer so the ordinary untraced
business branch can be measured with stage timings. All other trace validation
remains intact. No application whitelist is changed by this experiment; traced
production use of this branch still requires its own correction and validation.

### Follow-on compact and namespace-isolation combinations

The user requested investigation of combining compact storage with namespace
partitioning. The earlier 210+108 matrix was superseded by the reduced screening
above. These combinations remain research, not completed measurements or an
adopted production schema.

The current partition candidate partitions only `document_map_unit_tokens`.
Compact publications write zero new token rows, and compact discovery does not
read that table. Combining those exact implementations primarily tests BASE
bootstrap, migration and coexistence with legacy row-format namespaces. It may
also reduce shared I/O contention from concurrent legacy writers; static
background growth alone cannot establish that effect. Bootstrap must carry an
explicit token `scope_id` predicate before partition pruning can be credited.

Investigate two combinations against their corresponding compact controls:

| Combination | Target and required evidence |
| --- | --- |
| Compact PG plus namespace isolation | Identify the remaining authority/hydration and object-storage PG costs; compare scope-aware indexes with physical partitioning on the tables actually accessed. |
| Compact S3 plus namespace isolation | Apply the same comparison to the remaining PG authority/hydration tables; S3 object fetches and bytes remain separately measured. |

Use the current stage/query/I/O results to select those tables before changing
their schema. Compact-object tests must populate many other namespaces with
distinct valid packed objects and representative compressed/TOAST bytes; growing
only legacy token rows leaves the new object table almost empty and cannot
validate its scaling. Record cold loads, generation rebuilds, warm cache hits,
publication commits, partition pruning and child heap/index/TOAST I/O. Preserve
identical BM25 results, page citations and authority checks.

Namespace-partitioning the current digest-only object table requires a
scope-aware object key/read/write contract and a uniqueness decision. Treat
that as an explicit design change, rather than assuming a partition DDL alone
provides isolation. Partitioning broader document/unit tables also requires
primary/foreign-key compatibility work. Do not partition every table without
evidence of a relevant cost.

Run supplements serially after the current campaign, initially at `base0`,
`other3m` and `target3m`, with paired controls. Use a separate source snapshot,
identity, output directory and checkpoint so no in-flight frozen source or
existing sample is invalidated. Keep resources and business concurrency fixed.

The earlier full supplement specified six variants: compact PG and compact S3, each with
global keys, scope-leading keys/indexes, and LIST partitioning of the target
scope versus other scopes. Run all six at each of the three cells, with three
cold and three warm publications: **108 additional measured publications**.
That queued run is canceled; its adapters are reused by the reduced screening.
Rerun the global controls with the same compact background fixtures; comparisons
against the earlier campaign alone cannot isolate the extra namespace effect.
The legacy token table retains the same four-index layout in all six variants.

The narrow schema candidates are `document_map_unit_indexes`,
`poc_compact_scope_heads`, `poc_compact_document_heads` and, for the PG backend,
`poc_compact_objects`. Scoped and partitioned variants also add the same
namespace-leading document authority index. They leave document, unit, section,
chunk and job primary/foreign keys unchanged. Marker identifiers become unique
within a scope, and scoped PG object addresses become `(scope_id, digest)`;
these are explicit compatibility/adoption decisions, not production migrations.
The experiment preserves marker values, legacy zero defaults and NULL statistics.

Populate compatible background namespaces with real BASE, per-document index,
metadata and hydration artifacts. Report excluded incompatible documents,
logical TF volume, distinct raw/gzip bytes, deduplication, and measured physical
heap/index/TOAST sizes. Do not infer memory pressure merely from legacy TF
growth or from payload bytes below the fixed database memory envelope.

Each arm keeps the same matched publication history and runs the six frozen
queries both before and after those writes, including cold index loads and
five further episodes in the same reader process. The final reader updates
the explicit case's revision pin to the published current revision; queries,
filters and automatic pin capture remain unchanged. This measures BASE plus
the changed-document overlay. It does not establish a long-lived API cache's
behavior across a concurrent publication.

Implementation and local PostgreSQL contracts are in the independent
`experiments/compact-namespace-validation` snapshot. The old 108 queue is
canceled. The new screening uses its own protocol and checkpoint, validates the
subset takeover proof under the original database lease, and preserves original
completed samples and source files. The 210-sample campaign remains incomplete.

## Measured starting point

One production run of a 188-page, 1.66 MB text-native PDF took 141.395 seconds.
The following intervals are sequential. Token persistence is a subset of
publication, not an additional interval.

| Interval | Observed seconds | Planned treatment |
| --- | ---: | --- |
| Admission and file preparation | 4.532 | Observe; no speculative rewrite |
| Baseline parsing | 41.624 | Move detailed asset detection behind baseline publication |
| Package and snapshot upload | 4.007 | Keep current archive path |
| Working raw upload and metadata | 12.033 | Remove redundant baseline-image uploads |
| Publication through owner commit | 48.594 | Retire legacy lookup; 43.185 seconds were token persistence |
| Webhook through Hipo local publication | 30.605 | Separate readable publication from image replication |

The detailed asset-probe interval was 15.843 seconds. Baseline page-image
rendering took 14.141 seconds and remains in the foreground. The Hipo window
contains delivery, queueing, SDK synchronization, and local publication; it is
not a measured image-copy duration. Only the last 4.197 seconds, from SDK commit
to Hipo publication, have a separately established boundary in this run.

The earlier 116–121 second estimate covered only parser/upload changes. The
combined PG4 planning model below supersedes that partial estimate. Neither
number is a measured production improvement.

## Production planning estimate: the same 188-page PDF

Use the historical 141.395-second run and its exact sequential boundaries.
Oracle verifies the direction and feasibility of reducing index I/O; its
absolute 5.317/9.360-second PG4 publication medians are not Aurora predictions.
The separate historical five-index campaign and the new four-index campaign
also cannot be subtracted to estimate the causal effect of index retirement.

For a planning scenario, assume a **35–60% reduction in the production token
stage**. This is an explicit transfer assumption, informed by the controlled
same-engine SQL retirement reductions of 37.2% warm and 63.5% cold and the earlier
approximately 38% application token-stage observation. It is not a measured
production range, confidence interval or percentile commitment. Hold the other
5.409 seconds of publication constant.

| Sequential interval | Historical production, s | Combined planning scenario, s | Basis |
| --- | ---: | ---: | --- |
| Admission and file preparation | 4.532 | 4.532 | No saving credited |
| Baseline parsing | 41.624 | 25.781 | Subtract only the observed 15.843-second detailed asset probe; retain 14.141-second page rendering |
| Package and snapshot upload | 4.007 | 4.007 | No archive-path saving credited |
| Working raw upload and metadata | 12.033 | 2.033–7.033 | Assume the previously budgeted 5–10-second reduction; actual required-input transfer must be measured |
| Publication through owner commit | 48.594 | 22.683–33.479 | Token stage 17.274–28.070 plus unchanged 5.409 seconds |
| Webhook through Hipo local publication | 30.605 | 30.605 | Retain the entire observed window; no quantified Hipo saving credited |
| First ready | **141.395** | **89.641–105.437** | Sum the sequential stages once |

Calculation: `141.395 - 15.843 - (5 to 10) - 43.185 * (0.35 to 0.60)`.
The two endpoints pair the smaller/larger upload and token savings consistently.
Token persistence is already within publication; the probe is already within
parsing. Neither saving is counted twice.

Use **approximately 90–110 seconds (1 minute 30 seconds to 1 minute 50 seconds)**
as a rounded delivery budget for this 188-page workload, compared with the
historical **141.4 seconds (2 minutes 21 seconds)**. This budgets roughly
30–50 seconds less first-ready wait. The unrounded model saves 35.958–51.754
seconds, or 25.4–36.6%; the rounded upper budget allows modest uncertainty.
Index retirement alone, before parser/upload/Hipo changes, models
115.484–126.280 seconds: approximately 15–26 seconds saved.

Hipo's early-readable path and 188-to-one batch capability requests may provide
additional improvement, but request-count reduction is not measured wall-time
reduction. The separately bounded 4.197-second SDK-commit-to-ready tail does not
measure removable copying or cap the possible Hipo benefit. Keep that upside
unquantified until a complete trace separates admission, core transfer,
readability and replication. Do not promise an 80-second target from these terms.

This model assumes equivalent input/output/token load, comparable external
latencies and no additional admission backlog. Aurora scaling, live physical
index layout/cache state and exact remaining upload inputs can change the
outcome. It estimates first readability/searchability, not enhancement
completion, complete image replication, p95/p99 or throughput. The exact original
PDF replay and production verification below remain required.
The arithmetic and explicit assumptions are retained in
`.benchmarks/progressive-first-ready-20261028/pg4-production-planning-scenarios.json`;
this file contains planning scenarios, not new measured runs.

## Work package 0: preserve the measurement contract

Status: historical stage evidence and component counters are available; exact
original-PDF replay and the final release telemetry audit remain open.

Use the same 188-page PDF and the previously measured 407-page large PDF.
Record separately:

- Knowhere admission, classification/text scan, detailed asset probe, page-image
  rendering, first snapshot upload, working-input upload, publication, and outbox
  delivery.
- Hipo callback receipt, durable admission, queue wait, core-snapshot transfer,
  readable publication, remaining image replication, and final local commit.
- File count, bytes, duplicate image PUT count, token rows/batches, and failure
  stage. Keep document bodies, credentials, and signed URLs out of telemetry.

Use existing tracing and durable identities. Add timing/counter attributes where
they are missing; no new public controls or monitoring subsystem are required.
Do not attribute the entire 30.605-second Hipo window to transfer or promise its
complete removal.

## Work package 1: slim text-native baseline profiling

Status: implemented and locally verified; staged deployment and exact-input
timing remain open. The following defines the implemented acceptance contract.

Primary owners:

- `apps/worker/app/services/document_agent/coordinator.py`
- `apps/worker/app/services/document_parser/profiling/doc_profiler.py`
- `apps/worker/app/services/page_memory/memory_service.py`
- `apps/worker/app/services/page_memory/scope_enrichment_service.py`

Introduce an internal baseline profiling boundary after classification and the
full text scan, before detailed image/table/drawing detection. Select it from
the existing progressive ingestion path. Keep full ingestion and the scanned
baseline path unchanged for this first optimization.

Preserve sampled classification, page count, header/footer text bands, original
page numbering, complete extractable text, source PDF persistence, and eager
page-image generation. Do not move page rendering or native text extraction
behind readiness. Native baseline visual embeddings remain deferred exactly as
they are today.

Resume the deferred asset probe in the existing background structure task,
before TOC/anatomy consumers require its results. Persist and consume the
completed anatomy features through the existing claim-specific Phase B outputs.
An unperformed probe means unknown, not an assertion that a page has no assets.
Verify the asset flags reaching fine-scope extraction; merely skipping the
foreground call is insufficient.

Acceptance: the native baseline retains equivalent page text, page references,
and readable image assets; the foreground omits detailed asset probing; the
background runs it before dependent processing; the enhanced result retains
its table/figure coverage. Confirm full/scanned-path behavior separately.

## Work package 2: remove duplicate baseline-image uploads

Status: implemented and locally verified; staged deployment and real two-Worker
resume/publication acceptance remain open.

Primary owners:

- `apps/worker/app/services/document_ingestion/success_finalization.py`
- `packages/shared-python/shared/services/jobs/lifecycle/result_writer.py`
- Result raw-prefix consumers and background artifact resolution

Before this optimization, the baseline is uploaded under its immutable snapshot
prefix, then every raw file is uploaded again into the working prefix. This
second upload loop is part of the measured 12.033-second interval.

Make the immutable baseline upload the serving source for its page images and
other published assets. Keep only the inputs actually needed to resume Phase B
in the working-input transfer. Keep claim-specific generated outputs in the
working/staging location and final artifacts under a new final snapshot prefix.

Represent the distinction between serving assets and background inputs
explicitly. The current `result_raw_prefix` is consumed by APIs, retrieval
hydration, Agent image reading, and background work; changing that one value
without updating its consumers is not a complete implementation. Retain
compatibility with in-flight jobs that still use the older working-prefix layout.

Acceptance: no second PUT of all baseline page images; baseline/Agent image
reads work before enhancement; background resume works on another Worker;
final publication and retained baseline provenance remain readable. Tiny
required working inputs may still be copied; do not replace a proven layout
with a broad storage rewrite merely to eliminate negligible bytes.

## Work package 3: publish Hipo readability before image replication

Status: Knowhere, Node SDK and Hipo implementation and local composition passed.
SDK publication, Hipo's real dependency pin and staged deployment remain open.

Owners: Knowhere snapshot delivery, Node SDK parsed-document storage/sync, and
Hipo durable document synchronization and reading.

The pre-optimization Hipo flow already enqueues synchronization. Its SDK adapter
returns a snapshot descriptor only when `result.completed` is true, and Hipo's
durable repository publishes readiness at terminal synchronization. Detaching
the HTTP request alone would leave this readiness dependency unchanged.

Implement two explicit milestones within the existing durable workflow:

1. **Readable snapshot:** obtain and validate pinned manifest/chunk/navigation
   metadata and the original PDF; include any non-image content required for
   reading. Establish an authorized read path for every referenced image to
   the same immutable snapshot. Atomically publish the Hipo snapshot, ready
   status, and existing notification/receipt transition.
2. **Image replication complete:** continue the existing checkpointed copying
   of image resources. Prefer local image URLs once a matching complete-copy
   commit certifies the replica; before that, resolve images through fresh pinned
   origin access. Byte reads may reuse individually verified local objects.
   Issuing URLs must not read and hash every image. Record full synchronization
   only after its existing completeness/integrity conditions pass.

Keep Knowhere's existing page-image generation. No renderer runs in a Vercel
request and no server-side on-demand generation is introduced. Hipo can reuse
its original native PDF when original-version identity is verified; converted
PPTX still requires Knowhere's converted source PDF. Do not claim a large-file
transfer benefit without measuring it.

SDK changes must distinguish readability from full local-copy completion. Do
not publish the existing full-sync commit/checksum marker prematurely or weaken
`validateRequiredObjects`. Use immutable snapshot delivery for bootstrap;
reading a live document by Job Result ID alone can race final publication,
because baseline and final can share that ID. Extend delivery references only
where the existing snapshot API cannot supply the required pinned objects.

First-view authorization is also part of this boundary. A real local HTTP / SDK
probe of 188 page chunks found 188 individual image-capability requests when
URL hydration ran before replication completed, despite downloading no image
bodies. Resolve the selected references in bounded batches against one pinned
snapshot, validating owner and inventory once per batch. Reuse descriptor and
full-copy certificate metadata within the read; do not introduce an unbounded
cache or increase transfer concurrency. Metadata-only consumers can opt out of
automatic URL hydration and use their authorized resource port instead.

Hipo's continuation stays on the durable queue with the existing attempt,
generation, lease, and parse-operation fences. No detached process-local Promise
is relied on after a Vercel response. A late baseline continuation must never
replace a displayed final snapshot. A replica-copy failure does not downgrade
an already readable Source.

Audit consumers of `syncStatus`, not just the browser's ready flag. In particular,
pre-optimization MCP Library browsing requires `syncStatus === "completed"`, and
`markParsedSnapshotReadable` can turn a failed synchronization into completed
after a successful content read. Replace these assumptions with the appropriate
readability or replication-completeness contract. Reading available text must
not certify that every image has been copied.

Keep replication identity pinned to a snapshot independently of the displayed
snapshot's publication authority. If final readability is published before
baseline image copying finishes, the retained baseline can still finish copying
without moving the display pointer or emitting another parse-completion event.
Verify both snapshots remain available for their permitted reads. Preserve the
existing global transfer limits and initial-sync admission permit. A readable
core does not wait for its own bulk copying to finish, but can still queue for
that shared permit behind another admitted operation; this optimization does
not promise zero admission wait or increase concurrent archive initialization.

Keep asset authorization bound to the owning Project or its Archived Evidence,
including moves and explicit provenance reads. Resolve fresh origin access
through authorized immutable references; do not persist expiring signed URLs
as permanent asset identities. Exports/shares must either resolve the complete
snapshot or wait for their own required copies without blocking Source ready.
Deploy compatible readers before relying on the new readability marker, and
retain a rollback reader that can still open already published readable snapshots.

Acceptance: pause image copying and demonstrate that List, flat baseline Tree,
Ask eligibility, original-page citations, and image reads all work. Resume after
process termination, expired leases, duplicate callbacks, and reordered final
events. Demonstrate full-copy completion, bounded retries, correct authorization,
and final pointer consistency. Keep upload/sync concurrency settings unchanged.

### User-experience decisions and performance acceptance

- **Complete exports while replication is pending:** accepted on 2026-10-27:
  keep the document readable and queue the export until its required pinned
  snapshot copies are complete, then produce the complete export automatically.
  The current export path excludes snapshots without completed synchronization;
  an already ready document must not be silently omitted. See Hipo ADR 0054.
- **Replication failure presentation:** accepted on 2026-10-27: healthy copying
  stays quiet. After automatic retries are exhausted, only document details show
  `资源同步未完成` with `重试同步`; no Library-list synchronization error is added.
  Keep the document readable and retrievable. The retry continues copying the
  same snapshot without reparsing or recharging, and this state remains separate
  from parsing/enhancement failure. See Hipo ADR 0053.
- **First-read performance:** test first page and uncopied-image access separately
  from first-ready time. An image read must not wait for all background copies.
  Original-PDF page display remains the established reading fallback; determine
  request/loading thresholds from measured access latency. No additional search
  coverage warning or healthy-copy progress badge is authorized by this plan.

For a waiting export, pin its requested source snapshots rather than chasing a
changing latest revision. A final enhancement appearing during the wait must
not produce a mixed-revision archive. Export retry/deadline behavior must remain
bounded and visible through the existing durable export job.

## Work package 4: retire the legacy token lookup

Status: existing operator/rollback and Oracle correctness/performance checks
complete; live catalog refresh and production execution remain open.

Select the existing guarded retirement of the nonunique
`idx_document_map_unit_tokens_lookup`. Retain text unit IDs, the primary key,
binary token lookup, unit index, unit-leading covering lookup, current reader
predicates, tokenizer, frequencies, and insertion batching. No new application
parameter or automatic deployment-time deletion is required.
Use the existing [operator and restoration runbook](legacy-token-lookup-retirement.md).

### Completed Oracle experiment

The September production dump was fully restored with 12,423,317 background
token rows. Two offline builds reconstructed the real first snapshot into the
same 188 units / 28,917 frozen token rows. The write harness exercised the real
gevent-compatible `execute_values` path, 29 batches of 1,000 or fewer rows,
deferred-FK commit, exact tuple parity, and fixture cleanup. Full normalized
background equality passed. The original dump in Downloads remains intact.

The complete SQL matrix has 30 committed write samples and six read reports.
Retained indexes were rebuilt between SQL layouts to control physical age.
The following transaction times are medians of three observations per layout
and cache condition. Read and WAL columns are for cold INSERT execution.

| Layout | Cold transaction seconds | Warm transaction seconds | INSERT buffer reads | WAL, decimal MB |
| --- | ---: | ---: | ---: | ---: |
| Existing five indexes, text keys | 19.626 | 3.232 | 31,703 | 219.71 |
| Legacy removed, text keys retained | 7.159 | 2.029 | 9,005 | 73.10 |
| Legacy removed, BIGINT keys | 6.271 | 1.608 | 8,274 | 65.34 |
| Text keys, unit-leading binary-hash replacement | 7.795 | 2.276 | 9,553 | 72.36 |

Legacy retirement reduced the local cold transaction median by 63.5%, warm time
by 37.2%, INSERT buffer reads by 71.6%, and INSERT WAL by 66.7%. The legacy index
itself accounted for a median 21,644 buffer reads during cold insertion. These
are PostgreSQL buffer misses, not counts of physical disk requests.

Do not adopt the BIGINT migration for this optimization: its additional cold
improvement was 0.887 seconds, with substantially more schema, mapping, reader,
writer, and rollback work. Do not adopt the unit-leading binary-hash replacement:
it did not improve writes and lost index-only access in tested lighting cases.
The predicate-only binary lighting experiment preserved results and improved
some large scopes, but did not establish a general read improvement; keep the
current predicates in this delivery.

Earlier work already implemented bulk insertion and 1,000-row batching. The
previous retirement study observed about 38% token-stage and 10.5% publication
improvement without an established end-to-end HTTP improvement. Its denominator
differs from this SQL transaction comparison. Neither study directly measures
the new production duration; the planning estimate above labels its assumed
transfer range separately. See
`legacy-token-lookup-retirement.md` and
`publication-runtime-failure-diagnosis.md` for that history.

The subsequent [complete owning-publication comparison](four-index-publication-validation.md)
also starts both arms from the same full production-dump four-index archive.
For the same frozen parsed input, PG4 medians are 9.360 seconds cold and
5.317 seconds warmed; S3 medians are 4.193 and 3.816 seconds. Thus the S3
candidate adds 5.167 seconds cold and 1.501 seconds warmed savings in that
environment. Warmed-write savings miss both predefined writer thresholds, and
the serving/backfill/compaction/rollback work is still outstanding. Retain these
results as research evidence and proceed with PG4; no S3 work is a release gate.
The old five-index and new four-index publication campaigns are not a controlled
retirement pair and are not used for the production benefit calculation.

### Application readers and bounded performance confirmation

Current source at `94d0b66e0e03` was exercised directly through classic
discovery/ranking/assembly and map lighting. The same frozen serving fixtures
were used before and after removing only legacy lookup. The accepted diagnostic
control has nine chunks; an initial three-chunk setup was removed before the
accepted baseline. Four nullable historical statistics columns were added only
to the disposable database to run the current reader against the older dump.

| Confirmation | Result |
| --- | --- |
| 16 fixed real-reader cases per layout | Exact normalized parity for candidate order, scores, result identities, physical pages, and content/evidence digests |
| Isolation and filters | Owner/namespace, document include/exclude, section/signal, page/image/table, subtree, valid revision pin, and no-hit cases passed |
| Four representative requests, five repetitions per layout | All 40 samples preserved results; database execution did not exceed both the 10% and 20 ms regression thresholds |
| One reverse-order confirmation for three client-wall triggers | All 30 additional samples preserved results; server execution and I/O remained comparable; client-wall limitations are recorded below |
| Two-minute coexistence per layout, one posting writer and two readers | Five-index 55 reads / 17 writes; four-index 33 reads / 20 writes; zero errors, timeouts, isolation violations, or result/page-reference differences |

The legacy index is used by a real current reader. Although the original 24 SQL
probes never selected it, all five `lighting_control` baseline repetitions used
its ordinary Index Scan. After retirement, that request used the retained
`unit_lookup` Index Only Scan, returned the same row, retained 81 shared hits and
zero shared reads, and reported zero heap fetches. Table visibility/statistics
were unchanged across this targeted comparison. Do not describe the index as
universally unused or infer that from cumulative production counters.

For repeated reads, compare server execution with cross-SSH client wall time.
Diagnostic EXPLAIN runs after each request and is therefore warmed. The initial
client medians triggered the agreed single reverse-order confirmation for the
common-word, multiword, and no-hit requests. The confirmation produced:

| Request | Five / four index client median, seconds | Five / four index diagnostic SQL median, ms |
| --- | ---: | ---: |
| Common word | 4.078 / 4.216 | 268.884 / 267.406 |
| Multiword | 3.657 / 3.584 | 7.836 / 7.942 |
| No hit | 1.116 / 1.545 | 11.359 / 11.513 |

The no-hit client-wall flag remains: do not label all client latency checks as
passed. Its server difference was 0.154 ms with zero shared reads in both
layouts; client ranges overlapped. Actual statement statistics, separate from
warmed EXPLAIN, also showed no SELECT execution matching the initial 31-second
client sample's magnitude. Those aggregate statistics do not establish exact
request-level attribution. The bounded conclusion is no demonstrated database
access-path regression in these cases; public API latency/SLO is not certified.
No further test expansion is required for this investigation.

In the mixed windows, posting-transaction medians were 2.398 seconds with five
indexes and 1.825 seconds with four. Median INSERT buffer reads per transaction
were 38,770 / 11,211 and WAL was 29.71 / 24.23 MB. Compare SQL statistics per call
and writes per transaction, while retaining total counts: different completed
request counts are not a throughput comparison. Fixture DELETE/VACUUM maintenance
was excluded from transaction timing but remained part of the mixed interference.
This exercises token posting and real readers, not full document publication,
parsing, asset upload, vector writes, or the two-Worker end-to-end flow.

### Decision boundary and production activation

The Oracle result supports retiring legacy lookup for the checked application
and workloads. The following is a historical compatibility audit, not a fresh
claim about today's running release. A live read-only ECS/ECR/source audit on
2026-10-27 America/Los_Angeles completed the then-current/selected-rollback
compatibility gate:

| Role | Release | Source commit | API / Worker task definitions |
| --- | --- | --- | --- |
| Deployed production | `v1.2.35` | `23ab68c2a9e2` | `knowhere-api-prod:67` / `knowhere-worker-prod:63` |
| Preferred application rollback | `v1.2.34` | `e025ca1a0722` | `knowhere-api-prod:66` / `knowhere-worker-prod:62` |

All four running production tasks were healthy and matched the immutable image
digests in their task definitions. The rollback task definitions remain ACTIVE
and their images remain available. For both API and Worker, image manifest and
configuration digests were verified, and the embedded `APP_VERSION` and
`GIT_COMMIT` matched their release tags/commits; this check did not rely on a
mutable `prod-latest` tag.

Discovery, lighting, token publication/index preparation, corpus routing,
maintenance/operator code, and `uv.lock` are byte-identical to the Oracle-tested
source at both releases. The three token/map-unit model classes are also
identical. No runtime code outside maintenance references the legacy index by
name. The two releases have identical migration trees, so application rollback
to `v1.2.34` requires no schema downgrade or index restoration. Their token
contracts also remain compatible while API/Worker versions overlap during a
rolling change. No live application rollback was performed by this audit.

An additional `v1.2.33` check found the same token surfaces, but that release has
a different migration history and predates the progressive fixes in `v1.2.34`.
It is not the selected application rollback target for this change. Supporting
an older full application rollback requires its own assessment; token-query
identity alone does not certify older workflow behavior.

Include production legacy-lookup retirement as a planned independent operation:

1. Immediately before execution, confirm production still matches the checked
   release/digests and the selected rollback remains available. Recheck version
   compatibility if either changes; do not repeat the Oracle matrix for
   byte-identical token surfaces.
2. Inspect the live catalog using the existing protected read-only connection.
   Verify the exact legacy/replacement definitions and valid/ready state, with
   no conflicting index maintenance in progress. This catalog check remains an
   execution-time prerequisite; it was not replaced by the source audit.
3. Run the release-pinned existing operator with `--apply` under the protected
   migration/maintenance role with index ownership, using its concurrent DDL,
   prerequisite checks, and bounded lock/statement timeouts. Prepare the
   operator environment explicitly: the API image copies `apps/api/scripts`,
   while the root `scripts/retire_legacy_token_lookup.py` is a source-checkout
   tool. Do not assume it is present at `/app/scripts` in the current image.
   Supply the reviewed existing script in the protected operator environment;
   no application image or public parameter change is needed for retirement.
4. Verify that only legacy lookup is absent and the other four indexes remain
   valid/ready. Check a real publication, classic retrieval, lighting, and
   citation pages, and observe token timing/I/O and retrieval errors/latency.
5. Keep application rollback to the pinned `v1.2.34` definitions independent of
   index restoration. If the index change itself must be reversed, use the
   existing `--restore` procedure and verify the completed rebuild.

Safe audit evidence is retained under
`.benchmarks/token-lookup-production-preflight-20261028`. No additional production
replica is required. Production DDL remains a separate authorized operation;
this request approved compatibility verification and planning only.

Restoration requires a concurrent rebuild; application rollback does not restore
the index. Preserve the existing fail-closed definition checks and recovery
procedure in `legacy-token-lookup-retirement.md`. No production DDL was performed
as part of this validation.

The test host has two ARM CPUs and approximately 12 GiB RAM, PostgreSQL 15.15,
128 MiB shared buffers, and durability enabled. Rootless cgroup-v1 CPU/memory
declarations were not enforced. A logical restore does not reproduce production
bloat; lower storage caches are uncontrolled. These are bounded local medians,
not p95, an Aurora timing forecast, or a production capacity proof. LLM/visual
calls and public authentication/cache routing were excluded; actual image bytes
were not fetched. Safe raw evidence is retained locally under
`.benchmarks/token-io-oracle-20261028`; private source content is excluded.

## Delivery and final acceptance

The remaining work is release preparation, operational activation and complete
acceptance, not a new storage-engine implementation. In dependency order:

1. Refresh the three repositories against their target branches, review the
   resulting trees and run the relevant checks. Open one reviewable PR per
   repository, retaining separate compatibility/activation commits. Include the
   guarded index operation, deployment order, rollback target and evidence in
   Knowhere's review description. No PR was opened by this plan update.
2. Schedule the existing token-retirement operator as an independent operational
   slice. Refresh production and rollback source compatibility and the live
   exact index definitions/valid-ready flags, then inspect/apply/verify using the
   protected operator. Normal startup/migration does not automatically drop the
   index; at an already-migrated head, rerunning `upgrade head` does not reexecute
   the opt-in migration. Keep `--restore` and its concurrent rebuild available.
   Verify publication, classic retrieval, map lighting and original-page
   citations. The source-audited `v1.2.34` rollback is index-only compatibility,
   not an artifact-layout rollback for the optimized parser.
3. Deploy the Knowhere compatibility stage to every API and both Workers,
   then activate native probe deferral, duplicate working-image omission and
   the separate serving prefix together. Deploy the batch-capability API before
   the SDK/Hipo fast path. Recheck cross-Worker continuation, page/Agent image
   reads and final publication. Index activation is independent of these code
   releases; neither requires changing concurrency or minimum database capacity.
4. Publish the reviewed Node SDK minor release, update Hipo from its current
   `2.7.1` pin to the actual registry version and regenerate the lockfile. Rerun
   the real HTTP/Blob composition and relevant regression checks against that
   package. Deploy Hipo's readable-marker/resource/export consumers and required
   migrations before its prepare-only synchronization producer. Retain a
   rollback release that understands already-readable snapshots.
5. Reacquire the exact 188-page and 407-page original PDFs using the recorded
   SHA-256 identities. The frozen 188-unit parsed output is available and was
   used for publication experiments; it cannot substitute for an original-PDF
   parser/transfer benchmark. Run full mixed flows through two Workers in
   different namespaces, covering parsing, retained page rendering, archive and
   artifact upload, token/vector writes, concurrent retrieval and the single
   final publish. Include duplicate/reordered events, Worker termination/resume
   and pinned-snapshot consistency. Keep existing concurrency unchanged.
6. Audit the release's measurement fields and compare the same six sequential
   first-ready stages against 141.395 seconds under comparable conditions.
   Separate Hipo queue/core-readability/image-replication timings. Record CPU,
   peak RSS, database reads/WAL/waits and concurrent retrieval latency; verify
   baseline/final List and Tree, Ask, original-page citations, image reads,
   complete waiting exports and copy-only retry without recharging. Report
   production first-ready measurements separately from the planning estimate
   and from enhancement/replication completion.

Use Knowhere contract tests and the SDK/Hipo repository's boundary tests.
Report first-ready time separately from enhancement completion and full image
replication. Record memory/CPU/I/O and retrieval latency as well as correctness.
An absolute local SQL timing is not an Aurora latency or capacity claim.

Local implementation is not production deployment or end-to-end performance
acceptance. The bounded Oracle one-writer/two-reader coexistence check and the
isolated full-publication comparisons do not satisfy the mixed two-Worker flow
above. There is no additional S3, C-extension or Redis acceptance gate.

### Rolling deployment and rollback

Knowhere requires two publishable stages. Updating only the upload producer is
unsafe while an older Worker can claim the background task.

1. **Compatible readers:** deploy immutable-origin resolution, completed-probe
   recovery, serving-prefix readers, and snapshot delivery to every API / Worker.
   At this stage, producers retain `stop_after="asset_probe"`, upload all raw
   working files, and do not emit `result_serving_raw_prefix` on the baseline Job.
   Keeping only duplicate uploads is insufficient: an older finalizer cannot
   replace a baseline serving-prefix override when it publishes the enhanced
   result. All three old producer behaviors must remain together.
2. **Activate the optimizations:** after all Workers run the compatible code,
   change the native baseline boundary, omit duplicate working image uploads,
   and emit the separate serving prefix together. Roll back this stage to the
   compatibility release if necessary. The previous `v1.2.34` / `v1.2.35` releases
   are not rollback targets for newly produced optimized artifact layouts.

The previously checked `v1.2.34` rollback remains relevant only to the independent
token-index retirement operation; it does not certify these new workflow changes.

Publish the Node SDK before raising Hipo's pinned dependency and regenerating
its lockfile. Deploy Hipo's compatible readable-marker consumers, resource
authorization, replication records, and export waiting logic before activating
the prepare-only synchronization producer. Once a readable snapshot is published,
rollback must retain its readable-marker readers and migrations. Do not roll
back to a release that requires a full-copy commit to open every ready Source.
The integration keeps the published old-SDK full-sync fallback for compatibility;
the new fast path must additionally pass the explicit composed suite with the
new SDK. Local symlinks are verification aids, never deployable dependency pins.

### Local verification and remaining measurement gates

The implementation adds native/scanned parser contracts, cross-Worker immutable
asset-origin contracts, owner/pinned-object delivery API contracts, SDK snapshot
integrity and cancellation tests, Hipo PostgreSQL lease/retention/export boundary
tests, and UI / MCP readability regressions. Full synchronization retains its
existing commit/integrity checks. Resource copying does not reopen parsing,
emit another completion receipt, or change the displayed final snapshot.

The original 188-page and 407-page PDF bytes were unavailable in the bounded
local cache search on 2026-10-28 (41 PDFs and PDF members in 19 ZIPs checked).
Their recorded lengths and SHA-256 identities, search scope, and explicit lack
of a same-sample benchmark are retained in
`.benchmarks/progressive-first-ready-20261028/exact-input-availability.json`.
Representative contract and composed checks do not replace that measurement.
Reacquire the exact original bytes before comparing wall time, CPU, peak RSS,
output digests, and retrieval latency for those samples. No substitute document
or hypothetical aggregate saving is reported as a measured production gain.

### Local acceptance record (2026-10-28)

| Repository / boundary | Result |
| --- | --- |
| Knowhere compatibility and activation trees | Each passed 88 Worker and 21 API contracts, full Ruff, and Python 3.12 Pyright for API / Worker / shared |
| Knowhere batch-capability follow-up | 23 API contracts, full Ruff and Pyright passed; real ASGI 188-object request used two SQL statements, one inventory HEAD / GET, and no image HEAD / GET |
| Node SDK | 527 tests passed; typecheck, CJS / ESM / declaration builds, and formatting passed; lint had zero errors and three existing warnings |
| Hipo unit suite | 2,434 tests passed; seven existing or opt-in cases skipped |
| Hipo PostgreSQL boundaries | 58 selected sync, export and MCP boundaries passed against isolated migrated PostgreSQL |
| Hipo browser | 18 real Chromium List / Tree / citation / reader-retry component cases passed |
| New SDK + Hipo composition | Three real local HTTP / Blob composition cases passed, covering core-only reads, pinned origins, authorization and retained baseline / final identity |
| Hipo static checks | Whole-project TypeScript and lint passed; changed-file formatting and diff checks passed |

Expected failing regressions preceded fixes for foreground probing, duplicate
PUTs, readable/full-copy confusion, replica failures, pinned export waiting,
MCP origins, corrupted-core presentation, client bundling, and batch access.
The synthetic 188-page first-view probe reduced capability HTTP requests from
188 to one, with zero image-body downloads. Descriptor / commit method calls
fell from 189 each to two each, but Blob's existing positive cache / in-flight
deduplication already prevented 189 backing-store reads; do not report those
method counts as network savings. Evidence is retained in
`.benchmarks/progressive-first-ready-20261028/hipo-asset-batch-request-counts.json`.

Local Knowhere commits are `634ab8721c75` (compatible readers), `84ef53124690`
(activate the three producer changes), and `e63c7b479040` (batch capabilities).
Deploy the compatibility stage completely before the activation plus batch
follow-up, and both server APIs before new SDK / Hipo activation. The SDK local
implementation is `22d637a`; its minor changeset is not a published version.
Hipo still pins the published 2.7.1 dependency, so its new path remains a local
new-SDK verification until publication, registry pinning, and staged deployment.
No code was pushed, deployed, or released, and no production DDL was run.
