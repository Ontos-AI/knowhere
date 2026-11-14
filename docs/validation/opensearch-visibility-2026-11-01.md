# OpenSearch acknowledged-visibility verification

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Date: 2026-11-01, America/Los_Angeles.

Result: the officially documented visibility mechanism passed six real-node
protocol cases. A deliberately unsafe negative control failed as intended.
These results support using visibility acknowledgment as a publication gate;
they do not accept an implemented Knowhere publication flow.

## Accepted requirement

Parsing completion must be independently observable and make parsing results
available to result-only clients. Terminal job success (`done` and
`job.completed`) must mean that the processed revision is ready for retrieval.
The owner accepts official visibility semantics; thirty seconds is a reference,
not a hard deadline or a required vendor latency guarantee.

The PostgreSQL lexical index remains a deliberate migration-rollback backup.
It must not become a normal runtime fallback.

## Official basis

- [Refresh Index API](https://docs.opensearch.org/latest/api-reference/index-apis/refresh/):
  OpenSearch recommends `refresh=wait_for` for indexing followed by immediate
  search. The indexing operation waits for visibility before returning.
- [Bulk API](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/):
  bulk operations have independent item outcomes, and `refresh=wait_for`
  waits for visibility. An HTTP 200 response alone does not prove completeness.
- [Index settings](https://docs.opensearch.org/latest/install-and-configure/configuring-opensearch/index-settings/):
  explicitly setting `index.refresh_interval=1s` maintains periodic refresh
  scheduling independently of search traffic. Leaving the setting implicit
  permits idle shards to stop background refreshes.

These guarantees establish a visibility acknowledgment, not a thirty-second
end-to-end publication SLA or a promise of uninterrupted future availability.

## Environment and scope

- Official image: `opensearchproject/opensearch:3.9.0`, ARM64.
- Image repository digest:
  `sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e`.
- Dedicated temporary Colima VM: 2 virtual CPUs, 4 GiB memory.
- Container: 2 CPU limit, 2 GiB memory limit, 1 GiB JVM heap.
- Single node; each probe index has one primary shard and zero replicas.
- Only `127.0.0.1:19289` was published. Security was disabled solely for this
  isolated local test node. No production configuration was changed.
- All test data was synthetic. No production documents, identifiers, or
  credentials were used.
- Both lexical fields used whitespace analysis and the bound custom BM25
  similarity with `k1=1.5`, `b=0.75`.

This is smaller than the proposed 32 GiB production node and is not a capacity
or production-latency benchmark. Existing Docker workloads remained in their
original VM and were not stopped or reconfigured.

## Results

| Case | Observation | Verdict |
| --- | --- | --- |
| Unsafe negative control | With automatic refresh disabled, 188 writes all acknowledged, but immediate search returned 0. The probe exited 1 with `Premature completion: acknowledged 188 writes but visible=0`. | Expected failure proves the check detects premature completion. |
| Pending visibility barrier | A real-time document GET confirmed ingestion while search still returned 0. `refresh=wait_for` had not returned. After an explicit controlled refresh, the pending request returned and immediate search found all 188 units. | Passed. |
| Scheduled refresh, immediate search, revision replay | Three rounds of 352 units using explicit `1s` refresh. Each acknowledgment was immediately followed by searches that found the entire latest revision in both fields. No search retries or manual refreshes. Reusing stable IDs kept the total at 352. Later rounds followed idle-threshold waits. | Passed. |
| Partial bulk failure | HTTP 200 contained one accepted item and one rejected item. Search found one unit; the probe's completeness guard rejected the two-unit revision. | Passed; HTTP status alone is insufficient. |
| Close, reopen, and replace | Closed search returned HTTP 400. Opening with `wait_for_active_shards=1` restored all 188 units. An acknowledged replacement exposed all 188 new-version units and zero old-version units. | Passed. |
| Token and mapping contract | Whitespace preserved repeated pre-tokenized words and case. GET mapping/settings confirmed both fields used the custom BM25 parameters. | Passed. |
| Namespace isolation | Two separate one-shard indexes contained one and two units respectively; each explicit-index query returned only its own count. | Passed. |

The three scheduled bulk-and-refresh HTTP requests took 995.139, 801.773,
and 819.316 milliseconds in this local run. These are observations only, not
percentiles, throughput claims, production estimates, or timing guarantees.

The disabled-refresh cases intentionally control the visibility boundary.
Manual refresh in that case is test instrumentation, not a recommendation
to force a refresh for every production batch.

## Reproduction and evidence

Probe: [verify-opensearch-visibility.py](../../scripts/probes/verify-opensearch-visibility.py).
It requires an isolated local OpenSearch 3.9.0 node and deletes only the indexes
it created. The following commands were run from the repository root:

```bash
uv run --no-project --python 3.12 python scripts/probes/verify-opensearch-visibility.py --skip-visibility-barrier --report /tmp/knowhere-opensearch-visibility-20261101/negative-control.json
# Expected exit: 1; acknowledged writes are not yet visible.

uv run --no-project --python 3.12 python scripts/probes/verify-opensearch-visibility.py --report /tmp/knowhere-opensearch-visibility-20261101/verified.json
# Actual exit: 0; six protocol cases passed.
```

Retained evidence:

- Negative-control result (`opensearch-visibility-2026-11-01/negative-control.json`).
- Six passing cases (`opensearch-visibility-2026-11-01/verified.json`).
- Environment and index cleanup (`opensearch-visibility-2026-11-01/environment.json`).

The script passed Ruff lint, Ruff formatting, Python compilation, and targeted
Pyright checking (zero errors and warnings; the repository's optional `.venv`
was absent).

## Remaining integration work

Source inspection confirmed that the current job finalizer publishes before
marking a job completed and emitting `job.completed`. Current job responses do
not expose an independent parsing-completion state or a per-revision search-
readiness field. The existing readiness signal is a short-lived namespace-
level Redis cache populated by retrieval.

No OpenSearch-backed Knowhere publication adapter or phase-state API was
implemented in this verification. Before end-to-end acceptance, implement and
test durable parsing-result availability, per-revision state and notifications,
all-item indexing success, visibility acknowledgment, revision activation and
cache coherence, and failure/retry behavior without PostgreSQL fallback.

No real-client chain, full-corpus ingestion, production concurrency, ranking
quality, recovery duration, or multi-node scaling was accepted here.

## Cleanup

Every probe index was deleted, and a post-run inspection found no remaining
`knowhere-visibility-*` indexes. The dedicated test container, its anonymous
volume, and the temporary Colima VM were then removed. The original Docker
context and existing workloads were preserved.
