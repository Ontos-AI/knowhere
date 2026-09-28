# Publication performance optimization plan

Status: Phase 0 completion gate passed on 2026-09-23. Measurement-only
telemetry and the candidate implementation are available. The compact-template
formal cold campaign completed on 2026-09-25: 59 candidate samples per owner
all finished below 10 seconds. Campaign admission remains open because state
parity, correctness cases, SQL row-count completeness, and capacity/retrieval
interference have not all passed. The 2026-09-26 capacity stage-one sample
failed the retrieval p95 and index-growth gates at concurrency 10; a second
paired diagnostic reproduced both failures. Benchmark artifacts are under
`.benchmarks/publication/reports/` and are not release approval.

## Phase 0: execution contract

Phase 0 turns this design into a repeatable local workflow. The scripts named
below are Phase 0 deliverables; they do not exist yet and must be implemented
before Phase 1 telemetry is considered complete. Every script writes a manifest
and a machine-readable result; a terminal exit code of zero means only that the
script ran, not that the associated verification gate passed.

### Repository and artifact layout

All generated benchmark state stays outside tracked source files:

```text
.benchmarks/
  publication/
    inputs/spacex-s1-production/
      manifest.json
      chunks.json.gz
    clones/<run-id>/
      clone.json
      postgres.log
      publication.jsonl
      state-before.json
      state-after.json
    reports/<run-id>/
      summary.json
      summary.md
      resource.json
      gate-results.json
  retrieval-parity/
    requests.jsonl
    baselines.jsonl
    reports/<run-id>.json
```

`.benchmarks/` remains ignored by Git. Generated files must contain no raw
production user IDs, namespaces, source filenames, document text, token text,
artifact paths, credentials, or SQL bind values.

### Required command interfaces

Phase 0 must provide these commands under `scripts/publication_benchmark/`:

```bash
uv run python scripts/publication_benchmark/freeze_input.py \
  --source-db-url-file /tmp/knowhere-prod-db-url-read-only \
  --source-revision <parse-input-revision> \
  --output .benchmarks/publication/inputs/spacex-s1-production

uv run python scripts/publication_benchmark/clone_db.py \
  --source-volume knowhere-prod-restore-data \
  --run-id <run-id> \
  --postgres-config production

uv run python scripts/publication_benchmark/run_publication.py \
  --owner sync \
  --db-url-file .benchmarks/publication/clones/<run-id>/database-url \
  --input .benchmarks/publication/inputs/spacex-s1-production \
  --strategy baseline \
  --mode cold

uv run python scripts/publication_benchmark/run_correctness.py \
  --case LP-001 \
  --owner sync \
  --strategy baseline

uv run python scripts/publication_benchmark/run_capacity.py \
  --execute --owner sync --strategy both \
  --run-id <listed-clone-id> --report-id <capacity-report-id> \
  --template-id <sealed-template-id> \
  --input .benchmarks/publication/inputs/spacex-s1-production \
  --probe-spec /tmp/retrieval-probe.json

uv run python scripts/publication_benchmark/aggregate.py \
  --report-dir .benchmarks/publication/reports/<run-id>
```

The command implementations must refuse to run against the original source
database, a read-only URL, an unlisted clone, or a missing input digest. Clone
creation must record the source volume digest, schema revision, PostgreSQL
settings, index inventory, and database identity in `clone.json`.

The example commands show one matrix point. The runner must be invoked again for
the other owner (`async`), strategy (`candidate`), mode (`warm`), concurrency,
probe-rate, and case IDs; shell alternation syntax must not be used in command
lines.

### Frozen input contract

`freeze_input.py` must fail unless the generated payload verifies all of the
following:

- 922 chunks: 670 text, 99 image, and 153 table;
- metadata present on all 922 chunks;
- artifact paths on 252 chunks;
- 830 map units;
- approximately 88,551 token rows, with the exact count recorded in the
  manifest;
- canonical ordering and a stable SHA-256 digest;
- no artifact file bytes embedded in the publication payload.

The manifest records schema version, source revision, counts, digest, generation
timestamp, and the opaque corpus fingerprint. Any mismatch invalidates all
existing benchmark results using that input.

### Strategy and environment contract

The temporary global setting is named `KNOWHERE_PUBLICATION_STRATEGY` and has
only two values: `baseline` and `candidate`. Its default is `baseline`. Worker
and API processes must print the selected value once at startup and reject any
other value. No request or namespace may override it.

`clone_db.py` supports two explicit PostgreSQL profiles:

- `production`: match the observed production settings as closely as the local
  machine permits and record every deviation;
- `conservative`: retain the local dump-container settings to expose I/O
  sensitivity.

Each run records code commit, dependency lock digest, strategy, owner, profile,
PostgreSQL version/settings, Redis namespace, input digest, clone ID, host ID,
and start/end timestamps. Baseline and candidate runs are invalid if any of
these fields differ except strategy and raw clone identity. Independent clone
runs may use different Redis namespaces only when each exactly matches
`publication-benchmark:{run_id}`; the report must record this namespace
isolation exception before comparing the pair.

### Credential and external-service contract

Publication benchmark, telemetry, lifecycle parity, atomicity, recovery,
capacity, and resource-envelope runs replay already parsed chunks. They do not
call an LLM and do not require a real LLM key. They require only the benchmark
database clone, the benchmark Redis namespace, and test-local storage fixtures
when an adapter-specific artifact pointer contract is exercised.

Classic Retrieval Semantic Parity (`use_agentic=false`) also does not require an
LLM key. It exercises the deterministic map-unit/BM25 route. Real agentic
router/stop-reason parity does require the configured agentic provider (the
default `cursor_sdk` path requires `CURSOR_API_KEY`) unless the test replays
recorded model responses. `LLM_MOCK_ENABLED=true` is acceptable for plumbing
and failure-path tests only; mock responses are not production semantic-parity
evidence. Full document parsing is outside this plan and has its own provider
credentials.

Contract tests may use non-functional placeholder provider values required only
for settings import. No production LLM, database, storage, or Redis credential
may be copied into benchmark artifacts, logs, or committed configuration.

### Verification case IDs

`run_correctness.py` must implement and report these stable case IDs:

```text
LP-001  lifecycle parity: new document
LP-002  lifecycle parity: replacement revision
SI-001  two users × two namespaces isolation
ST-001  state parity: new document
ST-002  state parity: replacement revision
AT-001..AT-008  failure after each publication stage and before commit
RC-001  connection loss before commit
RC-002  connection loss during commit
RC-003  process interruption after commit before effects
RC-004  unknown commit outcome retry/reconciliation
ID-001  duplicate completion after successful commit
ID-002  concurrent replacement in opposite completion order
ID-003  stale completion after newer revision
ID-004  namespace movement
ID-005  archived-document publication
ID-006  all-duplicate input no-op
SC-001..SC-004  baseline/candidate read-write compatibility matrix
PE-001  rollback has no cache invalidation or webhook enqueue
PE-002  successful effects occur once
PE-003  post-commit effect failure recovery
```

Each case stores normalized `state-before.json`, `state-after.json`, the
expected outcome, the observed outcome, and a pass/fail result. Case IDs are
part of the report contract; adding or removing a case requires updating this
document before collecting candidate evidence.

### Frozen statistical rule

The final cold-run duration gate is fixed before candidate results are viewed:
collect at least 59 independent samples per owner/profile/strategy and require
all 59 Publication Duration values to be below 10 seconds. This is the
one-sided zero-failure rule for a 95 percent confidence statement that at least
95 percent of runs meet the threshold. Twenty samples remain an exploration
minimum only. Candidate-vs-baseline improvement uses a bootstrap 95 percent
confidence interval over paired, interleaved runs; overlapping intervals require
more samples and an unresolved result is rejected.

### Phase 0 completion gate

Phase 0 is complete only when:

- the frozen input manifest and digest pass validation;
- a writable clone can be created, queried, reset, and destroyed without
  touching the source volume;
- both owner commands execute a no-op baseline publication against a clone;
- the strategy setting is visible and rejects invalid values;
- every verification case ID has a runnable placeholder that fails loudly rather
  than silently skipping;
- report files contain no prohibited production data;
- the 100% terminal-trace accounting check has a known input/output contract.

Only after this gate may Phase 1 telemetry implementation begin.

## Objective

Reduce cold-run p95 Publication Duration below 10 seconds for a production-
shaped new document in the measured Worst-Case Publication Corpus. The target
starts when parsed chunks enter shared Publication and ends when its transaction
commits.

The same shared implementation must pass through both production transaction
owners:

- the worker sync SQLAlchemy/psycopg2 path used by ordinary parsing;
- the API AsyncSession `run_sync`/asyncpg path used by featured-project
  materialization.

Document parsing, model calls, artifact upload, queueing, publication-capacity
admission waiting, and post-commit effects are outside the 10-second boundary.
In-process work required by Publication, including tokenization, graph assembly,
snapshot compression, database persistence, and commit, remains inside it.

## Non-goals

- Do not create a demo-only publication implementation.
- Do not remove `document_map_unit_tokens` or production indexes based only on
  their size.
- Do not split Publication into partially committed batches.
- Do not weaken serving-generation, active-revision, or retrieval semantics.
- Do not preselect COPY, a batch size, an index change, or another optimization
  before telemetry identifies the dominant p95 stage.
- Do not include parse or artifact-upload latency in the publication target.

## Current evidence

The production read-only database reports:

| Relation | Rows |
| --- | ---: |
| Documents | 3,229 |
| Document chunks | 178,854 |
| Document map units | 113,616 |
| Document map-unit tokens | 13,216,322 |
| Document map-unit indexes | 3,229 |

Every non-primary token index inspected has meaningful production scan counts.
Index removal is therefore not an optimization candidate without separate query-
plan and load evidence. ADR 0009's compatibility constraint remains in force.

The token lookup retirement migration is admitted only after a production-shaped
write benchmark shows that the superseded token-leading and compact candidate
indexes dominate token persistence and a read probe records the binary candidate
index serving classic discovery. The production-used map-unit lookup and unit
lookup indexes remain in place. Retired indexes remain reconstructible through
Alembic downgrade, and the binary candidate is created before retirement runs.

The two heaviest real namespace candidates currently have these anonymous
shapes:

| Candidate | Active documents | Chunks | Map units | Indexed tokens | Graph edges | Compressed snapshot |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 965 | 25,353 | 14,817 | 2,724,794 | 12,701 | 5.3 MB |
| B | 647 | 61,061 | 43,367 | 5,541,898 | 20,657 | 15.8 MB |

Telemetry calibration, not row counts alone, selects the slower namespace as
the sole formal Worst-Case Publication Corpus.

The frozen publication input is the production-shaped SpaceX parse:

- 922 chunks: 670 text, 99 image, and 153 table;
- 830 map units;
- approximately 88,551 token rows;
- metadata on all chunks and artifact paths on 252 chunks.

The 227-chunk demo representation is not an acceptable substitute for this
input.

## Safety invariants

Every candidate must preserve the atomic publication decisions in ADRs 0006,
0007, 0008, 0009, and 0010:

- the document revision and serving index publish in one transaction;
- the completeness marker is written last;
- partial serving state never becomes active;
- retrieval captures coherent revision pins and serving generation;
- incomplete historical indexes retain the exact legacy fallback;
- `document_map_unit_tokens` remains part of the serving contract.

The validation order is:

1. telemetry overhead and trace completeness;
2. Publication Lifecycle Parity and Publication Scope Isolation;
3. Publication State Parity;
4. Publication Atomicity Matrix and Publication Recovery Gate;
5. idempotency/race and Publication Strategy Compatibility;
6. Retrieval Semantic Parity, including cold and warm serving-cache paths;
7. Concurrent Publication Convergence and Retrieval Interference Gate;
8. Publication Resource Envelope;
9. Publication Duration and Publication Capacity statistics.

A candidate that fails an earlier gate is not benchmarked as an admissible
optimization.

## Phase 1: frozen measurement layer

Add measurement-only shared Publication telemetry before changing publication
behavior. Both ordinary parse and featured-project materialization pass an
explicit Knowhere-owned `PublicationTrace` through the same shared modules.

The transaction owner creates and completes the trace. Nested modules only
record named stages. There is no implicit global or ContextVar tracker and no
direct Logfire dependency.

### Trace output

Emit exactly one structured Loguru event on terminal success or rollback. Do
not add a telemetry table or any publication-transaction write. During baseline
and candidate production verification, record 100 percent of publication
traces.

The event may contain:

- job, result, and document opaque identifiers;
- a stable opaque scope fingerprint;
- job type and parse track;
- input counts by chunk type;
- section, chunk, map-unit, token, graph-edge, SQL-statement, and SQL-batch
  counts;
- namespace active-document count;
- manifest and snapshot compressed and uncompressed byte counts;
- per-stage duration, SQL duration, and outcome.

It must not contain raw user IDs, namespace values, source file names, queries,
document content, token text, asset paths, SQL text, bind parameters, or other
business payloads.

### Timing stages

Measure at least:

- connection-pool checkout wait;
- existing-document row-lock wait;
- document resolution, revision update, and result binding;
- sections and chunks preparation and persistence;
- serving-index preparation;
- map-unit persistence;
- token persistence;
- statistics and serving-manifest persistence;
- graph preparation and persistence;
- namespace-generation lock wait;
- namespace-snapshot decode/merge/encode preparation and persistence;
- commit or rollback.

Serving-index, graph, and namespace-snapshot work must distinguish in-process
`prepare` time from database `persist` time. Lock and pool waits are separate
from both.

Within each stage, aggregate SQL observation as `statement_count`,
`total_sql_ms`, and `max_statement_ms`. Do not emit one log per SQL statement.
Targeted SQL analysis uses `EXPLAIN (ANALYZE, BUFFERS, WAL)` only on a writable
benchmark clone after stage evidence identifies a suspect statement.

### SQL instrumentation seam

The transaction owner explicitly attaches the trace to its SQLAlchemy
connection. Engine hooks read connection-local metadata so ORM flush statements
are included. The attachment is always cleared in `finally`, and a contract test
must prove that pooled connection reuse cannot leak one publication's trace into
another. A future adapter that bypasses SQLAlchemy cursor hooks, such as native
COPY, records its database operation through the same trace interface.

### Telemetry admission gate

Before collecting the formal baseline, compare trace disabled and enabled on
the same frozen input:

- Publication State Parity must be exact;
- telemetry must add no SQL statements or database writes;
- cold-run p95 overhead must be at most `max(2%, 50ms)`;
- memory must not grow continuously;
- pooled connections must retain no trace state.

After this gate passes, freeze the telemetry revision. The formal comparison is
`origin/main behavior + frozen telemetry` versus `candidate behavior + the same
frozen telemetry`.

The telemetry contract also requires exactly one terminal event per publication
attempt, no missing or duplicate terminal events, stage counters that reconcile
with persisted rows, no trace state leakage through pooled connections, and no
continuous process-memory growth.

## Phase 2: reproducible benchmark assets

Store the ignored publication input under:

```text
.benchmarks/publication/inputs/spacex-s1-production/
  manifest.json
  chunks.json.gz
```

The manifest records the source revision, payload schema, counts, and content
digest. It contains parsed publication input, not artifact files.

Keep the September production dump immutable in its stable Docker named volume.
The current source volume is `knowhere-prod-restore-data`; do not relocate the
dump or benchmark databases into `/tmp`. The original `knowhere` database is
read-only benchmark source state. Every baseline or candidate sample uses a
separate writable database clone, and a clone modified by another candidate is
not baseline evidence.

The code baseline is a freshly fetched `origin/main`. The current dirty
experimental worktree and the previously modified `knowhere_prepost_ab`
database are not formal baseline inputs.

## Phase 3: correctness harnesses

### Publication Lifecycle Parity

Compare the complete normalized lifecycle, not only retrieval-serving rows:

- JobResult and JobChunk payloads and their document binding;
- document revision and active-revision pointer;
- job terminal state, version, and state-audit record;
- webhook outbox intent;
- featured-project materialization claim and canonical artifact pointer through
  its adapter-specific transaction/recovery contract;
- post-commit cache visibility and durable effect intent.

The worker's single lifecycle transaction and the featured-project adapter's
separate claim-finalization transaction are tested according to their actual
contracts. A publication commit followed by claim-finalization failure must
have a deterministic reconciliation path; it must not report an unqualified
failure while leaving an undiscoverable published document.

### Publication Scope Isolation

Run serial and concurrent publication cases for two users and two namespaces.
Verify that document revisions, map units, tokens, graph nodes/edges, serving
manifests, namespace snapshots, generations, cache versions, and retrieval
results never cross either user or namespace scope.

### Publication State Parity

Normalize generated IDs and timestamps, then compare all business fields and
relationships for:

- document and job-result binding;
- sections and chunks;
- map units, tokens, and map-unit statistics;
- serving manifest;
- graph state;
- namespace snapshot;
- active revision and serving-generation transition.

### Publication Atomicity Matrix

Inject failure after sections/chunks, map units, token persistence, serving
manifest, graph publication, namespace snapshot, and immediately before commit.
Run the matrix for both a new document and a replacement revision.

A failed new-document publication leaves no publication state. A failed
replacement preserves the complete old state, active revision, snapshot, graph,
and generation. Immediately retry the same payload after every rollback; the
retry must match a clean publication run.

Inject interruption or connection loss immediately before commit, during commit,
and immediately after commit. If the commit outcome is unknown, retry or
reconcile by job identity and verify one coherent outcome: no duplicate active
revision, graph relationship, snapshot entry, job completion, materialization,
or webhook/cache effect.

### Idempotency and race matrix

Verify duplicate completion after a successful commit, concurrent replacement
of the same document, stale completion arriving after a newer revision,
namespace movement, archived-document publication, and all-duplicate input.
Every case must preserve the active-revision and generation invariants.

### Publication Strategy Compatibility

With the global strategy setting switched between `baseline` and `candidate`,
verify the four read/write combinations: each strategy must read and replace
state written by either strategy. Include incomplete or legacy serving indexes,
warm and cold caches, retry/recovery, and a rollback while existing candidate
state is present.

### Post-commit effect contract

Rollback must not increment retrieval cache versions or enqueue webhooks. A
successful commit must produce each effect exactly once; an effect failure must
not roll back committed database state. A process interruption between commit
and effects must be recoverable without permanently stale retrieval or duplicate
webhook delivery.

### Retrieval Semantic Parity

Use the read-only production-data harness and the ignored baseline store:

```text
.benchmarks/retrieval-parity/
```

The fixed corpus covers approximately 58, 227, and 922 chunks. For fixed scope
and coherent revision pins, compare chunk IDs, ordering, rounded scores,
citations, source sections, evidence, asset references, router, and stop reason.
Avoid HTTP routes that write hit statistics or cache state.

## Phase 4: cold publication baseline

Calibrate candidate A and candidate B using the same frozen input. The slower
cold-run p95 namespace becomes the sole formal Worst-Case Publication Corpus.

Each cold sample uses:

- an equivalent fresh writable database clone;
- restarted PostgreSQL;
- a fresh Python process and pool;
- cleared relevant Redis keys;
- exactly one publication into that clone;
- clone reset or destruction after the sample.

Collect at least 20 independent cold samples for exploration and candidate
selection. Do not use host-wide `drop_caches`; record operating-system cache
state as a limitation. Report cold and warm results separately.

The final `<10s` claim uses a predeclared one-sided statistical rule rather than
the exploratory sample count. For example, requiring at least 59 independent
cold samples with every sample below 10 seconds gives a conservative 95 percent
binomial statement that at least 95 percent of runs meet the threshold. A
different order-statistic or bootstrap rule is acceptable only if fixed before
looking at candidate results and reported with its confidence bound.

Run the same shared publication input through both the worker sync transaction
owner and the API async transaction owner. The 10-second target applies only to
Publication Duration for each path, not to parsing or artifact upload.

The primary scenario adds a new document. Publishing a new revision of an
existing document remains a required correctness and timing regression, but is
not initially required to meet the 10-second target.

## Phase 5: capacity and retrieval interference

The primary capacity scenario concurrently adds documents to the same
Worst-Case Publication Corpus namespace. Different-namespace load is diagnostic,
not the admission gate.

Use one worker replica and concurrency levels `1`, `2`, `4`, `8`, and `10`,
matching the current sync pool's default total capacity. Use two-stage sampling:

1. Run five independent fresh-clone batches at every level to locate the
   saturation knee.
2. Run 20 independent batches at the knee, at concurrency 10, and at every
   level showing a possible regression.

Interleave baseline and candidate batches on the same host and configuration to
reduce environmental drift.

All successful concurrent publications must converge on a final generation that
contains every committed active revision in the serving snapshot and graph.
Completion order must not lose a document.

Capacity admission requires:

- zero publication errors, timeouts, or deadlocks through concurrency 10;
- complete Concurrent Publication Convergence;
- throughput at the knee and concurrency 10 at least 90 percent of baseline;
- arrival-to-commit p95 at most 110 percent of baseline;
- every observed SQL statement below 20 seconds;
- additional samples for results near a threshold.

### Publication Resource Envelope

For baseline and candidate, record per-publication WAL bytes, table/index size
delta, temporary bytes, peak process RSS, CPU, connection usage, lock waits,
checkpoint/replication pressure where available, and free database disk. A
candidate that meets elapsed-time gates by materially exceeding the baseline
resource envelope is rejected or requires an explicitly approved capacity
decision.

### Retrieval Interference Gate

While the publication batch modifies the worst namespace, continuously issue
read-only retrieval probes against pre-existing documents that the batch does
not modify. Each request captures fresh coherent generation and revision pins.
Freeze route, query, filters, and ranking options so LLM planning variance does
not pollute the database-interference measurement.

Use open-loop load at one request per second as the primary gate and three
requests per second as a burst diagnostic. These rates match the current 30-day
production `retrieval_runs` evidence: active-second p95 of one request and an
observed peak of three requests. Bypass cache and hit-stat write-back.

Admission requires:

- zero semantic mismatches, errors, and timeouts;
- coherent revision pins throughout each request;
- candidate retrieval p95 no more than 10 percent above the equivalent baseline
  under the same publication and probe load;
- additional samples for results near the threshold.

Run the complete agentic/router/stop-reason Retrieval Semantic Parity suite
before and after the capacity batch. The continuous probe intentionally measures
the deterministic retrieval core.

## Phase 6: evidence-led optimization loop

Do not select the first optimization before the frozen baseline exists. For each
candidate:

1. Select the largest relevant p95 stage from the trace.
2. Form one testable hypothesis for that stage.
3. Implement one candidate behind that stage's existing or newly introduced
   narrow interface.
4. Run correctness gates before performance gates.
5. Compare end-to-end cold Publication Duration, not only a stage
   microbenchmark.
6. Keep the candidate only when the end-to-end p95 improvement has a clear
   bootstrap 95 percent confidence interval. Add samples when intervals overlap;
   reject added complexity when improvement remains inconclusive.
7. Repeat only if the 10-second target is not yet met.

Native COPY, batch resizing, graph-query changes, snapshot changes, or index
changes remain hypotheses until their measured stage is dominant.

## Release and rollback

Use one simple global candidate-specific configuration at the optimized stage's
composition root:

```text
baseline | candidate
```

Do not add per-user, per-namespace, per-request, percentage, or automatic
fallback routing. Deploy the candidate code with the global setting on
`baseline`, verify telemetry, then switch globally to `candidate`. If production
gates regress, switch globally back to `baseline`.

Baseline and candidate implementations satisfy the same narrow interface and
contract tests. Candidate indexes are introduced additively first. A separate
index-retirement migration is allowed only after the recorded query-plan, scan,
load, and state-parity evidence for the candidate; it must retain candidate
coverage and recreate the retired indexes on downgrade. Remove the baseline
implementation and temporary setting after production confirmation; do not
retain permanent dual paths.

Production confirmation requires the same Publication State Parity,
Publication Lifecycle Parity, Publication Recovery Gate, Publication Scope
Isolation, Publication Strategy Compatibility, Publication Duration,
Publication Resource Envelope, error/timeout, Publication Capacity, and
retrieval-interference signals used locally. A local result is the admission
gate, not a claim of production latency.

## Exit criteria

The optimization is complete only when:

- both real transaction owners pass all correctness gates;
- lifecycle, scope-isolation, atomicity/recovery, idempotency, and strategy-
  compatibility gates pass;
- new-document cold Publication Duration p95 is below 10 seconds on the
  production-configured benchmark;
- the conservative local configuration is reported separately as I/O
  sensitivity evidence;
- the candidate stays within the approved Publication Resource Envelope;
- Publication Capacity and Retrieval Interference gates pass;
- production telemetry confirms the result after global candidate enablement;
- the temporary baseline path and feature flag are removed after the observation
  period.
