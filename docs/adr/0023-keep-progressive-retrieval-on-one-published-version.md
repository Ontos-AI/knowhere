# Keep progressive retrieval on one published version

## Status

Accepted on 2026-11-10. Staging verification is pending.

## Context

One document revision can become searchable after coarse parsing and receive
several successive refinements. The existing writer replaces chunks within
that revision, where each source chunk path is unique. A refinement can
reuse a section ID while changing its text or retrieval units. A query that
scores the old units and then reads the new chunks can return unrelated
text. A query that applies new filter IDs to the old index can miss matching
evidence.

This decision applies to classic retrieval on PostgreSQL and OpenSearch.
It extends [ADR 0006](0006-atomically-publish-retrieval-serving-index.md) and
defines the progressive-publication case of
[ADR 0007](0007-use-coherent-retrieval-serving-generations.md).
[ADR 0022](0022-open-citations-on-the-enhanced-original-page.md) governs
client navigation to original pages.

## Decision

- **Publish each ready refinement independently.** Prepare the new chunks,
  filter metadata, and index before activation. Activate them together once
  the index is searchable. New queries select the active version.
- **Use the same version throughout each query attempt.** For each selected
  document, use its captured version for filtering, scoring, loading chunks,
  and assembling citations. Return usable evidence as soon as the attempt
  finishes. If a read detects that a selected publication became unavailable
  and the attempt has no usable evidence, capture the current authorized
  scope and active versions and retry once. A completed search without
  matches returns an empty result without a publication recheck. Report
  backend failures as errors. Bind cached
  responses to the selected publications, backend, and authorized search
  scope. Queries started after an archive completes use the current scope
  even when the query text is identical.
- **Keep locks confined to atomic state changes.** Ordinary searches read
  published data concurrently with writers. Parse, prepare chunks, build the
  index, and wait for search visibility outside the publication lock. Use a
  short transaction to activate a prepared version if its base version is
  still current. Task claims and cleanup eligibility may also need short
  transactions. A concurrent publisher can merge its parsed changes into the
  latest version if the result is still applicable. A replacement source
  file or a superseding final publication invalidates older work.
- **Publish an immutable OpenSearch build list per namespace.** Prepare the
  candidate build and the list in OpenSearch before publication. Commit the
  document's publication and the namespace's active list ID in one PostgreSQL
  transaction, conditional on the expected document and namespace state.
  If another document publishes first, apply the pending document's change to
  the latest list and retry the switch. Reuse the prepared build if its own
  source and publication are still current. Changes to document visibility
  update the list through the same publication mechanism.
- **Pin one OpenSearch list for both search channels.** Read the active list ID
  for the authorized namespace and pass it to OpenSearch terms lookup in both
  path and content queries. Retain the build ID on hits for chunk hydration.
  PostgreSQL retrieval continues to use its own published terms and metadata.
  Switching back to PostgreSQL rebuilds the current terms from the retained
  index inputs and verifies coverage before enabling PostgreSQL reads.
- **Keep page citations stable.** A saved citation identifies the document,
  the original file version, and the physical page or supporting pages.
  Fine parsing can change chunk IDs and chapter paths. Each returned chunk
  carries its supporting source pages, and the saved citation still opens the
  same source page in the enhanced reader. Retain the source file and any
  required citation assets independently of the search index lifecycle.
- **Retain replaced chunks in the initial implementation.** Keep the old
  text and the metadata needed to read its version. Share unchanged chunks
  between versions when their content and metadata can be reused as-is.
  An in-flight query that already has usable hits can load their matching
  chunks after index cleanup. Defer physical cleanup of historical chunks
  until the impact on readers, evidence, and the document lifecycle is known.
- **Reclaim retired indexes in periodic maintenance.** New searches can use
  the replacement immediately. Keep the retired index data and search-only
  metadata available to in-flight searches until the retention period ends.
  Start that period after retirement is committed. Cover the enforced time
  from version selection through the last index read, including wait time.
  Choose the duration after verifying that expired attempts stop using the
  selected index. Searches and publishers continue while cleanup is pending.
  Maintenance deletes only eligible retired builds. Keep failed work pending
  across restarts and backend transitions, retry when maintenance can resume,
  and monitor overdue work. Apply the same retention rule to published list
  documents so in-flight lookups can still resolve them. Reclaim unreferenced
  candidate lists from failed or superseded publication attempts.

## Consequences

A failed publication leaves the active version readable. Retrying a
publication after an uncertain response must recognize a successful
activation. Cleanup must verify that a candidate is retired and eligible
before removing its data.

One replacement leaves a changed chunk with coarse and refined versions.
Repeated refinement can retain more versions, and their byte sizes can
differ. Measure historical chunk storage and retired index storage
independently. Each current OpenSearch build covers a whole document. A
concurrent refinement may need to rebuild its candidate from the newer
published version. Repeating that index write is acceptable.

## Verification

Verify the decision on both backends:

- Pause a search after version selection but before scoring. Publish a
  refinement and run maintenance before the retention period ends. Verify
  that the in-flight search can finish and new searches use the replacement.
  After the period ends, verify that expired attempts stop using the old
  index, maintenance can delete it, and usable old hits can still load
  their original chunks. Verify that a completed search without matches
  returns empty without rechecking publications. Simulate a cleanup failure
  and verify a later run retries the pending deletion.
- Publish two refinements concurrently. Verify that both survive, and test
  a failed activation and a retried activation.
- Force two different documents to publish against the same OpenSearch list.
  Verify the final list contains both updates, the captured list remains
  readable, and other namespaces' builds cannot enter the results. Compare
  the original inline build filter and terms lookup with the retained
  188-page parse and existing queries before changing any deployment.
- Repeat the same cached query after an archive completes. Open a saved page
  citation after refinement and again after retired index cleanup. Verify the
  source file version and the physical pages, including a multi-page result.
- Pause index writes and verify that searches can complete on the active
  version. Under concurrent search and refinement, compare search P95 and
  PostgreSQL connection use against the baseline. Record lock hold time,
  index build count, written bytes, and peak retained storage.

## Staging rollout

Amendment (2026-11-13): manifests are no longer optional. The owner removed
`KNOWHERE_OPENSEARCH_PUBLISHED_MANIFESTS` and `KNOWHERE_OPENSEARCH_MANIFEST_LOOKUP`;
OpenSearch publication always maintains the namespace manifest, and eligible
classic reads always use it. A manifest is only the list of current build IDs.
Manifest reads hydrate a current build from its live chunks and a replaced
build from the copy its publication captured just before replacing those
chunks, so publications copy only the build they retire and seeding writes only
the list. A namespace without a manifest keeps the per-request build list until
its next publication or until `seed-manifests` runs. Run that command once when
this change first reaches an environment that already serves from OpenSearch.
Every switch to OpenSearch retires all manifests, so the release bridge seeds
them again before customer traffic returns. The steps below describe the
original staged enablement.

1. Ship the additive schema and compatible readers. Keep the existing inline
   OpenSearch build filter available with version-aware hydration. Drain old
   publication workers before enabling the new publisher. The two manifest
   settings default to disabled.
2. Seed each namespace list from its current published builds. Check the
   namespace generation at activation so a concurrent publication or archive
   cannot make the seeded list stale. Reuse the current indexes. Use the
   reviewed `launch-issuer.py --kind operator` path to run `seed-manifests`
   at the exact serving epoch with
   `KNOWHERE_OPENSEARCH_PUBLISHED_MANIFESTS=true` for the operator process.
   Use the `seed-manifests` approval purpose and bind the transition script
   digest, namespace batch limit, and step timeout in the operator plan.
   Reseed manifests from PostgreSQL after native restore.
3. Enable manifest publication in the API and worker, then set
   `KNOWHERE_OPENSEARCH_MANIFEST_LOOKUP=true` on the staging API after coverage
   and scoped credentials pass verification. Initial lookup covers explicit
   classic private retrieval without visual candidates or signal-path filters.
   Small corpora and other routes retain their existing search selection. Run
   the same queries through the real API at concurrency 25 while publishing
   refinements. Compare latency, evidence, page citations, and visibility after
   archive and across accounts. Exercise the inline
   filter fallback and the separate PostgreSQL rollback on staging.
