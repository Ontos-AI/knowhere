# Retrieval Serving Index Rollout Runbook

## Scope

This runbook rolls out the retrieval serving-index performance changes without
changing retrieval semantics. It covers the additive schema migration, the
existing-data statistics backfill, readiness checks, retrieval parity probes,
monitoring, pause/resume, and application rollback.

This rollout does not use a runtime feature flag. The reader selects the
optimized path only when every revision required by a request has a coherent
format-v2 index and all four channel statistics. Otherwise it uses the existing
legacy reader automatically.

Planner, Harvest, Control, prompts, BM25 formulas, and citation-selection
semantics are outside this rollout.

## Preconditions

- Deploy only a build produced from the reviewed retrieval optimization branch.
- Confirm the target is the intended production environment before every
  command.
- Use the API image for Alembic and maintenance commands. It contains:
  - `/app/alembic`
  - `/app/scripts/backfill_map_unit_statistics.py`
  - `/app/scripts/backfill_map_unit_indexes.py`
- Confirm database backups and the normal application-version rollback path are
  available.
- Record current retrieval p50/p95, errors, timeouts, and the output of the
  temporary two-or-three-query quality set before deployment.
- Check free database disk space. The token-leading index is additive and the
  existing indexes must remain in place.

## Local acceptance evidence

The production-shaped local restore used for final verification contained:

- 2,996 documents;
- 125,835 sections;
- 170,280 chunks;
- 107,032 map units;
- 12,423,317 map-unit token rows;
- 644 active documents in the benchmark namespace.

Final readiness on that namespace:

```text
alembic head: c2d3e4f5a6b7
covering index: idx_document_map_unit_tokens_token_lookup
statistics check: would_update=0 complete=644 skipped=0 documents=644
coherent current indexes: 644/644
```

Classic parity queries preserved chunk IDs, ordering, sources, and evidence.
The maximum observed score delta was below the accepted `1e-4` tolerance.
The v1 and v2 classic entry points returned the same chunk IDs and evidence
hash. A complete map-nav smoke returned `stop_reason=completed`, 23 result rows,
and 27 referenced chunks.

These figures describe the local restored copy. They are not substitutes for
the production checks below.

## Phase 1: Preflight inventory on the existing schema

Before migration, record active/current revision counts using only the existing
schema. Do not run the statistics checker yet because it reads columns added by
revision `b1c2d3e4f5a6`.

```sql
SELECT count(*) AS active_current_documents
FROM documents
WHERE status = 'active'
  AND current_job_result_id IS NOT NULL;

SELECT count(*) AS current_map_indexes
FROM documents
JOIN document_map_unit_indexes AS indexes
  ON indexes.document_id = documents.document_id
 AND indexes.job_result_id = documents.current_job_result_id
WHERE documents.status = 'active'
  AND documents.current_job_result_id IS NOT NULL;
```

Record both counts and investigate any pre-existing difference. This inventory
does not determine format-v2 readiness.

## Phase 2: Apply the additive migrations

The production release workflow runs migrations before updating ECS services.
Record the workflow job URL and output. For a manual rehearsal, run Alembic from
the API image:

```bash
cd /app
python -m alembic upgrade heads
python -m alembic current
```

The expected head for this rollout is:

```text
c2d3e4f5a6b7
```

The three relevant additive migrations are:

1. `a0b1c2d3e4f5`: creates
   `idx_document_map_unit_tokens_token_lookup` on
   `(channel, token_hash, map_unit_id) INCLUDE (token, frequency)`;
2. `b1c2d3e4f5a6`: adds nullable path/content document-count and total-length
   columns to `document_map_unit_indexes`;
3. `c2d3e4f5a6b7`: repairs the covering index if it is missing or PostgreSQL
   reports `indisvalid=false` or `indisready=false` after an interrupted build.

The index migration uses `CREATE INDEX CONCURRENTLY` in the normal Alembic
execution path. Monitor lock waits, database CPU, I/O, replication lag, and
free disk space while it runs. Do not drop either pre-existing map-unit-token
index during this rollout.

Verify the schema:

```sql
SELECT version_num FROM alembic_version;

SELECT
  classes.relname AS index_name,
  indexes.indisvalid,
  indexes.indisready,
  pg_get_indexdef(indexes.indexrelid) AS index_definition
FROM pg_index AS indexes
JOIN pg_class AS classes ON classes.oid = indexes.indexrelid
WHERE classes.relname = 'idx_document_map_unit_tokens_token_lookup';

SELECT column_name
FROM information_schema.columns
WHERE table_name = 'document_map_unit_indexes'
  AND column_name IN (
    'path_document_count',
    'path_total_length',
    'content_document_count',
    'content_total_length'
  )
ORDER BY column_name;
```

The covering index must exist with `indisvalid=true`, `indisready=true`, and
the expected key/include columns. A failed concurrent build can leave an
invalid index. If either flag is false, stop the rollout, drop only that invalid
index with `DROP INDEX CONCURRENTLY`, and rerun the migration.

## Phase 3: Deploy the application build

Deploy the application after the additive migrations finish. New publications
will write coherent format-v2 statistics. Existing revisions with NULL channel
statistics remain on the full scope-first map-unit reader until maintenance
completes; missing, legacy, or unusable indexes remain on the legacy reader.

Immediately verify:

- API health checks pass;
- no migration or model-loading error appears in API logs;
- classic and map-nav requests still complete;
- incomplete-index warnings distinguish statistics-incomplete map-unit serving
  from `fallback=legacy_fts`; neither case may return partial or empty results;
- no increase appears in retrieval errors or timeouts.

## Phase 4: Backfill existing format-v2 indexes

After the migration, run the statistics inventory:

```bash
python /app/scripts/backfill_map_unit_statistics.py \
  --check \
  --batch-size 100
```

Record the final summary line:

```text
would_update=<count> complete=<count> skipped=<count> documents=<count>
```

Interpretation:

- `complete`: already coherent format-v2 revisions;
- `would_update`: existing format-v2 indexes whose four statistics need an
  in-place update;
- `skipped`: missing or legacy indexes that require the full index backfill.

Run exactly one maintenance process against the database. `--batch-size` is a
serial session grouping, not a concurrency setting.

First rehearse one namespace or document:

```bash
python /app/scripts/backfill_map_unit_statistics.py \
  --apply \
  --batch-size 100 \
  --user-id <user-id> \
  --namespace <namespace>
```

Then run the full statistics backfill:

```bash
python /app/scripts/backfill_map_unit_statistics.py \
  --apply \
  --batch-size 100
```

The command:

- reads existing `document_map_units`;
- computes positive-length document counts and total token lengths separately
  for the path and content channels;
- commits each revision independently;
- does not regenerate token rows, manifests, or namespace snapshots;
- is idempotent and safe to restart.

If `skipped` is non-zero, list and process those documents separately with the
existing full index command:

```bash
python /app/scripts/backfill_map_unit_indexes.py \
  --apply \
  --document-id <document-id>
```

Do not use `--tokens-only` unless investigation proves that only map-unit token
data is missing and the serving manifest and namespace snapshot are already
coherent.

## Phase 5: Final readiness gate

Repeat the read-only check until it exits successfully and reports:

```text
would_update=0 skipped=0 complete=<documents> documents=<documents>
```

```bash
python /app/scripts/backfill_map_unit_statistics.py \
  --check \
  --batch-size 100
```

Also verify that all retrieval-visible active revisions are coherent:

```sql
SELECT
  count(*) FILTER (
    WHERE indexes.format_version = 2
      AND indexes.path_document_count IS NOT NULL
      AND indexes.path_total_length IS NOT NULL
      AND indexes.content_document_count IS NOT NULL
      AND indexes.content_total_length IS NOT NULL
  ) AS ready_revisions,
  count(*) AS current_revisions
FROM documents
LEFT JOIN document_map_unit_indexes AS indexes
  ON indexes.document_id = documents.document_id
 AND indexes.job_result_id = documents.current_job_result_id
WHERE documents.status = 'active'
  AND documents.current_job_result_id IS NOT NULL;
```

With the `LEFT JOIN`, `current_revisions` includes active documents with no
index. `ready_revisions` must equal `current_revisions`. Also require:

```sql
SELECT count(*) AS missing_index_rows
FROM documents
LEFT JOIN document_map_unit_indexes AS indexes
  ON indexes.document_id = documents.document_id
 AND indexes.job_result_id = documents.current_job_result_id
WHERE documents.status = 'active'
  AND documents.current_job_result_id IS NOT NULL
  AND indexes.id IS NULL;
```

`missing_index_rows` must be zero.

Run the complete serving-index readiness checker:

```bash
python /app/scripts/backfill_map_unit_indexes.py --check
```

For every namespace, require `status=READY` and inspect the report rather than
only its exit code. Require:

```text
missing_from_snapshot=0
missing_map_index=0
missing_revision_manifest=0
```

`suspicious_zero_idf` is diagnostic only. Zero average IDF is valid for some
small corpora, including a two-unit corpus where each token occurs in exactly
one unit; it must not independently block readiness.

## Phase 6: Retrieval-quality gate

Run the frozen temporary quality set. Use the same two or three queries,
namespace, top-k, filters, and revision generation captured before deployment.

For classic retrieval, require no change in:

- router;
- selected chunk IDs and order;
- source document and section;
- evidence content/hash;
- asset references;
- rounded scores, with an absolute tolerance of `1e-4`.

Exercise at least:

1. v1 `use_agentic=false`;
2. v2 `use_agentic=false` with equivalent retrieval fields;
3. one `use_agentic=true` map-nav smoke;
4. one request with `use_agentic` omitted, confirming it routes to map-nav;
5. one filtered request, confirming filtered-scope semantics and the safe
   fallback where required.

Map-nav LLM output is nondeterministic. For production smoke, require successful
completion, valid citations, expected namespace isolation, and relevant
evidence. Do not require byte-identical ordering between independent Planner
runs. Deterministic map-score parity remains covered by the contract suite.

Stop the rollout if classic result parity fails. Do not refresh the baseline to
hide a difference.

## Phase 7: Performance and reliability observation

Monitor at least one normal traffic window after the backfill. Classic public
result parity must have zero differences, total non-LLM p95 must not exceed the
recorded baseline, and retrieval error/timeout rates must not regress.

- retrieval request p50/p95 and maximum latency, separated by `router_used`;
- classic `search.map_unit_discovery` stages: units, frequencies, indexes,
  statistics, scoring, and hydration;
- map-nav snapshot, episode, and hydration stages;
- PostgreSQL statement timeouts, lock waits, CPU, I/O, and connection usage;
- Redis errors and namespace snapshot cache misses;
- retrieval errors, incomplete-index fallbacks, and response timeouts;
- process CPU and maximum RSS from the corrected map-nav resource log.

Do not mix classic and map-nav latency distributions. Do not treat Redis-warm
snapshot measurements as cold-request performance.

## Pause and resume

To pause, stop launching new maintenance command processes. Wait for the
current revision to finish if database health permits, then send `SIGINT` or
`SIGTERM` to the one-off task. Each completed revision has already committed;
the interrupted transaction rolls back and remains on the safe reader path.

To resume, rerun the same `--apply` command. Completed revisions are detected
and skipped. Follow it with `--check` and retain both summary outputs in the
deployment record.

## Rollback

If application errors, timeouts, or quality regressions occur:

1. stop the backfill process;
2. redeploy the previous application version;
3. verify classic and map-nav requests using the frozen quality set;
4. retain the additive columns, index, and already-computed statistics unless
   database health specifically requires their removal.

Application rollback is sufficient because the previous application ignores
the additive schema. Avoid running Alembic downgrade during an incident: a
concurrent index drop or table alteration adds operational risk and is not
required to restore the previous behavior.

## Completion record

Attach the following to the deployment ticket:

- deployed application image digest and Git commit;
- pre-migration active/current inventory;
- post-migration statistics `--check` output;
- Alembic `current` output;
- covering-index and column verification output;
- rehearsal and full `--apply` summaries;
- final `--check` output;
- ready/current revision counts;
- frozen-query parity results;
- classic and map-nav latency summaries;
- observed fallback, error, and timeout counts;
- rollback decision or explicit confirmation that rollback was not required.

## ECS one-off task requirements

Run maintenance as a one-off task created from the newly registered production
API task definition. Reuse its task role, execution role, VPC subnets, security
groups, Secrets Manager injection, and CloudWatch log configuration. Override
only the container command with one of the commands in this runbook.

Do not execute maintenance inside a long-lived API task. Do not copy, export, or
place the production database URL in the ECS command, shell history, workflow
input, deployment ticket, or logs. The one-off task must receive it through the
same production secret as the API task.
