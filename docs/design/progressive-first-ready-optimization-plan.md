# Progressive first-ready optimization plan

Status: agreed non-publication work is locally implemented; the Oracle full-Hipo
functional acceptance wave passed, with hosted performance and release gates open.
Publication optimization is deferred until the completed Oracle PoC is reviewed
and an implementation design is approved. Release
and production acceptance remain pending. Updated 2026-10-29
America/Los_Angeles. The complete-dump Oracle comparisons and the recorded
production / rollback compatibility audit are complete. No production index,
deployment, database capacity policy, or business concurrency was changed.
Namespace-growth and compact-storage research has completed its reduced screening PoC.
The 2026-10-29 discussion remains in planning: namespace-aggregate retirement
and token reconciliation are not authorized for implementation. The user
confirmed the retirement direction and replacement acceptance criteria as plan
decisions only. An implementation draft was isolated and removed from the
active worktree; it is not delivered work. The new development authorization
excludes publication optimization, including these aggregate/token candidates.
The user replaced the remaining 210+108 full matrix on 2026-10-29, preserving
completed evidence and the already-started base0/S3 work. The initial
twenty-call screening consumed one committed call whose telemetry failed. The
user subsequently approved collector-v2 recovery with at most twenty fresh
screening publications and twenty-one cumulative calls, keeping the original
ten cold/warm pairs. All twenty fresh publications are now committed and
validated; the original telemetry-failed committed call remains separate.
See the completed screening checkpoint below for findings and adoption limits.

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

On 2026-10-29 the user confirmed the Ask waiting boundary: a request uses the
text and original-page images currently available without waiting specifically
for the Fine work triggered by that read. Fine continues in the background.
Published enhancement output becomes available to subsequent retrieval requests
and can improve later answers. Do not automatically rewrite an answer already
shown to the user. New user questions or user-requested answer regeneration can
use the updated evidence; this does not require a new regeneration feature.
Keep the established original-page citation and provenance rules when a user
opens an older answer's references.

Acceptance must delay or fail the triggered Fine task and verify that the
current Ask can finish using available evidence without awaiting that task.
Also keep initial structure generation pending and verify that a readable
baseline remains eligible for Ask before chapter navigation becomes available.
Then publish the enhanced scope and verify that a later retrieval can use the
new content while the previously displayed answer remains unchanged. This is
an accepted planning boundary, not completed end-to-end Ask acceptance.

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

On 2026-10-29 the user authorized parallel implementation of the already agreed
non-publication work. Initial-directory delivery, lightweight Hipo synchronization,
browser SSE, and the agreed reading/export recovery policies can proceed.
Publication optimization, including database/index activation and aggregate-write
retirement, is deferred until the Oracle PoC concludes and its results are reviewed.
The separately approved PoC completed within its existing budget. Completion
does not authorize publication implementation or production activation.
No deployment, production DDL, resource increase or concurrency change is authorized
by this development decision.

The user selected the existing PostgreSQL four-index direction on 2026-10-28,
then deferred all publication optimization on 2026-10-29. Its guarded retirement
and restoration evidence remain reference material for a separately authorized
change. This delivery does not remove `idx_document_map_unit_tokens_lookup`,
ship an index operator, or alter token rows, batching or readers.

Do not implement the S3 lexical-index candidate in this delivery. Compact-PG
frequency payloads, native C serving, Redis caching, BIGINT keys, new search
services and database replacement are also deferred. Existing S3 document
snapshots, page images and source PDFs continue to serve their existing purpose;
deferring the lexical-storage experiment does not remove those assets.

Keep both Workers, their concurrency settings, transfer admission/limits, API
resources and the current Aurora autoscaling/minimum-capacity policy unchanged.
The retained baseline page images are included in both implementation and the
production estimate below. Research-only adoption gates do not block this
non-publication delivery. Deferred local research is retained separately in
`four-index-publication-validation.md` and `publication-storage-alternatives.md`;
those research files and experiment implementations are outside this runtime PR.

| Workstream | Completed and verified | Remaining |
| --- | --- | --- |
| PG token index | Complete-dump Oracle SQL, real-reader parity, bounded read/write coexistence, guarded retirement/restoration procedures and recorded production/rollback source compatibility | Deferred; obtain separate implementation/activation authorization before refreshing live checks or changing indexes |
| Knowhere native first pass | Detailed asset probe deferred to enhancement; text scan and eager page images retained; full/scanned-path contracts and exact 188-page Oracle upload-to-Hipo reading passed | Matched maintenance-window deployment; comparable production first-ready measurements |
| Knowhere asset transfer/access | Duplicate working image PUTs removed; immutable serving origins, partial Fine assets and cross-Worker recovery covered; real Hipo origin reading and fresh automatic full-copy completion passed | Deploy matching API and both Workers; hosted origin/recovery verification |
| Node SDK | Readable-core preparation, pinned batch access and directory reads; manifest-derived VERSION and beta post-version rebuild; 515 root SDK tests passed; actual packed candidate used by Oracle Hipo | Review/merge the SDK PR, publish the minor release and validate its registry package; a local tarball is not a release |
| Hipo | Readable publication, coarse directory/SSE, citations and pinned exports passed in full Oracle flows; exact 188-page Tree/page-150-to-151 navigation, automatic copying and page-188 Ask passed; retained baseline citation reopened final page 2 without provenance changes; production build and whole-project TypeScript passed | Exact registry SDK pin/frozen install, CI, hosted metrics and matched maintenance-window deployment |
| Full-dump storage comparison | Complete production-dump writable clones, same frozen 188-unit / 28,917-frequency parsed input, cold and warmed PG4/S3 owning-publication pairs | No S3 implementation or additional storage-replacement acceptance in this delivery |
| Namespace growth and compact-storage research | Twenty fresh committed/validated publications and twenty-one cumulative calls completed; 80 evidence hashes, 192 compact reader comparisons and 16 navigation comparisons independently verified | Review compact-PG design, migration/rollback, namespace aggregate retirement, trace compatibility and concurrent-owner behavior; memory pressure and production capacity remain unvalidated |
| End-to-end performance/capacity | Earlier Knowhere mixed-load/lost-claim evidence retained; full Oracle Hipo generated and exact 188-page flows passed, with first-ready/first-view, directory synchronization and export integrity measured separately | Hosted commit-to-visible Tree target, production timing and capacity acceptance; Oracle's two physical CPUs do not establish production throughput |

All implementation statuses in this table mean **local completion**, not release.
At Oracle runtime validation, no PR had been created. The SDK package and Hipo
dependency still name published version `2.7.1`; the new SDK path has passed local
composition and the isolated Oracle Hipo runtime using a packed candidate, not
a newly published registry dependency.

### Non-publication implementation checkpoint: 2026-10-29

The authorized work is implemented across Knowhere, the Node SDK and Hipo. It
has not been committed as this slice, published or deployed to production.

- Knowhere stores the immutable initial directory and one logical
  `job.document.directory_ready` outbox event in the same coarse transaction.
  Duplicate, stale-claim, ownership, namespace, archive and revision guards are
  covered by PostgreSQL contracts. Structure reads transfer metadata only and
  do not trigger Fine. Historical coarse results without the new metadata
  freeze their recognized outline once under the existing publication locks;
  they do not remain permanently pending after manual refresh.
- The Node SDK exposes `documents.getStructure` with optional revision and
  namespace fences, typed chapter/page data and the raw webhook type. A real
  local HTTP test verifies wire conversion, authorization and timestamps.
  The package still names `2.7.1` for local composition; changesets do not mean
  the new code is available from the registry.
- Hipo has independent durable directory admission, leases, bounded retries,
  existing-sweep reconciliation and authoritative manual refresh. Browser SSE
  reuses committed PostgreSQL notifications through one bounded listener per
  Web process and one subscription per project page. Reconnection/foreground
  reads close notification gaps; interruption and sustained-loss behavior retain
  navigation. Fresh session checks bypass authentication-cookie caching.
- The Tree displays recognized coarse descendants without Fine. Incomplete
  snapshots hide standalone extracted-image/table controls. Page images remain
  available, and image failures renew access only for the same selected snapshot
  and physical page, with one automatic attempt followed by local manual retry.
  An old pending renewal cannot replace a newly selected snapshot. Missing
  citation locations have a local retry interface without guessing a page.
- Exports preserve their pinned inventory, wait up to thirty minutes from the
  original request, reuse active copying and admit one atomic copy recovery per
  export/snapshot. Authorized Archived Evidence remains eligible after moves
  or soft deletion. Actual packaging retains its 270-second limit. Waiting
  failures must not terminate a duplicate worker's legitimate running archive.

Local evidence includes 32 API and 29 Worker PostgreSQL contracts, all 531 Node
SDK tests, a combined 82-case Hipo PostgreSQL boundary run and a 29-case Chromium
run, application contracts and independent-session committed notification
delivery. A fresh disposable PostgreSQL database applied all 87 Hipo migrations
through `0086`, verified the new table/columns, and reran the normal migrator
successfully without applying them twice. The disposable database was removed.
These are separate verified seams, not a live
Knowhere-to-QStash-to-Vercel browser delivery measurement or a capacity result.
The subsequent review reproduced the intermittent Chromium
`ResizeObserver loop completed with undelivered notifications` warning in the
actual reader. An above-viewport page height correction caused TanStack's
ResizeObserver callback to adjust scroll and synchronously resize its observed
ScrollArea ancestor during the same observer delivery. A deterministic real
virtualizer/ScrollArea regression failed before the fix. The reader now uses
TanStack's supported animation-frame observer scheduling, retaining the verified
880-to-820-pixel scroll compensation. The original five browser flows plus that
regression passed 30/30 without the warning. No warning filter was introduced.
The application directory timestamp is recorded just before outbox flush and
commit, not from PostgreSQL's commit-timestamp facility. End-to-end timing must
retain that distinction. The five-second normal visibility target, real
two-Worker mixed-load acceptance, exact-input first-ready measurements, registry
SDK dependency pin, deployment and production verification remain open.

The Web deployment needs the existing session-capable
`HIPO_MCP_LISTENER_DATABASE_URL` for PostgreSQL notifications when its ordinary
database driver is serverless. No new database service or polling fallback is
introduced. The selected downtime release remains the delivery method.
Publication SQL/index/storage optimization is on hold pending the Oracle PoC;
this implementation does not activate its experimental arms or change capacity
or concurrency.

### Implementation review checkpoint: 2026-10-29

The review covers the committed first-ready work and the current directory,
SSE, citation and export changes across all three repositories. It does not
activate any publication-storage experiment. The following defects were
reproduced before their fixes:

- Archive versus progressive publication could deadlock through opposite
  Document/generation lock order. The authorized archive read now takes the
  Document write lock first and refreshes state after waiting. A real
  two-connection PostgreSQL regression verifies both operations complete and
  the persisted document remains archived. This was an existing lifecycle bug,
  not a new directory-event requirement.
- The new missing-directory recovery clause also admitted completed ordinary
  documents, which have no progressive directory to recover. Restrict that
  clause to sources with progressive enhancement metadata; retain missing-
  directory recovery for completed progressive sources.
- Citation preview selection and the existing backend region lookup could
  choose an unrelated visual when an exact anchor was missing. Both now require
  the known physical page or an exact chunk/content identity. Exact nonpaginated
  text remains valid. Exceptions retain the answer and offer local recovery.
- A Library location retry fetched data without applying the returned location
  or reloading an incomplete body index. It now does both, retains the highlight
  wait, and fences response/finally updates by coherent view and request owner.
  Reverse response completion can no longer discard a newer successful retry.
  Ask retries location and content together.
- The reader's observer scheduling caused the proven resize loop described
  above. The fix keeps scroll anchoring and needs no warning suppression.
- SSE project lookup returns lifecycle metadata even for a deleted project.
  The stream now checks that lifecycle on initial authorization and every
  subsequent hint/heartbeat, while retaining authorized archived-project reads.
  Direct stream tests cover fresh-session revocation, project deletion,
  ownership, listener cleanup and a 10,000-notification slow-consumer burst.

The independent coverage pass also identified and corrected the accepted bounded
scope recovery omission. Each structure, scope and generic-recovery path now
uses an ordered batch of at most 100. Scope retry and exhausted-claim cleanup
share one locked batch; inspection timestamps rotate revision candidates
without holding scope and revision locks together. Queued scopes use the
existing one-hour task window before redispatch; genuinely idle untouched
scopes do not generate generic recovery tasks after required stages complete.
Incomplete structure/vector work, exhausted retries and all-refined final
packaging remain discoverable. Seventeen real PostgreSQL cases cover caps,
fairness, duplicate dispatch avoidance, live/expired claims, failure settlement
and repair discovery. No queue priority, claim lifetime, Worker concurrency or
resource capacity changes; publication performance optimization remains deferred.
The limit bounds materialized candidates and dispatched work per path, not the
number of physical database rows inspected or total query I/O. It is not a
data-size-independent database performance claim.

Current local verification: 51 Knowhere API/PostgreSQL contracts, 59 Worker
contracts, all 531 SDK tests, 85 Hipo PostgreSQL boundary cases, 172 Hipo unit
cases and 35 Chromium component cases passed. Knowhere Ruff and production
Pyright, SDK ESLint/TypeScript, Hipo TypeScript and all 86 changed TypeScript
files' lint/format checks passed. SDK ESLint reports three existing return-type
warnings in unchanged test files and zero errors. The Worker count includes the
seventeen new bounded-recovery cases. Independent source reviews found no
remaining actionable finding in these fixes; they do not substitute for the
recorded executable tests. The Hipo Ask boundary uses the real PostgreSQL source repository
with a controlled Knowhere response: ready baseline documents remain searchable
when enhancement is running/failed and copies are pending, and later evidence
does not mutate earlier returned output. It does not substitute for actually
delaying/killing a Fine Worker during an end-to-end Ask.

Still required before release/capacity claims: real failed-Fine Ask,
production-sized two-Worker mixed processing and loss/recovery runs, same-input first-ready and
citation latency measurements, hosted directory visibility, actual registry SDK
pinning, maintenance-window deployment and production verification. The Oracle
publication PoC remains a separately running task and decision gate.

### Completed Oracle screening checkpoint: 2026-10-29

The reduced matrix completed with twenty new committed and validated owning
publications, twenty-one cumulative calls including the separately retained
telemetry-failed commit, and no extra warmup or replay calls. All eighty evidence
files passed independent byte-length and SHA-256 verification. The reviewer
recomputed all twenty timing/SQL projections, 192 normalized compact-versus-PG
reader comparisons and sixteen navigation comparisons successfully.

Core publication seconds include the owner commit and any S3 writes:

| Growth scenario | PG four-index cold / warm | Compact PG global cold / warm | Compact S3 global cold / warm |
| --- | --- | --- | --- |
| Other namespaces +3M token rows | 10.937 / 5.966 | 3.390 / 2.855 | 4.146 / 3.802 |
| Current namespace +3M token rows | 11.170 / 6.038 | 3.357 / 2.981 | 4.195 / 3.843 |

Compact PG global is the next design candidate. These are single cold/warm
trajectories per arm using the same frozen 188-unit / 28,917-frequency publication
input, with retained revision growth. They combine compact storage and namespace
aggregate retirement. They neither isolate each change's benefit nor establish
memory capacity or the superiority of physical namespace partitions. Scoped and
partitioned compact combinations were screened only for other-namespace growth.

The experiment intercepts an existing trace counter mismatch: oversized namespace
snapshots emit `namespace_snapshot_skipped`, which `PublicationTrace` currently
rejects. Both current-namespace PG4 samples needed this observer compatibility
wrapper once. Do not copy that wrapper into product acceptance or treat those
samples as proof of unchanged trace-enabled production behavior. Effective trace
settings and the size bound must be checked before release. Existing maintenance
tools also still read/write namespace aggregates; their retirement, backfill,
rollback compatibility and concurrent publication require a separate design.

The controller/supervisor exited, shared lease was released, temporary growth
caches were retired through the approved cleanup, and the original archive and
frozen source/evidence were retained. The database is stopped and experiment
Worker/API containers are paused. Final Oracle free disk was 10.69 GiB; disk,
resource limits and business concurrency were not increased.

Evidence is under
`.benchmarks/compact-namespace-oracle-20261028/collector-epoch-candidate-20261029/coordinator-fix1/evidence/results/`:
`summary.json`, `completion-proof.json`, and `manifest.json`. Publication changes
remain deferred pending design review; the Windows functional run uses the
ordinary product implementation.

### Windows integration checkpoint: 2026-10-29

At the user's request, heavyweight builds and live integration now run on the
isolated Docker Desktop host `suguanyang@100.101.51.87`, under
`E:\codex\knowhere-progressive-e2e-20261029`. The Mac only packages source and
coordinates the run. The API, two ordinary Workers, PostgreSQL/pgvector, Redis,
LocalStack, Hipo, local signed QStash and a real empty Memento instance are
running against fresh test databases and generated test identities. The current
SDK source is packed and installed inside the Hipo image; this is not a registry
release. No production database, bucket, queue, deployment or index is changed.
Both Workers retain two CPUs, 4 GiB and concurrency 10; the API retains half a
CPU and 2 GiB. Machine capacity and business concurrency remain unchanged.

- Knowhere's real HTTP runner passed 114 checks in 75.532 seconds across two
  fresh namespaces. Per-container task logs confirm both Workers processed
  baseline and coarse work across document ownership. Requested Fine tasks also
  completed; unread scopes remained coarse. This uses a synthetic three-page
  fixture and is not the production 188/407-page capacity comparison.
- The full URL-ingestion path initially stopped before parsing because the new
  test environment lacked S3 notifications. The harness now configures actual
  S3 ObjectCreated -> SNS -> API delivery and verifies one confirmed subscription
  after two setup executions. No synthetic callback or confirm-upload rescue is
  counted as that path's acceptance.
- For a fresh Hipo source, real storage events started parsing, and both
  `job.completed` and `job.document.directory_ready` returned a processed
  acknowledgement on their first delivery. Admission-to-readable was 38.057
  seconds for that small fixture, while the independent copy operation was
  still pending. Directory commit-to-Hipo readiness was 0.918 seconds. The
  browser decoded the original-page image and observed the project stream.
- A supplied three-chapter native PDF then passed actual List image decoding,
  visible coarse Tree, complete project export download and ZIP inventory
  checks. Visual inspection exposed a real omission: overlapping sibling page
  ranges could leave a recognized chapter with no page reference. The fix
  retains page references in independent matching branches, preserves the
  deepest match within each ancestry, shares page payloads, and counts unique
  chunks per subtree. Ordinary text and final placement are unchanged. The
  exact old-code Chromium reproduction failed before the fix. Independent
  review also found that a literal slash in a title could collide with a nested
  path. Encoding individual internal ID segments fixes that collision without
  changing labels or an external/persisted contract. Eight model tests and
  36 Chromium component tests subsequently passed on Windows; independent
  review found no remaining actionable issue in these changes.
- An earlier controlled run stopped before Ask because Windows translated its
  test-only signal's line ending. Both Workers were resumed, the controller was
  fixed to write exact bytes, and a fresh rebuilt full-chain run passed. During
  that run both Workers were paused, all three Fine scopes remained queued with
  zero attempts, and a real classic-mode Ask completed in 25.984 seconds with
  the correct 42,000 / 12,000 / 30,000 amounts and original-page-3 citation.
  This demonstrates that the current answer does not await Fine; it is not
  acceptance of the unconfigured Cursor-based Deep search path.
- After unpause, all three Fine scopes completed on their first attempt. The
  final outbox committed after 15.454 seconds and its webhook was processed on
  its first delivery. Hipo made the final snapshot readable after 27.190 seconds
  and completed copying after 48.195 seconds. Knowhere's current snapshot,
  Hipo's displayed/target pointers and the retained revision all match final
  `snapshot_1494adbd7ce948b4bed015e1e6c694e4`. A separate browser reopening the
  old baseline citation read that final snapshot and decoded the same original
  page 3 at 1224 by 1584 pixels, without selecting historical tracing.
- Direct PostgreSQL inspection confirms three non-null visual vectors, pages
  1--3, stored as `halfvec(3072)` with actual database dimensions 3072, bound to
  the correct owner, namespace, document, revision and published chunks.
  `vector_repair` is complete. These rows were created during automatic
  background structure work before Fine; their presence is not attributed to
  Fine creating new vectors.
- The downloaded export passed every ZIP entry CRC. It contains the exact
  original PDF bytes, manifest, chunks and all three page images. Post-run
  inspection found ten services running, every configured health check passing,
  both real Worker heartbeat checks passing, neither Worker paused, no observed
  OOM-killed container and zero container restarts. These are current-state
  observations, not peak-resource or capacity measurements.

The final browser manifest is
`artifacts/progressive-e2e-331bc6b1-52a7-46b2-9a1c-dd4c19f2f990.json` in the
remote test root. `fine-delay-controller-d.json`,
`hipo-pipeline-331bc6b1.json`, `export-integrity-latest.json`,
`runtime-health-latest.json` and `citation-reopened-after-final.json` retain the
corresponding phase, delivery, vector, archive, runtime and citation evidence.

The test browser uses full Chromium and an exact-origin development exception
for the isolated HTTP hostname. This does not validate browser TLS, hosted
Vercel SSE or managed SNS/QStash. Bulky public demo PNGs were excluded from the
test source bundle; demo-image browsing is not part of this acceptance.
LocalStack recreation did not retain the
earlier bucket state despite its volume mount; earlier runner evidence is
historical, and later runs use new documents. The supplied production-sized
originals, actual Worker loss/claim recovery and production capacity remain
unverified. Publication optimization still waits for review and implementation
approval after the completed Oracle PoC.

### Large-document follow-up preparation: 2026-10-29

Both historical original PDFs were reacquired using read-only conditional S3
downloads. Local physical page counts, lengths and SHA-256 identities match:

| Original | Pages | Bytes | SHA-256 |
| --- | --- | --- | --- |
| `psd.pdf` | 188 | 1,659,085 | `48cee0a4a0dfeeea6617b71252bf989df8a616ca389b9fc9e1bb5b1627441d61` |
| `spacex.pdf` | 407 | 153,808,384 | `c658f6ca50366e03833d8fcf53ab2085f59b3542d1aa48201a84db3cf57b9df9` |

Task-owned copies and provenance are under
`/tmp/knowhere-windows-e2e/fixtures/`. SCP returned success for their Windows
transfer; independent destination hashing has not completed.

Prepared acceptance combines the 188-page actual Hipo UI pipeline with the
407-page direct Knowhere pipeline in separate namespaces, two bounded retrieval
clients and unchanged API/two-Worker configuration. It records upload,
first-decoded-page, full chunk index, original page 150, directory visibility,
source-byte/image integrity and sampled runtime resources. Test-driver review
fixed invalid SQL model fields and required failed/empty/wrong-revision queries
to fail acceptance. New test preparation is not a measured performance result.

An initial Worker-loss injection missed its window: the old owned three-page
document completed Fine normally in about twelve seconds, before controller
readiness. No Worker was killed, and no one-hour recovery result is claimed.
The controller now requires an explicit armed marker before the read. Another
owned, untouched three-page document was identified for the next attempt;
one-hour claim expiry remains unchanged. A separate working-manifest fault
helper preserves immutable serving objects and backs up/restores only the
owned Fine input; it has not been executed.

The Windows host became unreachable before the mixed run or second fault
injection started. SSH and ICMP timed out; Tailscale reported the peer offline
while the local node remained online. Last confirmed runtime health had ten
running services and both Workers at 2 CPU / 4 GiB / concurrency 10. Pending
transport actions and controller-file identities must be reconciled after
reconnection before any injection. No fresh claim-expiry observation is active.

Repository refresh found Knowhere and the Node SDK main already included by
their task branches. Hipo main has newer commits, including
`df477f187b1993240618f9f119b6beca8a362cf9`, which defaults new parses to `full`
and allows explicit `HIPO_KNOWHERE_INGESTION_MODE=progressive`. Preserve that
production default when integrating; explicitly opt this isolated acceptance
environment into progressive. Existing Windows image results belong to their
recorded source snapshot and do not validate the refreshed Hipo main.

Hipo's migration histories also diverged after their common main ancestor:
main uses 0083/0084/0085 for Ask edits, edit leases and session handoffs, while
this branch uses overlapping numbers for readable replication, pinned exports
and directory synchronization. Preserve main's published migrations and append
the unreleased branch schema changes after main with a consistent snapshot
chain. Renaming SQL files alone is insufficient: Drizzle compares journal
timestamps against the database's latest applied timestamp, so older timestamps
would silently skip the appended schema changes. Boundary tests that name the
old files must follow the reconciled lineage. The disposable Windows database
already applied the old branch lineage; preserve its evidence before using a
fresh, correctly migrated isolated database for refreshed-main acceptance.
The main merge is now recorded locally as Hipo `3d468339`; `origin/main`
`c2a1e8a8` is included. All 86 already published main SQL migrations remain
byte-for-byte unchanged. The four unpublished feature migrations now follow as
`0086_readable-snapshot-replication`, `0087_pinned-export-snapshots`,
`0088_source-directory-sync` and `0089_export-replication-recovery`. Journal
indices and timestamps strictly increase, and the combined generated snapshots
retain one chain through main's `0084`, then `0087` and `0089`. The latter
snapshots include main's handwritten shared-conversation changes as well as
the progressive changes. Existing migration filename assertions were updated.
The old uncommitted work remains preserved in a named stash; it was restored
after the merge. No old Windows database was rewritten.

A new journal-order regression and the existing schema-coverage test both
failed before reconciliation and pass afterward. Oracle then executed all 90
migrations on an empty isolated database and 86 main migrations followed by the
four feature migrations on another; repeating the normal migrator left both
90-row ledgers unchanged with exactly matching hashes and timestamps.

Strict snapshot/catalog comparison found six inherited declaration errors:
main's published `0038` enables and forces RLS on the five promotion tables and
`hipo_subscription_commitments`, while main's `0084` snapshot and runtime table
declarations omit it. Both actual migrated databases already enforce RLS.
The minimal correction adds `.enableRLS()` to those six declarations and records
the existing enabled state in the two new unpublished snapshots. No published
SQL/metadata or database permission was changed. A regression reading the
published `0038` table list failed for all six before the fix; all three source
checks pass afterward. A Drizzle generation round trip reports no new changes.

Fresh read-only acceptance against the original two databases now strictly
matches all 82 tables, 915 columns/types/nullability and every RLS flag. Complete
normalized catalogs, including constraints, indexes, policies, functions,
triggers and sequences, are identical; explicit old/new service policies and
export defaults pass. No SQL rerun or extra database was needed for the metadata
correction. The original strict failed receipt is retained next to the final
`migration-readonly-final-a137e90b2029.json` receipt under
`/tmp/hipo-lineage-acceptance-20261029/epoch-rls-corrected/` and its Oracle copy.
Combined typechecking on Oracle passed in 74.34 seconds. The full unit run had
2,542 passes, seven existing skips and one generated-OpenAPI mismatch. The
branch's optional `ParsedDocumentSection.pageNumbers` and
`ProjectExportJob.waitingForResources` fields were absent from the checked-in
document. The official generator updated only those definitions and its related
export-component references; no API schema code was changed to make the test pass.
All six source-scan files / 23 checks then passed. Application code is identical
between the full-test epoch and the final epoch; only release prose and the
generated document changed afterward. There is no unresolved test failure, but
the full suite was not rerun after these documentation-only updates.

Final source identity is
`f6f6c70317b7749590769f95c803b0b9c21ce314b80bd2607650c7e27322a1e6`
across 4,511 files, matching the local tree. The test used explicitly attested
cached dependencies, OpenWriting 0.1.16 and the current unpublished Knowhere SDK
dist with directory support. It was not a frozen install against a published
SDK. Package/lock/workspace files and the old shared dependency cache were
unchanged. No Web, browser, Memento or live model call ran in this non-DB check.
The temporary compiler process needed a 3 GiB heap cap after its initial 2 GiB
cap was insufficient; host memory remained available and no API/Worker quota
or concurrency changed. The final receipt is
`/tmp/hipo-main-integration-nondb-20261030-3d468339/final-validation.json` and its
Oracle copy. Hipo's existing
`full` ingestion default from main is preserved; progressive must be selected
explicitly in the isolated acceptance configuration. Its three Hipo Compose
services now share that explicit progressive setting; source YAML expansion was
verified without changing the product default.

A follow-up found one new, avoidable upstream read in directory reconciliation:
each existing enhancement-recovery batch called `getStructure` even when the
same revision's directory was already synchronized. `reconcileJobDirectory` now
checks the repository's coherent local view first and skips the upstream read,
admission and dispatch only for a matching known revision with a ready/empty,
committed and fully completed directory. Missing/incomplete/unknown-revision
views keep the recovery path; manual Refresh still authoritatively reads
Knowhere and validates identity. No new protocol, service or polling frequency
was introduced. The old `useProjectParsePolling` hook has no production caller;
the active browser uses the accepted SSE flow, not an additional fifteen-second
polling loop.

Both ready/empty regressions failed before the eleven-line application guard.
Oracle then passed a fresh whole-project typecheck, 32 tests in four related
unit files and all 23 source-scan checks. This is a separate epoch 6 receipt;
epoch 5 remains intact and the full suite was not repeated. Final Hipo source
identity is now
`5d1bf0550f1c84e0435963f754c7e824290c5f5432f863577d176ca5e333bb86`.
Only `source-directory.ts` and its unit test differ from epoch 5. Package/lock,
SDK payloads and the shared dependency cache are unchanged. See
`directory-reconciliation-epoch6-attestation.json` beside the earlier Hipo
non-DB receipt; dependency validation remains cached composition rather than a
published-SDK frozen install.

### Oracle fallback integration checkpoint: 2026-10-29

The user confirmed that Windows had gone offline and authorized assessing Oracle
as the replacement test host. Live inspection found native ARM64, two CPUs,
12,521,361,408 bytes RAM, no swap and 11,445,501,952 bytes initially available
on the existing 48,277,495,808-byte root filesystem. The full-dump PoC artifacts,
original archive, frozen source epochs, existing containers and pgdata were
retained. The ordinary user Docker context is rootless and initially empty;
the existing PoC uses the separate rootful Unix socket. All new test commands
explicitly target the rootful socket and their own Compose project.

The new isolated root is
`/home/suguan/knowhere-progressive-e2e-20261029`, project/network
`knowhere-progressive-oracle-e2e`. Current API/Worker/shared source, including
uncommitted work, was bundled without credentials, research data or large demo
images; bundle SHA-256 is
`9bd3e744fdef115d77e9bb4e2cf90bbc0389689c7bf88e7ce4d39691280a1beb`.
Both exact original PDF hashes and lengths were independently verified again
on Oracle. Production databases, buckets, queues and service credentials were
not copied; model credentials use the previously validated test configuration.

The ordinary production Dockerfiles built native ARM64 images successfully:
API `d46665458901b1b38d956a10f6151ae4e75bcc7f58ea13641c236bbda1002fa8`
and Worker `b1386ce8fed8dbcca9497bf010b095523827ed8bab495028aa1e61da18c86f91`.
New PostgreSQL/pgvector, Redis and LocalStack volumes were created separately
from the PoC. Migrations, identity bootstrap and real S3-to-SNS notification
subscription passed. All six persistent services became healthy. API remains
0.5 CPU / 2 GiB; each of the two Workers remains 2 CPU / 4 GiB / concurrency 10.
No production deployment or concurrency/resource increase occurred.

Only unused build cache created during this new run was retired: rootful build
cache was zero before the builds. Available disk increased from 3,243,495,424 to
5,207,658,496 bytes. Before/after guards verified both new image IDs and four
preserved PoC container identities/states unchanged. No image, container or
volume prune was executed. Existing old Hipo dependencies and pnpm store are
hard-linked and are not independent multi-GiB cleanup opportunities.

Two setup defects were corrected without changing product behavior. The host's
default Python 3.8 lacks zlib, so control scripts use the existing Python 3.12
interpreter. The first private-setting transfer omitted `ALI_URL`; the valid
key pool consequently received provider 401s at the default endpoint. That
failed run, its resource samples and logs were preserved under names containing
`missing-ali-url` or `setup-failure`; it is not a latency/capacity result. The
endpoint was restored from the validated configuration, verified in API and both
Workers, and the transfer allowlist now rejects unclassified settings. A clean
new run was then created with fresh namespaces/jobs.

That clean three-page run passed all 114 HTTP checks in 34.411 seconds. Alpha and
bravo first-done polling observations were 21.553 and 20.862 seconds, and frozen
directory observations were 30.536 and 29.394 seconds. This verifies native
parsing, eager images, immutable source bytes, publication, directory identity,
namespace fences and read-driven promotion on Oracle; it is not an improvement
estimate against a different host or the historical 188-page production run.

A real Worker-loss test then used the same owned alpha revision and a semantic
budget query. It returned current original page 3 and budget evidence before
the controller uniquely correlated the `p3-3` claim to Worker A, killed and
restarted only that container, and verified the exact unchanged post-kill claim.
Both actual Worker heartbeats recovered under unchanged limits. The real claim
is dated `2026-10-30T01:32:55.724158Z`; no replacement is eligible before
`2026-10-30T02:32:55.724158Z` (19:32:55 America/Los_Angeles on October 29).
At 01:34:13 UTC, ordinary retrieval still returned current page 3 with
42,000 / 12,000 / 30,000 evidence while the original claim remained running.
The real-time observation completed with 125 samples. All 114 samples before
the original expiry preserved the original claim, attempt count and document
identity. Ordinary background recovery finished the scope at
`2026-10-30T02:37:50.219873Z`, approximately 4 minutes 54.5 seconds after expiry,
within the existing five-minute sweep cadence. It used attempt 2 and a new claim
generation, with no manual recovery, timestamp change or recovery-triggering
read. The observer saw the refined result at 02:38:00 UTC and then made its one
planned consumer query: current original page 3 still contained 42,000 / 12,000 /
30,000 budget evidence. Both actual Worker heartbeats and Docker health passed.

The final read-only state preserves the Job's done status, current revision and
baseline snapshot/checksum. `p1-2` is refined on attempt 1, `p3-3` is refined on
attempt 2, while the untouched `p2-3` remains coarse with attempt 0 and no hit.
Structure and vector repair are complete. Whole-document final packaging is
therefore not required; finalization remains pending without any artificial
additional read. This is Knowhere retrieval/recovery evidence, not Hipo
Ask/browser acceptance. Receipts are `oracle-worker-loss-hour-observation.json`
and `oracle-worker-loss-final-state.json` in both task evidence roots.

The exact 188/407-page HTTP mixed test completed in 647.389 seconds in two new
namespaces (`ef308c14f75c4bfb`), with two bounded classic retrieval clients, each
requesting once every five seconds. All 58 driver checks and 260 query response
checks passed, including current-revision identity, expected source evidence,
and namespace fences. This is approximately 0.4 aggregate queries per second;
it is a functional mixed-load sample, not a saturation benchmark. Both baseline
inventories, source SHA-256 identities, all expected page-image paths and sampled
first/page-150/last image decoding passed. First-done observations after upload
were 59.035 seconds for the 188-page input and 156.965 seconds for the 407-page
input. The 144-section and 24-section directories were observed at 205.220 and
465.546 seconds after test start, approximately 189.7 and 449.0 seconds after
upload. Another full 180-second query window followed both directories becoming
live. The large structure-generation time is separate from the five-second
commit-to-Tree-delivery target; no Hipo synchronization ran in this test.

Query latency observations, in seconds (nearest-rank p95), were:

| Target | Queries | Median | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Stable three-page document | 114 | 0.021 | 0.138 | 0.745 |
| Exact 188-page document | 83 | 0.016 | 0.185 | 1.109 |
| Exact 407-page document | 63 | 0.075 | 0.275 | 1.591 |

The 130 resource samples within the mixed-test window showed no OOM or resource
guard warning. Minimum available host memory was 7,919,800,320 bytes and minimum
available disk was 2,869,633,024 bytes. Sampled Docker working-set peaks were
1,201,517,101 bytes for Worker A and 1,351,840,956 bytes for Worker B, below their
unchanged 4 GiB limits. These are sampled container working sets, not lifetime
process peaks. All six persistent services were healthy after the run; one-off
HTTP clients are excluded from the Worker count. Both initial parses ran on
Worker B under unchanged scheduling. Exact same-Worker received/succeeded task
UUID and revision correlation verifies Worker A completed the 407-page coarse
structure and the 188-page Fine scope; Worker B completed both baselines and
the 188-page coarse structure. Both Workers therefore participated, but this
must not be described as one initial parse per Worker.

Post-run vector verification found a real correctness failure, so the overall
large-document acceptance is not complete despite the HTTP checks passing.
There are five dangling page-vector references on the 188-page input (physical
pages 4–8) and two on the 407-page input (18–19). All seven refer to immutable
baseline chunks that no longer exist in the current published chunks after
coarse structure replaces the baseline layout. The 188-page set exactly matches
its excluded TOC pages; the two 407-page pages are outside its coarse skeleton's
eligible scopes. Current eligible pages have no missing vectors. The existing
rebinding and partial-repair loops leave ownerless vectors in place, and the
empty-owner early return also prevents cleanup. The minimal correctness repair
in `page_embedding_index.py` removes only vectors without a current page owner
and fences reads/deletes by user, namespace, document and revision. It preserves
valid vectors and rebinding behavior. Five real-PostgreSQL TEMP-table regressions
failed before and passed after the same module-only overlay; nine related
focused tests passed. The five formal CI cases also passed through the normal
PostgreSQL fixture/migration lifecycle in 6.86 seconds, and targeted Pyright and
Ruff passed. Five additional normal PostgreSQL publication cases passed in
5.63 seconds, covering private/demo page previews, scope replacement and actual
baseline/coarse/Fine embedding reuse. No TOC eligibility, model, tokenizer, BM25,
schema or publication-
optimization decision changed.

The original failed persistence receipt is retained. A separately labeled
post-measurement repair used the corrected normal rebind service under both
owned document locks in one transaction. A dry-run fingerprint pinned row IDs,
vector hashes, models, dimensions and current owners before applying. Exactly
the seven orphan vectors were removed; all 588 retained vector payloads were
unchanged. After-commit namespace cache versions advanced from 4 to 5 and from
5 to 6. Only those two test revisions/namespaces were touched. The running API
and both Workers still use their original images, preserving the separate
one-hour recovery observation; this is not a new natural-pipeline run or a
rollout of the source fix.

Read-only post-repair verification then passed for 183/183 and 405/405 current
eligible page owners, including halfvec(3072), owner/revision references and
complete repair state. It also streamed and verified both immutable baseline
ZIPs: 44,928,379 bytes / 192 entries and 308,479,174 bytes / 411 entries. All 603
entries passed CRC; inventory entries also matched size and SHA-256. Both exact
original PDFs, all 595 baseline page-image entries and physical-page coordinates
remain intact, including pages deliberately omitted from coarse retrieval.
One GiB of free disk remained reserved during each temporary archive download.
No whole-document final archive is required while unread scopes remain coarse.

After the real one-hour recovery finished, the isolated API and both Workers
were recreated with a read-only corrected-module overlay on their original
images. Before/after checks verified image IDs, environment/command hashes,
resource limits and concurrency unchanged. A fresh exact 188-page natural
pipeline run reached baseline after 44.641 seconds from upload; source bytes,
all 188 image inventory entries and sampled image decoding passed. Coarse
structure completed, but the run correctly failed on vector-repair state before
making its only planned retrieval request.

This exposed an integration omission in the cleanup fix: coarse publication
still calculated missing vectors as old physical-page candidates minus stored
vectors. It consequently scheduled the five deliberately excluded TOC pages for
repair, while insertion correctly rejected those ownerless pages. The stage
then reported missing provider coverage. This is not a provider outage; repair
eligibility must use the current published page owners, including when resuming
an older pending list. A shared scoped coverage helper now supplies the coarse
and recovery paths. Vector-repair settlement alone also takes the existing
Document-to-JobResult lock order, checks active/current-revision/result binding
and claim generation, then recomputes actual coverage. This applies to successful,
exception and zero-pending exits: owner removal cannot falsely fail the stage,
and newly missing eligible pages cannot be falsely completed. Other stage
settlement semantics, ownership policy and claim lifetimes are unchanged.

Real PostgreSQL RED/GREEN cases cover excluded/mixed pending lists, genuine
missing pages, partial success, owner removal during provider failure, owner
addition before settlement and archived-document fencing. The related 33 Worker
and seven API contracts passed, with clean static checks and independent review.
Final index/service hashes are
`8a97001879d420f3eaa33e548f675599354149fb6ed37e09b61cfdc068695491`
and `3d2c8f5fc06d1887eb13dcb0a3e7961196289bf4983bb27e9cecca137df018c9`.
A separate read-only module epoch was activated under `owned-coverage-v3`,
preserving the original images, current test data, limits and previous code
files. Its fresh exact 188-page natural run (`e7c10d1ce0004215`) reached baseline
46.660 seconds after upload and complete coarse/vector repair at 152.286 seconds
after test start. All 183 eligible current page owners had halfvec(3072)
embeddings with valid references; there were no missing or orphan vectors.
The five excluded TOC pages did not become false repair obligations. Exact
source bytes, all 188 baseline image entries and sampled images passed.

The run issued its sole retrieval, passed current-revision and relevant consumer
evidence checks, then stopped because the harness required physical page 2.
Read-only recovery of that exact original Redis response instead proved a valid
page-188 citation containing both author names; the original PDF supports the
names on pages 2, 3, 12 and 188. Only the hit scope `p188-188` subsequently
refined. This is an over-specific test assertion, not a ranking or citation
product defect. No replacement query, reparse or retrieval-quality change is
authorized or needed to correct that assertion.

Keep `oracle-natural188-corrected.json` and the v3 failed driver receipt intact;
record the read-only continuation separately. The failed harness did not persist
pre-Fine per-page vector fingerprints or directory/inventory body hashes, so
historical equality of those fields cannot be reconstructed for this run. The
separate PostgreSQL contracts and earlier 588-vector comparison retain their
own narrower evidence. A separately recorded read-only continuation passed:
the sole hit scope refined on attempt 1, all other 126 scopes remained untouched
coarse, structure/vector repair stayed complete and all 183 vectors retained
valid current references without missing eligible pages. The baseline pin,
188-image inventory and all five previously sampled object sizes/hashes
(chunks, original PDF and images 1/150/188) were retained after Fine. The
verifier made no additional parse, retrieval or provider call and no database
write. Its receipt is `natural188-v3-readonly-continuation.json`, SHA-256
`4c22c2181671e10c6eca6f23059ece8b62b11910ae58c9f8cc4f30e9ba7cb427`,
in both task evidence roots. This closes the bounded natural-pipeline repair
check; do not relabel the failed driver itself as passed or treat the earlier
post-measurement repair as natural-pipeline acceptance. It does not establish
Hipo runtime, all-scope finalization, ranking quality or production capacity.

A separate local-only v4 harness removes the hardcoded page number, requires
actual original-page support within the unique hit scope, and persists safe
query evidence plus pre-Fine vector/directory/inventory checkpoints before
assertions. All 22 offline guards pass, including valid pages 2/188, empty or
out-of-scope rejection and evidence retention on failure. This does not rerun
the PDF or alter the frozen v3 driver and its original receipts. The local
preparation receipt is
`/tmp/knowhere-oracle-e2e-20261029/natural188-harness-v4/offline-validation.json`.

These measurements use an isolated small database and a two-CPU host shared by
both Workers and the API. Their configured CPU limits do not create four
physical CPUs. Oracle can provide functional/recovery evidence; it cannot
establish production four-CPU throughput. ARM64 also uses the existing syntok
word-tokenizer fallback because the optional BlingFire native library is not
usable there. No tokenizer code was changed. Hipo's full ten-service Docker
stack has not been started on Oracle; remaining disk/build space is limited.

Safe receipts are retained both under the Oracle task's `artifacts/` and
`/tmp/knowhere-oracle-e2e-20261029/`, including
`oracle-http-acceptance.json`, `oracle-fixture-identities.json`,
`build-cache-retirement.json`, `model-endpoint-restoration.json`,
`oracle-worker-loss.json` and `oracle-worker-loss-running-evidence.json`.
The completed HTTP and resource receipts are `oracle-native-mixed.json`,
`oracle-native-resource-samples.jsonl` and `mixed-resource-summary.json`.
Additional receipts include `worker-health-post-mixed-task-attribution.json`,
`page-vector-owner-contract-red-expanded.json`,
`page-vector-owner-contract-green.json`,
`oracle-page-vector-reference-repair-dry.json`,
`oracle-page-vector-reference-repair-applied.json` and
`oracle-native-persistence-post-repair.json`. Keep the initial failed
`oracle-native-persistence.json` alongside them.

### Oracle full-Hipo acceptance checkpoint: 2026-10-29

The complete Hipo runtime on Oracle passed real browser upload, Knowhere parsing,
readable publication, page-image decoding, coarse directory/SSE, pinned export
and ordinary Ask flows. The fresh exact 188-page run passed in 348.193 seconds
while Hipo still displayed its baseline and whole-document enhancement remained
running. Upload start to durable Hipo readability was 58.902 seconds and to
observed ready was 62.748 seconds; reader click to a
fully decoded first page was 23.874 seconds, and upload start to decode was
86.622 seconds. Tree contained 144 directory nodes; physical page 150, its page
counter and Next to page 151 passed. The single ordinary Ask cited physical
page 188. The 197-entry export passed every CRC and retained the uploaded PDF's
SHA-256 `48cee0a4a0dfeeea6617b71252bf989df8a616ca389b9fc9e1bb5b1627441d61`.

Directory commit to durable Hipo readiness took 670 ms. Opening Tree to visible
chapters took 362 ms, but Tree was opened 6.983 seconds after the coarse commit.
This run therefore does **not** establish the five-second commit-to-visible
target. Hosted delivery latency remains a separate acceptance gate.

Fresh queue evidence proves copying progressed automatically: four workflows
returned `RUN_SUCCESS` for the same attempt, generation 1 and retry count 0.
The first three successor deadlines were rounded up to whole seconds. Copying
completed 82.550 seconds before export admission, with zero export recoveries.
This closes the automatic-copy check independently of export-assisted recovery.
The actual defect had rounded `04:01:28.277` down to `04:01:28.000`; a callback
at `04:01:28.155` correctly failed the repository's due guard and ended without
a successor. The three-file fix rounds absolute deadlines up only at the queue
boundary, retaining due/lease guards and concurrency. Sixty related unit tests,
the real PostgreSQL early-claim regression and independent review passed.

The generated three-page full flow passed in 94.767 seconds, with 30.414 seconds
to observed ready and 4.127 seconds from reader click to decoded page. Its
12-entry ZIP passed CRC/PDF identity checks, and the budget Ask cited page 2.
After final publication, reopening that existing baseline citation read the
enhanced snapshot's same physical page 2; stored citation provenance and the
retained baseline descriptor were unchanged, with no new Ask run or citation.

The earlier interrupted original run was continued on its existing account,
Source, baseline and accepted export in 145.411 seconds. Its export-assisted
copy recovery had completed **before** the timer fix and is not evidence for
that fix. The continuation downloaded the same 197-entry pinned export, checked
CRC/source-PDF identity and completed one ordinary page-188 Ask. Node read-socket
retry, a premature Tree assertion and lost browser export-storage continuity
were harness failures; the latter was restored from the one accepted export ID.
The raw Mac Blob path also stalled after HTTP 200 and about 1.1 MB of a 3.15 MB
page image. Oracle fetched the complete exact-SHA object in 0.800 seconds; the
continuation decoded the same URLs through Oracle SOCKS egress. This is a test
transport workaround, not a product change, a passed raw-Mac path or evidence
of artifact corruption. Original failed receipts remain intact.

The read-only continuation guard confirms one unchanged Parse operation, one
parse-pages ledger entry for 188 pages and one export, with unchanged parse and
parse-ledger fingerprints. The requested Ask added one ordinary recall usage;
the earlier total-usage assertion was overbroad and remains separate failed
evidence. See `original188-phase2-storage-continuity/no-reparse-or-recharge-readonly.json`.
Final read-only PostgreSQL checks found 183 valid 3072-dimensional page vectors
per 188-page document, no orphan owners or invalid dimensions, and 119 coarse /
eight refined scopes after Ask. Neither document was forced to whole-document finalization.

Oracle's Hipo Next.js production build passed in 242.192 seconds; whole-project
TypeScript passed after installing the corrected packed SDK, SHA-256
`174ba6f714e5d904e5180d939a7804a1816ba044b738bda01fefea327fa0a56b`.
The SDK now derives VERSION from its package manifest and rebuilds beta artifacts
after version assignment; its root suite passed 515 tests. Separate checks
passed 85/85 real PostgreSQL boundaries and the relevant 28 browser components /
87 units, including initial page-counter and observer scheduling regressions.
Combined Standards and Spec reviews found no actionable finding.

Safe receipts are under `/tmp/hipo-oracle-chain-20261029/artifacts/`:
`original188-fixed/{progressive-e2e-a4da2833-18a5-4418-ad27-d56a9d5ea380.json,launcher-receipt.json,hipo-state-readonly.json,export-integrity-readonly.json}`,
`generated-full/` (including the successful `citation-reopened-after-final-2026-10-30T04-09-55-508Z.json`),
`original188-phase2-storage-continuity/`, `original188-phase2/asset-transfer-diagnosis.json`
and `typecheck-after-deadline-fix.json`. Automatic-copy and regression receipts
are in `/tmp/hipo-failure-acceptance-map/`, especially
`fixed188-automatic-copy-timer.json` and `workflow-deadline-change-receipt.json`.

This was isolated functional acceptance: exactly two Workers retained 2 CPUs,
4 GiB and concurrency 10 each; the API retained 0.5 CPU / 2 GiB on Oracle's two
physical CPUs. No production, disk expansion, concurrency, index, storage
architecture or BM25 change was made. Remaining release gates are SDK PR/release
and registry verification, Hipo's exact pin/frozen install, CI, latest-main
integration, hosted measurements and the matched maintenance-window deployment.
The checked-in SDK 2.7.1 lacks the new directory types; packed-candidate checks
do not establish registry-dependency CI success.
Hipo main `cf7af3a1` was subsequently merged locally as `25486700`. Its changes
since tested `c2a1e8a8` affect only plugin/ignore/documentation files; Web/API,
reader, test, migration and dependency bytes remain unchanged by that merge.
The runtime receipts precede PR creation; source publication is separate from
registry or production release. Publication optimization remains deferred.

### Separate existing lexical recall finding

The initial fault trigger exposed an existing scoring edge case, independently
replayed through read-only current-revision statistics. Three overlapping map
units cover pages [1,2], [2,3] and [3]. A page-three-only marker, and the ordinary
number `42000`, occur in two of those units. Their content average IDF is
-0.5306173375763645. The current BM25 negative-IDF replacement multiplies this
by 0.25, leaving negative scores; the positive-score filters then discard all
matching lexical candidates. The corrected semantic budget query succeeds via
the section-path channel. The marker query can fall through to visual discovery
and return unrelated Launch evidence; its exact vector ordering was not replayed.

Tokenizer replay preserves the marker exactly, and both matching persisted
content rows/tokens are intact. This is neither a missing page nor an ARM token
filtering failure. `git blame` attributes the scoring behavior to `bb846aefe`
on 2026-08-28; the relevant files match current main. The native lexical PoC
preserves this behavior, so its 192 parity comparisons do not establish recall
quality for this small overlapping corpus. The initial 114 checks exercised the
favorable first-page marker; they do not close this separate quality finding.

Keep the failed trigger and scorer replay separate from the valid semantic
Worker-loss capture. On 2026-10-29 the user explicitly deferred retrieval-quality
issues that were not introduced by this change. This main-inherited finding is
record-only and is not a release gate for this optimization. Do not expand this
delivery into a scoring correction or a new quality-regression workstream.
No tokenizer or scoring change has been implemented or silently included in the
Oracle run. Regressions attributable to the current optimization still require
diagnosis and correction.

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
storage for this fix. Deliver the origin readers and producer together in the
accepted downtime cutover, with old Workers stopped before new-format work starts.

Hipo local commit `d65f80ba` treats a ready, displayed snapshot as usable even
when upstream enhancement remains pending/running. It shows `Processed` and a
neutral explanation that search detail can improve as pages are read. Upstream
failure/retry remains visible. Reconciliation continues independently of the
visual pending state so later failure or final publication is still discovered.
No timeout is used to infer that coarse work is complete or the worker is idle.

The following boundaries remain explicit:

- Hipo copies immutable baseline/final snapshots, not every same-revision
  coarse or Fine body update. The independent initial-directory contract now
  synchronizes the recognized coarse outline while retaining baseline page
  reading. It does not copy every partial Fine body or later discovered heading.
- Fine citations with genuine physical page numbers can open retained baseline
  source pages even when the refined chunk ID changes. Same-revision references
  without page coordinates must not be assumed to belong to that baseline;
  verify normal page coverage and use the accepted citation exception interface
  below for missing or unresolvable locations. No historical repair workflow is
  selected.
- Exports pin the currently readable snapshot and wait for that snapshot's
  resource copies. They do not wait for every unread scope to run Fine.
- Public enhancement state still lacks an explicit on-demand-idle/coarse-ready
  signal. Existing recovery checks remain independent of browser notification
  delivery. The user selected SSE, now implemented locally; a truthful activity signal is still
  needed because the transport alone cannot distinguish idle scopes from work
  actually running. The browser SSE path is implemented locally; this remaining
  activity-state limitation is distinct from notification delivery.

Validation on the merged Knowhere tree: 24 PostgreSQL/ASGI API contracts,
65 Worker contracts/unit tests, 21 table/image tool tests, and the focused
46-test asset/evidence suite passed. Expected failing image/table regressions
preceded their fixes. Full Ruff and API/Worker/shared Pyright passed. Hipo's
focused 42 unit and 8 browser reader tests, whole-project TypeScript, and
changed-file lint/format checks passed. These checks establish local seams,
not production rollout, complete UI acceptance or capacity.

### Directory notifications and local refresh (accepted plan)

On 2026-10-29 the user accepted one logical initial-directory-ready notification
per parsed document revision. Reuse Knowhere's webhook delivery to the Hipo
server. Create the notification with the durable coarse-directory state and
dispatch after commit. Delivery retries can repeat an event; Hipo must apply it
idempotently and reject an older document revision that would replace newer
navigation. A new parsing revision can emit its own initial-directory event.
The local implementation names this event `job.document.directory_ready` and
provides a revision- and namespace-fenced `GET /api/v2/documents/{id}/structure`
read. Its stable identity includes owner, namespace, document and parsed revision.

Hipo synchronizes lightweight chapter titles, hierarchy and original-page
coordinates in the background. This operation does not require copying the
original PDF, all images or another complete parsed archive. On 2026-10-29 the
user selected SSE to notify the browser of committed directory and observable
state changes, superseding the proposed browser polling path. The browser reads
the affected authoritative data and updates Tree without losing the current
source page or navigation position. Knowhere-to-Hipo delivery continues to use
the webhook mechanism; SSE serves the Hipo-to-browser hop.

Do not send Hipo one notification for every Fine scope. This delivery exposes
the immutable initial directory and expands its recognized descendants locally.
Later Fine-only heading refresh requires a separate coherent current-subtree
contract and is outside this initial slice; it does not delay the accepted
pre-Fine Tree. On 2026-10-29 the user clarified that Fine serves
retrieval: ordinary PDF reading, directory expansion and navigation refresh must
not trigger it. Retain the existing retrieval-read-driven enhancement boundary.

The primary user-facing goal is prompt automatic Tree visibility. Display the
baseline page navigation when the first snapshot becomes readable. Once
Knowhere commits coarse chapter structure, its directory-ready notification and
Hipo's lightweight synchronization must automatically replace the page-only
navigation with the chapter Tree. Do not wait for Fine, all scopes becoming
refined, a final archive, or a user opening/expanding the directory. Later
chapter expansion is a freshness check, not the initial discovery mechanism.
Preserve the current source page and navigation position during replacement.

The user explicitly accepted the pre-Fine directory depth on 2026-10-29:
show all chapter and child-chapter paths already recognized by the initial
structure pass together. Existing child nodes must not be withheld until a
retrieval triggers Fine. Fine can discover additional headings later, but
matching the eventual fully refined Tree depth is not a requirement for this
delivery. Do not add a separate full-document fine-heading scan just to satisfy
navigation. Documents without a usable recognized hierarchy retain available
page navigation rather than waiting for retrieval enhancement to become readable.

Use SSE as the normal browser change-delivery path. Read current state on initial
connection, reconnection and return to the foreground; coordinate subscription
readiness and that read so a concurrent directory commit is not missed. Treat
notifications as wake-up hints, coalesce duplicates and fence older revision or
directory versions. Keep missed-webhook reconciliation separate from browser
delivery, and do not implement SSE by polling the database per connection.
The user accepted a normal-operation visibility target on 2026-10-29: at most
five seconds from the committed Knowhere initial directory to the updated Tree
actually being visible in Hipo. This end-to-end interval includes notification
delivery, Hipo queue/synchronization, SSE delivery and browser rendering. Do not
restart its timer on webhook receipt or synchronization start. Initial directory
recognition time is measured separately. Also report coarse commit-to-Hipo-
directory-ready and Hipo-ready-to-visible-Tree latencies to locate bottlenecks.
Failed delivery/retry recovery is a separate reliability scenario; retain and
report delayed cases rather than excluding slow intermediate work to claim the
five-second target. This is an agreed acceptance target, not a current measured
result or an already deployed guarantee.

The baseline completion event remains unchanged, and the final completion event
still occurs only if all scopes eventually finish. Webhook retries and bounded
status reconciliation must cover missed delivery. Runtime implementation now
exists locally; live QStash/Vercel delivery and the five-second target remain
unverified deployment and acceptance work.

On 2026-10-29 a read-only check confirmed the delivery-loss boundary: normal
webhook delivery is immediate, while the existing Knowhere orphan sweep handles
persisted PENDING events with zero attempts only after five minutes and runs
every thirty minutes. If commit succeeds but the event is not handed to QStash,
this path alone can take roughly thirty-five minutes plus queue/dependency delay
to recover. QStash's own delivery retries are a separate path. Two existing
orphan-recovery/duplicate-Beat contracts passed; they do not establish recovery
for the new directory event. Hipo's existing enhancement reconciliation also
does not yet synchronize the initial coarse directory independently of Parse
completion, so its existence alone does not close this gap.

On 2026-10-29 the user accepted reusing existing bounded background recovery
and reconciliation instead of adding a dedicated fast-recovery
service or changing global sweep frequency. An explicit directory refresh/retry
for the current document should also check authoritative upstream directory
state and admit or resume its existing lightweight synchronization if ready.
Rereading an unchanged local Hipo row alone cannot repair a missing upstream
notification. Coalesce repeated refreshes against the same revision, retain
available page navigation/reading position and apply normal ownership/version
checks. The operation must not reparse, trigger Fine or restart bulk resource
copying. This supplements the accepted manual refresh behavior; it does not
promise automatic failure recovery within the normal five-second visibility
target or change Worker concurrency.

On 2026-10-29 the user accepted starting with Hipo's existing Next.js/oRPC
streaming capabilities. Share one connection within the current project page
rather than creating a connection for every document or component. Reconnect
with backoff and read current authoritative state after a disconnect or return
to the foreground; close the project subscription when leaving the project.
Stream disconnection does not fail parsing, restart a job or change readiness.

On 2026-10-29 the user accepted the SSE connection-failure experience: reconnect
silently after transient disconnects without an error toast. After sustained
reconnection failure, show a localized "Updates temporarily unavailable /
Refresh" hint in the directory area. Keep the last displayed navigation and
reading position, and do not disable reading or Ask solely because this change
stream is disconnected. Reconnection automatically rereads current state and
updates navigation. Manual refresh reads state; it does not restart parsing or
enhancement. Connection loss is distinct from a failed directory-generation or
directory-synchronization task. Acceptance must cover both a transient closure
with no error notice and sustained failure with the localized hint, then recovery
without losing the current page or applying stale navigation.

Hipo currently uses Next.js 16.4.3 and already has an oRPC streaming Ask
procedure. Next.js officially supports streaming Route Handlers through Web
Streams and `Response`. Vercel-hosted connections inherit function-duration
limits and provisioned-memory billing while requests wait for events. Measure
live-connection memory, listener connections, event bytes, reconnection frequency
and commit-to-visible-directory latency on the actual hosted path, compared with
the existing polling path. Do not assume SSE is cheaper because it removes
frequent empty reads. The normal-operation five-second directory target and
separate reporting of recovery cases remain unchanged.

Keep the existing ECS Gateway as a conditional hosting alternative if the
measured Vercel cost or connection behavior is unsuitable. Reusing its committed
PostgreSQL notification listener and fan-out lifecycle would avoid a new
fixed-capacity service, but it currently exposes MCP transport and Source waits,
not a browser project SSE endpoint. Browser authorization and cross-origin
delivery would need a separate typed adapter. No Gateway migration, new service,
capacity increase or Gateway implementation is selected by this decision.

Retain Hipo-owned typed application boundaries and web-client integration.
Send lightweight source/revision/version change hints,
not full directory, document, image or chat-history payloads on heartbeats.
Reuse committed notifications across replicas and reread durable authorized
state after listener loss or reconnection. An instance-local event bus alone
cannot deliver changes committed by another Vercel instance; verify the shared
notification path across instances and bound database listener resources.
Bound connection queues, coalesce updates and clean up subscriptions on
disconnect, logout and project changes. Cover timed connection closure,
disconnect during a directory commit, subscription/read races, duplicate and
stale hints, authorization revocation, and shared List/Tree subscriptions.
SSE does not add per-Fine-scope Knowhere webhooks or make unreported upstream
activity observable. Verify existing notification coverage and any required
activity contract explicitly. Hosting references:
[Next.js streaming Route Handlers](https://nextjs.org/docs/app/api-reference/file-conventions/route#streaming),
[Vercel streaming duration](https://vercel.com/docs/functions/streaming-functions#function-duration),
and [Vercel function billing](https://vercel.com/docs/functions/usage-and-pricing).

The user accepted the directory-sync failure experience on 2026-10-29: keep
available page navigation and keep the document readable and available to Ask
while directory synchronization retries automatically. Once automatic retries
are exhausted, show a localized "Directory temporarily unavailable / Retry"
action in the Tree area. Do not mark the whole document failed. Manual retry
continues directory synchronization without re-ingesting the source, starting
Fine, repeating publication settlement or charging for parsing again. This
decision covers directory synchronization and uses a synchronization-only retry.

On 2026-10-29 the user also accepted recovery for a failed Knowhere initial
structure-generation task after the baseline is already readable. Keep the
document readable and its existing retrieval available. Retain page navigation
and show a localized "Directory generation failed" message in the Tree area.
After automatic retries are exhausted, use the existing "Retry retrieval
enhancement" action to resume background enhancement. No source re-upload or
restart of baseline parsing is required. This generation retry is distinct from
the synchronization-only retry above.

Acceptance must distinguish a failed directory-generation task, a successful
structure pass without a usable hierarchy, and a failed Hipo directory sync.
Exercise generation failure after first-ready and recovery through the existing
enhancement retry. Verify that reading, existing retrieval, original-page
citations and page navigation remain usable, and that recovery does not restart
baseline ingestion. Local browser tests now distinguish these three states and
the retry actions reuse the separate directory/enhancement paths. Actual hosted
generation failure and recovery remain an end-to-end acceptance item; this is
not a production-release claim.

### Citation location exceptions (accepted plan)

On 2026-10-29 the user accepted a friendly exception interface for PDF citations
whose original-page location is missing or cannot be resolved. Normal baseline,
coarse and Fine PDF citations are expected to retain original physical-page
coordinates; older saved citations with valid source identity and page
coordinates continue to follow the established same-original-page rule.
Their age alone is not an error condition.

Keep the cited passage visible and show a localized "The original location
cannot currently be found" message. Offer the existing retry interaction for
reading/location, and an original-file action when the cited file version and
authorized access can be established. Do not guess a page or select an unrelated
chunk to fill the preview. Retry does not restart ingestion or trigger Fine.
Do not add automatic reparsing, a historical-citation migration or a separate
repair workflow. Non-paginated source formats retain their existing reader
contracts; absence of a PDF page number is not a general citation error.

Acceptance must verify original-page coordinates across normal baseline,
coarse and Fine citation paths, including partial enhancement and snapshot
changes. Separately exercise missing coordinates and an unresolved target,
checking the exception interface and preserving the saved answer. A friendly
exception view is a planned requirement, not evidence of a production citation
incident or completed implementation.

### User experience and stability review (2026-10-29)

The user requested separate user-experience and development/operations reviews.
The read-only review used Knowhere `7e414fbd` and Hipo `d65f80ba`. The findings
below are source observations and proposed acceptance gates, not confirmed
production incidents or authorization to implement product changes. Some risks
are already present in integrated main rather than introduced by this branch.
Keep independent PG4 delivery separate from adoption of the new navigation/SSE
workflow and from the compact-storage research.

User-facing policies confirmed in discussion; implementation and concrete
verification remain:

| Area | Observed boundary | Agreed handling | Status |
| --- | --- | --- | --- |
| Partial-Fine table list | Hipo `chunks-panel.tsx` gates resource access on `displayed.stage !== baseline`, while on-demand documents may never produce a whole-document final snapshot | Hide the standalone table-view option during baseline/coarse/partial-Fine reading. Keep tables visible on original PDF pages and through Ask references; do not build an incremental table catalog for this delivery | Implemented and covered by local browser tests |
| Partial-Fine image list | Extracted-figure browsing has the same current baseline gate, but original-page images are already available for reading | Hide the standalone extracted-image option during baseline/coarse/partial-Fine reading. Keep complete original pages and Ask references available; preserve existing image filtering for a complete displayed snapshot/full parse | Implemented and covered by local browser tests |
| Citation opening and highlighting | `reader-chunks-panel.tsx:140-151` waits for highlight location even when the physical page is already known | Retain the existing wait for location to settle before focusing the citation. Include that work in click-to-target latency measurements; do not introduce the proposed page-first asynchronous-highlighting interaction | Existing behavior retained by user on 2026-10-29 |
| Actual first-read latency | Reader loads the complete chunk index; `parsed-content.ts` reads all chunks before filtering a known physical page | Measure cold/warm click-to-first-page and far-page citation opening, separating index, capability, media and highlight costs. Change the read path only if the measured result requires it | Engineering acceptance; no blanket paging rewrite selected |
| Individual image access failure | Page images can use expiring origin URLs | Refresh access for the same immutable pin with bounded attempts, then allow a local retry. Keep reading position and document availability; do not reparse, restart enhancement or change source version | Implemented; seven browser recovery cases include late-response snapshot fencing |
| Export waiting and fidelity | Export pins a snapshot; replication waiting must remain separate from interrupted-job detection and archive generation | Allow up to 30 minutes for the selected snapshot's resource copies, proceeding immediately when complete. Show the waiting state and an actionable timeout. Keep replication waiting separate from execution and interrupted-job limits; verify that later directory/Fine updates cannot mix snapshots into the export | Implemented; local application and PostgreSQL tests cover waiting, one-time recovery, original deadlines and duplicate-worker settlement |

The user confirmed on 2026-10-29 that the standalone table-view option should
not be shown for the progressive initial/partial-enhancement stage. A source
page remains distinct from an extracted table: the page image, available native
text and original-page citations still expose the table's content. The user
confirmed this presentation: the current reader displays referenced page images;
when a page has no image reference and the source PDF is available, the browser
renders the original PDF page. This is not a promise to switch automatically to
PDF rendering after an image load error. Fine may
produce standalone table resources for retrieval without exposing a partial
table-browsing catalog. Preserve existing table browsing for a complete displayed
snapshot/full parse. This decision does not remove tables from parsing or
retrieval. The user subsequently accepted the same UI boundary for the standalone
extracted-image option: hide that control during initial/partial progressive
reading while retaining whole-page images and original-page citation access.
Preserve existing extracted-image browsing for a complete displayed snapshot/full
parse. Neither decision disables Fine asset extraction or asset use in retrieval;
no incremental image/table browsing catalog is selected for this delivery.

On 2026-10-29 the user chose to retain the existing citation-focus waiting
behavior. Wait for highlight location to succeed or fail before focusing the
citation; the proposed change to open a known page before highlighting is not
selected. Preserve the current fallback that can still open an explicitly known
page after highlight lookup fails. A missing highlight and a missing original-
page location are different conditions; only the latter uses the previously
accepted location-exception interface. Measure highlight/location latency as
part of citation opening without changing this interaction boundary.

On 2026-10-29 the user accepted local recovery for a failed page-image read.
Refresh that image's authorized access for the same immutable snapshot and retry
a bounded number of times. If it still fails, show a localized "Image temporarily
unavailable / Retry" action at that page. Preserve reading position and do not
downgrade the whole document or disable Ask solely because of this image failure.
Retry reads the image; it does not reparse the document, trigger Fine or restart
bulk replication. Keep recovery scoped to the affected image, coalesce concurrent
attempts and stop automatic retry rather than generating continuous requests.
Existing authorization checks remain authoritative. Acceptance includes transient
network failure, an expired URL first used after a long idle period, exhausted
automatic retries and successful manual recovery without changing source version.

Stability and complexity gates for the complete progressive workflow:

1. **Consistent lifecycle lock order.** The review reproduced the previous
   Document/generation lock inversion as an actual PostgreSQL deadlock between
   archive and a scope writer. Archive now obtains an authorized Document write
   lock before namespace generation, refreshing its state after any wait.
   The deterministic two-connection regression passes, as do the surrounding
   document lifecycle contracts. Preserve that order in future lifecycle work;
   revision-replacement and full two-Worker stress remain distinct acceptance.
2. **Real activity and bounded recovery.**
   `shared/services/jobs/lifecycle/scope_enrichments.py` now bounds each recovery
   path at 100 ordered candidates, including exhausted-claim cleanup. Fair
   revision rotation does not hold scope and revision locks in opposite order.
   Queued scopes must age past the existing task lifetime before redispatch;
   fresh running stage claims retain ownership. Read-promoted retryable scopes
   remain recoverable, while untouched coarse scopes with complete required
   stages remain idle. Failed/exhausted stages and final packaging repair remain
   discoverable. Real PostgreSQL regressions cover these policies. Public
   activity reporting still has the explicit metadata limitation noted above;
   neither this fix nor SSE introduces a new public activity contract. Worker
   concurrency, priorities and queue services stay unchanged.
3. **Independent directory-event recovery.** Reuse the existing transactional
   outbox and durable task machinery, with an explicit directory event mapping,
   logical event identity, parse revision and navigation version. The new Hipo
   callback handling bypasses baseline terminal settlement and admits independent
   directory work; Knowhere explicitly maps the new event to completed status
   and gives it a stable logical identity. Deliver consumer and producer in the same maintenance
   window and verify both before restoring normal traffic and task admission.
   Persist directory synchronization independently of already-terminal baseline
   parsing. Late events must respect deletion, move, current-operation and
   revision fences without repeating completion settlement.
4. **Cross-instance SSE recovery and resource ownership.** Prove a commit on one
   instance reaches a subscriber on another. Bound listener connections and
   per-stream queues; coalesce hints and release resources on cancellation.
   Cover listener loss, reconnect/read races, expired authorization and slow
   consumers. Reuse committed shared notifications and durable state, not an
   instance-local event bus or database polling per browser connection.
5. **Concurrent publication and reads.** Existing code already serializes the
   document writer and validates the serving generation across a retrieval
   episode with one retry. Keep those mechanisms. Add two-Worker sibling-scope
   races, boundary/shared assets, rollback, classic and agentic reads, and stale
   cache write-back checks. Measure the latency and work of that bounded retry
   when read-triggered Fine commits during an Ask; do not add a long database
   transaction across model inference or a new distributed-lock service.
6. **Process-loss recovery and useful redundancy.** Terminate a Worker during
   generation, artifact upload, publication and copying. Resume from durable
   inputs on the other Worker; preserve original-page access, leases, revision
   ownership and idempotent settlement. Retain usable baseline reading during
   enhancement/copying failure. Use existing queue, outbox, claims and immutable
   artifacts rather than adding a second publication path or new services.
7. **Truthful completion, rollout and measurement.** Record readability,
   directory readiness, actual work, replica completeness and optional final
   packaging separately. Test baseline to coarse with zero, one and multiple
   Fine scopes; final packaging is required only if all scopes finish. Preserve
   the accepted downtime cutover and artifact-compatible recovery. Report normal five-second Tree
   visibility separately from failure recovery: the existing orphan webhook
   path has a five-minute age threshold and a thirty-minute sweep, so it cannot
   establish five-second recovery. Performance acceptance includes user-visible
   cold/warm reads, query latency, CPU/memory/I/O and durable measurement evidence;
   missing counters are unavailable, not zero.

The user-facing policies in this review are now accepted or explicitly retain
existing behavior. First-read performance and the engineering gates still need
concrete verification; accepted policies are not completed implementation or
release approval. Resolve code-answerable recovery, consistency and resource-
ownership details through engineering review and tests rather than introducing
additional product choices. Return to the user if those findings require a new
user-visible trade-off or a change to the agreed scope. These planning items do
not authorize product implementation, additional services, ingestion parameters,
capacity or concurrency changes. Validate each source risk before attributing a
production regression or broadening a refactor.

#### Code confirmation: Worker loss during directory generation

A read-only check on 2026-10-29 used Knowhere `7e414fbd` and confirmed the
existing coarse-structure recovery path. The scenario starts after a readable
baseline is already published; it does not certify initial-parse recovery or
Hipo replication recovery.

- Both Worker replicas consume the structure and recovery queues. The periodic
  progressive sweep runs every 300 seconds and can redispatch unfinished
  structure work. Structure claims remain owned until
  `TASK_TIME_LIMIT_SECONDS`, currently 3,600 seconds, has elapsed from claim
  acquisition. A hard process loss soon after acquisition can therefore leave
  directory generation waiting for about an hour, plus sweep/queue delay.
  A five-minute sweep is not a five-minute takeover guarantee. The one-hour
  claim rule already exists in integrated source; this optimization did not
  introduce it. A handled exception can release its claim earlier.
- The replacement task downloads persisted Phase B inputs through ResultStorage,
  including the source PDF and saved baseline features/text. It starts a new
  task-local workspace, reruns unfinished structure generation and uses
  claim-specific artifact paths. This is stage recovery, not a checkpoint after
  every model call; it does not guarantee reuse of every partially computed
  directory or embedding result.
- Coarse publication locks the Document and JobResult, verifies the active
  revision and current structure claim token, and atomically publishes structure
  with its completed state. A stale claim cannot publish the directory over a
  replacement claim. Enrichment failure does not finalize the already-completed
  baseline Job as failed. These observations concern this publication boundary,
  not a blanket proof that every artifact/vector side effect is fenced.
- Four existing local contracts passed: enrichment routing/baseline failure
  isolation, redispatch of text-stage revisions, baseline-to-coarse publication
  with duplicate-claim suppression, and rejection of an exhausted structure
  generation. PostgreSQL was isolated; storage/model/broker boundaries were
  substituted. No two real Workers were killed or restarted, so actual failure
  detection, takeover latency and end-to-end media availability still require
  the planned process-loss acceptance.

On 2026-10-29 the user accepted retaining the existing one-hour claim-expiry
threshold for this delivery. Do not shorten that interval, add a new recovery
service or introduce finer-grained checkpoints. Keep the actual two-Worker
process-loss acceptance above; code review and substituted contracts do not
establish immediate takeover or complete failure recovery.

#### Code confirmation: duplicate completion and directory notifications

A read-only check on 2026-10-29 used Knowhere `7e414fbd` and Hipo `d65f80ba`.
Hipo already ignores repeated baseline-completion callbacks after Parse success.
Usage reconciliation locks the same reservation, returns when already reconciled
and records usage under a unique operation key. Synchronization admission locks
the Source and reuses an operation with matching document/revision identity;
claiming an operation with a live lease does not start another copy segment.
These mechanisms prevent treating notification delivery as a new parse request.
They do not imply that every delivery or object PUT occurs exactly once.

Before this implementation, Hipo's callback schema accepted only completion,
final-enhancement completion and failure, and its terminal-Parse guard would
discard another ordinary completion hint. Knowhere's outbox defaulted unknown
event types to failed status with a fresh UUID per row. The implementation now
adds explicit directory-event handling on both sides and a deterministic event
identity; it does not rely on an event name alone for uniqueness.

On 2026-10-29 the user confirmed the following duplicate-notification policy.
Reuse the transactional outbox and durable synchronization path, adding explicit
directory handling. Persist the one logical initial-directory event identity
with the committed parsed revision, retain it across delivery retries and fence
application by document/revision/navigation version. Hipo needs a separate
directory-sync checkpoint after baseline Parse success. A duplicate while sync
is pending or failed must resume the admitted work rather than being discarded
solely because the event was received before. A duplicate after successful
application must not repeat settlement, enqueue another active sync or replace
newer navigation. Synchronize the lightweight directory without re-ingestion,
Fine, additional parsing charges or a full snapshot-copy restart.

The earlier read-only check ran nine existing Hipo webhook/synchronization unit
contracts and did not yet establish the new directory event's behavior. The
implementation checkpoint above now includes PostgreSQL contracts for duplicate
concurrent delivery, independent admission/leases, failed-directory retry and
reordered revision/deletion/move fences. No new deduplication service was added.

#### Code confirmation: retrieval during background publication

A read-only check on 2026-10-29 used Knowhere `7e414fbd` and Hipo `d65f80ba`.
Knowhere already captures the namespace serving generation, validates it across
the retrieval episode and retries the entire episode once when publication
crosses a serving read. A second unstable episode raises the existing conflict
instead of returning the potentially mixed evidence. This protection predates
the proposed publication optimizations. Fine publication advances that same
namespace generation transactionally; it does not retain a long-lived database
snapshot through model inference. Validation is at namespace scope, so a
publication elsewhere in the queried namespace can also invalidate the episode.

Nine existing PostgreSQL contracts passed: no change, one publication and two
publications across discovery, hydration and agent-tool boundaries. They use a
real concurrent writer and substituted model/cache boundaries. The writer
replaces the complete revision scope; these tests do not establish the remaining
two-Worker sibling-Fine, sustained-load, cache-coexistence or latency acceptance.
An episode retry can repeat model/tool work and increase latency; no measured
overhead or production conflict frequency is established by these contracts.

Hipo currently carries retrieval-side failures into Library Search failureReason,
which the Ask Agent can observe. The direct retrieval adapter reduces an HTTP
failure to a generic status message, so this review does not establish a stable
user-visible "document updating" message or that every conflict fails the whole
Ask Run. Do not equate a failed retrieval call with a normal empty result.

On 2026-10-29 the user accepted choosing the simplest existing error-handling
path. Retain the one automatic episode retry. If existing error information can
identify a publication conflict without a new error-transport contract, the
specific message may be "Documents are being updated. Please try again shortly."
Otherwise reuse the existing service-unavailable handling and a "The service is
temporarily unavailable. Please try again shortly." message. Do not add an API
or SDK change solely to carry the more specific wording. Preserve the question
and conversation, and do not represent a retrieval failure without usable
document evidence as a successful empty search. Do not classify every HTTP 409
or transport error as a background-update conflict. No additional unbounded
automatic retries, new publication path or concurrency change is selected. How
other independently successful evidence is handled remains under the existing
evidence contract; this decision does not silently disable it or declare every
mixed-source Ask Run failed.

#### Code confirmation: deletion during background enhancement

A read-only check on 2026-10-29 used Knowhere `7e414fbd` and Hipo `d65f80ba`.
Hipo's Source deletion checks unfinished upload/Parse execution, then calls the
Knowhere document-archive adapter before committing its local deletedAt value.
Failure to confirm upstream archival leaves deletion unsuccessful; do not show
success solely because an archive request was attempted. Readable Parse success
is already terminal independently of later Fine or resource replication, so
remaining background work is not itself a new deletion blocker.

Knowhere structure/Fine publication locks the Document and checks active status,
current revision and the task claim before publishing. Hipo copy claim/finish
checks Source/Project ownership and copy authority; publication authority also
requires a live current Source and attempt. A late callback or task must not
resurrect a deleted Source, update its Tree or return it to the retrieval pool.
The new directory notification must preserve these same checks. Independently
owned Archived Evidence from an earlier Source move can still require copying;
its existing retention rules are not a deleted Source's display authority and
are not changed by this optimization. Deleting its owning Project removes that
copy authority.

Fourteen existing Hipo Source-edit and archive-adapter unit contracts passed,
including local HTTP vendor-response checks. Existing database copy/ownership
contracts were inspected rather than rerun. The static opposite lock order of
Knowhere archive and publication remains under the earlier dedicated race gate;
these tests do not prove that race deadlock-free or validate task termination.

On 2026-10-29 the user accepted retaining the existing cancellation boundary.
A queued enhancement checks validity before starting, while an already executing task may
continue until its guarded publication boundary and must discard stale output.
Hipo copy segments can likewise finish in flight without permission to publish
or start another unauthorized segment. Do not add immediate cross-Worker model-
request termination or hard cancellation solely for this delivery. This saves
implementation complexity but permits some post-deletion model/CPU/I/O work;
it does not promise immediate resource reclamation or physical asset deletion.

### Publication optimization after the orchestration change

Source review confirms that each Fine scope update still rebuilds all map units,
token rows and BM25 statistics for the document's current revision. This full
revision algorithm predates the new main changes. Main now invokes it for each
read-driven Fine publication and also patches the aggregate namespace MAP and
advances generation on each scope publication. On-demand Fine may process fewer
scopes, so source review alone does not establish higher total document cost.

The next bounded development candidates, before another large benchmark, are:

1. Plan to retire aggregate namespace MAP maintenance. The user accepted
   superseding the old aggregate-specific parity/atomicity requirements and
   updating operational checks, rather than preserving a copy without an
   identified current-main business reader. Existing backfill/readiness and
   benchmark consumers must be updated in the same future change. Keep per-
   revision manifests, generation locking/advancement, after-commit cache
   invalidation, and all user-visible reading/retrieval/citation guarantees.
   Retain the existing tables and historical data initially. This is an approved
   plan direction, not implementation authorization or a measured speedup.
   Current outline reads live sections; older deployments and any selected
   rollback release still need a bounded compatibility check. The pending
   shared-demo documentation PR 458 must reflect retirement before merging
   wording that still describes aggregate maintenance as current behavior.
2. If token DML remains dominant, keep the complete authoritative map-unit
   constructor and BM25 statistics but reconcile logical units. Retain unchanged
   unit IDs/token rows; replace changed/removed units. Do not filter solely by
   scope ID: ancestors, interstitial units and shared boundary assets can change
   across scope boundaries. This reduces writes, not the whole-revision CPU pass.
3. Cover same-revision A-to-Fine/B-coarse updates against a full rebuild, equal-
   count content changes, rollback, sequential/concurrent sibling updates,
   generation/cache invalidation, shared assets and exact source-page parity.

These are planning items, not implemented optimizations or measured savings.
Token reconciliation remains an unproven candidate: reading/comparing old data
can outweigh avoided writes, especially when most units change. A future bounded
same-input comparison must measure complete transaction time, reads, writes and
WAL for a small scope change, a boundary/ancestor change and a broad change,
while checking exact retrieval parity. It does not replace first-publication
index construction and must not be counted as a first-ready saving.
These items do not add comparison arms to the approved twenty-fresh-publication
screening or change its twenty-one-call cumulative limit.
Oracle continues using frozen source epoch `c61a48c78ea2`; local main integration
does not replace its source. Its revision-replacement component results cannot
establish the cost or correctness of new same-revision on-demand Fine updates.

### Oracle screening history before completion

Historical checkpoint on 2026-10-29, before collector-v2 recovery: authorized cleanup allowed the prepared
`other3m` cache to be created and validated. Its size is 6,667,013,388 bytes;
resources and disk capacity were not increased. The resumed c61 controller made
one real PG4 cold publication, which committed the expected 188 units and 28,917
frequency rows. Its post-commit telemetry validation then failed. Thus one of
the initial twenty-call allowance was consumed, but zero complete publication
reports are available. Do not replay that committed call or reconstruct its
missing wall/process timings from cumulative database statistics.

Read-only diagnosis found that an invalid pg_stat_statements interval was treated
as zero INSERTs by the runner. Statement eviction is the strongly supported
trigger; the original before/after window was not persisted, so that exact
window cannot be recovered. A separate local collector-v2 candidate now retains
raw/timing/commit evidence before validation, reports invalid counters as
unavailable, and proposes an explicitly audited untimed per-arm statistics reset.
Its candidate tests and static checks pass; it is not deployed, and no additional
publisher or actual statistics reset has run. The old source/cache identity and
failed-call evidence remain intact. Keeping all ten cold/warm pairs would need
explicit approval of a total twenty-one-call budget, which the user subsequently
granted on 2026-10-29 as recorded below. These screening results still do not
establish production capacity.

The final local collector-v2 review on 2026-10-29 closed all five original
findings. The complete candidate tree was independently checked against 489
identity entries and the preserved anchor against 482, with no byte/hash
mismatches. The candidate source identity is
`b5db29a72c4e92e0edb0d335a70eed1f6f2a39a3d2d8c98c3599ca2eed46bf49`;
recovery plan SHA is
`0de17eb3e7103a4383ecee200a48de983c5d5fbf4f1a35142eb99b126be830ad`.
Reported local verification is 119 tests plus 66 subtests, Ruff and Pyright;
independent review verified the final plan/proof/checkpoint/approval-template
bindings and the six-file corrective diff. Root review inspected the final
approval/reservation guards, evidence-on-failure path and unchanged 20-sample
matrix. This completes local technical review, not ARM/Oracle integration.

On 2026-10-29 the user explicitly selected continuation A: retain the failed
committed sample separately and execute twenty fresh samples for the original
ten cold/warm pairs, for at most twenty-one cumulative calls. Keep the production-
dump clone, 188-page parsed fixture, existing layouts, two growth scenarios,
resource limits, concurrency and disk capacity unchanged. There are no warmup
publisher calls or added comparison arms. The nineteen-fresh-call alternative,
which omits the other-namespace PG4 warm baseline, was not selected. The user
authorization covers staging the reviewed measurement/recovery candidate and
resuming the Oracle experiment after real-path, ARM, resource, disk and scoped-
credential checks. It does not authorize product implementation or production
deployment. No successful remote preflight, launch or new measured sample is
implied by recording approval. Final local review evidence is under
`.benchmarks/compact-namespace-oracle-20261028/collector-epoch-candidate-20261029/review.json`.

After that approval, the Oracle recovery passed actual ARM preflight: original
dump and prepared-cache SHA/guards, complete staged source identity, both native
module imports, PostgreSQL/Valkey checks, prefix-scoped S3 list/HEAD access and
unchanged resource limits. The supervisor and a single remote controller were
launched, with the controller owning the existing shared lease. At the initial
running handoff, no fresh publisher call had yet been reserved or completed;
the prior committed/telemetry-failed call still consumed one of twenty-one.
The controller was verifying storage before restoring the existing other3m
cache, without rebuilding its growth/seed preparation. This establishes
successful preflight and launch, not a new measurement result or completed PoC.

The first PG4 cold/warm arm subsequently produced two publication reports, but
the recovery controller failed to retain arm progress in its checkpoint. Source
review confirmed that reservePublication deep-copied the checkpoint and runStep
replaced its root while runMatrix still held references to the previous nested
cell/arm objects. Sampling was paused for evidence verification and a minimal
checkpoint-reference repair. Preserve the two actual calls and their original
source/report hashes; do not replay them because the progress counter is zero.
The conservative cumulative count is the original failed call plus these two,
three of twenty-one. Report and checkpoint reconciliation, technical review and
resumption were pending at that stop. This is an experiment-controller issue,
not evidence of a product publisher/SQL failure or authorization to change the
tested work.

The minimal repair subsequently passed independent technical review: update
only the budget subtree so runMatrix retains its cell/arm references. A real
coordinator-order regression covers completion, persisted restart, skipping the
completed arm and entering the next arm. All four PG4 safe reports, two raw
publication records, two claims and original approval evidence retain their
original b5db identities/hashes. The successor helper has source identity
`235e3c28968024b4e703bc8a7a30705938afed7e32385114f593796b083ec9f1`
and plan SHA
`588f4e3d13fa11c020c354e351b2cdd108c1b998ee2dbfd7db196fd8195015c7`.
A new controller passed the original guards, acquired the shared lease, skipped
PG4 and began the existing cache restore for compact_pg_global. At that handoff
the checkpoint contained two complete publication reports and two validated
reservations, cumulative three of twenty-one, with eighteen new calls remaining
and no replay. The PG4 core publication durations were 10.937 seconds cold and
5.966 seconds warm; both retained the required 29 token INSERT calls and 28,917
rows. These are single-sample Oracle publication measurements, not full PDF
ingestion timings, comparative compact-storage results or production capacity.

The following paragraphs retain the earlier cache/disk recovery history rather
than describing the latest stopped state.

The remaining full matrix was replaced, rather than declared complete. The
90-sample continuation and queued 108-sample supplement were stopped. The four
original base0 groups completed 24 measured publications; their evidence and
takeover proof were retained. The reduced controller holds the same exclusive
database lease during execution. At the initial cache-related stop, the first
`other3m` cell completed growth/index preparation, background packing and S3
upload, but physical-cache creation hit its disk-headroom guard. The partial
archive was cleaned automatically; the prepared database/seed tables and
original evidence remain. Zero of the 20 new publisher calls ran. The controller
is stopped with its lease released; continuing requires a separate capacity or
cache-plan adjustment. Do not restart or change the frozen protocol implicitly.
The screening does not require or fabricate an original-210 completion certificate.

A read-only subagent follow-up on 2026-10-29 reverified all 482 frozen identity
entries and the protected original archive SHA. Prepared PGDATA occupies
15.08 GiB and only 4.08 GiB remains on its sole persistent filesystem; the
one-GiB reserve leaves at most 3.08 GiB for a prepared archive. No cache manifest
was committed, so restarting the controller directly would repeat preparation.
The original 24 samples remain separate; screening is still 0/20 with no extra
warmup publications. Target-namespace growth preparation has not begun.

The user explicitly rejected disk expansion on 2026-10-29 and authorized a
cleanup of regenerable caches, temporary artifacts and controlled obsolete logs
on the Oracle host. Disk expansion is not an allowed continuation plan. Preserve
the original database archive, source files, parsed inputs, historical evidence,
frozen source identity and prepared PGDATA/seed state. Do not remove PostgreSQL
WAL or live database files. Record cleanup batches and actual free-space gains;
report any remaining cache-space shortfall rather than resizing implicitly.
An external-cache transport remains only an unapproved alternative: it would
move a complete production-derived database copy and needs explicit storage
authorization and a separately identified controller change. Continuing still
requires an audited cache-checkpoint handoff and renewal of the expired
prefix-scoped S3 session. Keep resources/concurrency and disk capacity unchanged,
and do not pool new screening samples with the original 24.
The background payload is only 0.517 times the two-GiB reference working set and
is explicitly not a cache-pressure proof. No disk expansion, protocol change,
database transfer, service restart or resume was performed by the read-only
follow-up. The newly authorized cleanup does not authorize product-code changes
or silent changes to the frozen comparison protocol.

The authorized cleanup completed in four audited batches on 2026-10-29 without
expanding storage. Batch-level available-space gains total 3,230,478,336 bytes
(about 3.01 GiB); the final recorded root-filesystem availability is
7,613,140,992 bytes (about 7.09 GiB), compared with about 4.08 GiB beforehand.
Removed only an unreferenced rootless Docker image, regenerable uv/APT/npm/Go
caches, unreferenced pnpm store objects and three inactive old NPX dependency
directories. Rootful images, installed tools, dependency lockfiles, recent logs,
validation evidence and all protected campaign inputs/state were retained.
The Snap cache was left alone because its installed-file hardlinks would not
release space. Per-batch audit JSON is retained under the campaign's safe
directory as `disk-cleanup-20261029-batch01.json` through `batch04.json`.
No cache recreation, service restart or new publication sample is implied by
this cleanup. Cache creation still must pass its original one-GiB headroom
guard before any audited continuation; do not claim the archive fits merely
from the estimated compression ratio.

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
Deliver readable-marker readers and producers together during the maintenance
window. Verify already-published snapshots before reopening traffic, and preserve
access to their data if the release needs recovery.

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

The user accepted a 30-minute resource-replication waiting limit on 2026-10-29.
Start that budget when the export is requested and proceed as soon as the pinned
snapshot's copies are complete. Reuse the existing durable workflow waits rather
than keeping a Vercel request open. Separate this waiting budget from actual
archive execution and interrupted-worker detection; preserve the existing
270-second archive-generation timeout. This compatibility work requires copying
and completeness checks before the existing export packaging, without reparsing
or waiting for additional Fine enrichment of the document.

On 2026-10-29 the user accepted automatic recovery when an export needs resource
copies whose synchronization has already failed and stopped. Resume copying the
export's pinned snapshot once through the existing durable synchronization path;
reuse an active synchronization operation rather than starting a duplicate. Do
not reparse, trigger Fine or charge for parsing again. This recovery must not
reset the export's original 30-minute waiting deadline. Duplicate deliveries or
status checks must not repeatedly renew the failed copy attempt. If recovery
fails again or the deadline expires, expose the existing actionable export
failure rather than waiting indefinitely or omitting that document.

## Work package 4: retire the legacy token lookup

Status: existing operator/rollback and Oracle correctness/performance checks
complete. Further implementation and operational activation are on hold until
the current Oracle PoC concludes and its results are reviewed.

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

The subsequent complete owning-publication comparison
(recorded separately in `four-index-publication-validation.md`)
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

The current discussion has resolved the required user-facing choices. No new
mandatory product decision was identified in the final planning pass. Remaining
technical findings are implementation/acceptance work; return to the user only
if evidence requires changing an agreed behavior, resource boundary or delivery
scope. Oracle storage screening remains separate and does not select a new
production storage design implicitly.

The first remaining cross-repository implementation slice is automatic initial
directory availability:

1. Knowhere commits the initial-directory logical event with coarse structure
   and exposes the existing authorized outline with sufficient revision/version
   identity. Preserve baseline completion and do not emit per-Fine webhooks.
2. The Node SDK carries the required event/outline types and access without
   introducing a new ingestion parameter.
3. Hipo admits an independent, idempotent directory-sync operation, supports
   missed-event reconciliation/manual refresh and sends a lightweight project
   SSE hint after the directory is durable. Tree shows the recognized children
   while preserving the current source page and navigation position.
4. Verify duplicates, stale revisions, deletion, failed sync/resume, missed
   notification and the complete five-second normal commit-to-visible path.
   Reading or expanding the directory must not trigger Fine or full asset copy.

The user authorized this implementation slice and the other already agreed
non-publication policies on 2026-10-29. Development is in progress; it is not
completed release acceptance. Publication optimization remains deferred pending
the Oracle PoC. Other accepted presentation/export changes and engineering gates
remain required in the work packages above before release acceptance.

The remaining work includes the newly agreed directory-event, independent
directory-sync, browser SSE, error/retry presentation and export-wait policies,
followed by release preparation, operational activation and complete acceptance.
Those product runtime changes are now authorized for development; earlier local
verification does not certify their implementation.
No new storage engine is selected. The release dependencies below remain
engineering requirements rather than additional product switches or ingestion
parameters. In dependency order:

1. Refresh the three repositories against their target branches, review the
   resulting trees and run the relevant checks. Open one reviewable PR per
   repository; existing compatibility/activation commits may remain as review
   history within the same deployed release. Include the matched downtime
   deployment order, artifact-compatible recovery boundary and evidence in
   Knowhere's review description. Keep publication/index experiments outside
   these runtime PRs.
2. Keep token retirement deferred. The separate index-only rollback audit does
   not certify parser artifact compatibility, and this non-publication release
   neither activates that operator nor drops an index through migration/startup.
3. Use the accepted maintenance window for one matched deployment of Knowhere
   APIs and both Workers, including compatible origin readers, native probe
   deferral, duplicate working-image omission, serving prefixes and batch access.
   Stop old Workers before new-format work can run. Recheck cross-Worker
   continuation, page/Agent image reads and publication before reopening traffic.
   Index activation remains deferred; do not change concurrency or minimum
   database capacity.
4. Publish the reviewed Node SDK minor release, update Hipo from its current
   `2.7.1` pin to the actual registry version and regenerate the lockfile. Rerun
   the real HTTP/Blob composition and relevant regression checks against that
   package. Include Hipo's readable-marker/resource/export consumers, required
   migrations and prepare-only synchronization producer in the same matched
   maintenance-window cutover. Verify readable snapshots before reopening traffic.
5. Reacquire the exact 188-page and 407-page original PDFs using the recorded
   SHA-256 identities. The frozen 188-unit parsed output is available and was
   used for publication experiments; it cannot substitute for an original-PDF
   parser/transfer benchmark. Run full mixed flows through two Workers in
   different namespaces, covering parsing, retained page rendering, archive and
   artifact upload, token/vector writes and concurrent retrieval across baseline,
   coarse and zero/one/multiple Fine scope publications. Exercise final packaging
   only when all scopes are refined. Include duplicate/reordered events, Worker termination/resume
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

### Downtime deployment and recovery

On 2026-10-29 the user selected downtime deployment instead of a separate
compatibility release followed by an activation release. Ship the required
readers and optimized producers in one matched release. Do not add rollout
switches, public ingestion parameters or another compatibility deployment.
Existing local commits may remain separate for review; they do not require
separate production deployments. This is a planning decision, not permission
to stop production services now.

Prepare and verify the release artifacts, publish the reviewed SDK and pin its
actual registry version in Hipo before the maintenance window. The checked Hipo
tree still pins 2.7.1; a local SDK link is not a deployable dependency. The new
fast path must pass composition checks against the published package.

During the maintenance window, pause relevant request/task admission, let
currently executing work finish where practical and stop the old Workers.
Preserve durable queued work and checkpoints; do not wait for unread Fine scopes
to finish or clear the queues. Apply required migrations and deploy matching
Knowhere API/Worker and Hipo versions together. The release includes immutable
origin readers, completed-probe recovery, native first-pass deferral, omitted
duplicate image uploads, serving prefixes, batch access, the new directory event,
SSE and Hipo readability/export handling. Verify these paths and existing data
before restoring traffic and the same two Workers at their existing concurrency.

Keep the maintenance window closed if cutover checks fail. Preserve durable
tasks and artifacts during recovery. Before any new production-format output is
admitted, a tested previous version can be restored where schema compatibility
allows. Once new-format production artifacts exist, recovery must preserve their
readability: repair/deploy matching code or use a specifically verified rollback
target. Downtime does not authorize deleting new user data or restoring an older
database over it. No extra predeployment compatibility release is required.
The older v1.2.34 index-only rollback audit does not certify parser artifact
compatibility; token-index restoration remains its separate guarded operation.

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
These commits can be delivered together in the accepted downtime cutover;
their historical separation is not a two-deployment requirement. The SDK local
implementation is `22d637a`; its minor changeset is not a published version.
Hipo still pins the published 2.7.1 dependency, so its new path remains a local
new-SDK verification until publication, registry pinning, and staged deployment.
No code was pushed, deployed, or released, and no production DDL was run.
