# Progressive ingestion hardening plan

Status: Historical investigation and acceptance rationale. Application-code
implementation was subsequently authorized and progressed; the overall
requirement remains open for further discussion as of 2026-10-27. Current List
and Tree decisions are recorded in the development plan's discussion checkpoint.
The accepted citation-navigation amendment in ADR 0022 remains to be implemented.

The execution-oriented development sequence is maintained in the
[development plan](progressive-ingestion-development-plan.md). This document
retains the investigation evidence, agreed behavior, and acceptance rationale.

## Evidence baseline

- PR #471 is merged at `6ddea434d`; its head is `29b570943`.
- Relative to the earlier reviewed head `81e701648`, the only later source change
  is in `apps/api/scripts/ensure_test_environment.py`. The four reviewed
  reliability paths are unchanged.
- PR head `29b570943` passed Lint, Typecheck, Test, CodeQL, and secret scanning.
  The earlier failed CI run is no longer an outstanding blocker.
- This plan distinguishes source-confirmed failure windows from capacity
  hypotheses. No production fault injection has been performed.
- Hipohipo consumer inspection uses `Ontos-AI/hipo` main `d0127c1d9` and its
  pinned Knowhere Node SDK 2.5.0 (`knowhere-node-sdk` main `0b30e2b25`). These
  are source-level findings, not proof of deployed client acceptance.
- GitHub recheck during plan consolidation confirms that PR #471 is merged,
  all recorded head checks passed, and remote Knowhere main remains `6ddea434d`.
- A read-only AWS check at approximately 2026-10-26 17:28-17:32 UTC found a
  concrete deployment prerequisite failure: new API revision 63 and Worker
  revision 59 cannot start because the referenced Secrets Manager JSON lacks
  `VERCEL_KEY`. Existing API revision 62 still has two running tasks; the Worker
  service has one old task running against a desired count of two. These are
  deployment observations, not an inferred provider outage or a completed
  progressive load test. No production configuration was changed.

## Confirmed scope constraints

- Retain the existing `ingestion_mode=full|progressive` request parameter and
  its default of `full`. No additional public parameter is justified yet.
- Ordinary, bounded changes to shared code are acceptable. A changed path is
  not itself a defect or a launch blocker.
- Prefer changes to existing modules and recovery mechanisms. Any broader
  mechanism must be justified by a concrete failure scenario.
- Server upgrades, a new rollout platform, separate worker services, and a
  vector search redesign are not approved changes. Capacity measurements may
  motivate a later decision.

## Proposed work

| Work | Existing location | Proposed change | Acceptance evidence |
| --- | --- | --- | --- |
| Recover missing scope dispatch | `shared/services/jobs/lifecycle/scope_enrichments.py` | Reconcile expired queued scopes in the existing sweeper; preserve conditional task claims. Evaluate an outbox only if reconciliation cannot meet the agreed guarantee. | Terminate after the queued transaction commits but before broker publication; another worker eventually performs the scope without conflicting publication. |
| Recover final package creation | `app/services/page_memory/scope_enrichment_service.py` and the existing enrichment sweeper | Track finalization durably and retry after scopes and missing-vector repair complete, without repeating successful stages or removing the baseline. Admit final completion and its webhook only after the immutable package is durable. | Fail S3 upload once; the baseline remains readable and a later recovery run reaches final completion. Exhausted retries expose enhancement failure; no premature final success is emitted. |
| Invalidate after commit | `shared/services/retrieval/publication_scope_content.py` and its callers | Move namespace cache invalidation after successful publication commit, following the existing post-commit pattern. | A concurrent retrieval cannot retain pre-publication content under the new cache version. |
| Preserve retrieval failure semantics | `shared/services/retrieval/search/visual_discovery.py` and agent tool dispatch/harness error handling | Keep required query embedding failures fatal to the retrieval request. Do not add lexical-only fallback. Verify classic error propagation and ensure agent exception handling cannot convert this dependency failure into a successful incomplete response. | With lexical hits present and query embedding failing after retries, both retrieval routes report failure through the existing API contract; no partial result is published as success. |
| Repair missing page vectors without blocking viewing | `shared/services/ai/embedding/multimodal_client.py`, M0 publication, and existing background recovery | Allow a viewable baseline to publish despite missing vectors. Persist missing page identities and repair them in the background without reingestion. Distinguish configuration errors from transient failures; bound retries. | Partial and all-page embedding failures preserve required reading assets and initial availability. After provider recovery, pending vectors are filled without reopening or reingesting the document. |
| Align the public mode description | `shared/models/schemas/job.py` | Describe automatic full-document background enrichment and priority promotion for accessed scopes. Preserve the existing request parameter and scheduling policy. | The request description agrees with automatic refinement of scopes that are never accessed. |
| Connect Hipohipo to progressive ingestion | Hipo `src/infrastructure/knowhere/resumable-parse-client.ts` | Select the existing progressive mode for compatible PDF/PPTX inputs. Continue using the current full mode for other formats. Include synchronized ready baselines in Ask without waiting for final enhancement. | A supported progressive request reaches M0, synchronizes its assets, becomes viewable, and participates in Ask before background enrichment finishes; citations open corresponding pages. |
| Refresh Hipohipo's enhanced snapshot safely | Knowhere result artifact storage, Hipo document synchronization, and Knowhere SDK parsed storage | Retain immutable baseline and final snapshots for provenance. Switch ordinary reading and citations to the committed final reader at the same original-file page. Keep source-page identity separate from parse-snapshot provenance. | Final content becomes visible without reparsing, charging again or mixing snapshots; old citations open the enhanced same page, with explicit access to retained provenance. |
| Notify final enrichment and refresh the reader | Knowhere webhook outbox and Job result projection; Hipo webhook route, durable synchronization, and reader polling | Reuse the existing callback for initial `job.completed` and final `job.enrichment.completed` events. Persist webhook receipt before acknowledging, verify current upstream identity, and schedule an idempotent final snapshot refresh. Reuse ten-minute server reconciliation for pending progressive results and fifteen-second Hipo-local status checks while the client page is visible. | Lost, duplicate, delayed, and out-of-order notifications eventually converge; browser closure does not prevent synchronization; initial Job completion is not confused with final enhancement. |
| Retry failed background processing from Hipo | Knowhere Job enrichment state/retry command; Hipo Source retry application flow and Library/reader status | Keep the Source readable, expose enrichment failure separately, and allow an authorized retry of unfinished work under the existing Job. Proposed UI reuses the Library row retry interaction and adds a nonblocking reader notice. | After automatic retries are exhausted, a user resumes failed work from Hipo without reuploading, losing the baseline, rerunning successful scopes, or repeating ingestion charges. Duplicate clicks do not create concurrent work. |
| Render progressive Tree View | Hipo reader tree construction, resource filters, and final-snapshot refresh | Show the baseline as a document root with source-ordered `Page N` leaves for both native and scanned PDFs. Pages is selected; Images/Tables remain disabled until final sync. Replace it once with the final chapter/resource hierarchy, preserving mode and source-page focus. | Baseline pages remain navigable without prebuilt images; final refresh preserves mode/page and stable source-page citation anchors. |

Repository-relative paths in the table omit the `packages/shared-python/` or
`apps/worker/` prefix where the application root is unambiguous.

Visual retrieval in this plan means the page-image embedding branch inside
Knowhere's existing retrieval implementation, not a separate deployed search
service. It embeds the query through an external provider and searches persisted
page vectors in the database, alongside lexical discovery. Page-based document
storage, the page result filter, and displaying synchronized page assets do not
by themselves require that query embedding call. The accepted failure policy
concerns searches that actually enter this branch with eligible page vectors.

## Accepted decisions

- Progressive ingestion automatically refines the entire document. Accessed
  scopes receive priority, but access is not required for refinement. This
  preserves the current scheduling behavior; demand-only refinement is not the
  target. See [ADR 0011](../adr/0011-complete-progressive-enrichment-automatically.md).
- The user's acceptance condition for early completion is actual client
  original-page viewing. Native PDF/PPTX baselines may omit pre-generated page
  assets if the client can display the immutable source PDF page by page,
  including figures, tables and layout, without waiting for enrichment.
  Initial Job completion or extracted text alone is not proof of that experience.
- Once baseline synchronization makes a Hipo Source ready, include it in Ask
  immediately. Enhancement can continue with limited initial search coverage;
  citations must open corresponding source pages. Do not add a final-enhancement
  admission gate or a separate public request switch. Required query-embedding
  errors still fail the search request under the accepted error policy.
- Native PDF/PPTX M0 need not pre-render page images on the server; the client
  must support original-page display from the retained source PDF. Scanned PDFs
  keep their page-image baseline before complete OCR. Preserve initial Job completion after baseline publication;
  Hipohipo readiness follows successful baseline and asset synchronization.
  Full-document enrichment is a separate completion milestone. See
  [ADR 0012](../adr/0012-publish-a-viewable-baseline-before-enrichment-completes.md).
- Hipohipo keeps the baseline visible during background enrichment and updates
  once after the complete final result has synchronized. Intermediate scope
  refinements do not independently replace the reader snapshot. See
  [ADR 0013](../adr/0013-refresh-readers-once-after-full-enrichment.md).
- Reuse the existing Job webhook callback for two successful progressive
  milestones: initial `job.completed` and final `job.enrichment.completed`.
  Full-mode ingestion keeps its existing single completion event. Final refresh
  does not reparse or charge again. See
  [ADR 0014](../adr/0014-notify-both-progressive-completion-milestones.md).
- Retain the baseline and final snapshots with their required resources. After
  final synchronization, ordinary reading and existing citations use the final
  snapshot at the same original file version and physical page. Creation-time
  snapshots remain available for explicit provenance inspection only.
  Intermediate scopes do not require additional reader snapshots. The default
  navigation rule in ADR 0015 is superseded by
  [ADR 0022](../adr/0022-open-citations-on-the-enhanced-original-page.md).
- A required query embedding failure fails the retrieval request, even when
  lexical discovery already found results. Do not add partial-success fallback
  or a new request switch. Apply this policy to classic and agent retrieval.
  Initial ingestion page-vector coverage is governed separately. See
  [ADR 0016](../adr/0016-fail-retrieval-on-required-query-embedding-errors.md).
- Allow a viewable baseline to publish and reach initial `done` despite missing
  page vectors. Durably track missing pages and repair them in the background;
  do not require reingestion. Required reading assets and the client snapshot
  commit gate still apply. See
  [ADR 0017](../adr/0017-publish-viewable-baselines-with-missing-page-vectors.md).
- If background enrichment fails after automatic retries, preserve the readable
  baseline, expose the failure separately, and allow manual retry of unfinished
  work only after automatic retries are exhausted. Pending, running, automatically
  retrying, and completed work do not expose manual retry. User-facing wording
  describes retrieval enhancement and confirms that reading remains available. See
  [ADR 0018](../adr/0018-keep-baselines-readable-after-enrichment-failure.md).
- Reuse Hipo's ten-minute server task-reconciliation cadence as a fallback for
  pending progressive results. Normal webhook receipt starts synchronization
  promptly; recovery does not depend on an open browser. See
  [ADR 0019](../adr/0019-reuse-ten-minute-hipo-enrichment-reconciliation.md).
- While the relevant page is visible and enhancement remains pending, check
  lightweight Hipo-local status every fifteen seconds. Update after final snapshot
  commit; stop on completion or terminal failure. See
  [ADR 0020](../adr/0020-check-visible-hipo-enhancement-status-every-fifteen-seconds.md).
- Final enrichment completion requires successful text/structure enhancement,
  repair of all recorded missing page vectors, and a durable final result package.
  Exhausted retries keep the baseline readable and expose enhancement failure.
  Hipo shows completion only after its final snapshot commit. See
  [ADR 0021](../adr/0021-gate-final-enrichment-completion-on-all-required-work.md).
- Tree View is available for the baseline as one document root with source-ordered
  `Page N` leaves. Native pages open their retained original PDF page on demand
  in List; scanned pages use their existing images.
  Do not create a page-named chapter wrapping another page node. Final snapshot
  synchronization updates the tree once to its chapter/resource hierarchy;
  intermediate scope changes do not independently rebuild the Hipo tree.
- Final snapshot adoption preserves the current List/Tree mode and source page.
  If the user is viewing page 12 in Tree, reveal the final chapter containing
  page 12 and keep that page focused. Map by source-page identity rather than
  requiring baseline node/chunk IDs to remain unchanged. Explicit provenance
  inspection retains access to the original parsed snapshot.
- In the baseline Tree, Pages is selected and usable for both text-only and
  image-backed pages. Images/Tables remain visible but disabled, with an
  enhancement-pending hint. Restore normal filtering after final synchronization.
  Derive capabilities from the snapshot actually being read, not merely the
  Source's latest state.
- An old citation defaults to the enhanced reader at its same original-file
  page. Historical-version notices apply only when the user explicitly inspects
  creation-time provenance. Returning to current reading preserves source-page
  identity without rewriting saved evidence or reusing incompatible highlights.

## Current runtime sequence

Both modes retain the asynchronous API, Job, Celery, and Worker architecture.
The API creates one Job; progressive ingestion changes its publication boundary
and schedules subsequent work, rather than streaming the HTTP response.

1. `/v2/jobs` accepts progressive ingestion for PDF and PPTX page-memory inputs.
   Omitting the mode continues to select full ingestion. Source upload/download
   and initial Worker dispatch follow the existing Job lifecycle.
2. The initial Worker normalizes the source to PDF and profiles it. Progressive
   text-native inputs publish flat page text. With visual retrieval enabled,
   scanned inputs render all pages and attempt image embeddings before publishing
   image-backed page rows; full OCR and hierarchy construction are deferred.
   This is a document baseline, not publication of each page as it finishes.
3. The Worker uploads the initial package, referenced assets, source PDF, and
   resumable enrichment inputs, then publishes the baseline and marks the Job
   `done`. The completion transaction records `job.completed`; post-commit
   effects dispatch structure enrichment and the webhook. Their completion order
   is not guaranteed. Hipo becomes readable only after copying and committing
   the initial snapshot and required assets.
4. A separate structure task resumes profiling, including deferred scanned-text
   extraction, TOC, and structural anatomy. It publishes coarse sections and
   schedules every scope for background refinement. A scope is a logical document
   region with a page range. Eligible coarse scopes read through `corpus.read`
   receive higher queue priority; a Hipo-local reader view alone is not that call.
5. Scope tasks perform detailed parsing, publish content and assets, and mark
   their scope refined. They may execute on different Workers using durable
   database and object-storage inputs. The Job stays `done` throughout. Current
   database-backed reads can observe these progressively refined scopes, while
   Hipo's committed local snapshot does not automatically follow them.
6. Once all scopes are refined, the current implementation rebuilds and uploads
   the final package and sets enrichment metadata to `complete`. It currently
   updates the same Job Result and archive location. The accepted immutable
   baseline/final snapshots, final completion webhook, and Hipo final refresh
   require implementation; they are not already provided by this sequence.

Query embedding is separate from ingestion image embedding. During an eligible
retrieval, the existing hybrid discovery first performs lexical discovery, then
embeds the user's question and matches it against stored page vectors. That
external call happens when searching, not whenever a reader displays an existing
page asset. Native-text M0 creation does not use the scanned-page image embedding
path. Hipo currently submits full-mode Jobs, so adopting this progressive sequence
also requires its planned submission and synchronization changes.

### Cost of skipping initial page embeddings

Initial progressive publication already defers some retrieval capabilities:
text-native M0 lacks final structure and asset understanding, and visual scanned
M0 lacks OCR text. Initial `done` therefore does not promise retrieval equivalent
to the final enriched result. The accepted ability to view the baseline and the
decision to fail a search on a dependency error address different guarantees.

Skipping a failed image embedding does not omit that page row or its existing
image asset. It leaves a gap in vector-search coverage while allowing the
baseline and background structure work to proceed. Before OCR text is published,
content-based discovery can miss the affected page; browsing or explicit page
reading can still reach it. Successful later OCR can provide lexical discovery,
but it does not recreate the missing visual vector. Current scope publication
rebinds existing vectors to current chunks, rather than generating missing ones.
Failures are logged, but there is no durable failed-page repair workflow in this
path. ADR 0017 accepts initial publication despite missing vectors and requires
durable tracking and background repair. Incompleteness alone does not reject
the baseline, since the accepted progressive experience allows viewing before
full enrichment. ADR 0021 requires repair before final completion; exact retry
bounds remain implementation work.

## Hipohipo consumer findings

### Submission and view readiness

- Hipo currently omits `ingestion_mode` from its `/v2/jobs` submission, so it
  continues to use Knowhere's default full mode.
- A completed Knowhere Job starts Hipo's durable parsed-document synchronization.
  Hipo Source readiness follows successful snapshot synchronization; it does
  not follow the upstream completion signal alone.
- SDK 2.5.0 prefers the result ZIP, copies it in bounded resumable segments,
  checks metadata-referenced page assets, and publishes a snapshot commit only
  after required objects exist. Hipo's reader then resolves page images to its
  own Blob URLs.
- `PageChunkCard` displays page images when `pageAssets` exists and otherwise
  displays readable text. An empty page body does not prevent image rendering.
- Knowhere scanned M0 publishes `page_citation_assets` and includes them in both
  its ZIP and raw uploads before Job completion. Text-native PDF M0 currently
  publishes text rows without page images. Neither path has been validated in
  a real progressive Hipohipo browser session during this discussion.

### Background enhancement and stored snapshots

- Knowhere enriches the same `job_result_id` and replaces the final ZIP without
  issuing a new Job Result revision.
- Hipo deduplicates automatic synchronization for the same parse/document
  identity after the attempt has completed. Its reader uses the pinned committed
  Blob snapshot before querying remote document chunks.
- SDK 2.5.0 records the archive identity in sync progress and rejects a changed
  archive under the same revision. This check also runs when a previous sync is
  already complete. An explicit Hipo retry alone is therefore insufficient.
- A deliberate completion/refresh contract is required before claiming Hipo
  supports the full progressive lifecycle. A single reader update after final
  completion, two-stage webhooks, and retention of both published snapshots are
  accepted. Ten-minute server reconciliation and fifteen-second visible-client
  checks are accepted. Internal synchronization and artifact identities still
  require technical design.

## Completion notification flow

The same-Job two-event webhook contract is accepted in ADR 0014, ten-minute
server reconciliation in ADR 0019, and fifteen-second visible-client status checks
in ADR 0020. Detailed synchronization, polling, and recovery integration remain
implementation proposals, not claims that these capabilities already exist.

1. Knowhere keeps the existing `job.completed` event for initial baseline
   publication. Once the full enriched package is durable and its identity is
   committed, persist a separate `job.enrichment.completed` event in the same
   transaction as final enrichment completion. Dispatch after commit through
   the existing webhook outbox and QStash delivery path.
2. The final event identifies the event, Job, Document, Job Result, and final
   result checksum. Emit it only for progressive Jobs with a configured webhook.
   Hipo authenticates it, durably admits work, and acknowledges without waiting
   for the asset transfer. Deduplicate by final result identity and reject
   superseded Source/parse mappings. Verify completion through an authorized
   upstream read rather than trusting notification arrival order.
3. Hipo reuses durable segmented synchronization to copy the final snapshot.
   Keep the baseline ready during transfer and switch the visible snapshot only
   after the final content and assets pass the commit gate. SDK archive identity
   protection must remain effective for interrupted transfers; a legitimate
   final generation must not be treated as a changed in-flight archive. Retain
   both published generations and the resources referenced by their citations.
   Creation-time provenance remains immutable; default citation navigation
   follows that same original-file page in the committed enhanced reader.
4. A bounded Hipo server-side reconciliation checks only known progressive
   results awaiting final completion. It recovers missed notifications even
   while no browser is open. Reuse the existing recovery workflow and its accepted
   ten-minute cadence. Query pending results in bounded, fair batches. This
   replaces the earlier tentative five-minute proposal; live cadence still needs
   deployment verification. Large-document transfer time and backlog draining
   are separate from the recovery check interval.
5. The visible Hipohipo reader checks lightweight Hipo-local enhancement/snapshot
   state every fifteen seconds while a final result is pending. Coalesce checks
   per active Project and pause when hidden. Do not fetch Knowhere or copy assets
   on each browser poll. Refresh reader data when a newly committed snapshot
   identity is observed and stop checking on completion or terminal failure.
   Manual retry can resume checking. No new browser WebSocket/SSE service is proposed.

Existing integration constraints:

- `SyncJobWebhookOutbox.create_event` currently maps every event other than
  `job.completed` to failed status. Extend event semantics deliberately; passing
  a new event name alone would create an incorrect failure payload.
- Payload enrichment currently recognizes only `job.completed`. The new final
  event needs its own complete identity and compatible result projection.
- Hipo's webhook schema accepts only `job.completed` and `job.failed`, and its
  parse handler ignores already-terminal initial parse operations. Route the
  distinct final event to refresh handling rather than reopening initial parse
  execution or charging again.
- Hipo browser polling currently runs every three seconds only while Sources are
  uploading, queued, or parsing; it stops at initial ready. Enhancement tracking
  must not require marking a readable Source as parsing again.
- Neither an event nor a polling observation alone means the new snapshot is
  visible: the committed Hipo snapshot identity is the reader's update signal.

## Hipohipo retry interaction and implementation proposal

Current source evidence:

- The Library document row already renders a failed-status pill as a `Retry`
  action (`modules/library/source-retry-button.tsx`). Its retry hook invokes
  the Source retry application flow.
- `retryProjectSource` currently retries failed/pending result synchronization,
  Catalog materialization, or failed initial parsing. A ready Source with a
  completed sync has no upstream enrichment retry branch.
- The reader renders readable content for ready Sources, and its failed-reader
  retry action is only shown for initial Source failure. Marking the Source
  failed merely to obtain that button would remove the accepted reading path.
- Knowhere's current v1/v2 Job and Document routes do not expose a manual
  progressive-enrichment retry action. Aggregate terminal enrichment failure
  also needs an explicit readable status projection.

Failure-only retry and retrieval-enhancement wording are accepted. The backend
mechanisms below remain implementation proposals. Ten-minute server
reconciliation and fifteen-second visible-client status checks are accepted.

1. Keep the Source ready. Describe the background work as retrieval enhancement
   in user-facing copy. Only after automatic retries are exhausted, show a
   separate `Search enhancement failed` status on its Library row and the
   nonblocking reader notice `You can still read this document. Search enhancement
   could not be completed.`, with a `Retry` action. Pending or running work,
   including automatic retry, shows `Enhancing search...` without this action.
   Completed work does not expose it. Revalidate eligibility on the server when clicked.
   Reuse the existing Library retry interaction and Hipo Source retry entry point;
   add an enrichment branch based on durable state rather than a new ingestion
   mode or a new user-supplied selector.
2. Hipo authorizes the Source operation and resolves its current Job/result
   identity. If upstream enrichment failed, call a new Knowhere enrichment retry
   action. If upstream work is complete and only Hipo copying failed, retry just
   final synchronization. Do not invoke `/v2/jobs` again.
3. Knowhere validates ownership and the active revision, durably admits only
   unfinished work, and dispatches through existing background tasks. Preserve
   active claims and completed scopes; duplicate requests share the admitted
   attempt. Reuse existing recovery after a dispatch interruption.
4. Acknowledge queued work promptly and show pending/running status. Keep the
   baseline and citation provenance available throughout. Final success follows the
   accepted final webhook, synchronization, and atomic reader-switch contract.
5. Hipo learns failure or stalled progress through the proposed server-side
   reconciliation and exposed upstream enrichment state. This proposal does not
   add a third webhook event. Reuse the accepted ten-minute recovery cadence.

## Reader interaction contract

Baseline Tree presentation, final-refresh state preservation, resource-filter
availability, and historical-citation navigation have been discussed and agreed.

Source evidence from Hipo `d0127c1d9`:

- `buildSectionTree` combines stored manifest sections with chunk section paths.
  `ChunksPanel` enables tree rendering once all chunks are loaded; it does not
  require a final enrichment state.
- Knowhere M0 rows use flat `filename/pN` paths. That is sufficient to construct
  a mechanical tree, but does not provide the final semantic chapter hierarchy.
  Passing the paths through unchanged can create a page-named section wrapping
  a page leaf, rather than a useful chapter tree.
- Hipo derives page-resource filtering from the presence of page assets. A
  text-native M0 can contain page chunks without such images, so initial tree
  and filter behavior need explicit coverage for both baseline types.
- The tree component retains expansion, selection, and camera state while its
  Source is mounted. Its current key is Source ID, not snapshot identity. A final
  snapshot can therefore change node identities without resetting those states
  coherently. Existing List/Tree mode behavior does not establish the desired
  cross-snapshot experience.

The baseline Tree View choice is accepted: render a document root with ordered
page leaves, then update once to the final tree. Readability and navigation stay
available if enhancement fails or is retried. The final update preserves the
current List/Tree mode and source-page focus; Tree reveals that page's chapter.
Baseline resource filtering is also accepted: Pages is selected and usable;
Images/Tables are visible but disabled, then normal filtering returns after
final synchronization.

Existing citations default to the committed enhanced reader at the same original
file version and physical page. Old snapshots are retained only for explicit
provenance inspection. In that historical view, Tree shape and resource
capabilities follow the displayed snapshot: a baseline does not gain final
chapters merely because enhancement is now complete. Historical notices must
describe the selected version without reporting current enhancement as pending.
The default-navigation amendment is accepted but not yet implemented.

Reader acceptance matrix:

| State | Reading and Tree | Status and action |
| --- | --- | --- |
| Text-native baseline | Original PDF pages rendered on demand in List, with extracted text available on preview failure; root with ordered page leaves | Enhancement in progress; Images/Tables disabled |
| Scanned baseline | Complete required page images; root with ordered page leaves | Enhancement in progress; missing vectors repair in the background |
| Background scopes changing upstream | Keep the committed baseline content and tree | No per-scope reader replacement |
| Final result copying to Hipo | Keep the baseline and its required assets | Remain pending until final local commit |
| Final local snapshot committed | Adopt final content, chapter tree, and normal resource filtering together; preserve List/Tree mode and source-page focus | Enhancement complete; no retry action |
| Automatic retry still active | Keep readable content and tree | Progress only; no manual retry action |
| Terminal enhancement or final-copy failure | Keep readable baseline content and tree | Enhancement failed; Retry resumes the failed stage |
| Old citation after final completion | Open the enhanced reader at the same original file version and physical page, independent of changed chunk/chapter IDs | Retained snapshot available only through explicit provenance inspection |
| Explicit provenance inspection | Open the creation-time snapshot, tree and assets | Historical-version notice; saved evidence remains unchanged |
| Return from provenance to current reading | Open the same original-file page in the enhanced reader; preserve mode/page and clear incompatible old highlight | Saved citation and provenance remain unchanged |

Large-tree layout performance, resource filtering correctness, mapping obsolete
node/expansion state, and query cache identity are engineering validation work.
Do not add new M0 title parsing merely to invent a chapter tree. Source-page
counts must come from the document page count, not the number of tree nodes or
final chunks. Existing permission and source-deletion rules continue to apply.

## Early Ask availability

Early Ask is accepted after initial synchronization makes the Source ready.
Current Hipo `buildRetrievePool` includes a Source when its status is ready and
it has a Knowhere Document ID; it does not check an enrichment milestone. Keep
that admission behavior for the progressive baseline.

Ask can use currently published content while enhancement continues, with limited
coverage. Required query-embedding failures still fail the search. An enhancement
failure does not itself revoke a readable Source's ready state or remove it from
Ask; its incomplete enhancement remains visible and retryable as already agreed.

Verify that early Ask evidence opens a corresponding page in
the committed client snapshot. Upstream final publication can precede Hipo's
local synchronization, so equal Job Result IDs do not by themselves prove that a
retrieved chunk exists in the client snapshot. Hipo's reader can resolve a citation
by source page even when its chunk ID changes, but source-page availability,
asset references, and creation-time provenance need end-to-end verification. Do
not solve an unresolved citation by silently changing the retained evidence.

## Remaining engineering work

Core workflow decisions are agreed. Resolve internal snapshot identity across Knowhere
artifacts, SDK synchronization, and Hipo provenance reads; apply existing bounded
retry and claim mechanisms to stage recovery; measure representative workload
and resource headroom before concluding that production capacity is sufficient.
These are technical design and validation tasks, not outstanding choices in the
reviewed reader flows and not reasons to reopen accepted workflow decisions or
add public ingestion parameters. Discuss any newly discovered product trade-off
on its own evidence during implementation.

## Implementation sequence

### 1. Make background work recoverable in Knowhere

- Extend the existing scope sweeper and conditional claim logic in
  `shared/services/jobs/lifecycle/scope_enrichments.py` to recover queued dispatch
  gaps. Recovery must tolerate duplicate broker messages rather than rely on
  exactly-once delivery.
- Persist typed structure, missing-vector repair, and finalization progress with
  attempts, next retry eligibility, failure classification, and an expiring claim.
  Prefer the existing Job Result metadata and scope rows over a new workflow
  system. Reuse the current three-attempt scope policy as the starting task-level
  retry budget; keep provider request retries bounded separately. Permanent
  credential/configuration errors must become actionable failures, not endless
  retries.
- A claimed task must verify its active document revision and claim generation
  when publishing. Current scope rows have state/attempt counts but no claim
  token. Add an additive claim field if needed to reject a late publisher after
  its lease was recovered. Do not hold a database transaction during model calls
  or object transfer.
- Persist missing page identities before M0 completion and repair only those
  pages. Keep images available. Use existing low-priority background dispatch
  and recovery; no separate Worker service is required by this design.
- Invoke the final completion gate after either scope refinement or vector
  repair advances, and from recovery. All scopes refined is insufficient while
  vector repair remains pending. A durable finalization attempt uploads the
  final package and can resume after interruption without rerunning refinement.
- Move scope-publication namespace invalidation after SQL commit. Do not change
  full-mode parsing or introduce lexical-only search fallback. Carry required
  query-embedding failure through classic retrieval and both agent harnesses as
  a request failure, even where generic tool dispatch currently catches errors.

### 2. Give published artifacts stable identities

- Preserve one business Job and its existing Job Result. The database enforces
  one Job Result per Job, so a second final Job Result is not a drop-in solution.
- Publish separate immutable baseline and final artifact descriptors, each
  containing its upstream Job/Document/Job Result identity, snapshot identity,
  checksum, and artifact location. Use snapshot-specific or content-addressed
  archive and asset keys; never overwrite an artifact retained by a citation.
- Store both descriptors and a current-result pointer through typed metadata
  or an additive storage model justified during implementation. Upload first;
  commit the descriptor/current pointer only after its required content exists.
  A failed upload may leave an unreferenced object, but must not expose a partial
  publication. Existing deletion/retention rules still govern retained objects.
- Extend Job result output with the snapshot descriptor and explicit enrichment
  state/retryability. Preserve current fields and full-mode behavior. These are
  result identity/state fields, not additional ingestion request switches.
- In the final completion transaction, record `job.enrichment.completed` in the
  existing outbox with the final identity; dispatch after commit. Extend the
  hardcoded event/status mapping and payload enrichment deliberately. Reconcile
  duplicate finalization and outbox admission by logical result identity.

### 3. Adapt and release the Node SDK snapshot contract

- Update `src/knowledge/knowledge.ts` and streamed parsed-document synchronization
  to pin one immutable artifact descriptor throughout a segmented transfer.
- Separate the upstream Job Result revision used for server validation from
  the local snapshot key used for storage and citations. A baseline and final
  snapshot share the former and must differ in the latter. Do not send a
  synthetic composite snapshot key as an upstream `job_result_id`.
- Keep ETag/archive/checksum and required-asset checks. A new committed generation
  receives a separate checkpoint and storage prefix; an in-flight generation
  still rejects changed archive contents. Never repair the lifecycle by clearing
  identity checks or silently substituting current database chunks for a pinned
  archived snapshot.
- Cover baseline-only, final-only, interrupted transfer, superseded upstream
  revision, and old-citation reads. Release the compatible SDK before updating
  Hipo's dependency; stale locally installed SDK 2.2.0 is not validation of the
  inspected 2.5.0 behavior or the future change.

### 4. Expose status and retry, then connect Hipohipo

- Add an authorized Knowhere Job enrichment retry command. Admit it only for a
  current, retryable terminal failure; a repeated request returns the current
  attempt. Preserve completed stages, active claims, initial `done`, and usage
  settlement. This command does not call Job creation again.
- Hipo's submit adapter selects the existing progressive mode for compatible
  PDF/PPTX sources. Other formats continue through full ingestion.
- Extend the webhook schema and route final events to enhancement synchronization,
  separately from the already-terminal initial parse handler. Record receipt or
  durable synchronization admission before acknowledging. Authenticate, dedupe,
  and verify current Source/parse identity; stale events cannot publish a result.
- Add durable enrichment state separately from Source readiness and initial
  parse billing. Final sync gets a distinct snapshot attempt. The initial Blob
  commit remains readable while final copying, failure, and retries occur.
- Commit final content and required assets before switching the Source's current
  snapshot pointer. Preserve citation provenance keys for explicit tracing;
  default citation navigation and ordinary reading use the final representation
  at the same original file version and physical page.
- Render M0 navigation from ordered source page identity, without interpreting
  its synthetic page path as a semantic section. Support native-text pages even
  when page-image assets are absent. Build the final tree and manifest sections
  from the same committed snapshot as reader content. Preserve the current
  List/Tree mode and map source-page focus to the corresponding final chapter;
  discard obsolete tree-node IDs and recompute ancestor expansion as needed.
  Apply a coherent camera update when revealing the mapped node.
- Carry the committed snapshot key through reader query inputs and normalized
  chunk entity IDs. Hipo already namespaces chunk entities by Source, revision,
  and chunk. Distinct baseline/final snapshot keys must reach that namespace so
  final refresh cannot overwrite the cached evidence used for provenance.
- Derive Tree shape and resource-filter capabilities from the actual read
  snapshot's identity/stage. Explicit provenance inspection can open a baseline after the Source's
  latest result is complete; that does not make the historical snapshot gain a
  chapter tree or extracted resources. Avoid a misleading enhancement-pending
  message in that case. The accepted version notice must coexist with existing
  citation-location and asset-failure notices rather than overwrite them.
  Returning to the current result preserves the same original-file page and
  requires a readable, authorized committed snapshot. Ordinary citation opening
  does not first force the user into this historical context.
- Extend the existing Source retry application flow to route failed enhancement
  upstream or retry only failed local synchronization, depending on durable
  state. Apply the agreed failure-only Library/reader interaction and wording.
- Add bounded pending-enhancement checks to the existing ten-minute recovery
  workflow. Extend visible-client polling with the fifteen-second lightweight
  Hipo-local status projection, shared per active Project. No new scheduler,
  browser push service, or public ingestion parameter is required.

Any schema additions use new migrations. Existing migrations and unrelated
checkout changes must remain untouched. These packages can be implemented in
this dependency order; no application-code changes have been made in this plan.

## Resource and deployment assessment

Read-only production configuration observed during this consolidation:

| Component | Observed configuration | Implication for this change |
| --- | --- | --- |
| API | Desired/running 2; 0.5 vCPU and 2 GiB per task | Query embedding is a new external request dependency on eligible searches; test latency, timeouts, and request concurrency. |
| Worker | Desired 2, currently running 1 old task; 2 vCPU, 4 GiB, 21 GiB ephemeral storage, concurrency 10 per task | Initial rendering, scope work, and package transfer share resources; the desired count is not the currently available capacity. |
| Worker scaling | 2-20 tasks, CPU target 35%, Fargate Spot | This is existing infrastructure. CPU-only scaling may not react to network/provider-bound backlog; Spot interruption recovery must be exercised. |
| Database | Aurora PostgreSQL 15.15, one writer, serverless 0-4 ACU | Measure publication locks, connection/pool waits, and page-vector query plans; configured limits alone do not establish available capacity. |
| Worker embedding calls | Local default 8 per M0 Job, Worker task concurrency 10 | Up to 80 concurrent page-embedding calls per task container are theoretically possible; 160 at desired count 2 and 1,600 at scaling ceiling 20. These are upper-bound arithmetic, not observed demand or provider quota. |

For the preceding 24-hour window ending 2026-10-26 17:28:35 UTC, AWS/ECS
Worker service metrics had 24 hourly samples: mean of hourly CPU averages 2.94%,
CPU maximum 98.96%, mean of hourly memory averages 18.33%, and memory maximum
42.24%. These service-level historical values do not prove progressive headroom;
the new task revisions have not started successfully in the observed deployment.

The immediate deployment blocker is configuration, not a demonstrated need for
larger servers. PR #471 added `VERCEL_KEY` secret injection to both task templates;
ECS explicitly reports that key missing from the referenced JSON. This startup
failure affects the container before request-mode selection, including full-mode
service startup. Supply a valid value in the owning secret through the normal
credential-management process, verify secret/IAM references and provider access,
and verify healthy API/Worker rollout before claiming deployment success. A
task-definition reference alone is not evidence of a usable credential. Never
print secret values in diagnostics. No credential or deployment was changed here.

Capacity work should target demonstrated amplification:

- Each scope currently downloads the whole source PDF and supporting inputs.
  Measure network bytes, scratch-disk high water, and repeated work at document
  size and scope count; do not introduce a shared filesystem or cache without
  evidence that the existing approach misses the required envelope.
- Scope publication rebuilds revision metadata under a document lock. Measure
  lock duration, database pool waits, and query contention before redesigning it.
- Initial `done` releases the foreground admission slot while background work
  remains. Test a burst of newly admitted Jobs while prior Jobs enrich; testing
  a single fixed batch misses this accumulation.
- Queue priority does not reserve resources or preempt running work. Verify that
  background tasks do not unacceptably delay initial parsing or retrieval.
- Measure embedding gateway concurrency, rate-limit responses, and retry volume.
  Reuse existing quota primitives for an aggregate bound if required by measured
  load/provider limits. The existing VLM limiter does not establish this bound.
- Retaining two published snapshots adds object storage and transfer cost.
  Measure original/baseline/final sizes, Hipo Blob copies, model usage, and elapsed
  time to client readability rather than only upstream Job `done` time.

Do not preemptively resize servers or add a rollout framework. Existing
`ingestion_mode` continues to select the behavior; measure first and tune existing
concurrency/capacity only when justified. The missing credential, recovery gaps,
and incomplete Hipo lifecycle are concrete work; capacity insufficiency remains
unproven.

## Acceptance sequence

1. Run focused contract checks for queued-dispatch interruption, duplicate
   delivery, expired claims with a late publisher, vector repair after M0,
   finalization upload failure, and post-commit cache invalidation. Verify no
   repeated usage settlement and no premature final success event.
2. Verify duplicate/out-of-order/lost webhooks, stale Source mappings, missing
   required assets, interrupted SDK transfer, and old citations after the final
   switch. Verify required query-embedding failure reaches the caller in classic
   and agent retrieval even when lexical hits already exist.
3. Use real Hipo browser acceptance for a text-native PDF, a scanned PDF, and
   PPTX. Demonstrate early readability, background operation with the browser
   closed, final update, failure-only retry, and the accepted status wording.
   Keep initial and final processing charges reconciled as one ingestion.
   Include the agreed initial/final Tree View, resource filters, current display
   mode and page selection across refresh, default citation navigation to the
   enhanced same page, and explicit old-snapshot provenance inspection.
   Run Ask before final enhancement, including missing initial vectors and an
   upstream scope already refined while Hipo still shows M0. Verify that each
   retained citation opens its corresponding source page and required visual
   evidence, including text-native baselines without initial page-image assets.
   Existing source-page fallback is a useful primitive, not proof of acceptance.
4. Compare full and progressive with the same permitted fixtures. Include small,
   representative, and large documents, multiple scopes, and one then several
   concurrent Jobs. Exercise the configured upper envelope after lower-load
   checks pass. Add simultaneous retrieval and successive admissions after M0.
5. Record time to upstream M0 and Hipo readability, time to final completion,
   queue age, task CPU/RSS/disk, database connections and lock waits, retrieval
   latency/errors, embedding requests/retries, and total model/storage/transfer
   usage. Establish a supported workload from results; do not infer it from the
   autoscaling maximum. No real progressive load acceptance has run in this plan.

Use the existing configured resource envelope first. Require recoverable task
interruption, intact required assets, consistent snapshots, and no silent
partial-success search on dependency failure. Sustained resource saturation,
unbounded queue growth, or a material foreground regression requires tuning and
retesting before enabling Hipo progressive submissions.

## Validation and deployment checks

- Recheck CI on the merged revision; do not carry forward the earlier failed-run
  result after the test-environment fix.
- Verify required embedding credentials and database extension compatibility
  before enabling progressive requests. Previously observed production settings
  are historical evidence, not proof of current configuration.
- Validate the agreed behavior using focused recovery and retrieval contracts.
- Include a real Hipohipo progressive view acceptance: scan-image rendering,
  text-native baseline rendering, Blob commit visibility, background completion,
  and safe snapshot refresh. Existing full-mode synchronization tests alone do
  not establish progressive compatibility.
- Measure M0 and background work separately under representative document sizes
  and concurrent ingestion plus retrieval. Adjust resources only when measured
  evidence warrants it.

## Decision records

ADR 0011 records automatic full-document background enrichment. ADR 0012 records
the accepted viewable baseline and distinct initial/client readiness milestones.
ADR 0013 records a single reader refresh after final completion. ADR 0014 records
two distinct completion webhooks on the same Job and callback. ADR 0015 records
retention of baseline and final snapshots for citation provenance; its original
default-opening rule is superseded by ADR 0022. ADR 0016
records failure of the retrieval request when required query embedding fails.
ADR 0017 permits viewable initial publication with missing vectors and requires
background repair.
ADR 0018 preserves reading after terminal background failure, allows failure-only
manual retry, and records the retrieval-enhancement wording.
ADR 0019 reuses Hipo's ten-minute server recovery cadence for pending progressive results.
ADR 0020 selects fifteen-second Hipo-local checks while the client page is visible
and enhancement remains pending.
ADR 0021 gates final success on refinement, missing-vector repair, and durable
result publication, followed by Hipo's local snapshot commit.
ADR 0022 makes existing citations open the enhanced reader at the same original
file version and physical page by default, retaining old snapshots only for
explicit provenance inspection. This accepted follow-up is not yet implemented.
Record further durable trade-offs after agreement. Keep domain definitions in `CONTEXT.md` and
implementation choices in this plan or an ADR.
