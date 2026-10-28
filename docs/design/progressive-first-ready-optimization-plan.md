# Progressive first-ready optimization plan

Status: implemented and locally verified, 2026-10-28
America/Los_Angeles. Oracle token-index validation and the checked production /
rollback compatibility audit are complete. Parser, immutable delivery, SDK, and
Hipo changes have passed local regression and combined seam checks.
No production index, deployment, or business concurrency was changed.

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

Retain the existing progressive orchestration: publish the baseline once, stage
background enhancement outputs, and publish the complete enhanced index once.
Retain the existing two completion webhook milestones, List/Tree behavior,
source-page citation identity, and enhancement retry policy.

Excluded: on-demand image generation, archive-format/cache optimization,
historical-data cleanup, tokenizer or BM25 changes, new public ingestion
parameters, Worker scheduling changes, and concurrent-transfer tuning.

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
| Publication transaction | 48.594 | Token-index investigation; 43.185 seconds were token persistence |
| Webhook through Hipo local publication | 30.605 | Separate readable publication from image replication |

The detailed asset-probe interval was 15.843 seconds. Baseline page-image
rendering took 14.141 seconds and remains in the foreground. The Hipo window
contains delivery, queueing, SDK synchronization, and local publication; it is
not a measured image-copy duration. Only the last 4.197 seconds, from SDK commit
to Hipo publication, have a separately established boundary in this run.

The narrow estimate previously discussed for the parser/upload changes remains
approximately 116–121 seconds for this sample, assuming removal of the observed
asset probe and a 5–10 second upload reduction. This is a planning estimate,
not a measured improvement. Do not add hypothetical token/Hipo savings to it
until those paths have their own measurements.

## Work package 0: preserve the measurement contract

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

Primary owners:

- `apps/worker/app/services/document_ingestion/success_finalization.py`
- `packages/shared-python/shared/services/jobs/lifecycle/result_writer.py`
- Result raw-prefix consumers and background artifact resolution

Currently the baseline is uploaded under its immutable snapshot prefix, then
every raw file is uploaded again into the working prefix. The second loop is
part of the measured 12.033-second interval.

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

Owners: Knowhere snapshot delivery, Node SDK parsed-document storage/sync, and
Hipo durable document synchronization and reading.

The current Hipo flow already enqueues synchronization. Its SDK adapter still
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
MCP Library browsing currently requires `syncStatus === "completed"`, and
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

Select the existing guarded retirement of the nonunique
`idx_document_map_unit_tokens_lookup`. Retain text unit IDs, the primary key,
binary token lookup, unit index, unit-leading covering lookup, current reader
predicates, tokenizer, frequencies, and insertion batching. No new application
parameter or automatic deployment-time deletion is required.

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
differs from this SQL transaction comparison. Neither study predicts the new
duration of the production sample's 43.185-second token stage. See
`legacy-token-lookup-retirement.md` and
`publication-runtime-failure-diagnosis.md` for that history.

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
and workloads. A live read-only ECS/ECR/source audit on 2026-10-27
America/Los_Angeles also completed the current/selected-rollback compatibility
gate:

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

1. Add the missing stage counters and preserve the frozen comparison inputs.
2. Prioritize the existing token-retirement path as an independent operational
   slice. Deployed `v1.2.35` and selected rollback `v1.2.34` compatibility are
   confirmed; fresh catalog/target checks and production execution authorization
   remain. It requires no BIGINT migration, reader rewrite, or new application flag.
3. Implement and verify native-profile slimming and duplicate-upload removal
   in Knowhere. These changes need not wait for production index activation.
4. Deliver the SDK/Hipo readable-snapshot slice with the required immutable
   delivery seam in Knowhere; publish the SDK before updating the Hipo dependency.
5. Re-run the same small/large document flows and concurrent read checks on the
   resulting tree, including restart recovery, namespace isolation, and the
   single final publication. Keep the two-Worker configuration unchanged.

Use Knowhere contract tests and the SDK/Hipo repository's boundary tests.
Report first-ready time separately from enhancement completion and full image
replication. Record memory/CPU/I/O and retrieval latency as well as correctness.
An absolute local SQL timing is not an Aurora latency or capacity claim.

Keep related changes in one reviewable PR per repository, with separate commits
for the compatibility and activation boundaries below. Local implementation is
not production deployment or end-to-end performance acceptance.

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
