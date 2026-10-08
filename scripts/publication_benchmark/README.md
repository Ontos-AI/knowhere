# Publication benchmark

Phase 0 of `docs/design/publication-performance-optimization-plan.md` established
frozen input, writable clones, the publication runner, and report aggregation.
The runner now supports measurement-only `PublicationTrace` and the candidate
chunk and token COPY path. Phase 5 adds executable capacity and retrieval
interference checks. A completed command is evidence for its recorded source,
database schema, and template; it does not establish production performance.

## Commands

```bash
uv run python scripts/publication_benchmark/freeze_input.py \
  --source-db-url-file /tmp/knowhere-prod-db-url-read-only \
  --source-revision <parse-input-revision> \
  --output .benchmarks/publication/inputs/spacex-s1-production

uv run python scripts/publication_benchmark/clone_db.py \
  --run-id <run-id> \
  --postgres-config production \
  --template-id <template-id> \
  --action create

uv run python scripts/publication_benchmark/run_publication.py \
  --owner sync \
  --db-url-file .benchmarks/publication/clones/<run-id>/database-url \
  --input .benchmarks/publication/inputs/spacex-s1-production \
  --strategy baseline \
  --mode cold \
  --trace disabled

# Reset the same run ID from a sealed template before the enabled sample.
uv run python scripts/publication_benchmark/clone_db.py \
  --run-id <run-id> \
  --template-id <template-id> \
  --action reset

uv run python scripts/publication_benchmark/run_publication.py \
  --owner sync \
  --db-url-file .benchmarks/publication/clones/<run-id>/database-url \
  --input .benchmarks/publication/inputs/spacex-s1-production \
  --strategy baseline \
  --mode cold \
  --trace enabled

uv run python scripts/publication_benchmark/run_correctness.py \
  --case LP-001 --owner sync --strategy baseline

uv run python scripts/publication_benchmark/run_capacity.py \
  --execute --owner sync --strategy both \
  --run-id <listed-clone-id> --report-id <capacity-report-id> \
  --template-id <sealed-template-id> \
  --input .benchmarks/publication/inputs/spacex-s1-production \
  --probe-spec /tmp/retrieval-probe.json

uv run python scripts/publication_benchmark/aggregate.py \
  --report-dir .benchmarks/publication/reports/<run-id>

uv run python scripts/publication_benchmark/formal_campaign_report.py \
  --clones-root .benchmarks/publication/clones \
  --report-path .benchmarks/publication/reports/formal59-compact-20260925/formal-campaign.json

uv run python scripts/publication_benchmark/compare_state.py \
  --run-id <run-id> \
  --disabled-sample-id <disabled-sample-id> \
  --enabled-sample-id <enabled-sample-id>

uv run python scripts/publication_benchmark/admit_telemetry.py \
  --run-id <run-id> \
  --memory-run-id <small-test-run-id> \
  --pool-run-id <pool-test-run-id>
```

The example commands show one matrix point. Re-run the runner for the other
owner (`async`), strategy (`candidate`), mode (`warm`), and case identifiers.
Shell alternation syntax must not be used in command lines.

The optimized publication implementation is unconditional. Active runners accept
only `candidate`; baseline labels remain in historical reports for comparison.
Run each sample in its own process to isolate settings and connection pools.

`formal_campaign_report.py` validates the split 59-round compact-template
campaign across 236 independent clones. It checks sample identity, frozen
counts, clone schema and template, duration, paired improvement, and terminal
trace accounting. Its overall status remains `insufficient_evidence` until
separate state parity, correctness, SQL observation, capacity, and retrieval
interference gates have supporting evidence.

### Phase 5 retrieval interference probe

`run_capacity.py --execute --strategy both` resets the listed clone from the
sealed template before every baseline and candidate batch. It runs five
interleaved batches at concurrency 1, 2, 4, 8, and 10, then twenty more at the
baseline saturation knee, concurrency 10, and possible regression levels.
`--concurrency 1 --smoke --probe-duration 3` runs one pair as a partial check;
it cannot pass the full admission gate. Results are written incrementally to
`.benchmarks/publication/reports/<capacity-report-id>/capacity.json`.
Use `--source-volume` and `--source-container` when the sealed source runs under
different local Docker names. These identify the source for clone safety checks.

Prepare a private JSON spec with `user_id`, `namespace`, `query`,
`protected_document_ids` (pre-existing documents that the publication batch
does not modify), and `top_k`. These documents must already exist in the
sealed template. Keep the same spec for both strategies. The
probe calls classic map-unit discovery, ranking, and hydration with fresh
revision pins in a repeatable-read, read-only transaction. It bypasses the
retrieval cache, hit-stat writes, and Redis index-readiness publication.

The capacity runner starts this probe before concurrent publication and
continues at the configured open-loop rate until after it finishes. The default
is one request per second; use `--probe-rate 3` for a burst diagnostic. It
records SQL duration, WAL, table and index growth, temporary bytes, peak
connections, container RSS and CPU, checkpoint activity, and free disk. Missing
resource measurements require review; a failed publication or probe remains
red even in a partial smoke run.

The standalone read-only probe remains useful for diagnostics. Capture an
equivalent baseline publication-plus-probe report first, then supply that
report to the candidate run:

```bash
uv run python scripts/publication_benchmark/run_retrieval_probe.py \
  --run-id <baseline-clone-id> --probe-spec /tmp/retrieval-probe.json \
  --owner sync --strategy baseline --concurrency 4 \
  --rate 1 --duration 30 --report-id <baseline-report-id>

uv run python scripts/publication_benchmark/run_retrieval_probe.py \
  --run-id <candidate-clone-id> --probe-spec /tmp/retrieval-probe.json \
  --owner sync --strategy candidate --concurrency 4 \
  --rate 1 --duration 30 --report-id <candidate-report-id> \
  --expected-report .benchmarks/publication/reports/<baseline-report-id>/retrieval-interference.json
```

Use `--rate 3` for the burst diagnostic. The report records per-request
duration, scheduling lag, redacted result and revision digests, plus a p95
comparison. The comparison requires the same host, sealed template, PostgreSQL
settings, source clone, spec, owner, concurrency, rate, and duration. Run full
agentic/router/stop-reason semantic parity before and after a capacity batch;
this probe covers only the deterministic classic retrieval core.

`clone_db.py` also accepts `--action query|reset|destroy` to verify, re-create,
and tear down a clone. Destroyed clones stay listed with `state=destroyed` so a
stale database URL can never be reused as evidence.

### Reuse a sealed database template for cold samples

The default `create` and `reset` commands still run a fresh logical restore and
`VACUUM (ANALYZE)`. For repeated cold samples, first restore and settle one
unused clone per PostgreSQL profile, then seal it:

```bash
uv run python scripts/publication_benchmark/clone_db.py \
  --run-id production-template-1 \
  --postgres-config production \
  --action create

uv run python scripts/publication_benchmark/clone_db.py \
  --run-id production-template-1 \
  --action seal-template
```

`seal-template` stops the clone, requires a clean PostgreSQL shutdown, refuses
external tablespaces or WAL symlinks, fingerprints the full stopped PGDATA, and
removes its ordinary clone record. Its volume remains intact and is mounted
read-only during copies. A sampled clone cannot be sealed.

Each sample still gets a separate writable Docker volume, container, port,
process, and pool. Use a unique `--run-id` for each sample, or reset the sample
after its prior state has been consumed:

```bash
uv run python scripts/publication_benchmark/clone_db.py \
  --run-id sample-001 \
  --postgres-config production \
  --template-id production-template-1 \
  --action create

uv run python scripts/publication_benchmark/clone_db.py \
  --run-id sample-001 \
  --template-id production-template-1 \
  --action reset
```

The template and sample must use the same profile and PostgreSQL 15 image.
The sample clone record retains the September source identity and records
`database_identity.template_id` plus `template_content_digest`. Keep baseline
and candidate samples on the same sealed template; run comparability and state
parity both check the template digest. The template's
database password is reused for its physical copies and remains in ignored
`database-url` files with mode `0600`, on loopback-only PostgreSQL ports.

Physical copying avoids repeated logical restore and VACUUM, but still copies
the full PGDATA volume. Measure one copy before scheduling a large run. Template
creation reads the full volume once for its fingerprint; each later copy checks
the clean shutdown metadata but does not rehash every file. PostgreSQL system
identifiers are identical across physical copies, so use the clone's own volume,
container, port, URL digest, and run ID for identity. This workflow does not
clear host page cache; the benchmark plan records that limitation and requires
interleaved baseline and candidate samples.

Phase 0 records the sample mode and enforces that cold samples start from a
freshly created or reset clone. Warm-sample pool and Redis reuse semantics land
with Phase 4, together with the namespace calibration that selects the formal
Worst-Case Publication Corpus.

## Artifacts

All generated state stays under the git-ignored `.benchmarks/` tree:

```text
.benchmarks/
  publication/
    inputs/spacex-s1-production/{manifest.json,chunks.json.gz,freeze-result.json}
    clones/<run-id>/{clone.json,database-url,postgres.log,publication.jsonl,state-before.json,state-after.json}
    reports/<run-id>/{summary.json,summary.md,resource.json,gate-results.json}
```

`publication.jsonl` holds one run record per sample: code commit, dependency
lock digest, strategy, owner, mode, PostgreSQL profile/version/settings digest,
Redis namespace, frozen input digest, clone identity, host identity, start and
end timestamps, outcome, and wall-clock publication duration. Baseline and
candidate records are comparable only when their recorded environment and input
identities match. Independent clone runs derive isolated Redis namespaces from
their run IDs; a campaign report must validate that exact naming protocol and
record the exception to raw namespace equality before comparing pairs.
Each record also stores `trace_enabled`. The default is `--trace disabled`.
Both trace modes store benchmark transaction SQL observations as statement and
write statement counts; affected row counts are advisory when any database
driver row count is unknown.
With `--trace enabled`, the runner writes one safe terminal payload to
`publication-traces.jsonl` and its attempt reference to
`trace-accounting.json`. Aggregation reports duration statistics separately
for enabled and disabled samples and reconciles terminal events only against
enabled attempts. A missing or duplicate enabled terminal event fails the
accounting gate.

The runner retains each sample's state under
`clones/<run-id>/samples/<sample-id>/`. `compare_state.py` requires one committed
sample of each trace mode from the same run ID, frozen input, synthetic scope,
source filename, owner, strategy, PostgreSQL profile/settings, host, and
template. It writes a separate redacted artifact under
`reports/<run-id>/parity/pair-<digest>.json` for every pair. A parity pass
checks publication-owned persisted rows and serving artifacts; telemetry
admission rereads those snapshots, compares SQL/write counts from both modes,
and checks cold p95 overhead within identical comparison identities. Memory
growth and pooled connection reuse remain insufficient evidence until their
independent measurements are recorded. The memory and pool checks use separate
small test runs; pass their run IDs explicitly to `admit_telemetry.py`. Admission
requires 20 paired cold samples per owner for its exploratory p95 overhead check.
The final publication duration claim still requires 59 independent cold samples
per owner, profile, and strategy.

Report artifacts and normalized state snapshots contain counts, digests, and
opaque references only. Raw user ids, namespaces, source file names, section
paths, chunk text, token text, artifact paths, credentials, SQL text, and bind
values are rejected before anything is written.

## Gate status

`aggregate.py` records the Phase 0 gate results in `gate-results.json`:

| Gate | Meaning |
| --- | --- |
| `frozen-input-digest` | Manifest and payload digest validate, counts match the documented SpaceX shape |
| `clone-lifecycle` | A listed clone exists with recorded source digest, schema revision, settings, index inventory, and database identity |
| `publication-samples` | Samples exist and committed |
| `no-op-publication-per-owner` | Both `sync` and `async` owners have a recorded sample |
| `verification-cases` | Case registry matches the design document; implemented and placeholder cases are reported separately |
| `terminal-trace-accounting` | Phase 0 passes after freezing the input/output accounting contract; when a trace file is present, every publication attempt must have exactly one terminal event. Phase 1 supplies the terminal events. |
| `duration` | Frozen one-sided rule: 59 cold samples, all below 10 seconds |
| `redaction` | Report artifacts were redaction-checked before writing |

The duration gate evaluates per `owner/profile/strategy` group and only claims a
result once the frozen 59-sample rule is satisfied. Twenty samples remain an
exploration minimum and never produce a `<10s` claim.

## Safety rules

- The September production dump stays in its Docker named volume. Every sample
  uses its own writable clone; the source volume is only ever mounted read-only.
- Commands refuse a read-only URL, the source database, an unlisted clone, a
  clone whose database URL digest does not match its record, and a frozen input
  without a digest.
- Cold samples require a freshly created clone. Post-commit effects are recorded
  as intents; Phase 0 runs never enqueue webhooks or touch a production Redis
  namespace.
