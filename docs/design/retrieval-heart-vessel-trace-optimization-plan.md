# Retrieval optimization plan from the fresh `心血管` trace

## Scope and evidence

The detailed stage breakdown below is based on a newly issued production
request, not a historical request:

```json
{
  "namespace": "default",
  "query": "心血管",
  "top_k": 3,
  "use_agentic": null
}
```

- Trace: `01a05e530f7787333c0e32ca16633b85`
- Route: `/api/v1/retrieval/query`
- HTTP status: `200`
- Router: `mapnav`
- Stop reason: `completed`
- Server span: `42.365 s`
- Client wall time: `44.690 s`
- Result: `29` referenced chunks, approximately `11,998` evidence characters

The current production baseline is restricted to the last-night and today
window in Beijing time (`2026-09-01 20:00` through `2026-09-02 11:00`, or
`2026-09-01 12:00Z` through `2026-09-02 03:00Z`). Logfire recorded 203 v1
retrieval roots and one v2 root in that window; all successful roots were from
`deployment.environment=production`. The v1 traffic includes both the direct
`/v1/retrieval/query` path and the externally prefixed `/api/v1/retrieval/query`
path. The FastAPI route is `/v1/retrieval/query` in both cases.

Logfire does not currently record a git commit in `service_version` or another
resource attribute. These traces are therefore the latest observed production
behavior, but they are not proof that production matches a particular local
HEAD. The runtime reports Python `3.12.14`, built on `2026-09-01`, which is
useful deployment context but not a source revision identifier.

v1 and v2 share the same retrieval execution plan. v2 only adds the optional
`llm_config` request field and passes it into the shared plan; when it is absent,
the retrieval SQL, route selection, scoring, hydration, and public projection
are the same. The one v2 request in this baseline had `llm_config=null`.

The timings below are parent/child timings. Parent stages include their child
stages and must not be summed with those children.

## Non-negotiable quality invariant

Every optimization in this plan must preserve retrieval semantic parity for the
same pinned request: selected chunk IDs, ordering, rounded scores, source
sections, citations, evidence content, and asset references must remain
unchanged. Score comparisons use a maximum absolute tolerance of `1e-4` to
avoid treating harmless floating-point accumulation-order differences as a
retrieval change; IDs, ordering, source sections, citations, evidence content,
and asset references have no tolerance. A latency improvement that has not
passed the parity checks is not shippable, and the exact legacy retrieval path
remains the fallback.

## Current optimization boundary

This phase covers non-LLM work only: SQL plans, indexes, serving-index row
transfer, cache behavior, snapshot decoding, map scoring, and instrumentation.
Planner, Harvest, and Control prompts, models, thinking settings, and model-call
orchestration are deferred until this phase reaches semantic-parity and latency
gates.

The optimized reader is an internal implementation shared by v1 and v2. Public
API versioning is not a performance variant: every non-LLM change must preserve
the same Retrieval contract for v1 and v2. v2's optional `llm_config` may change
model settings, but it must not change lexical retrieval semantics.

Implement and validate slices sequentially on the immutable local production
copy. Instrumentation is measurement-only and must first prove that the
baseline result is unchanged. Each subsequent SQL, index, cache, or local
scoring change is evaluated independently against the temporary parity set
before the next change begins; several individually validated slices may be
deployed together afterward.

## Stage breakdown

| Stage | Observed time | Evidence |
| --- | ---: | --- |
| Scope threshold probe | 1.838 s | `count_scoped_chunks` applies `LIMIT top_k + 1` before the outer `count(*)`; this is a bounded small-corpus gate, not an exact count |
| Snapshot load | 3.064 s | 644 documents, 115,892 references |
| Planner | 19.996 s | One subgoal; Planner LLM 19.995 s |
| Tree build | 0.966 s | 50,112 sections |
| Map-index load | 8.523 s | 644 index rows; 42,794 unit rows; 3 query tokens |
| Unit scoring | 0.190 s | 16,662 scored units |
| Map scoring aggregate | 9.802 s | Parent aggregate around scoring/pooling work |
| Harvest | 5.260 s | Harvest LLM 2.762 s; one wave/subgoal |
| Control | 1.256 s | Control LLM 1.248 s |
| Orchestration | 6.518 s | Parent around Harvest and Control |
| Evidence pack | 0.262 s | 23 chunks |
| Final hydration | 0.444 s | 23 results |
| Episode | 36.633 s | Parent from episode start through evidence pack |

The map-index sub-stages were:

- index metadata: `0.028 s`, 644 rows;
- map-unit rows: `4.288 s`, 42,794 rows, `cache_hit=false`;
- frequency rows: `3.866 s`, 42,794 units and 3 tokens.

The `sections=50112` value in the current trace is now accounted for at the
source boundary: both the `tree_build` and `map_pooling` log sites computed it
as `sum(len(tree_by_doc[doc_id][0]) ...)`, where the first tuple member is the
reachable section-node map. It therefore means **section nodes**, not
section-child edges, leaf sections, or chunks. The local instrumentation now
also records `section_edges` and `leaf_sections` for the map scorer, and
`section_rows`, `section_paths`, `root_sections`, and `leaf_sections` for the
snapshot loader. A previous isolated fixture contained the target namespace's
644 documents, 50,112 sections, 60,187 chunks, and 42,794 map units. That
fixture has been discarded after CSV transfer caused excessive local write
amplification; the replacement DevOps dump is pending and must be treated as
the only current end-to-end parity corpus.

For planner work, the discarded fixture contained 41,105 real rows for the
three-token probe plus 1,390,687 non-matching filler rows. Its 1,431,792 total
rows preserved the observed production channel ratio (content 1,275,676; path
156,116) and kept the real target-token distribution. Production had
13,948,605 rows (content 12,524,037; path 1,423,950), so those measurements were
from a scaled production-shaped copy rather than an exact row-count clone.
They remain historical investigation evidence only; repeat them on the
replacement dump before using them for an implementation decision.

On that discarded copy, with all 42,794 map units in scope, the existing
scope-first frequency query returned 36,478 rows in every run and measured
`103–121 ms` across five warm repetitions. The materialized token-first
candidate also returned 36,478 rows (symmetric difference `0`) but measured
`323–378 ms`; PostgreSQL used the existing non-covering lookup index by
default. A token-selective unit projection driven by a materialized
matching-token CTE returned 13,573 units and measured `205–223 ms`; PostgreSQL
still chose a unit-leading lookup for the join after an index-only token scan.
The scaled copy has a much higher target-hash fraction than production, so
this does not predict the full-scale join order. These results validate row
parity but do not yet demonstrate an end-to-end latency benefit; the reader
must not change until rows/bytes transferred and retrieval quality are measured
through the complete caller path.

The historical SQL-output proxy confirms the potential transfer reduction: the
existing
unit projection emitted 42,794 rows / 4.05 MB, while the token-selective
projection emitted 13,573 rows / 1.29 MB. This is a database-client output
measurement, not an application latency claim; Python decoding, filtering,
scoring, and cache behavior still need to be measured together.

Using the existing persisted BM25 scorer with the same full-corpus channel
denominators, the complete and token-selective projections both produced
13,573 positive-scoring units for `心血管`; the maximum score delta was `0.0`
and their top-100 ordering was identical. This confirms that dropping
zero-frequency units is score-safe for this probe, but it is not yet a public
API retrieval-quality gate.

The actual `ReadOnlyChunkStore.load_persisted_score_corpus` caller was also run
against 508 revisions in that discarded fixture whose serving index had format
version `1`.
It loaded 44,894 section rows, returned the same 13,573 score units, and took
`1.089 s` for loading plus `0.168 s` for scoring. Revisions with incomplete
serving indexes correctly returned the existing fallback (`None`); they were
not silently included in the optimized sample.

A temporary scorer baseline was previously frozen at
`/tmp/knowhere-retrieval-parity-baseline.json.gz`. That artifact was generated
before rebasing onto the current main tokenizer and is now historical evidence
only; it must not be used as the acceptance baseline for this branch. Generate
a new baseline from the current `origin/main` behavior after the local copy has
been rebuilt with v2 tokens.

The classic-route SQL shape was then measured against the same historical
namespace and revision scope. After one cold run, five repetitions of the legacy projection
measured `716–763 ms`; the token-selective projection measured `781–844 ms`
(one outlier at `1.73 s`). Output fell from 42,794 rows / 9.53 MB to 13,573
rows / 2.93 MB. This is a substantial transfer reduction with no stable SQL
latency win yet, so the next gate is application-level p95 rather than a claim
that the query itself is faster.

The earlier discovery-level comparison and three-query digests were also
captured before the rebase, against the old tokenizer/data state. They are
retained only as historical investigation notes and do not establish current
latency or retrieval parity. Re-run both the baseline and optimized discovery
paths after the v2 local backfill, using the current main code as the baseline.

The replacement local dump is now available on PostgreSQL port `55433`.
After applying the additive schema migration, populating the four channel
statistics from the existing map units, and running `VACUUM (ANALYZE)`, the
token-leading covering index uses an index-only scan (`Heap Fetches=0`). For
the `心血管` probe, the isolated SQL projection measured approximately
`150 ms` for scope-first versus `77 ms` for token-selective, and the frequency
lookup measured `2.7 ms`. Ten repeated classic application calls across
`心血管`, `心脏`, and `肺` preserved chunk IDs, ordering, sources, and evidence;
the maximum score delta was `1.7e-5`. Warm p50 improved by about `24 ms` for
`心血管`, was effectively unchanged for `心脏`, and regressed by less than
`1 ms` for the empty `肺` probe. Treat this as a SQL/transfer improvement with
no yet-established broad end-to-end latency win; production rollout still
requires the same migration, backfill, and rollback checks below.

The current classic reader now reuses the immutable revision pins captured at
request start when validating an unfiltered scope. This removes a redundant
`SELECT DISTINCT` over scoped map units; requests without pins retain the
original query. On the restored dump, the removed validation query measured
approximately `0.35–0.6 s` before the change. Stage instrumentation shows the
remaining warm classic work is dominated by database reads and hydration, not
BM25 statistics or Python scoring. Result IDs, ordering, sources, evidence,
and score deltas remain within the existing `1e-4` parity tolerance.

For the local restored PostgreSQL (which does not enable SSL), agentic smoke
must set `DB_SSL_MODE=disable` for the global async engine and provide the
plain libpq form through `KNOWHERE_DATABASE_URL` for the synchronous map-nav
reader. Without these local-only settings, final reference hydration attempts
an SSL upgrade and fails even though the database is healthy.

The reference trace above is not representative of request mix. In the current
baseline window, 196 successful v1 roots used `use_agentic=false` (classic),
three used the default map-nav route (`use_agentic=null`), and one explicitly
used `use_agentic=true`. The classic roots had P50 `10.99 s`, P90 `25.46 s`,
and a maximum of `36.65 s`. There were also three unauthenticated v1 attempts
(`401`) and one successful v2 root. The explicit `use_agentic=true` v1 request
took `41.31 s`, and the single v2 request took `109.36 s`; neither is combined
with the classic latency distribution because they exercise different route
and/or model settings.
Across their SQL children, `WITH knowhere` spans had P50 `1.15 s`, P90
`8.24 s`, P95 `10.46 s`, and a maximum of `18.90 s`. In a representative
27.91-second classic request, map-unit discovery took `4.02 s` and the
token-frequency query took `18.90 s`. These current-window classic measurements
are part of the baseline for the shared v1/v2 reader.

The resource log still renders literal format placeholders
(`cpu_seconds=%.3f process_max_rss_kb=%d`), so this trace cannot prove CPU
saturation or request-level memory usage.

## Findings

### Confirmed bottlenecks

1. In the reference map-nav trace, the Planner LLM consumes approximately 20
   seconds. Outbound HTTP spans are only a few hundred milliseconds, so most of
   the elapsed time is provider generation/thinking or uninstrumented client
   wait, not network transfer.
2. The first request transfers all 42,794 map-unit rows before applying the
   three query tokens. This costs 4.288 seconds and creates unnecessary Python
   allocations.
3. The frequency lookup still costs 3.866 seconds even for only three tokens.
   The current reader already filters `document_map_unit_tokens` by
   `token_hash` before joining the pinned scope; the remaining question is
   whether a token-leading covering index or a different join shape helps on a
   full-scale corpus. The local production-shaped benchmark above does not
   show a benefit yet.
4. The map scoring aggregate is 9.802 seconds, while the explicitly measured
   unit scoring and pooling work is only 0.190 and 0.094 seconds. The gap is an
   instrumentation boundary and/or additional map-scoring work that must be
   measured before CPU tuning.
5. The bounded scope threshold probe costs 1.838 seconds. The default map-nav
   route does not need an exact chunk count for ranking, but the probe still
   distinguishes corpora at or below `top_k` from larger corpora before route
   selection. The trace alone does not establish that an `EXISTS` rewrite or
   snapshot metadata would be faster.
6. Classic discovery previously re-scanned scoped map units solely to derive
   revision keys after the request had already captured revision pins. Reusing
   those pins removes that duplicate read without changing the completeness
   check; the no-pin path remains unchanged.
   The same pins now drive the unfiltered index-metadata lookup directly,
   avoiding another scoped-unit CTE. On the restored dump this reduced the
   warm index stage from roughly `0.16–0.8 s` to `0.02–0.04 s` for the sampled
   namespace. The classic result parity gate still passes for the temporary
   query set.
7. The current production request mix is primarily classic retrieval. The
   token-selective reader must therefore be exercised through both the classic
   and map-nav callers; a map-nav-only benchmark would not represent the
   dominant workload.

### Already healthy or lower priority

- Snapshot load is material but not the largest stage at 3.064 seconds.
- Evidence pack and final hydration together are below one second in this
  request. They are not the current optimization priority.
- The previous connected-hydration change is active: no full `JobResult`
  chunk graph load appears in the trace.
- No prompt or retrieval-quality behavior should be changed as part of the
  first implementation slices.

## Prioritized optimization plan

### Validation gate: bounded scope threshold probe (not an implementation P0)

The current implementation already uses `LIMIT top_k + 1` inside
`count_scoped_chunks`, then counts that bounded subquery. Do not describe this
as an exact count or assume a 1.5–1.8 second saving from an `EXISTS` rewrite.
Benchmark the current probe against any equivalent early-exit query or trusted,
generation-pinned snapshot metadata. Retain this slice only if
`EXPLAIN (ANALYZE, BUFFERS)` and production-shaped repetitions show a meaningful
improvement; otherwise leave the current implementation unchanged and keep
this out of the optimization queue.

Acceptance criteria:

- route selection is unchanged for corpora below, equal to, and above
  `top_k`;
- no stale snapshot can incorrectly classify a small corpus;
- the baseline and candidate plans make the `top_k + 1` bound and early-exit
  behavior explicit;
- any candidate replacement is retained only when a latency improvement is
  demonstrated; otherwise this remains a validation note, not an optimization
  slice;
- selected chunk IDs, scores, ordering, and evidence remain unchanged;
- benchmark results record the route family (`classic`, `mapnav`, or
  `small_corpus`) and separate cold, warm, and response-cache-hit requests;
  cache-hit timings are not mixed into cold-request latency claims.

### P0: Make map-unit projection token-selective

The current map-nav reader already makes its frequency lookup token-selective,
but it still loads every revision-scoped map unit before applying the query
tokens. Change only the unit projection: start from
`document_map_unit_tokens` filtered by `channel` and `token_hash`, then join the
pinned revision/unit scope, and return only units matching at least one query
token. Keep the existing frequency SQL shape until a production-shaped plan
proves that a different join order is better.

Roll this out in two stages. The first stage may enable the token-selective
reader only when the request has no section exclusions, where revision-scoped
channel statistics are sufficient. Requests with section filters continue to
use the exact legacy reader until section-scoped denominator statistics are
implemented and pass semantic-parity checks. A missing, incomplete, or
incompatible serving index always uses the same fallback.

BM25 denominators must remain corpus-wide. Obtain exact corpus statistics using
persisted serving-index statistics while preserving the current per-channel
semantics. Extend `DocumentMapUnitIndex` with
`path_document_count`, `path_total_length`, `content_document_count`, and
`content_total_length`, calculated atomically when publishing each revision.
Units with zero path length must not contribute to path `document_count` or
`total_length`, and the same rule applies independently to content. Do not
reuse `DocumentMapUnitIndex.unit_count` for both channels, and never calculate
average length from only the matching subset.

Hypothesis to validate: this should reduce map-index latency, rows transferred,
and temporary memory for this workload; no fixed seconds-saving claim is made
before a full-scale or statistically equivalent local benchmark. The scaled
copy above is a negative/shape-control result, not an approval to change the
reader.

The current implementation slice only adds the per-channel statistics fields,
publication-time population, and the DevOps backfill/readiness contract. It
now enables token-selective projection in both persisted readers when the scope
is unfiltered and serving statistics are present. Section-filtered or
incomplete revisions continue through the legacy path. Both v1 and v2 call the
same shared readers; no route-specific lexical algorithm was introduced.

Acceptance criteria:

- a new idempotent migration adds the token-leading covering index required by
  this query, for example `(channel, token_hash, map_unit_id) INCLUDE
  (token, frequency)`. Keep the existing lookup index during this rollout;
  evaluate removal separately only after `EXPLAIN (ANALYZE, BUFFERS)` and
  production load confirm the new index is safe (or document the visibility
  conditions that prevent an index-only scan);
- serving-index statistics include positive-length `document_count` and
  `total_length` separately for path and content, aggregated over the exact
  pinned revision and section scope;
- frequency maps and scores are identical to the current implementation on a
  fixed production-data copy;
- duplicate token matches do not duplicate units;
- rows and bytes transferred are measured before and after;
- a safe fallback remains available if the persisted index is incomplete;
- v1 and v2 requests with equivalent retrieval fields produce identical public
  retrieval results; v2's optional `llm_config` is recorded as a model-setting
  difference, not as a separate lexical retrieval algorithm;
- the benchmark uses current-window classic requests as the primary sample and
  includes at least one map-nav and one v2 request as shared-plan regression
  samples;
- revision-scope cardinality and the size of the `(document_id,
  job_result_id)` parameter set are recorded, so large `IN` lists are not hidden
  inside token-query timing.

### P1: Optimize the frequency SQL plan

Remove or avoid a materialized full `scoped_units` side when the planner picks
the wrong join order. Drive from the token index, then apply channel,
token-hash, pinned revision, active-document, and namespace predicates.

Hypothesis to validate: the revised join order may reduce frequency-stage
latency; claim no fixed saving until production-shaped `EXPLAIN (ANALYZE,
BUFFERS)` and repeated local runs confirm it.

Acceptance criteria:

- returned `(map_unit_id, channel, token, frequency)` rows are identical;
- no archived or wrong-generation revision can enter the result;
- statement timeout is not approached on the production read-only database;
- the query remains correct for one token, many tokens, and no matching token;
- repeated measurements cover the observed frequency-latency range (warm and
  cold cache, one and many query tokens, and small and large revision scopes)
  before any saving claim is made.

### P1: Measure and then reduce the unaccounted map-scoring time

Add structured timers around map-pooling, section aggregation, ranking, and
projection so the `9.802 s` parent stage can be reconciled with its children.
Only after that measurement should we change data structures or algorithms.

Likely follow-up: precompute section owners, children, and postorder arrays
while decoding the snapshot, then reuse them during scoring instead of
rebuilding dictionaries and sets.

Hypothesis to validate: precomputation should reduce local map-scoring time;
the magnitude is intentionally left to measurement.

### Deferred: Planner and other LLM latency

The Planner is the largest single stage, but this phase makes no changes to
Planner, Harvest, or Control model calls. Keep their existing behavior and
fallbacks intact. Revisit model timing, thinking, prompts, and call
orchestration only after the non-LLM slices below have passed their semantic-
parity and latency gates.

### Deferred: overlap planner and query-independent map scoring (experimental)

The current route waits for the Planner before starting map scoring. For a
planner result whose `retrieval_query` is exactly the user query and whose
scope/filters are unchanged, map scoring can be started concurrently on a
separate read-only connection. The planner result is then used only to decide
whether the already-computed candidate set is sufficient.

This could hide part of the approximately `9.8 s` map-scoring aggregate behind
the approximately `20 s` Planner wait, but it changes LLM-boundary
orchestration. It is deferred until the Planner phase is explicitly in scope;
it must not run when the planner rewrites the query, adds a node filter, or
changes the revision/scope.

Potential saving: up to the overlapping map-scoring time for eligible simple
queries. This is an experimental concurrency change with risks around
connection-pool pressure, duplicate work, cancellation, and revision
coherence.

Acceptance criteria:

- the candidate scores are byte-for-byte equal to the serial path;
- the concurrent read-only store is isolated from mutable navigation state;
- planner rewrites and filters correctly disable the overlap;
- cancellation and database connection cleanup are covered by contract tests;
- concurrency load tests show no increase in statement timeouts or RSS.

### P2: Reduce snapshot parse allocations

Redis stores compressed snapshot bytes, but every request still decompresses
and JSON-decodes 115,892 references. The current parsing path creates both
canonical and owner-qualified reference keys and then copies the mapping.

Use one canonical representation with owner-aware fallback lookup and avoid
the second full mapping copy. Consider a versioned binary format only after a
benchmark; it is a larger migration and is not required for the first slices.

Hypothesis to validate: the representation change should reduce decode time and
temporary RSS; the magnitude is intentionally left to measurement.

Acceptance criteria:

- both existing lookup forms remain valid;
- revision pinning and selected references are unchanged;
- checksum/version validation and generation invalidation remain intact;
- cold Redis-miss and Redis-hit memory are measured separately.

### P2: Redis map-unit projection cache

The current unit cache is episode/process-local; the snapshot Redis cache does
not eliminate the map-unit SQL transfer. Add a generation- and revision-scoped
Redis cache for a compact map-unit projection and index statistics, with a
bounded TTL and size limit. Bind each key to `user_id`, `namespace`, serving
generation, `document_id`, `job_result_id`, and index format version. A
generation change naturally invalidates old entries; Redis misses, version
mismatches, and decode failures fall back to PostgreSQL.

This is not a guaranteed cold-first-request optimization. It helps only when a
previous publisher or request has populated the generation key. PostgreSQL
remains the source of truth and is the fallback on misses or Redis failures.

## Existing-data maintenance

Backfill only the currently retrieval-visible revision for each active
Document. Add the channel-stat columns with an additive migration and introduce
serving-index format version 2. v1 and v2 remain two internal read paths under
the same Retrieval contract and must produce identical results. For a complete
existing index, a resumable per-revision statistics backfill aggregates the existing
`document_map_units` rows, writes the four path/content values, and marks the
revision v2 in the same transaction. It must not regenerate token rows or
snapshots unnecessarily. Revisions with a missing or incomplete index use the
existing full `backfill_map_unit_indexes --apply` path.

Each revision is committed independently under the existing generation and
active-document locks. The job is idempotent and safe to interrupt: revisions
without a completed v2 marker remain on the exact legacy reader, and the
optimized internal reader is selected only after the backfill check reports
complete, coherent active revisions; otherwise the same Retrieval request uses
the exact legacy reader.

### DevOps handoff

DevOps runs the maintenance explicitly; API startup and user requests never
trigger it. The runbook should:

1. deploy the additive migration and verify the reader still falls back safely;
   Build the new index without dropping the existing lookup index, and monitor
   lock waits, build duration, and disk headroom;
2. deploy the application code that writes v2 for new publications and selects
   the fast reader only for complete v2 revisions; v1 revisions continue on the
   legacy reader;
3. run the read-only inventory/check command and record active/current revision
   counts;
4. run the resumable statistics backfill in bounded batches (with optional
   document selection), monitoring database load and generation changes. The
   command is:

   ```bash
   python /app/scripts/backfill_map_unit_statistics.py \
     --apply --batch-size 100
   ```

   Use `--user-id`, `--namespace`, or `--document-id` to narrow a rehearsal.
   `--check` is read-only and should return `would_update=0` before rollout.
   This command only aggregates existing `document_map_units`; it does not
   regenerate token rows or snapshots. Revisions with a missing or legacy
   index remain on the existing full backfill path.
5. rerun the check until every retrieval-visible revision has a coherent v2
   marker and no serving fallback is reported;
6. monitor semantic-parity probes, latency, errors, and timeouts. The reader's
   version/completeness checks automatically retain the legacy path when a
   revision is not ready.

The handoff must document the exact command, batch/concurrency limits,
pause/resume procedure, application-version rollback procedure, and the final
check output. A partial
backfill is an expected intermediate state, not a failed deployment, provided
that incomplete revisions remain on the legacy reader.

### Deferred: LLM orchestration improvements

- Do not add a deterministic shortcut around Control. `plan_control` remains
  the sole checklist-reconciliation authority; non-empty or seemingly related
  evidence is not sufficient to replace its accept/widen/drop/replan decision.
- Do not change subgoal parallelism or Harvest/Control materialization in this
  phase. These remain candidates only after the LLM boundary is explicitly
  revisited.

These changes must not alter prompt text or citation selection semantics.

## Validation protocol

Every implementation slice must be validated against a local copy of the
production namespace before deployment. Production rollout then relies on the
automatic legacy fallback for any incomplete or incompatible revision.

Validation has two layers. First, maintain a small temporary retrieval-parity
set (two or three representative queries, including the current `心血管` trace)
containing current legacy-reader outputs, and run deterministic non-LLM parity
checks on an immutable production-data copy with fixed revision pins, section
scope, and queries. Compare loader rows, frequency maps, channel statistics,
per-unit scores, and ordering against that set. This set is a quality guard for
the optimization work, not a new product behavior contract; do not refresh it
just to hide a regression. Generate it once from the current legacy reader and
freeze it for the whole optimization series; refresh it only after an explicit
decision to accept a retrieval behavior change. Second, run end-to-end
Retrieval requests to measure latency, rows/bytes, and memory. Do not use
nondeterministic Planner/Harvest timing as evidence for this non-LLM change.

1. Capture a baseline with the exact request, a cold process, and an empty or
   generation-scoped Redis key. Run at least 10 cold repetitions; measure p50,
   p95, stage timings, peak RSS, rows, and bytes transferred.
2. Run a separate warm-cache series. Never present warm-cache results as the
   first-request improvement.
3. For every SQL change, compare `EXPLAIN (ANALYZE, BUFFERS)` and exact returned
   rows against the current query using the read-only production database or
   an immutable local copy.
4. Compare retrieval quality per request, requiring zero public-result
   differences: selected chunk IDs, order, rounded scores, source sections,
   evidence content/hash, asset references, router, stop reason, and citation
   validity. Aggregate quality metrics cannot replace this exact parity check.
5. Exercise edge cases: empty result, one token, multiple tokens, small
   corpus, incomplete index, archived revision, namespace generation change,
   and Redis miss/failure.
6. Fix the malformed CPU/RSS log fields before making any CPU or memory claim.
   Use per-stage CPU deltas and cgroup metrics rather than process high-water
   RSS alone.
7. Roll out one optimization at a time, with automatic legacy fallback and
   production p50/p95/error/timeout monitoring. If rollback is required,
   redeploy the previous application version; leave additive schema/data in
   place for a later retry.

For each slice, retain it only when repeated local runs show a stable
improvement in the targeted stage and no regression in total non-LLM p95. The
temporary parity set is a hard gate regardless of the measured speedup.

## Recommended implementation order

1. Add missing structured timing and CPU/RSS instrumentation.
2. Benchmark the existing bounded `top_k + 1` scope probe; keep a replacement
   only if it demonstrates a measured improvement.
3. Implement one token-selective serving-index slice: add the covering index,
   persist per-channel BM25 statistics, and make unfiltered map-unit discovery
   token-selective with the exact legacy fallback for filtered/incomplete
   scopes.
4. Optimize the frequency join order and verify the production query plan.
5. Reconcile and optimize the unaccounted map-scoring work.
6. Consider snapshot allocation and Redis map-unit projection work as follow-up
   improvements.
