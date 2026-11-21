# Baseline and enrichment publication

This is the behavioral spec for publication and retrieval of one document
revision. Terms live in `GLOSSARY.md` and
`CONTEXT.md`. The decision and the rejected alternatives live in
[ADR 0030](../adr/0030-search-baseline-and-enrichment-without-a-namespace-lock.md)
and
[ADR 0031](../adr/0031-use-one-completion-notice-for-ingestion.md).

## Documents

Private ingestion publishes a new document with its own `document_id`.
When a caller reparses a saved original, the caller coordinates adoption
of the new document and archival of the previous one.

The reserved Demo Catalog has a maintainer-authorized ingestion path keyed
by `data_id`. Existing catalog entries keep their identity and published
bindings. Exercise catalog publication and Hipo Featured reads alongside
tests for private Reparse.
[Demo ingestion scope](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/services/document_ingestion/demo_scope.py#L28-L90)

A document has one revision. The baseline and later enrichment are two
representations of that revision.

Existing documents use the same retrieval and publication paths as newly
ingested documents. Their currently published chunks and index records are
the starting point, whether the available content is baseline, enriched, or
fully parsed. Existing Chunk IDs and successful job results are preserved.
Subsequent enrichment adds its own chunks and index records using the same
publication rules for all documents. Earlier representations that the old
publisher already replaced require no backfill or reparse.

### Reparse a Hipo Source

Reparse keeps the Hipo Source identity and creates a new Knowhere document
from the saved original in its current authorized index location. The
[current client](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/resumable-parse-client.ts#L449-L456)
sends `document_id` with `reparse: true` to parse into the existing document.
Submit a new-document job and use the old ID only to authorize a scoped
exception to duplicate admission. The
[current admission check](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/document_ingestion/source_content_admission.py#L141-L174)
rejects matching bytes when the old document is active. Limit the exception
to the exact active document in the same authorized owner and namespace.
Keep duplicate checks against other documents and in-flight jobs.

Keep the old document and its synchronized result available through parsing,
initial indexing, and Hipo synchronization of the replacement. Once the new
result, including its initial directory where applicable, is synchronized,
atomically publish the new document and snapshot binding on the same Source.
Keep the old document and snapshot identities addressable for authorized
historical references. The
[current evidence resolver](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/resolve-ask-evidence.ts#L75-L88)
uses the live document ID for the ordinary unmoved Source, so resolve older
pins using their saved document and snapshot together.

After the new binding commits, archive the previous Knowhere document using
its recorded ID and index location. Use the existing archival behavior below
to remove it from retrieval and retain its stored bodies. Record pending
archival durably so a failed call can be retried without parsing or swapping
again. Guard publication by the current operation and Source binding. A late
or repeated completion must never archive the document now bound to the
Source. A failed replacement leaves the old result current and searchable.
If a replacement is abandoned after Knowhere publishes it, archive that
unadopted document after checking that it is outside the current binding.

Exercise Reparse with identical bytes and a ready Source. Pause or fail the
replacement before Hipo synchronization and verify the old result remains
current. Confirm the successful path creates a distinct `document_id`, keeps
one Hipo Source, archives the old document after the binding commits, and
still opens a citation issued against the old result. Exercise an archival
failure and a duplicate completion, and confirm that a normal duplicate
upload still fails its existing admission check.

## Document archival

Archival succeeds after the document's archived status is committed.
Retrievals started after archival succeeds exclude the document from both
fresh and cached results. The document's status governs eligibility even
when its baseline or enrichment index records are still present. Maintenance
uses an exact `documentId` match in the document's namespace index to delete
all of those records in the background. Existing and newly published records
use the same `keyword` field. The document identity in the search records is
sufficient to target deletion. After writes already in flight settle,
maintenance can check the same document identity again for remaining records.

A retrieval already in progress may finish using the document bodies it read
before archival committed. Any further reads of the document for that
retrieval use its current archival status.

The existing
[reference lookup](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/hydration/reference.py#L85-L106)
only checks active document status when a revision is unpinned. Apply the
current status check when an Agent reads new evidence after archival, even
if the Agent started with a pinned revision. Test by letting it read one
body, archiving the document, and then asking it to load another body.

If archival interrupts initial indexing after the parsed output is stored, end the
unfinished job with error code `ABORTED` and reason `DOCUMENT_ARCHIVED`. Emit
`job.failed`. Archival after ingestion has succeeded preserves that success.

For example, pause a document's enrichment index write after the first
batch and archive the document. A query started after archival succeeds
omits the document, including a repeated query with a cached result.
Resuming the index write leaves the document archived. An Agent that read
its body before archival can still finish that answer.

Also archive a document while its initial indexing is in progress after
parsing. Verify that the job reaches the failed outcome once and a late worker
completion leaves the document archived. Exercise deletion by `documentId`
against both an existing and a newly indexed document, with a second document
in the same namespace to verify that its records remain searchable. During
the cleanup delay, ranked results still surface eligible documents when
archived records would otherwise occupy the highest candidate slots.

## Parsing completion

Both ingestion modes expose the same successful job outcome. The API uses
`done` and `job.completed`. Hipo presents it as Parsing Complete after
the initial result has synchronized.

- Full ingestion reaches Parsing Complete when the complete parsed body is
  stored and its initial index is searchable.
- Progressive ingestion reaches Parsing Complete when the baseline body is
  stored and its initial index is searchable.

For a new progressive PDF or PPTX, the baseline contains coarse chapter
chunks. Knowhere routes both formats through the
[same page-memory worker](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/services/document_ingestion/command.py#L11-L45).
The worker converts PPTX to PDF, extracts page content, and resolves
chapter ranges before the first publication. Use the existing
[whole-document structure fallback](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/page_memory/phase_a.py#L102-L122)
when chapter recognition finds no hierarchy. The first published body and
index use the coarse ranges, so the first baseline hit already identifies
an enrichment unit. Chapter chunks retain physical page or slide references.
Retain the embedded media and its page links that the
[PPTX worker already attaches](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/page_memory/memory_service.py#L133-L146)
to the initial result.

For either format, send `job.completed` when the coarse chunks and initial
directory have committed, the baseline package is available, and the initial
index is searchable. Hipo fetches and stores the coarse directory during
initial result synchronization. Deliver `job.document.directory_ready`
independently for directory subscribers. Hipo includes the Source in Ask
after it synchronizes the initial result and marks the Source ready.

The API, webhooks, SDK waits, and Hipo status display represent one
successful completion. Initial index visibility is a prerequisite for that
outcome. Search and on-demand enrichment do not introduce completion states
for the Source. The directory event continues to synchronize navigation.

Workers record checkpoints for retained parsing output, index writes,
enrichment attempts, and recovery. These checkpoints drive retries,
visibility, and cache updates. Once ingestion succeeds, on-demand
enrichment can add searchable results while the Source stays ready.

Pause chapter recognition during progressive PDF and PPTX jobs. Verify that
ingestion is still in progress and Hipo keeps the Source out of Ask.
Resume recognition and verify that the first published chunks are coarse
chapter chunks. Check original-page citations, the directory fetch, and
that the first Ask hit starts enrichment for its chapter. Exercise both
webhook delivery orders and measure upload-to-Ask-ready time on the same
inputs as the previous path.

Include a deck without recognized chapter headings and a deck with embedded
media. Verify that the first ready result supports original-slide citations
and that the first package includes the existing media links.

Verify that full and progressive jobs expose the same success state in the
API, webhook, SDK wait, and Hipo. After a progressive Source is ready,
exercise partial publication, success, and failure of a later enrichment.
Hipo continues to show Parsing Complete throughout.

The ZIP from that notice stays fixed. Full ingestion keeps the complete
package. Progressive ingestion keeps the baseline package. Finished enrichment
units do not cause the ZIP to be rebuilt.

## Initial indexing retry

If the initial index write fails after parsing output is stored, retain
the body, directory where applicable, and result package. The existing
indexing retry uses the same job and document and writes the index from
those retained outputs. It incurs no additional parsing charge.

Knowhere already exposes the failure stage and
[`POST /v2/jobs/{job_id}/indexing/retry`](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/api/v2/routes/jobs.py#L90-L99).
Hipo's
[failure webhook](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/app/api/internal/webhooks/knowhere/parse/route.ts#L14-L47)
reduces the failure to a code and the
[polling adapter](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/resumable-parse-client.ts#L266-L289)
can reduce it to a generic parse failure. Hipo's
[Retry action](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/project-documents.ts#L845-L883)
can then submit the original file again. Preserve the failure stage and job
identity from both observation paths. Route eligible initial index failures
to the indexing retry for that job. Keep that path separate from retrying
the parser and retrying a Hipo copy. Honor Knowhere's existing retry
eligibility, including an archived document's failed outcome.

Hipo currently
[ignores completion callbacks for terminal failed operations](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/durable-parsing.ts#L908-L936)
and releases a parse reservation when it
[settles a failure](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/durable-parsing.ts#L381-L427).
The same-job retry must let Hipo process a later success through an
explicit recovery of that operation. Account for its original allowance
settlement. A repeated click, timed-out retry response, or late failure
notification must leave the job's actual outcome and usage intact.

For each ingestion mode, fail the initial index write after the parsed
output is durable. Verify that the API has emitted no successful completion
and Hipo keeps the Source out of Ask. Exercise Hipo Retry once after a
webhook observation and once after polling with the webhook withheld.
Verify that each retries the same Knowhere job, the parser and parsing
charge each occur once, the job reaches `done` once, and Hipo can retrieve
the Source after syncing it.

## Moving a Source between Brains

Hipo allows a move once the receiving Brain authorizes it and the
Source's initial published result has synchronized. For progressive PDF and
PPTX ingestion this is the coarse chapter result. For full ingestion it is
the complete result. Preserve the Source's verified index binding and existing
ownership checks. A Source can move before any unit has been enriched,
while enrichment runs, or after an enrichment failure.

The current move transaction checks enhancement fields and unfinished parse
and sync rows. Check completion of the current Source's initial
synchronization and preserve destination access and index binding checks.
Remove the move veto for legacy final-snapshot work and its retry eligibility.
Verify the ready coarse copy can move with a stale pending or failed
final-snapshot operation and a late callback from the origin cannot overwrite
the moved Source's binding.
[Hipo move transaction](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/sources/move-indexed-source-in-transaction.ts#L100-L153)

Move a synchronized progressive PDF that has never been enriched.
Verify that the target Brain can open the directory and search the Source
and that the original Brain no longer lists it. Repeat while enrichment is
running and after it has failed. A move to a Brain without permission
leaves the Source in its original Brain.

## Publication

1. Full ingestion writes the complete body and index. Progressive ingestion
   writes the baseline body and baseline index. Either way, the one
   completion notice is sent.
2. Retrieval searches the namespace index and takes no lock. A hit on a
   baseline unit starts enrichment of that unit. A unit nobody has retrieved
   stays on the baseline. The ingestion job does not continue into enrichment.
   A unit has at most one enrichment running. A successful attempt produces
   one enrichment body for the unit. Different units of the same document can
   run and publish independently.
3. Enrichment parses the whole unit locally and stores its body before writing
   its index beside the baseline in batches. Each record can participate in
   retrieval as soon as it is searchable. The unit's baseline remains eligible
   throughout publication. A PDF or PPTX unit is one baseline page range.
   A spreadsheet unit is one sheet group. Text, Markdown, Word, and standalone
   audio and video use the whole document.
4. Other units of the same document stay on the baseline.
5. A unit is fully published once all its enrichment index records are
   searchable. Its covered pages can then contribute to baseline hit
   exclusion. Both representations of an active document keep their
   index records and stored chunks.

Each document writes only its own index records. Publication does not rewrite
a shared namespace roster. One document's publication does not block another
document or a retrieval.

Apply the document status and current revision check to the candidates
selected for retrieval. Currently published records and stored chunks
provide the starting point. Query the authorized namespace with the
request's document and section filters. Retire the namespace manifest and
the per-request namespace-wide build allowlist. Validate hits against
document, revision, and attempt eligibility before they supply evidence.

The old
[inline reader](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/lexical/reader.py#L75-L128)
loads every active document's revision and build before searching. Removing
only the manifest lookup leaves that namespace-wide work in place. Resolve
document identity and status for the request's relevant scope and returned
candidates. Verify the ordinary classic and Agent recall paths in small and
large namespaces. Inspect the SQL and OpenSearch requests for a namespace-wide
build enumeration and measure retrieval under concurrent writes.

An existing
[lexical unit](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/lexical/unit.py#L6-L18)
references a section, and current
[hydration](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/lexical/discovery.py#L141-L176)
returns the chunks that the section owns. Specify the mapping from each
incrementally published index record to its exact chunks before implementing
the writer. Keep that mapping independent of the chapter task boundary.
Reuse the existing scoring granularity where it can support incremental
publication. If the implementation changes what BM25 scores as a record,
compare ranking and duplicate crowding against the current corpus replay.

The 2026-11-20 production inventory found two noncurrent index records
alongside 127,136 current private-corpus records. Both belonged to an older
revision of the same active document. The OpenSearch count checked all 351
noncurrent builds of active documents recorded in PostgreSQL across 21
affected namespaces. The residuals were 0.23% of the affected namespace's
records. These two records are accepted under the current-revision check
and require no dedicated cleanup.

### Standalone audio and video

Keep the current format support and mode selection. The
[Knowhere API](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/services/document_ingestion/command.py#L14-L45)
accepts progressive media. The
[Hipo parse client](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/resumable-parse-client.ts#L449-L456)
uses full ingestion for audio and video under its current progressive MIME
allowlist. Full media ingestion runs the existing chaptering pass as part of
parsing and sends one completion after the initial index is searchable.

For progressive media, the first body is the existing
[flat transcript split into timestamped chunks](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/document_parser/formats/media/parser.py#L59-L97).
Publish the body and its initial index, retain the transcript input, and
send `job.completed` once the initial package is available and the index
is searchable. The whole file is one enrichment unit. The first retrieval
returning a transcript chunk starts one chaptering task that
[reads the retained transcript](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/document_ingestion/chunk_enrichment_service.py#L352-L374)
without running speech recognition again. Write the chaptered body and index
beside the baseline in the same revision. Keep timestamp citations and the
initial ZIP available. Recovery resumes requested work under the shared
attempt limit.

The parser derives the
[chunk content identifier](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/document_parser/formats/media/parser.py#L132-L165)
from the transcript text and its time range. The same segment can have the
same identifier under both the original and chaptered paths. Keep each
published row addressable in its own representation so existing references
still open their original bodies. During assembly, omit a baseline hit only
when the selected, fully published enrichment supplies the same transcript
content at the same source time range. A matching timestamp alone leaves
the baseline eligible.

Exercise progressive audio and video through the Knowhere API. Verify that a
transcript hit with its timestamp is available at completion, an idle file
stays on the baseline, and concurrent first hits start one chaptering task.
Verify that speech recognition runs once, old and new references resolve
to their respective bodies even when chunk content identifiers match, and
the ZIP and successful completion stay fixed. Include a full media job in
rollout coverage.

## Concurrent enrichment

An enrichment request identifies an existing unit of a document revision.
Use the unit's state to enqueue it once and to grant a single worker attempt
the right to publish. Repeated retrievals of the same unit can return
available results while that attempt proceeds. The transaction that claims
the unit ends before parsing or index writes begin. Different units of the
same document claim and publish independently.

Each index hit resolves to the exact chunk bodies and attachments published
with that hit. Baseline and enrichment may share a section title or source
path and still have different bodies. Existing references continue to
resolve to their original bodies and attachments.

Keep revision-wide sort order normalization, media reconnection, and
map-unit or page-vector replacement off the per-unit publication path.
Page and attachment associations must stay usable by both representations.
A unit can publish a link to a shared boundary page or asset while its
neighbor keeps its existing link.

The claimed attempt owns its stored body and index records. Completion
updates the unit only while that attempt still owns it. When recovery
confirms an incomplete attempt and supersedes it, exclude its late index
writes from results. The replacement attempt publishes its own records, and
failure cleanup removes the superseded attempt's records. These checks use
unit state, with the document's active status and revision checked when
returning a hit.

Exercise both cases when validating concurrency. Simultaneous searches that
hit the same unit should produce one publication attempt. Pausing a worker
while it writes one unit should allow another unit of the same document to
finish publication and be retrieved. Use adjacent PDF units that share a
boundary page and verify text, visual, and asset references on both. Include
a baseline and enrichment chunk with the same source path and open the
references produced by each hit. Compare retrieval latency before and after
the change under concurrent publication, including baseline hits that start
enrichment.

## Enrichment recovery

If a worker stops before recording the outcome, recovery checks that the
document is still active, the attempt's body is stored, and all expected
index records from that attempt are searchable. When those checks pass,
recovery records full publication and applies the corresponding cache
invalidation. It completes the existing attempt. While the write outcome is
uncertain, the baseline stays eligible for retrieval.

If an enrichment attempt fails, the baseline stays eligible for retrieval
and the job's completion notice stays as it was. Failure cleanup removes the
attempt's partial index records. Already-returned references can still open
their stored bodies. An interrupted write confirmed to be incomplete counts
as a failed attempt. The next retrieval returning that unit's baseline
can start one new attempt while the unit has an attempt remaining. Apply
the same trigger to an eligible cached baseline response. After three
failures and cleanup, the unit stays on its baseline. Recovery can resolve
an interrupted claim or redeliver work already requested by retrieval.

The current retrieval transition queues only coarse scope rows, while the
sweeper can queue a previously hit failed scope. Apply the retrieval trigger
to retryable failed units, including whole-file units. Make that transition
conditional on the unit state and remaining attempts so concurrent hits
claim one retry. Once failure is recorded, leave it for a new hit. Keep
broker redelivery and interrupted-claim resolution in recovery.
[Current retrieval handoff](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/scope_enrichments.py#L514-L566),
[current failed-scope sweep](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/scope_enrichments.py#L764-L811)

Report each recorded attempt failure to Hipo through the nonterminal
`job.enrichment.failed` event. The current
[Knowhere event schema](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/models/schemas/job.py#L15-L22)
and [Hipo callback](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/app/api/internal/webhooks/knowhere/parse/route.ts#L14-L47)
have ingestion failure and enrichment completion events. Use the existing
authenticated webhook delivery and outbox with an identity per failed
attempt. The [outbox](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/webhook_outbox.py#L37-L70)
currently deduplicates most events by job and event type. Record the
notification with the attempt's failure transition. Include the job,
document, unit, attempt, error code, and remaining retry eligibility. Hipo
records the failure for handling and deduplicates callback redelivery.
Preserve Parsing Complete, the current snapshot, and normal search access.
The callback does not enqueue enrichment.

For example, stop the worker after the last batch becomes searchable and
before it records completion. Recovery marks that attempt successful and
the next identical request runs a fresh search if it only has the old
cached result. If the worker instead stops with an incomplete write, the
baseline stays eligible and the failed attempt's index records are cleaned up.
Verify Hipo receives two distinct failures for two failed attempts in the same
job, including duplicate delivery of one event. Repeat the last query after
a failure, including a cache hit, and verify it can start one retry without a
Hipo retry request. Exercise the last allowed attempt and confirm later
retrievals still return the baseline.

## Retrieval result cache

While an enrichment unit is publishing, a fresh search can return any of
its searchable records. An identical request can reuse an unexpired cached
result during this time. After full publication, a new request can reuse
only a result computed with the completed publication available. An older
cached result gives way to a fresh search. A search started before full
publication may finish with its earlier results. Its late cache write cannot
supply results to requests that start after publication completes.

For example, cache a query while a unit has only its baseline. After the
first enrichment batch is searchable, repeating the query can still return
the cached baseline. Once the entire unit is published, repeating that query
runs retrieval again if the old result is the only cached result.

When an attempt is confirmed failed or superseded, subsequent requests
exclude its partial hits. That applies to direct search, visual results,
Agent evidence reads, and cached responses. A late response computed
before failure cannot make those hits eligible for new requests by writing
them to the cache. Previously returned references still resolve to their
stored bodies.

The same visibility guarantees apply when a cache invalidation fails or
arrives late. Retrieval can bypass a cached response when it cannot
establish that the response is current.

The first additive OpenSearch implementation uses fresh reads for private
retrieval because the old result cache cannot prove attempt and archival
visibility. Measure that path with the replay workload before deployment.
Reuse cached results once their freshness can be established without a
namespace-wide build enumeration.

Cache a response containing a hit from the first enrichment batch. Fail
the next batch and repeat the query after the failure is recorded, while
index cleanup is still pending. Verify that the query can return the
baseline or another eligible hit. Repeat with a cache write delayed until
after failure.

Save a reference to that first-batch hit and use a query that matches
only the failed body. After failure, check ranked search, visual search,
the small-corpus shortcut, and Agent text and asset tools as applicable.
None should discover the failed content. Open the saved reference with
the original authorization and verify its body and attachments.

## Text and visual retrieval

For an uncached search, query OpenSearch for text. When visual retrieval is
enabled, the result types allow it, and the authorized scope contains
eligible page vectors, query PostgreSQL for visual matches in parallel.
Decide visual eligibility before either branch returns matches. A small
corpus with eligible page vectors uses ranked retrieval so both channels
can contribute.

Both branches use the same authorized scope and current-document rules.
Concurrent database operations use independent sessions. Merge eligible
hits through the existing ranking logic after both branches complete.
Combine text and visual signals for the same stored chunk. Distinct
baseline and enrichment chunks remain eligible for the result selection
below. Preserve the existing required query-embedding error behavior.

Keep repair of missing original-page vectors independent of chapter
enrichment and the frozen ZIP. The existing repair renders pages from the
saved source PDF, but its scheduling depends on document-wide enhancement
state. Carry its pending-page checkpoint into the new ingestion flow so it
can recover for an active revision even if none of its chapters is retrieved
or a chapter has exhausted retries. When a repaired page becomes eligible,
invalidate older results as the current repair does. Test a partial vector
failure in a PDF or PPTX whose initial text index is ready. Let repair run
before any chapter hit, then verify a previously cached query can discover
the page visually.
[Current recovery filter](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/scope_enrichments.py#L813-L864),
[page-vector repair and cache update](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/page_memory/scope_enrichment_service.py#L1490-L1527)

Test classic retrieval and Agent recall with an eligible scan and a native
PDF that has page vectors. Hold either search branch open and verify that
the other starts. Include a scan that yields a visual hit without a text
hit, a scope with no eligible vectors, and a scope with both signals and
fewer stored chunks than the requested result limit.
Compare results and latency with both branches eligible.

## Assembly

A result can contain baseline hits and enrichment hits from the same unit.
For example, while a 5–10 page unit's enrichment index covers only pages 5–6,
both the enrichment records for those pages and the baseline for pages 5–10
are eligible for retrieval. The baseline still covers pages 7–10.

Prefer enrichment when the same response includes hits from fully published
enrichment that clearly replace a baseline hit's coverage. The retrieval
service can omit that baseline hit during assembly. Use the candidates
already available to the request. When coverage is uncertain or the
replacement hits do not fit within the final result limit, both
representations can remain eligible and overlap in the response.

- Page-range unit: the enrichment hits included in the response must cover
  every original physical page in the baseline hit. The returned content can
  include source content carried forward as a fallback. A page number alone
  leaves the baseline hit eligible. A shared boundary page can appear in both
  the finished enrichment and the neighboring baseline hit.
- Whole-document unit: apply the same per-hit rule using the source content
  of the baseline hit. An enrichment hit elsewhere in the document leaves
  that baseline hit eligible.

Apply exclusion while selecting the final results. Each returned hit loads
its own stored body. The retrieval service returns the assembled response
to all clients. Both representations keep their index records and chunks.

For example, a refined 5–10 page unit can count page 9 as covered when a
returned enrichment hit carries the original content of page 9 forward.
If the response lacks that content, the baseline hit covering pages 5–10
stays eligible even when the refined index is fully published.

Replay representative queries with baseline and enrichment both searchable.
Inspect whether overlapping hits crowd other relevant documents out of
the first results. Overlap remains acceptable under the result selection
rules above.

## Live document read

A document's chunk list can return baseline and available enrichment
chunks from the same unit. Fresh enumeration follows the same eligibility
rules as retrieval. Each previously returned chunk reference opens the
stored body that produced it, including a retained body from a failed
attempt or a baseline chunk omitted from a search response.

The v1 document chunk list currently defaults to the latest parsed revision
and counts and lists its rows by revision. Default to the current published
revision. Apply eligibility before counting and paginating, including on
revision-pinned bulk reads. Keep exact, authorized historical chunk lookups
by their stable row identity. Sign page, figure, and attachment URLs against
the stored artifact origin for that exact row. The current service uses
the result's raw prefix for each row in the revision. Keep enrichment
attempts' artifact locators addressable alongside the initial package.
[Current document chunk service](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/services/documents/lifecycle_service.py#L369-L483),
[chunk count and pagination queries](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/repositories/document_repository.py#L125-L176)

Verify a document awaiting initial indexing supplies no fresh list results.
Verify a failed attempt is excluded from both the count and the paginated
list while an already issued exact reference opens its original attachment.
Use artifacts with the same relative path written by different attempts to
check that the reference opens the right bytes.

Adjacent sections already share their boundary page in that list, whether
both are baseline or both are enriched. The earlier section stores that
page's text. The later section points at it, so the two entries match. While
one section is enriched and the neighbor is not, those entries can differ.

## Directory refresh in Tree View

Knowhere provides the latest published directory for the document revision.
Publish a newer directory version when successful enrichment changes the
chapter structure. The directory reflects those structural changes and
preserves the original-page references used by the viewer.

Update the Knowhere directory response schema and Node SDK response type
together. The
[Knowhere response](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/models/schemas/document_directory.py#L20-L29)
and the SDK type used by Hipo currently describe only versions zero and
one. Hipo's
[directory decoder](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/directory/decode-knowhere-directory.ts#L3-L20)
already accepts an integer. Test a first directory and a newer one through
the actual SDK call that Hipo uses.

Two independently published units contribute both of their directory changes,
regardless of which finishes first. Keep the rest of the tree available,
including its baseline entries. The version returned by Knowhere identifies
the exact tree in the response. Publish only the changes from successful
units so a failed attempt cannot remove or rename an existing entry.

When the user opens Tree View for a Source, render the saved Hipo directory
and start one background refresh request with the displayed directory
version. The Hipo endpoint checks the current Knowhere directory within
that request and returns either a newer directory or an unchanged response.
All directory version checks belong to directory reads and refreshes.

Use the Source's initial published result to identify its current revision
for directory and page reads. The same refresh can load an initial
directory that has yet to be saved in Hipo. Keep historical snapshot
identities usable for existing citations when retiring the public
enhancement status.

Apply the same Source and Brain read authorization as the existing tree.
Resolve the upstream namespace and credential from the Source's verified
index binding. This applies after a Brain move, when the displayed Brain and
the namespace that owns the index can differ.

If the directory has changed, save the newer directory in Hipo only if it
still belongs to the Source's current revision, then return it to the
browser. Apply the response only if it belongs to the displayed Source and
revision and is newer than the displayed directory. Keep rendering the saved
directory while the request runs. On an unchanged response or a check
failure, continue displaying the saved tree.

Open Tree View with a saved directory and hold the Knowhere response.
Verify the saved tree remains usable. Return a newer directory and verify
that the same open tree updates. Repeat with the same version and with a
failed upstream request. Verify the displayed tree stays usable in both
cases. Switch Sources before a delayed response returns and verify the new
tree stays bound to its own Source.

Create a ready Source with no document-level enhancement state. Open its
saved tree, follow a page reference, then move the Source to an authorized
Brain and refresh it there using its original index binding. Verify that
a viewer without read access cannot fetch its directory. Verify the first
Tree View refresh also fills a missing Hipo directory after the initial
directory notification was missed.

Finish two enrichment units that change different parts of the same tree.
Run the scenario in both completion orders and verify the next refresh has
both changes. Repeat with one failed attempt and verify that only the
successful change appears.

## Hipo page reading and citations

Use the same page reader for newly published progressive PDFs and PPTX
Sources. Knowhere's
[page-memory track](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/page_memory/memory_service.py#L93-L129)
converts PPTX to PDF and retains `source.pdf` for both formats. Hipo reads
the original physical pages from its saved initial package. Use the saved
`page_assets` entry for each page that has an image. Render the corresponding
page from `source.pdf` when that page has no image. The initial package
keeps its page mapping and can contain incomplete or empty page-image coverage.

Select the image or PDF rendering for each physical page. The existing
[PDF card fallback](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/modules/reader/parsed-chunk-card.tsx#L39-L55)
checks for an empty image array and selects the chunk's first page. A
coarse chapter can span multiple pages and have images for only some of
them. Test that even a missing page in the middle of that chapter can open.

Citations for these Sources use the original document revision and its
physical page numbers. This applies to text, table, and image hits, including
hits published after Hipo saved the initial package. Open the corresponding
page and show a highlight when matching coordinates are available. Retire
the separate viewer and fetch paths for newly extracted figures and tables
in this page reader. Knowhere continues to store and retrieve their exact
content and serve its existing chunk references. Hipo preserves resolution
of references already issued to independently stored historical assets.

Apply the page rule to citation-region inspection and browser preview. The
existing inspector prefers a standalone figure for image hits and can fall
back to another page's image. Resolve the exact physical page from the
authorized initial snapshot, including for an enrichment chunk that never
existed in the saved `chunks.json`. Use the same page coordinate space for
inspection and display. If the page or a matching region is unavailable,
keep the citation without a highlight. Historical previews resolve the
saved document ID and snapshot together through existing access checks.
[Citation-region input](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/ask/image-inspection.ts#L139-L175),
[historical preview binding](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/read-source-preview.ts#L49-L79)

Progressive DOCX also retains a
[converted PDF and per-page text](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/document_parser/formats/docx/baseline.py#L50-L70).
Its
[enrichment page alignment](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/worker/app/services/document_ingestion/chunk_enrichment_service.py#L479-L498)
can leave hits without reliable page numbers. Keep DOCX's existing chunk
reading routes for those references. Full ingestion, legacy DOC and PPT,
and other formats use their existing reader behavior.

Exercise PDF and PPTX with all images present, no images, and partial
coverage within a single chapter. After a new fine hit, open a text, table,
and image citation at its original page, including from an authorized
shared Brain. The target page opens using the saved initial package.
Also open an older saved asset reference and a DOCX reference without
a page number.

## Hipo implementation plan

Implement the Hipo changes with the Knowhere publication changes. Preserve
the published result identity of each synchronized Source. Existing
progressive Sources can read the Knowhere `jobResultId`, Hipo `snapshotId`,
and package checksum already stored with them. The new ingestion contract
uses the initial result's identity for synchronization and records no
document-wide enrichment progress.

1. **Decouple initial synchronization from enrichment.** The current
   [parse result publisher](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/parsing/durable-parse-adapters.ts#L25-L57)
   requires both a snapshot and an enhancement payload to request a pinned
   copy. Use the initial snapshot from the single completion outcome to
   start or retry synchronization. Preserve the same Source and snapshot
   when an initial copy fails. Populate the Hipo directory from the coarse
   result during initial synchronization.
   The sync repository also guards admission and ongoing writes against the
   old enhancement target. Carry the initial snapshot identity into these
   guards. Keep rejecting a stale copy after Reparse, deletion, or a Brain
   move using the operation and verified source binding.
   [Sync ownership guard](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/parsing/drizzle-document-sync-repository.ts#L200-L206),
   [sync admission](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/parsing/drizzle-document-sync-repository.ts#L1122-L1149)
2. **Route reading and citations through saved snapshot identity.** Move
   the lookup used by
   [Ask citations](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/ask-library-search.ts#L244-L258)
   and
   [historical previews](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/read-source-preview.ts#L16-L24)
   to published snapshot metadata. Use the availability of the prepared
   snapshot to choose the existing page-scoped read path. Keep stored
   snapshots addressable for older references under their existing access
   rules. Add the synchronous Tree View check and the progressive PDF/PPTX
   page reader described above.
3. **Update Brain movement, sharing, and export.** Verify the indexed
   revision using its Knowhere job result and namespace after a move or
   when opening a Brain for sharing. The
   [sharing inventory](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/brain-sharing/read-brain-sharing-inventory.ts#L102-L121)
   currently gets that ID from the displayed enhancement snapshot. Use the
   initial synchronization and destination authorization checks for moving.
   Pin exports to the saved snapshot with its identity, checksum, and
   required package contents, as in the existing
   [export reader](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/export/blob-project-snapshot-reader.ts#L119-L141).
4. **Retire the document-wide enhancement lifecycle.** Show Parsing
   Complete after the initial result has synchronized. Remove document-wide
   enhancement status badges, polling, and final-snapshot synchronization
   from the new flow. The
   [current recovery task](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/application/enhancement-recovery.ts#L30-L66)
   also reconciles missing directories. Initial directory synchronization
   and the Tree View check provide that recovery in the new flow. Remove
   the Hipo Retry Enhancement action and its upstream call. Keep initial
   parse, initial indexing, and initial synchronization failures actionable.
   Route recorded enrichment attempt failures through the dedicated callback
   above.
5. **Make Reparse a replacement.** Apply the Source binding, archival, and
   historical reference rules above. Retire the old existing-document job
   path after Hipo moves to the new contract.

Run the following cases after implementation:

- Deliver one `job.completed` with the initial snapshot and no enrichment
  status. Verify Hipo copies that snapshot, displays Parsing Complete, and
  includes the Source in Ask. Repeat with a failed initial Hipo copy and
  retry. The retry uses the saved snapshot.
- Open a previously issued citation and image preview for an existing
  progressive Source. Verify the authorized historical snapshot and
  physical page are readable and the page read stays bounded.
- Move a ready Source that has never been enriched, enable sharing where
  authorized, and export it. Verify the Knowhere version used for sharing,
  the exported snapshot and checksum, and the directory in the target
  Brain.
- Open Tree View with a missing, matching, and older directory version.
  Verify each response follows the directory contract above. Complete or
  fail a later enrichment and verify that the Source still shows Parsing
  Complete and Hipo issues no final-snapshot sync for that job.

## Rollout and index recovery

Let in-flight writes from the previous publisher settle before enabling
additive writes. Validate the deployment and rollback sequence so each
document uses one publication model at a time. Exercise rollback after a
fine unit has published. Verify that both bodies and their references
survive, including after any previously queued work is resumed.

Include scheduled maintenance and queued rollback copies. The current
maintainer can delete retired builds, collect indexing inputs, and close an
empty namespace. The PostgreSQL rollback copy tracks builds. Verify
pre-cutover work and delayed cleanup leave both representations of an
active document searchable and accessible. Verify coverage on any supported
rollback target. Rehearse with pending retired-build cleanup and a delayed
copy from before a newer enrichment was published.
[Current maintenance commands](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/lexical/maintenance.py#L28-L69),
[retired-build deletion](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/lexical/cleanup.py#L116-L146)

### Hipo compatibility before the Knowhere deployment

Use the Node SDK version pinned in Hipo to exercise directory reads, old and
new snapshot resources, and initial synchronization in staging. Verify the
Hipo poller and webhook use the same initial result, including when either
arrives first. Include a Source already ready before the upgrade and an
initial parse or copy still in flight while upgrading. Preserve the ready
Source's references and finish or safely resume the in-flight operation
without creating another job or copying the wrong result.
[Hipo SDK dependency](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/package.json#L44),
[Hipo job status classifier](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/classify-knowhere-job-status.ts#L1-L11),
[directory SDK call](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/directory/knowhere-directory-reader.ts#L31-L49)

Verify the new Hipo and Knowhere deployments submit and accept Reparse as a
new-document job. Exercise an in-flight legacy Reparse at the deployment
boundary. Settle it before deployment or leave the previous result current.
After deploying Knowhere, reject an old private update request before
creating or charging a job. Keep document lookup, archival, and the Demo
Catalog's `data_id` path available to Hipo.
[Current Hipo Reparse request](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/resumable-parse-client.ts#L449-L457)

Hipo submits its callback URL using the wire format accepted by the previous
Knowhere release. The new Knowhere deployment delivers attempt-failure events
for these subscriptions when the saved destination matches the trusted Hipo
callback origin and path for that environment. Preserve callback authentication.
Verify both an existing Hipo job and a new Hipo job receive and handle a failure.
[Current Hipo submission](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/resumable-parse-client.ts#L449-L457),
[Hipo callback identity](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/parse-webhook-auth.ts#L18-L30),
[Knowhere subscription lookup](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/webhook_outbox.py#L52-L57)

### Release order

1. **Rehearse in staging.** Use the candidate Knowhere API and workers, a
   packed Node SDK build or npm beta, and Hipo staging. Test the new SDK and
   Hipo receiver with old and new wire responses. Complete the cutover and
   rollback exercises below before scheduling the production deployment.
2. **Publish the Node SDK.** Publish the new major release through Changesets.
   It accepts whole-number directory versions and keeps reading existing
   snapshots. Document Reparse and the retirement of manual enhancement retry.
   Install the exact registry version in a clean environment and pin it in
   the Hipo release.
3. **Deploy Hipo in the coordinated release window.** Let old publications
   settle and hold affected writes, including Reparse, until both services
   have been deployed and checked. Rehearse this operational step in staging.
   Confirm the new Web/API accepts old job responses and both callback formats.
   Acknowledge late legacy enhancement completions for Sources that already
   synchronized their initial result. Verify MCP reads through the deployed
   application. When the changes affect Gateway inputs, deploy it after Web/API
   through the existing release workflow.
   [Node release workflow](https://github.com/Ontos-AI/knowhere-node-sdk/blob/79495e42695b03c66f8fd21a4c5f5520d5ed39fb/.github/workflows/release.yml#L59-L94),
   [current Hipo webhook schema](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/app/api/internal/webhooks/knowhere/parse/route.ts#L14-L47),
   [Hipo release boundaries](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/docs/hipo-infrastructure.md#L161-L175)
4. **Deploy Knowhere API and workers together.** Apply the schema changes
   through the release workflow, verify OpenSearch is serving, and keep queued
   legacy writers, finalizers, and maintenance from changing the new state.
   The deployed build publishes and searches additive records for private
   OpenSearch namespaces. Configure the matching Hipo callback origin as a
   trusted destination for existing subscriptions. The new-document Reparse
   request and single completion contract are effective with the deployed
   services. Run a scoped smoke check and resume affected writes. Existing
   documents use the same eligibility rules on their already published
   indexes.

Record the Knowhere API and worker versions, published npm version, and Hipo
deployment identity. Before admitting new-contract writes, the original
releases remain a coordinated rollback target. After a new write, pause
publications if a critical check fails. Use a rollback release only if it can
read both stored representations and preserve their references, pending writes,
and attempt eligibility. Otherwise hold writes and repair forward with the
compatible reader active. A Hipo rollback release must accept the active
Knowhere response and callback contract.

### Legacy enrichment retry retirement

The review found two callable paths for the legacy
`POST /v2/jobs/{job_id}/enrichment/retry`. Hipo uses a direct HTTP call and
the Node SDK exposes `jobs.retryEnhancement`, including in npm release
2.8.1. GitHub organization searches for the path and method names found no
additional callers in indexed repositories. Knowhere Logfire recorded no
matching route spans from 2026-10-24 04:56 UTC to 2026-11-21 04:54 UTC. The
same query found ordinary job reads and two POSTs to the indexing retry
route. The evidence covers indexed repository code and observed traffic.
[Hipo client](https://github.com/Ontos-AI/hipo/blob/3daa0aa3bc81dc6373bec8ae4f799cd5e6f0ef08/src/infrastructure/knowhere/resumable-parse-client.ts#L373-L389),
[Node SDK method](https://github.com/Ontos-AI/knowhere-node-sdk/blob/79495e42695b03c66f8fd21a4c5f5520d5ed39fb/src/resources/jobs.ts#L115-L120),
[published SDK](https://www.npmjs.com/package/@ontos-ai/knowhere-sdk/v/2.8.1)

The staging candidate removes the old retry route. Verify a request to it
returns `404` and leaves the job and attempt count unchanged. A later search
can trigger the fine parsing work again. Keep the same-job initial indexing
retry available. Before retiring the route in production, verify that the
Hipo release's browser, callbacks, and background work have no dependency on
it.
[API routes](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/apps/api/app/api/v2/routes/jobs.py#L79-L99),
[SDK retry documentation](https://github.com/Ontos-AI/knowhere-node-sdk/blob/79495e42695b03c66f8fd21a4c5f5520d5ed39fb/docs/streamed-parsed-document-sync.md#L188-L192)

### Rollout acceptance

| Area | Pass condition |
| --- | --- |
| Hipo wire contract and SDK | The SDK pinned by Hipo reads legacy directory versions and a new version above one. Full and progressive jobs send one `job.completed` after the initial index is searchable. Hipo handles poll-first and webhook-first delivery for the same result. A redelivered callback preserves its event identity. A pre-upgrade Source remains readable and an in-flight initial synchronization can finish. |
| Publication and recovery | A PDF and a PPTX first publish a coarse directory and searchable baseline. Searches see additive batches. A failed batch disappears from fresh and cached retrieval and its already-issued reference still opens. A later baseline hit queues one retry. Initial indexing retry reuses the same job and stored parsing output. Verify the supported rollback path with an already published fine unit and a pending legacy write. |
| Hipo browser and callbacks | From a signed-in session, upload a PDF and PPTX and confirm the Source enters Ask after initial synchronization. Open the saved tree and see a newer directory appear without blocking it. Verify a fine citation, a historical citation after Reparse, and moving a ready Source without waiting for fine work. Deliver an enrichment failure twice and verify it is recorded once, the Source stays ready, and Hipo issues neither a retry request nor a final-snapshot copy. |
| Hipo compatibility with the retired endpoint | Repeat representative PDF, PPTX, a full-ingestion Source, and an existing-document read with the candidate Hipo and Knowhere releases in staging. Exercise Reparse, fine parsing failure, and search-triggered recovery with the old route absent. Confirm same-job initial indexing retry and zero Hipo calls to the old route before authorizing production retirement. |
| Page image authorization | Open the same historical page citation in Ask and Library after a Brain move. Verify both render the original snapshot and page through an authenticated address. Revoke access and replay that exact address to confirm it returns `404`, including a conditional request carrying the cached validator. Restore access and verify the same address renders the page again. |

### Retention and rebuild

Rebuilding an active document's index restores its baseline and every
fully published enrichment unit. Reconcile interrupted attempts using
their recorded outcomes. Archived documents and failed or superseded
attempts stay excluded. The frozen ingestion ZIP remains usable as an
ingestion artifact. Rebuild also needs the retained publication inputs
for later enrichment.

The existing
[input collector](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/retrieval/lexical/input_collector.py#L190-L217)
recognizes a single assigned indexing input on each Job Result. If the new
writer stores separate inputs for units in the collected storage prefix,
make their durable references visible to the collector before it can classify
them as orphans. Alternatively, demonstrate a complete rebuild from the
retained per-unit content. In both cases, keep assets needed by still-valid
references through recovery and maintenance.

Rebuild an index for a namespace with an active document containing
several published enrichment units and a failed partial attempt, plus an
archived document. Exercise the input collector before rebuilding. Compare
eligible search records and open references from before the rebuild
against the restored index and stored bodies.
