# Frozen mixed-workload runner

`opensearch-workload.schema.json` is generated from the versioned schema in
`opensearch-workload.py`. The command defaults to offline validation:

```bash
uv run --offline --frozen python scripts/probes/run-opensearch-workload.py \
  --manifest /private/knowhere-mixed/workload-v1.json \
  --output /private/knowhere-mixed/campaign-v1
```

It verifies every source/read/prepared-file checksum and compressed/uncompressed
size, generates the exact schedule and compares offered totals, phase/cohort
counts and rolling peak against the frozen manifest. Query payloads must explicitly
set `use_agentic:false`. It makes no service call during this validation.

The arrival JSON files contain:

```json
[
  {"id":"observed-read-id","offset_ms":1234,"case_id":"reviewed-query-case"}
]
```

```json
[
  {
    "id":"observed-write-id",
    "offset_ms":2345,
    "case_id":"retained-prepared-output",
    "kind":"update",
    "job_ids_by_segment":{
      "REVIEWED_SEGMENT_ID":["TEN_EXACT_PREDECLARED_STAGING_JOB_IDS"]
    }
  }
]
```

The strings above are format examples, not reviewed inputs. Each mapped segment
requires exactly ten explicit write identities. Reusing a job for independent
arrivals is rejected; explicit retry arrivals are visible separately. Source
windows are translated/repeated at their original relative offsets, with frozen
tenfold multiplicity/inter-copy offsets. They cover exactly the measured hour and
the following five minutes. Generated rolling peak must equal 50 external read
requests per second. The read trace alone cannot supply post-parse volume or bytes.

Each prepared gzip JSON contains retained `chunks`, existing `result_s3_key`,
`checksum`, `zip_size`, optional summaries and stored count. Freeze actual source
revision, exact source file name, unit count, byte sizes and bulk-byte distribution; represent the retained
1,862-unit document. No parser/model call is used to generate load. A prepared
output is never represented as an observed production write-arrival distribution
without actual provenance or explicit human-reviewed derivation.

Freeze warm/cold/capacity-wait planned counts, positive lexical-sample minima,
preconditioning receipts and expected slot states. Freeze hot/large/small namespace
distribution, updates, retries, supersession, burst start/end boundaries and
snapshot/maintenance offsets before execution. Resource/pool/worker/prefetch limits,
input/configuration/source hashes and genuine staging/durability receipts are part
of the private manifest. Missing fields or labels remain input/acceptance gates.
Freeze `runtime.maximum_offer_lag_ms` and `maximum_start_lag_ms` before execution.
The evaluator compares actual offers/dispatch against those producer bounds; they
qualify the intended arrival profile and do not define a new production SLA.

## Real prepared-completion worker

Install the immutable reviewed source overlay and retained assets in the staging
API and worker. See [the staging image builder](../../deploy/opensearch/staging_image.py).
Use the existing broker and result backend. Configure the existing worker startup:

```text
APP_ENV=staging
ENVIRONMENT=staging
KNOWHERE_PREPARED_COMPLETION_CAMPAIGN=REVIEWED_CAMPAIGN_ID
KNOWHERE_PREPARED_COMPLETION_QUEUE=knowhere-prepared-REVIEWED_CAMPAIGN_SUFFIX
KNOWHERE_WORKLOAD_OBSERVER_CONFIG=/private/worker-observer.json
```

Use a lowercase suffix and freeze that exact queue in `runtime.worker_queue`.
The worker adds only that queue and explicit staging task registration. Configure
the API's observer separately. Both observer files are private mode 0600 JSON
with `schema_version:"opensearch-stage-observer-v1"`, `campaign_id`, `source_hash`,
`journal_path` and `maximum_journal_bytes`. Give API and worker different journals
and list both in `resources.journal_paths`. This staging registration preserves the
normal queues and startup; it does not replace the original parse task.

At arrival, the source adapter calls normal job creation and indexing admission,
including updates and supersession on the actual document. All planned job IDs
are reviewed in advance; create/update jobs are committed at their respective
arrival, not precreated. The adapter validates the retained prepared bytes and
preseeds the approved staging-only charge. The prepared task checks the exact
job/owner/campaign/event/source/prepared identity, claims parsing ownership, calls
the existing lifecycle commit and inline indexing, and returns actual
job/attempt/build state. Its result is enabled only for this staging task. It does
not call source transfer, a parser, a model, or charging/refund entrypoints. An
explicit retry uses the real synchronous retry endpoint only after checking
eligibility for the existing retained job.

## Actual campaign configuration

Freeze the exact bytes of the private execution JSON with
`runtime.configuration_sha256`. Review the generated plan, then launch the
approved, bounded staging campaign in its one-use physical issuer on the existing
operator host:

```bash
uv run --offline --frozen python deploy/opensearch/launch-issuer.py \
  --kind workload --environment-file /etc/knowhere-native-ops/staging-campaign.env \
  --maximum-seconds 4200 --token APPROVED_INVOCATION_UUID --execute -- \
  --manifest /private/knowhere-mixed/workload-v1.json \
  --execution-config /private/knowhere-mixed/execution-v1.json \
  --approval /private/knowhere-mixed/campaign-approval.json \
  --output /private/knowhere-mixed/campaign-v1
```

The execution configuration binds:

- `api_base_url`, optional `api_ca_file`, private mode-0600 `credential_files`,
  `broker_url_file` and `result_backend_url_file`.
- `job_owners`, `write_credentials`, `prepared_worker_paths`, by the exact frozen
  job/case identities.
- `operator_manifest_path`, `operation_state_directory`, and `issuer` containing
  `invocation_token`, `host_identity`, `operation_id`, `epoch` and a new private
  `receipt_path`. The command creates the actual receipt after the SQL claim.
- `receipts.staging`, `durability`, `review`: actual passed staging receipt files
  matching the three frozen manifest SHAs and aggregate source.
- `bindings.precondition`, `admission`, `resource`, `maintenance`, `stage-export`
  point to the delivered `scripts/probes/workload-source-adapter.py`.
  `bindings.snapshot` points to the delivered
  `deploy/opensearch/workload_snapshot.py`. Each binds `script_path`,
  `script_sha256`, bounded `timeout_seconds` and this exact argv form:
  `[RUNTIME_PYTHON, SCRIPT, "--configuration", PRIVATE_JSON,
  "--expected-configuration-sha256", FILE_SHA256, "--execute"]`.
  The runner appends new private request/receipt paths. Every non-snapshot binding
  uses the same private source configuration.

Approval binds the campaign ID, exact manifest SHA, canonical operator directory,
actual host fingerprint, installation manifest SHA, prior user approval reference,
bounded cost-plan SHA and expiry. The environment file enables
`KNOWHERE_WORKLOAD_SOURCE_CAMPAIGN=REVIEWED_CAMPAIGN_ID` and staging only.

The source configuration schema in
[workload_probe.py](../../packages/shared-python/shared/services/retrieval/lexical/workload_probe.py)
uses `schema_version:"opensearch-workload-source-v1"`, the exact campaign/source/
issuer, `cases`, `cohorts` and `resources`. Each case binds actual owner, namespace,
fallback document ID, optional `document_ids_by_job` for independent replicas,
predeclared job IDs, source file name, private prepared path/bytes/SHA/unit count
and approved preseed charge. Cohorts bind exact observed scopes and optional
reviewed capacity actions. Preconditioning observes each cohort in sequence;
actual per-event cold recovery or capacity wait still decides whether a sample
qualifies. Resource config binds the observer journals, a private incremental
`state_path`, new private `export_path`, broker credential file and actual parse
queue name. Source stage export writes checksummed campaign/event JSONL.

The backup configuration binds `campaign_id`, `source_hash`,
`operator_manifest_path`, `issuer_receipt_path`, `backup_plan_path`,
`backup_approval_path` and `backup_journal_path`. It reuses the workload's actual
physical issuer; it does not claim another. Set the snapshot binding's own
deadline long enough for actual full-snapshot verification. The runner accepts
only an independently verified full point and proves overlap by intersecting its
actual snapshot interval with observed indexing or LRU intervals. Simultaneous
schedule entries alone do not prove overlap.

Resource samples bind `runtime.resource_measurement_contract` and their observed
measurement kinds. `pg_lock_wait_sampled_lower_bound_ms` is a sampled lower bound,
and `gc_collection_time_ms` is JVM collection time. Exact per-lock wait and exact
stop-the-world pause remain unavailable from those sources; neither proxy enters
a latency percentile. Missing required observations do not become zeroes.

Read quality JSON contains namespace/final `top_k`, complete protected canonical
rows (`document_id`, `chunk_id`, revision/source/section, type, content SHA, asset
path/pages), and evidence groups with frozen `postgresql_found` and alternatives.
The client independently checks final public content/citations/asset identity and
retention of each PG-found group. Shared misses remain visible relative-quality
gaps; no human evidence is generated by the runner.

Actual telemetry JSONL entries bind generated event `id`, exact `campaign_id`,
`mode:"actual"`, `source_hash`,
and observed `lexical_ms`, `cold_first_ms`, `capacity_wait_ms`,
`wait_to_discovery_ms`, `lexical_executions`, path/content call counts, bypass and
indexing-operation timing and actual `native_bulk_bytes`. HTTP `X-Request-ID` is
`CAMPAIGN_ID:EVENT_ID`; Celery delivery carries campaign/event headers and a
deterministic campaign/event task ID. Duplicate correlated telemetry is rejected;
another campaign's rows cannot satisfy the measurement. Telemetry cannot overwrite
a failed HTTP/quality outcome. Only verified `archived`/`superseded` domain reasons
are excluded, with their event/job identities preserved in catch-up reports.
Record actual server stage origins and correlate local monotonic intervals through
IDs; do not subtract unrelated clocks or add separate P95 values.

Later telemetry exports can be joined without rerunning load. Offline reevaluation
requires the original report's exact manifest/source/schedule identities, writes a
new directory, and records the raw report and telemetry SHA values:

```bash
uv run --offline --frozen python scripts/probes/run-opensearch-workload.py \
  --manifest /private/knowhere-mixed/workload-v1.json \
  --evaluate-report /private/knowhere-mixed/campaign-v1/report.json \
  --telemetry /private/knowhere-mixed/actual-stage-export.jsonl \
  --output /private/knowhere-mixed/campaign-v1-evaluated
```

## Reports and component simulation

Every scheduled arrival is logged as offered before bounded execution admission.
Full queues produce explicit failures and do not reduce later offered traffic.
Reports preserve offered/started/completed/native/channel/bypass/retry denominators,
failed/time-out/cancelled work, frozen burst catch-up cohorts and reasoned verified
archival/supersession exclusions. Failing jobs do not count as drained backlog.
Warm/cold/wait P95 gates use actual lexical samples and require the frozen minima;
empty cohorts are unqualified. Five-second capacity waits and the ten-second
indexing operation bound retain failures in normal-load denominators.

`--simulate` is available only for manifests explicitly labeled `automated_test`.
It accelerates the local producer/executor exercise, writes private schedule/event/
report artifacts and always reports `actual_staging_campaign:false` and
`qualified:false`. It supplies no human relevance, target timing or resource result.

```bash
uv run --offline --frozen python -m pytest -q \
  scripts/probes/tests/test_opensearch_workload.py \
  scripts/probes/tests/test_opensearch_workload_command.py \
  scripts/probes/tests/test_opensearch_workload_execution.py
```

Original read timing alone, unreviewed write arrivals, absent cohort minima,
unverified update admission/dedup, missing canonical evidence, incomplete stage/
resource measurements, missing overlapping full backup, cancelled campaigns or
actual target failures prevent staging qualification. The development commands and
component tests do not waive any of these gates.
