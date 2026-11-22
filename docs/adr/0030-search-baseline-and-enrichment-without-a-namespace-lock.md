# Search baseline and enrichment without a namespace lock

## Status

Accepted on 2026-11-19. Updated on 2026-11-20 to allow enrichment records
to become searchable as they are written and to define archival and cache
behavior during publication. Both representations retain their index records
and chunks. Retrieval applies best-effort exclusion to the response.
Eligible visual searches run in parallel with text search.

Supersedes the single active version, the namespace build list, and the
activation lock in
[ADR 0023](0023-keep-progressive-retrieval-on-one-published-version.md).
Page citations and retained baseline bodies from that decision still stand.
This decision replaces the publication transaction in
[ADR 0006](0006-atomically-publish-retrieval-serving-index.md) for this serving
path. Full enrichment publication allows result-level baseline hit exclusion.

## Context

Many documents publishing into one namespace contended for the same namespace
index row. Waiters failed after the admission budget, then retried by
rewriting a shared list of build IDs. The list existed so a namespace search
could exclude every record except the selected build of each document.

That protection answered two cases. The first is replacing an existing
document's parsed content. The second is making enrichment searchable while
the baseline is still searchable. The first case is not a supported
operation. The second remains a product requirement: after the baseline index
is written, that document can be retrieved before its enrichment finishes.

## Decision

- **Private ingestion publishes a new document.** The caller adopts the
  synchronized replacement and then archives the previous document.
  Demo Catalog ingestion retains its existing identity rules.
- **A document writes only its own index records.** Publication does not
  rewrite a namespace roster, manifest, or generation. One document's
  publication does not block another document or a retrieval. Retrieval takes
  no lock.
- **Existing and new documents share the same serving and publication paths.**
  Existing documents start with their currently published chunks and index
  records. Subsequent enrichment uses the same additive writer for all
  documents. Retrieval checks candidates against the document's current
  revision. Search uses the authorized namespace and request filters without
  enumerating all builds for that namespace. The two legacy records observed
  during rollout assessment can remain under the current-revision check.
- **Archival governs retrieval through document status.** Retrievals started
  after archival succeeds exclude the document from fresh and cached results.
  A retrieval already in progress may finish using bodies it read before
  archival committed. Maintenance deletes records by the exact `documentId`
  in its namespace index, including any left by concurrent writes. The same
  field identifies records for existing and newly published documents.
  Archival during initial indexing after parsing completes retains the
  existing `ABORTED` failure and `job.failed` event.
- **The baseline is searchable when its index write finishes.** For a new
  progressive PDF or PPTX, the first baseline consists of coarse chapter chunks
  as defined in [ADR 0031](0031-use-one-completion-notice-for-ingestion.md).
- **Search both indexes in parallel when visual is eligible.** Text searches
  OpenSearch. Visual searches eligible PostgreSQL page vectors when allowed
  by the feature setting and request. Both channels apply the same visibility
  rules and merge their results regardless of the text hit count.
- **Enrichment is a second body and a second index beside the baseline.** The
  baseline body stays. There is no switch that withdraws the baseline index
  before the enrichment index is visible.
- **Enrichment records become searchable as they are written.** Enrichment
  parses the whole unit and stores its body first, then writes its index in
  batches. The baseline stays eligible throughout publication. A PDF or PPTX
  unit is one baseline page range. A spreadsheet unit is one sheet group. Text,
  Markdown, Word, and standalone audio and video use the whole document.
- **Each unit grants publication to one attempt at a time.** Claim the unit
  in a short transaction before parsing and index writes. Publish each
  unit's records independently. Unit state determines eligibility for
  records from a superseded attempt, including writes that arrive after
  recovery has assigned a replacement attempt.
- **Publication keeps each hit's body and attachments stable.** A baseline
  hit and an enrichment hit can share a source path and resolve to different
  bodies. A unit publishes without rewriting its neighbors' chunk and page
  associations.
- **Full enrichment publication invalidates older cached results.** Repeated
  requests can reuse an unexpired result while publication is underway. Once
  the unit is fully published, new requests use a fresh search or a result
  computed with the completed publication available.
- **Cached results respect failed and superseded attempts.** A search or
  Agent read started after the attempt becomes ineligible excludes its
  hits. Late cache writes cannot restore their eligibility.
- **Recovery verifies the publication outcome after an interruption.** For
  an active document, a stored enrichment body and a complete set of
  searchable index records let recovery finish the same attempt and apply
  the cache invalidation. A confirmed incomplete write follows the failure
  path in [ADR 0031](0031-use-one-completion-notice-for-ingestion.md).
- **Retrieval prefers clear replacements already available to the response.**
  Assembly can omit a baseline hit when the response includes hits from fully
  published enrichment that replace it. For page-range units, the included
  hits must cover every original physical page with searchable content.
  Source fallback content counts. For whole-document units, evaluate the
  source content of each baseline hit. Overlap is acceptable when replacement
  is uncertain or the hits cannot fit within the response limit.
- **Both representations keep their index records and chunks.** Exclusion
  happens in the retrieval service's response. Existing references can
  still open their original bodies.
- **Baseline and enrichment can coexist in a live document read.** The chunk
  list can return both representations for the same unit as enrichment
  becomes available. Each chunk reference resolves to its own stored body.

## Considered Options

- Keep a namespace build list and a short lock around changing it. Rejected.
  Documents publishing together still wait on that shared record, which is
  the failure this decision removes. The caller-owned new-document and delete
  path does not need the list, and enrichment is distinguished at assembly.
- Withdraw the baseline index, then make the enrichment index visible.
  Rejected. The gap is unnecessary because the baseline body is kept and both
  indexes can be searched.
- Hide a document until every enrichment unit finishes. Rejected. The
  baseline must be searchable before enrichment finishes.
- Release a whole enrichment unit in one step. Allowing partial enrichment
  hits alongside the baseline lets the existing search units remain
  independently ranked and avoids a separate visibility switch for the
  complete unit.
- Drop a whole baseline hit when any one of its pages has enrichment.
  Rejected. Adjacent units share a boundary page, so that rule removes the
  neighboring unit's remaining pages. Keeping the baseline hit, and showing
  the shared page twice, is the better result for an agent.
- Drop a baseline hit as soon as enrichment covering those pages is
  published. The baseline stays eligible when a query's actual results
  lack the replacement content.
- Clean up covered baseline indexes in the background. Retaining the index
  records preserves baseline-only matches and avoids maintaining a second
  lifecycle solely to reduce duplicate hits.

## Consequences

The namespace stores the baseline index plus the index records of enriched
units. Both representations can compete for candidate slots after full
publication. The response can include duplicates, including shared boundary
pages and baseline hits without a clear replacement among the selected hits.

A failed enrichment write leaves the baseline eligible. Failure cleanup
removes the failed attempt's index records. References already returned can
still open the stored bodies.

Index rebuild uses retained publication inputs to restore baseline and
fully published enrichment records. Rollout waits for previous writes to
settle. Rollback preserves both published bodies and their references.
