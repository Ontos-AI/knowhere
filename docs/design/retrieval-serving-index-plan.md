# Retrieval-serving index: online performance plan

**Status:** Proposed for review
**Reviewed against:** current `knowhere` retrieval and publication code, 2026-08-29
**Scope:** first-request retrieval performance; LLM planner/harvest/control time is excluded

## 1. Goal and non-goals

The target is predictable, bounded **Retrieval Non-LLM Work** on the current
production-sized namespace (about 643 active documents, 50k sections, and 60k
chunks). The measured baseline is roughly 160-170 seconds before map-nav's LLM
episode begins. We do not require a one-second absolute target in this phase;
we require that the request path avoid repeated full-corpus work and have a
clear linear/bounded complexity profile.

The target includes:

- snapshot or serving-index loading;
- classic or map-nav lexical scoring;
- ranking;
- selected-result hydration;
- citation and asset-reference assembly.

It excludes planner, harvest, control, and answer-generation model time. Those
stages must continue to have separate timings.

The plan does not change prompts, models, tokenization, BM25 formulas, RRF
weights, cache semantics, citation rules, or public HTTP response shapes.
It does not add LLM-response caching, process-wide serving caches, or startup
prewarming. The acceptance benchmark is a cold, uncached retrieval request,
and planner/harvest/control model time remains a separately reported
dependency. Episode-local reuse is allowed only while one request is active.

## 2. Verified current behavior

The current code has two retrieval routes in
`shared/services/retrieval/execution/routes.py`:

- `use_agentic=false` runs `bottom_discovery()` and then ranking/hydration.
- The default map-nav route calls `load_nav_snapshot(..., lazy=True)`, runs the
  synchronous navigation episode, then opens a fresh database context for
  reference resolution and result assembly.

`load_nav_snapshot()` currently:

1. Reads active documents and their current job-result IDs.
2. Loads all matching sections into memory.
3. Loads all chunk identities and `connect_to` metadata into memory.
4. Uses a lazy store for selected chunk content and asset paths.

The current persisted map scorer in
`shared/services/retrieval/nav/nav_knowhere.py` still loads all eligible map
units, then reads query frequencies and term scores from the persisted tables.
The scorer itself is fast; the broad database projection is not.

`bottom_discovery()` currently executes path, content, and term channels
sequentially. `term_channel()` uses substring predicates over lowercased text,
without a trigram index.

Publication currently writes sections/chunks and then calls
`replace_document_map_units()` in the same SQLAlchemy transaction. The existing
`document_map_unit_indexes` row is a per-document-revision completeness marker.
The existing backfill script only rebuilds that map-unit index; it does not yet
build the proposed serving manifest or namespace statistics.

The working tree already contains an uncommitted keyset-pagination change and
the `3f4a5b6c7d8e` section-order migration. Keep those changes separate from
the serving-index implementation and from unrelated documentation edits.

## 3. Architecture decision

Use PostgreSQL as the source of truth and add a persistent, revision-pinned
serving read model. Do not add OpenSearch, Elasticsearch, Tantivy, or another
search service in this version. PostgreSQL's existing FTS plus `pg_trgm` keeps
the current scoring and tie-breaking behavior directly testable.

### 3.1 Revision serving manifest

Add one compressed manifest row per `(document_id, job_result_id)`. The payload
contains ordered metadata only:

- document, revision, source filename, and job identity;
- section IDs, parent IDs, paths, titles, levels, summaries, and sort order;
- chunk IDs, section IDs, types, sort order, and `connect_to` target IDs;
- map-unit row IDs, unit IDs, unit kinds, token lengths, and sort order;
- root-asset IDs and remounted asset owners.

It must not contain full chunk content or asset file paths. Those remain in the
canonical tables and are loaded lazily for selected evidence.

Store the payload as canonical JSON compressed with the standard-library zlib
implementation, with a format version and checksum. The serving loader must
reject an unknown version, checksum mismatch, or incomplete payload.

### 3.2 Serving generations and statistics

Add namespace-scoped generation metadata and persistent scoring statistics:

- `retrieval_namespace_generations`: current generation per user/namespace;
- `retrieval_serving_revision_stats`: compressed per-revision contributions;
- `retrieval_namespace_stats`: aggregate unit counts, total lengths, vocabulary
  frequency histograms, and generation;
- `retrieval_namespace_token_stats`: queryable document frequency per channel
  and token hash.

The generation is a consistency marker, not a replacement for revision IDs.
Retrieval captures active revision IDs and one generation. If generation changes
while the snapshot is being captured or before scoring starts, retry once; if
consistency still cannot be proven, use the exact legacy reader.

Every retrieval route must carry that capture as an immutable revision pin set:
`{document_id, namespace, job_result_id}` plus the captured generation. The pin
set is the source of truth for the request after capture. Downstream queries
must constrain sections, map units, chunks, connected assets, ranking lookups,
reference resolution, and result assembly by the pinned `job_result_id`; they
must not re-join through the live `Document.current_job_result_id`. A generation
change after scoring has started must never cause a mix of old and new rows:
finish against the captured pins (or return an exact legacy result), and only
retry before work that depends on the snapshot begins.
Snapshot admission is therefore decided at capture time. If a later archive
must suppress an in-flight result, discard/retry the whole request; do not
replace its pinned revision with the document's new current revision.

Cache hits occur before route execution, so every operation that changes the
serving generation (publication, republish, archive, or namespace move) must
also advance the namespace retrieval-cache version, or store the generation in
the cache entry and reject mismatches. This keeps cached responses from
outliving the generation they represent without changing the public response
shape.

### 3.3 Indexes

Additive migrations should provide:

- a token-first covering index for map-unit token candidates. The existing
  `idx_document_map_unit_tokens_lookup` is token-first but does not cover the
  selected columns; the pending `2e3f4a5b6c7d` migration adds a unit-first
  covering index for a different access pattern, so the serving reader may
  need one additional token-first covering index;
- a revision/section lookup index for map units;
- a trigram GIN index on `document_map_units.term_search_text_lower`;
- a generated lowercased term field and trigram GIN index for
  `document_chunks.term_search_text`;
- the existing chunk ordering index plus the pending token-covering and
  section-order migrations (`2e3f4a5b6c7d` and `3f4a5b6c7d8e`).

Enable PostgreSQL's built-in `pg_trgm` extension. No separate search service is
required.

## 4. Retrieval changes

Capture the revision pin set and generation at retrieval-route entry, before
the small-corpus count or route selection. Pass that capture into whichever
route is selected; a route-local capture is allowed only when it is performed
as the same snapshot transaction. This prevents the count/load pair in the
small-corpus optimization from straddling a publication.

### 4.1 Fast map-nav snapshot

Extend `load_nav_snapshot()` to try the serving manifest first:

1. Capture active documents, current revision IDs, and namespace generation in a
   short read-only transaction, returning the immutable revision pin set with
   the snapshot.
2. Fetch one manifest row per active revision.
3. Decode and validate manifests.
4. Apply the existing document and section exclusion predicates.
5. Build the current `LazyKnowhereProvider` and `LazyChunkRefIndex` from the
   decoded metadata.
6. Pin the lazy chunk store to the captured revision IDs.
7. Verify generation stability before returning the snapshot.

For an unfiltered map-nav request, route selection may count chunks from these
same validated manifests instead of scanning `document_chunks`; filtered and
classic requests retain the exact SQL counter. The count shortcut must fall
back when any manifest is missing or invalid.

If any manifest is absent or invalid, use the existing legacy snapshot loader.
The legacy loader must return the same revision pin set and apply the same
downstream predicates. This fallback is automatic and exact; it is not a public
feature flag.

The serving path must preserve current ordering, duplicate bare/document-scoped
reference keys, root-asset remounting, section filtering, and revision pinning.
Keep the pin set available through the complete map-nav request. After the LLM
episode, either materialize selected rows (including connected assets) from the
pinned lazy store before closing it, or pass the pin set to
`resolve_workflow_references()` and `assemble_retrieval_results()`. Their SQL
must select the captured `(document_id, job_result_id)` rows directly, so a
republish during the episode cannot make final citations resolve against the
new current revision.

### 4.2 Exact persisted map scoring

Keep `PersistedScoreCorpus` and the existing scorer unchanged wherever possible.
Replace only the data-loading strategy:

- Prepare the immutable, revision-pinned unit projection and namespace scoring
  statistics once per retrieval episode. Checklist relight waves must reuse that
  projection; they may fetch or compute only query-specific postings/scores.
  A wave must not issue another full-namespace unit/statistics load for the same
  pin set. Instrument the loader call count and include it in the benchmark
  report so repeated projection loads cannot hide behind separate wave timings.
- use manifest map-unit metadata to represent all units, including zero-score
  units; the serving reader should not re-query `document_map_units` for these
  IDs, lengths, or section membership;
- query token postings only for tokens in the request;
- filter postings by captured revisions and allowed sections;
- discover term-channel candidates through the trigram index while retaining the
  current exact substring/token-hit scoring. The candidate predicate must use
  the trigram-indexed `LIKE '%term%'` form (with the same lowercased query and
  tokens), then apply the existing exact score expression; do not scan every
  unit's term text in Python.
- obtain normal-corpus lengths, document frequencies, and IDF-flooring data from
  persistent statistics;
- preserve the existing lexical sort key and RRF ranking.

Queries with document or section exclusions must remain exact. If adjusted
statistics cannot be calculated with certainty, use the legacy scorer for that
request rather than approximating them.

### 4.3 Classic retrieval — one pinned revision snapshot

Keep the existing channel implementations and result projection. In
`bottom_discovery()`:

- capture one revision pin set and generation before starting any channel;
- execute enabled channels concurrently;
- give each channel its own short-lived database session;
- pass the same pin set to every channel and constrain every channel query to
  those revisions;
- preserve channel limits, Python BM25, term scoring, RRF merge, score
  normalization, and all-or-error behavior;
- use the new trigram index only to narrow term candidates.

Do not share one `AsyncSession` across concurrent channel tasks.
Ranking lookups, duplicate suppression, connected-target hydration, and final
assembly must receive the same pin set as discovery. The classic result must
therefore contain rows from one revision per document even if publication
replaces a document while one of the channel sessions is running. The
small-corpus optimization must use this same captured snapshot/pin contract (or
the exact legacy equivalent), rather than loading all rows through live current
revision joins.

## 5. Publication and lifecycle behavior

Refactor publication so the same build pass produces:

- canonical sections/chunks;
- existing map-unit rows and completeness marker;
- the revision serving manifest;
- revision statistics and namespace-statistics deltas.

All of this happens synchronously in the existing publication transaction. The
completeness marker and generation update are written last. If serving-index
construction fails, the publication transaction rolls back.

New publication remains online during backfill. First publication, republish,
archive, and namespace-move paths must update statistics under the same
namespace generation row lock. The lock covers the active revision set,
namespace membership, revision contributions, and the generation increment, so
readers and writers have one lifecycle ordering.

Backfill must rebuild the complete derived serving state (map units, manifest,
revision contribution, and namespace-statistics delta), not only the existing
map-unit index. It must select only documents with `status = 'active'`, a non-null
`current_job_result_id`, and the intended user/namespace. Immediately before
writing a contribution, it must hold the namespace lock and re-read the
document, then require all of the following to remain true: active status,
unchanged user/namespace, and `current_job_result_id` equal to the captured
revision. Otherwise it skips that revision without adding statistics. This
active-status predicate is required in the selector as well as in the
commit-time guard; update `apps/api/scripts/backfill_map_unit_indexes.py` to
include it in the existing selector.

Archiving must atomically remove or invalidate that document revision's serving
statistics contribution while holding the same lock and advance the namespace
generation. `archive` currently changes `status` without clearing
`current_job_result_id`, so checking the revision pointer alone is insufficient
and would allow an in-flight backfill to re-add an archived revision.

## 6. Online rollout

There is no planned downtime, runtime feature flag, or production shadow-read
mode.

1. Deploy additive schema/index migrations, beginning with the pending
   `2e3f4a5b6c7d` and `3f4a5b6c7d8e` migrations.
2. Deploy code that automatically uses the serving reader only for complete,
   valid revisions and otherwise uses the legacy reader.
3. Run an explicit, idempotent, bounded backfill for existing active revisions.
4. Keep retrieval and publication online while backfill runs.
5. Verify manifest checksums, revision coverage, namespace statistics, and
   generation consistency.
6. Run strict legacy-versus-serving differential checks before considering the
   rollout complete.

If backfill is incomplete, affected revisions continue on the exact legacy
path. If online serving data is corrupted, reject it, alert, repair it with the
backfill/rebuild script, and do not serve partial data.

Before enabling the serving reader for a namespace, record an inventory of
active `(document_id, current_job_result_id)` pairs, manifest completeness, and
expected per-revision and aggregate unit counts. After backfill, reconcile those
same values and verify that every aggregate includes only active, namespace-
member revisions. Abort the fast-path rollout on any missing/extra revision,
checksum failure, count mismatch, archived contribution, or generation
discontinuity.

Any migration, backfill, or other database write—especially against
production—requires explicit approval immediately before execution. Read-only
inspection and benchmarking may proceed without that approval.

## 6.1 DevOps operations runbook

DevOps owns the production rollout mechanics; application code does not run a
startup backfill or create serving tables implicitly. Execute the following in
order:

1. **Preflight (read-only):** confirm the target account, database, migration
   head, available disk, connection headroom, and a recent rollback point. Record
   the active `(document_id, current_job_result_id)` inventory for each namespace
   that will be backfilled.
2. **Schema rollout:** with explicit approval immediately beforehand, apply the
   additive migrations in dependency order: `2e3f4a5b6c7d`,
   `3f4a5b6c7d8e`, `4a5b6c7d8e9f`, then `5b6c7d8e9f0a`. Run the trigram-index
   migration during a low-traffic window and monitor for blocking locks.
3. **Application rollout:** deploy the API and worker versions containing the
   serving reader and atomic publication changes. Verify health, error rate, and
   legacy fallback before starting the backfill.
4. **Bounded backfill:** with separate approval, run
   `uv run python apps/api/scripts/backfill_map_unit_indexes.py --apply` from a
   controlled operator environment. Limit concurrency, pause on database
   saturation, and resume safely; the operation is idempotent and stale or
   inactive revisions must be skipped.
5. **Reconciliation:** compare the preflight inventory with serving manifests,
   checksums, per-revision unit counts, namespace aggregates, and generation
   values. Confirm aggregates contain only active documents still belonging to
   the namespace. Investigate every missing, extra, stale, or invalid revision.
6. **Acceptance:** run the production read-only legacy-versus-serving
   differential harness and record latency, selected IDs, order, scores,
   citations, section paths, asset references, and fallback behavior. Declare
   the rollout complete only after zero semantic mismatches.

If migration or backfill must be stopped, leave the serving tables in place and
stop the operator job. The reader will continue using the exact legacy path for
incomplete revisions. Roll back application code first if necessary; do not
drop serving tables or indexes as an emergency rollback action. Repair a failed
revision by rerunning the bounded backfill after the cause is understood.

## 7. Contract tests and benchmarks

Use contract tests only. Add contracts for:

- manifest round-trip, checksum, version, and revision pinning;
- eager versus serving snapshot equivalence;
- exclusions, duplicate chunk IDs, document-scoped references, and root assets;
- exact Latin/CJK, empty, no-hit, phrase, token-only, and negative-IDF cases;
- incomplete serving data falling back to legacy;
- publication replacement, archive deltas, concurrent generation changes, and
  stale backfill protection;
- cache invalidation racing with a generation change, proving an old cached
  response is not returned for a newer serving generation;
- map-nav republish during the LLM episode, proving final hydration and
  connected-asset resolution stay on the captured revisions;
- classic publication replacement during concurrent channels, ranking, and
  final assembly, proving every returned row shares the channel's pin set;
- archive/backfill races proving archived or namespace-moved revisions never
  contribute to serving statistics;
- concurrent classic channels preserving IDs, order, scores, citations, and
  fallback behavior.

Race tests must use barriers or an equivalent deterministic hook to force a
republish during the map-nav episode, a publication between classic channel
sessions, an archive during backfill, and a namespace move during backfill.
Each test must assert both the returned evidence and the persisted statistics,
not merely that the request completed.

The validation harness must also inspect the generated SQL/query plans (or an
equivalent query-boundary assertion) to prove pinned reads do not use live
`Document.current_job_result_id` joins. Run cache-version/generation races and
verify an old cached response is rejected after a lifecycle change.

Run a differential harness against the production read-only database and
compare selected IDs, ordering, rounded scores, citations, section paths,
asset references, and fallback behavior.

Benchmark fresh processes and uncached queries. Report separately:

- serving capture/decode;
- map index projection and scoring;
- episode-local projection reuse (number of full projection loads and per-wave
  query-only scoring time);
- classic discovery;
- ranking;
- hydration/assembly;
- total Retrieval Non-LLM Work;
- planner/harvest/control LLM time.

The complexity check is explicit: one request may perform one full pinned
snapshot/projection pass, relight work should scale with query postings rather
than reloading the corpus, and hydration should scale with selected evidence
(`top_k`/references), not namespace size. Navigation wave count must not
multiply full-corpus database loads.

Record peak resident memory for a fresh worker during the same benchmark and
repeat it with the expected concurrent-request level. Memory is reported as an
operational trade-off rather than a latency acceptance gate for this phase;
before production rollout, any episode-local or process-local reuse still needs
an explicit byte/item budget and an agreed worker ceiling.

The fast path is accepted only after zero semantic mismatches and evidence that
the cold request performs one bounded serving projection, does not repeat
full-corpus loads per navigation wave, and meets an agreed latency budget for
the current production-sized corpus.

## 8. Main tradeoffs and risks

- Publication becomes slower and uses more storage because derived data is built
  synchronously.
- Existing documents need an explicit backfill before they use the fast path.
- A serving-index inconsistency causes a slower legacy request, not approximate
  evidence.
- PostgreSQL remains a scaling dependency; a future search-engine migration
  would require a new semantic-parity review.
