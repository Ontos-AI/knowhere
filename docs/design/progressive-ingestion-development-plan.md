# Progressive ingestion development plan

Status: In progress; requirement discussion remains open as of 2026-10-27.
Implemented and locally verified subsets do not close the overall requirement
or establish final product acceptance. SDK publication, dependency pinning and
deployed acceptance remain pending. Measured mixed-workload capacity evidence is
recorded separately in the [capacity follow-up](progressive-mixed-capacity-validation.md).
See the [implementation report](progressive-ingestion-implementation-report.md)
for earlier implementation evidence.

The agreed behavior and investigation evidence are recorded in the
[hardening plan](progressive-ingestion-hardening-plan.md) and
[ADR index](../adr/README.md). This document defines the development sequence.

## Discussion checkpoint: first-snapshot reading (2026-10-27)

The user explicitly keeps this requirement unfinished and wants to continue
discussing it. Retain the following two points as the current checkpoint:

1. **Page assets and List reading.** Text-native PDF/PPTX baselines do not
   pre-generate per-page image assets or native-document visual indexes. They
   retain page text and immutable `source.pdf` (the converted PDF for PPTX).
   Scanned/textless visual baselines already carry page images. Ordinary List
   reading must display original native pages from that retained PDF on demand,
   not only when opening a citation. Share the selected PDF document/worker and
   paint nearby visible pages; do not wait for VLM enhancement or render the
   whole document during initial ingestion. This follow-up is implemented in
   Hipo commit `787846ed`, with local verification; implementation is not final
   acceptance or deployed-client evidence.
2. **Tree reading.** The baseline is not empty: show the document root and
   source-ordered `Page N` leaves, with navigation to the corresponding List
   page. It does not yet expose the enhanced chapter/resource hierarchy.
   Images/Tables remain visible but disabled until final synchronization.
   Final publication replaces the flat page tree with the chapter/resource tree
   while preserving List/Tree mode and source-page position. Retained citations
   continue to read their own immutable snapshot. This behavior is implemented
   and locally tested; overall scope and acceptance remain under discussion.

Continue requirements discussion from these points. Do not describe the overall
progressive requirement as complete merely because this subset's tests pass.

## Target behavior

- Keep `ingestion_mode=full|progressive`, defaulting to full. Hipo selects
  progressive for supported PDF/PPTX inputs; other formats retain full ingestion.
- Publish a readable baseline before full enhancement. Text-native baselines
  expose page text and immutable source PDF for on-demand List page display;
  scanned baselines expose complete required page images.
  Missing page vectors do not block initial completion and are repaired later.
- One business Job uses multiple Celery tasks. After baseline publication the
  Job stays done while structure, scope refinement, and vector repair continue.
- Keep the baseline retrieval corpus unchanged throughout background enhancement.
  Store scope outputs and repaired vectors under immutable claim-specific keys.
  After every required stage and the final archive succeed, replace the live
  document content/index once and admit the final snapshot/event in that transaction.
  This supersedes intermediate scope publication for newly admitted revisions.
- Hipo becomes ready after its baseline content/assets commit, then immediately
  includes the Source in Ask. Search coverage may be limited during enhancement;
  a required query-embedding failure still fails the retrieval request.
- Retain immutable baseline and final snapshots. Final success requires completed
  text/structure work, repaired missing vectors, and a durable final package.
- Reuse the existing callback for `job.completed` and `job.enrichment.completed`.
  Final success does not create another ingestion or repeat its usage charge.
- Hipo keeps its baseline visible until final synchronization commits, then
  updates once while preserving List/Tree mode and source-page focus.
- Enhancement failure preserves reading and Ask availability. Manual retry is
  offered only after automatic retries are exhausted.
- Reuse ten-minute Hipo server recovery and fifteen-second visible-client local
  status checks. Stop client checks on completion or terminal failure.

## Sources and working rules

| Repository | Inspected baseline | Ownership |
| --- | --- | --- |
| Knowhere | `6ddea434d`, merged PR #471 | Lifecycle, parsing, publication, retrieval, API and webhook contracts |
| Knowhere Node SDK | `0b30e2b25`, SDK 2.5.0 | Immutable artifact transfer, checkpoints, local snapshot identity |
| Hipo | `d0127c1d9` | Source lifecycle, synchronization, Ask, reader and retry interaction |

At implementation start, check current main in each repository and reconcile
drift before applying these changes. Preserve unrelated checkout work. Use
owner-specific branches, such as `fix/wangbinqi/progressive-recovery` and
`feat/wangbinqi/progressive-reader`. Use uv/pnpm and existing repository checks.
Follow Hipo's Effect service/repository and typed-error patterns. Add new
migrations where necessary; never edit an existing migration.

In Knowhere file lists, `shared/` means `packages/shared-python/shared/` and
Worker `app/` means `apps/worker/app/`. SDK and Hipo paths are relative to their
respective repositories.

## Contracts to establish first

### Lifecycle and persistence

Keep three separate state dimensions:

| Dimension | States or milestone | Source of truth |
| --- | --- | --- |
| Initial Job | Existing pending/running/done/failed lifecycle | Existing Job lifecycle |
| Upstream enhancement | Pending/running/complete/failed, plus structure, scope, vector-repair and finalization progress | Typed progressive record on the Job Result, existing per-scope rows |
| Hipo publication | Baseline committed; final pending/syncing/committed/failed | Durable Hipo synchronization/enhancement state |

Use the existing Job Result metadata for compact typed stage progress and missing
page identities. Keep per-scope state in `document_scope_enrichments`. Each
recoverable work item needs attempts, next retry eligibility, an expiring claim,
and a publication fence. Add a claim-generation field to existing scope rows if
the current attempt counter cannot safely fence publication across manual retry.
Keep database transactions short and outside model calls or object transfers.

Use the existing three-attempt scope budget as the starting task-level policy.
Provider request retries remain separately bounded. Configuration/authentication
failures become actionable enhancement failures without endlessly retrying or
failing an otherwise viewable baseline. Manual retry renews unfinished work,
without resetting completed scopes or ingestion settlement.

### Snapshot descriptor and API

Use one Job and one Job Result, with two independently identified artifacts.
The descriptor must include:

| Field | Meaning |
| --- | --- |
| `snapshot_id` | Opaque immutable identity of one published reader snapshot |
| `stage` | Baseline or final |
| `job_id`, `job_result_id`, `document_id` | Existing upstream identities |
| `checksum` | Integrity identity of the committed result package |
| Archive location and signed URL expiry | Durable internal key and refreshable download capability |
| Required resources/manifest identity | Coherent content and assets belonging to this snapshot |

Persist baseline/final descriptors and a current-result pointer. Use separate
immutable archive/asset keys; never replace baseline bytes at the same retained
location. The upstream `job_result_id` remains distinct from the local SDK/Hipo
snapshot key. Existing result fields remain compatible for full-mode callers.

| Interface | Planned change |
| --- | --- |
| `GET /v2/jobs/{job_id}` | Add enhancement state/retryability and published snapshot descriptors; retain existing fields. Fresh reads can refresh signed URLs for retained descriptors. |
| `POST /v2/jobs/{job_id}/enrichment/retry` | New authenticated command with no new ingestion selector. Return 202 with the accepted/current retry state; duplicate requests share the active attempt. Reject superseded, unauthorized, or ineligible operations through existing API error conventions. |
| `job.completed` | Preserve initial completion; identify its durable baseline publication. |
| `job.enrichment.completed` | Identify the durable final snapshot. Persist with final completion, then dispatch through the existing outbox. |
| Hipo existing Source retry entry point | Select enhancement retry or local sync retry from persisted state. Keep the existing initial parse retry behavior for initial failures. |

The final webhook carries event, Job, Document, Job Result, snapshot and checksum
identity. It does not depend solely on a short-lived signed URL. Authenticate,
read back authorized state, and deduplicate by logical result identity. Emit no
new failure webhook: Hipo's existing recovery workflow observes enhancement state.

## Work packages and dependencies

| Package | Repository | Depends on | Deliverable |
| --- | --- | --- | --- |
| C0 | Knowhere | None | Typed lifecycle/snapshot contracts and additive persistence |
| K1 | Knowhere | C0 | Recoverable task dispatch, fenced publication and post-commit cache invalidation |
| K2 | Knowhere | K1 | Missing-vector repair, immutable artifacts and recoverable finalization |
| K3 | Knowhere | K2 | State/retry API, two completion events and retrieval error propagation |
| S1 | Node SDK | C0; server integration after K3 | Generation-aware transfer and retained snapshot reads |
| H1 | Hipo | K3 + S1 | Durable enhancement/sync lifecycle, webhook/recovery/retry integration |
| H2 | Hipo | H1 | Reader, Tree, version notices, early Ask and browser status checks |
| V1 | All | K1-K3 + S1 + H1-H2 | Failure injection, real Hipo acceptance and resource evidence |

### C0: Define state and artifact identity

Primary files: Knowhere `shared/models/schemas/job.py`,
`shared/models/schemas/job_metadata.py`, `shared/models/database/document.py`,
Job Result projection and read models under `apps/api/app/services/jobs/`.

1. Introduce precise typed enhancement progress and snapshot-descriptor models.
2. Persist the canonical progressive state and both artifact descriptors; expose
   additive API output. Old full-mode results remain readable without these fields.
3. Add claim fields with a new migration where required. Define atomic admission,
   lease expiry, stale-revision rejection and terminal retry eligibility.
4. Correct the progressive request description: full-document enhancement is
   automatic, with priority promotion for accessed scopes.

Done when schema/read contracts cover old full results, a baseline-only result,
pending/failed enhancement and a final snapshot, without changing Job identity.

### K1: Recover background work and publish safely

Primary files: `shared/services/jobs/lifecycle/scope_enrichments.py`,
Worker `app/services/page_memory/scope_enrichment_service.py`,
`app/core/tasks/enrichment_tasks.py`, and
`shared/services/retrieval/publication_scope_content.py` with its callers.

1. Recover the commit-before-broker-publication gap for queued scopes through
   the existing sweeper. Duplicate broker delivery remains safe.
2. Carry a claim generation into publication and failure transitions. Reject
   late results from a recovered or superseded claim, including finalization.
3. Extend existing recovery to persisted unfinished stages; do not create a new
   workflow framework or a separate Worker service.
4. Move namespace cache invalidation after successful SQL commit.
5. Stage background structure, scope content and repaired vectors without touching
   live content or query caches. Rebuild the document index once at final publication.
   Recover from upload/transaction failures using committed output references.

Done when worker termination before dispatch, duplicate delivery, and a late
publisher cannot strand work or overwrite a newer attempt, and concurrent
retrieval cannot cache pre-publication data under a new namespace version.

### K2: Repair vectors and finalize the right snapshot

Primary files: Worker `memory_service.py`, `phase_a.py`,
`scope_enrichment_service.py`; shared `ai/embedding/multimodal_client.py`,
`retrieval/page_embedding_index.py`, `storage/result_storage.py`,
`storage/job_file_storage.py`, and Worker ingestion `success_finalization.py`.

1. Record failed initial page embeddings durably before M0 completes; preserve
   its page rows and required reading assets.
2. Add bounded repair through existing background dispatch. Read/render only
   required missing pages and retain successful repair progress across retries.
3. Implement per-page vector upsert and owner rebinding. Do not call the current
   whole-revision replacement with a partial repair batch: that path deletes
   existing vectors before inserting its supplied batch. Prove that repairing
   page 7 cannot remove the vectors for pages 1-6 or 8 onward.
4. Publish baseline content/assets at immutable locations. Final content/assets
   get their own descriptor and checkpoint identity.
5. Run the final completion barrier after scope progress, vector repair and
   recovery. Require all accepted stages to succeed. Persist finalization claim,
   upload outcome and committed descriptor so a failed upload can be retried
   without repeating successful parsing or vector work.

Done when partial/all-page initial embedding failures still allow reading,
repair fills exactly the missing coverage, and final upload interruption neither
loses the baseline nor produces a premature final completion.

### K3: Deliver status, retry and completion correctly

Primary files: `apps/api/app/api/v2/routes/jobs.py`, jobs read/projection services,
`shared/services/jobs/lifecycle/webhook_outbox.py`, webhook payload enrichment,
`retrieval/search/visual_discovery.py`, agent tool dispatch and both harnesses.

1. Expose the state/descriptors and implement authorized idempotent retry.
2. Keep Job done and billing settled while retrying unfinished enhancement.
3. Commit final completion and its logical outbox event together after durable
   upload. Extend explicit event-to-status mapping: the current outbox otherwise
   maps an event other than `job.completed` to failed status.
4. Enrich both completion payloads with the right immutable result identity.
5. Preserve required query-embedding failure as a whole-request failure. Generic
   agent tool exception handling must not turn it into a successful answer from
   other evidence. Keep ordinary invalid-tool-argument handling recoverable.

Done when repeated retry, duplicate finalization and lost dispatch converge;
both retrieval routes report provider failure correctly; full mode still emits
its existing single success event and uses its existing billing path.

### S1: Teach the SDK to synchronize artifact generations

Primary files: `src/types/job.ts`, `src/resources/jobs.ts`, `src/knowledge/types.ts`,
`src/knowledge/knowledge.ts`, `src/storage/streamed-parsed-document-sync.ts` and
parsed-document storage types.

1. Decode additive state/descriptor fields and provide the enhancement retry call.
2. Pin one target descriptor throughout a segmented transfer. Refresh an expired
   URL for that same snapshot, not whichever result is now current.
3. Use distinct local storage/checkpoint keys for baseline and final; validate
   the real upstream revision separately. Retain archive identity, checksum and
   required-asset checks instead of clearing them to force a refresh.
4. Preserve existing full-mode synchronization and explicit pinned reads.
5. Add a release changeset, validate the packed build in Hipo integration, then
   release and pin the published version. Do not test against stale SDK 2.2.0.

Done when baseline copy resumes safely, final copy does not reject a legitimate
new generation, changed bytes within one generation still fail validation, and
old snapshot reads keep their content and assets after final publication.

### H1: Connect the Hipo backend lifecycle

Primary files: `resumable-parse-client.ts`, the Knowhere webhook route,
`durable-parsing.ts`, `durable-document-sync.ts`, `parsed-document-sync.ts`,
`drizzle-document-sync-repository.ts`, Source schema/repository, Source retry
application flow, and `app/api/internal/tasks/reconcile/route.ts`.

1. Select progressive for compatible inputs; track enhancement separately from
   initial Source readiness, parse completion and usage settlement.
2. Accept final events through a distinct path. Durably admit synchronization
   before acknowledgement; ignore obsolete Source/parse identities.
3. Give baseline and final independent transfer attempts. Commit required final
   content/assets before atomically updating the ordinary reader pointer.
4. Handle final-before-initial notification: verify upstream state, persist the
   final result, and allow it to become the first readable snapshot if no baseline
   was copied yet. Settle initial parsing once. A late baseline event must not
   downgrade a committed final result.
5. Add pending-enhancement checks to existing ten-minute recovery with bounded,
   fair batches. Browser closure must not stop enhancement discovery or copying.
6. Extend Source retry with a typed internal operation outcome. Enhancement retry
   calls Knowhere; failed final copying retries only local synchronization.
   Preserve the existing initial retry path. In web-RPC, do not emit the current
   unconditional `forgetSourceChunks` instruction for enhancement retry: it
   would clear the baseline/citation cache that must remain readable.

Done when event loss/reordering, repeated clicks, copy failure and a closed
browser converge without a second parse charge or loss of a committed snapshot.

### H2: Implement the reader, Tree and early Ask contract

Primary files: reader `build-section-tree.ts`, `chunks-panel.tsx`,
`tree-resource-filter.tsx`, `use-chunk-display-mode.ts`, reading-position helpers,
`all-chunks-query.ts`, chunk entity keys, parsed-content reader/projection,
Library retry/status components, and Knowhere Ask/citation adapters.

1. Show baseline Tree as document root with source-ordered Page N leaves, with
   no synthetic pN chapter wrapper. Native-text pages must work without images.
2. Select Pages; show Images/Tables disabled until final sync. Derive capabilities
   from the displayed snapshot, including historical citation snapshots.
3. After final commit, replace content/tree/sections coherently once. Preserve
   List/Tree mode and source-page focus, reveal that page's final chapter, and
   remap obsolete tree IDs/expansion without resetting the reader to the start.
4. Namespace queries and normalized chunk entities by actual snapshot identity,
   keeping old citations separate from the current result.
5. Keep ready baselines in Ask. Verify citations from currently published
   upstream baseline content can open corresponding retained source pages while Hipo still
   displays M0. Include native-text M0 citations to visual evidence that does not
   yet exist as an extracted local resource; page fallback alone is not sufficient
   unless the required evidence can actually be displayed. Resolve against the
   immutable original/source page as needed, without rendering every native-text
   M0 page up front or silently changing a saved citation's evidence.
   Any on-demand citation image is a separately pinned derivative of the immutable
   source page; do not mutate the baseline archive or replace its existing assets.
6. Implement enhancement status, terminal-failure-only Retry, and the cited-version
   notice with explicit View latest version. Compose these with existing locator
   and asset-error notices. Choosing latest leaves the citation pin, preserves
   source-page location and clears incompatible old highlights; it does not
   rewrite the saved citation.
7. Reuse client polling infrastructure for fifteen-second lightweight Hipo-local
   checks while visible and pending. Share checks per active Project, stop on
   completion/failure, and resume after an admitted retry or a visibility refresh.

Done when the approved reader acceptance matrix in the hardening plan passes
for native text, scans, historical citations, failed enhancement and final refresh.

## Verification and release gates

### V1: Verify behavior before enabling Hipo requests

| Test group | Required evidence |
| --- | --- |
| Distributed recovery | Terminate after queued commit/before send; duplicate messages; expired claim plus late publication; tasks handled by different Workers; no lost or conflicting publication |
| Repair/finalization | Missing some/all initial vectors; successful vectors preserved during repair; temporary/permanent provider errors; upload failure; completion event only after every required stage succeeds |
| Hipo integration | Native PDF, scanned PDF and PPTX baseline reading/Ask; required assets; final copy; two-event reordering/loss; browser closed; no repeat charge |
| Reader | Baseline/final Tree, resource filters, mode/page preservation, failure-only retry, old-citation version notice, explicit latest navigation and correct visual evidence |
| Retrieval failure | Classic and both agent harnesses fail the request on required query-embedding failure, including when lexical hits exist |
| Compatibility | Existing full/default requests, initial failure/retry, old results without descriptors, and stale/superseded Source identities |
| Capacity | Same permitted documents in full/progressive; small/representative/large inputs; simultaneous ingestion and retrieval; new admissions after prior Jobs reach M0 |

Use existing focused tests first, then repository checks. Relevant suites include
Knowhere's scope-task contract and embedding-gateway tests, SDK jobs/knowledge/
streamed-sync tests, and Hipo durable-sync/reader/Ask tests. Planned commands:

- Knowhere: `make check` and focused `uv run --all-packages pytest ...` suites,
  using each suite's existing test database/environment fixtures.
- SDK: `pnpm lint`, `pnpm typecheck`, focused `pnpm exec vitest run ...`, then
  `pnpm build` and existing release checks.
- Hipo: `pnpm lint`, `pnpm typecheck`, relevant unit/boundary/component suites,
  then targeted Playwright acceptance against the actual new SDK build.

The implementation report records the checks actually run and their results.

### Resource assessment

Start with the observed configuration, then measure the supported envelope:

- Worker: 2 vCPU, 4 GiB RAM, 21 GiB ephemeral storage, task concurrency 10;
  configured desired count 2 and scaling range 2-20, CPU target 35%, Spot capacity.
- API: two desired tasks, each 0.5 vCPU and 2 GiB.
- Database: Aurora PostgreSQL 15.15, one writer, serverless maximum 4 ACU.

Measure client time to readable baseline, final completion, foreground queue age,
retrieval latency/errors, Worker CPU/RSS/disk, DB connection/lock waits, provider
concurrency/retries, source-download amplification and model/storage/transfer use.
Repeated whole-source downloads per scope, document publication locks, and
foreground admission released at M0 are specific load scenarios. Two immutable
snapshots also add object and Hipo Blob storage/transfer cost.

The local image-embedding default of 8 can multiply across 10 Worker tasks and
replicas. It is not an aggregate provider quota. Reuse existing quota primitives
if measured concurrency/provider limits require a bound. Do not assume CPU-only
autoscaling reacts to I/O-bound backlog, and do not infer capacity from low
historical averages. Tune existing limits/resources only when results justify it.

### Deployment sequence

1. Recheck configuration and current production state. The 2026-10-26 audit
   observed API/Worker startup failure because the referenced Secrets Manager
   JSON lacked `VERCEL_KEY`. Resolve through the owning credential process and
   verify healthy tasks/provider access; task-definition injection is not proof
   that a usable value exists. Also verify database migration and halfvec support.
2. Deploy compatible Knowhere schema/API/Worker changes and prove recovery.
   Keep full as the API default.
3. Publish the validated SDK and pin it in Hipo's lockfile. Validate the actual
   installed package, not only repository source or a locally stale dependency.
4. Complete staging Hipo behavior/load acceptance, then deploy Hipo to select
   progressive for supported inputs through the existing request parameter.
5. Check real client behavior and operational error/backlog indicators after
   deployment. No new rollout platform or extra public switches are required.

For rollback, stop creating new progressive Hipo Jobs by using the existing full
mode selection. Keep compatible workers and snapshot serving available for
already-admitted progressive work and retained citations. Do not roll back across
an incompatible state/schema boundary or delete retained artifacts to restore
old behavior.

## Completion checklist

- All accepted lifecycle and reader contracts have executable verification.
- Baseline and final artifacts are immutable, independently readable and pinned.
- No queued/finalization/repair gap requires an operator to recreate the Job.
- Retry cannot double-charge, wipe readable evidence or rerun completed stages.
- Early Ask citations open their source pages and required visual evidence.
- Full-mode compatibility and cross-repository installed-package acceptance pass.
- Healthy deployment and supported-load measurements exist; neither is inferred
  from source review or green unit tests alone.
