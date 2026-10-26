# Progressive ingestion wire contract

This additive contract is shared by the API, Node SDK and Hipo implementation.
Existing full ingestion fields and behavior remain valid.

`GET /v2/jobs/{job_id}` adds `enhancement`, `snapshots` and
`current_snapshot_id`. These fields are absent for legacy/full results.
It also exposes the known `page_count` so status consumers do not download a
result archive to recover a count already persisted by ingestion.

```json
{
  "enhancement": {
    "status": "pending",
    "can_retry": false,
    "attempt": 1,
    "error": null
  },
  "current_snapshot_id": "snapshot_opaque",
  "snapshots": [{
    "snapshot_id": "snapshot_opaque",
    "stage": "baseline",
    "job_id": "job_1",
    "job_result_id": "result_1",
    "document_id": "doc_1",
    "checksum": "sha256-of-archive-bytes",
    "result_url": "https://signed-archive-url",
    "result_url_expires_at": "2026-10-26T20:00:00Z"
  }]
}
```

Enhancement status is `pending | running | complete | failed`. `attempt` counts
manual enhancement generations starting at one. `error` is an optional safe
display string. `can_retry` is true only for terminal retryable failure. There
are at most two snapshots: `baseline | final`. Every snapshot has its own ZIP
containing the coherent manifest and required assets. Its SHA-256 checksum and
snapshot ID never change. URL expiry can change without changing identity.

The SDK exposes normal camelCase projections. Its local storage revision key
for a snapshot is the opaque `snapshot_id`, distinct from the upstream
`job_result_id`. SDK document/chunk calls continue to use `job_result_id`.
Explicit retained snapshot reads stay pinned; ordinary reads follow
`current_snapshot_id` only after the selected snapshot commits locally.

`POST /v2/jobs/{job_id}/enrichment/retry` takes no body and returns HTTP 202:
`{"job_id":"job_1","enhancement":{...}}`. Duplicate requests during an
active generation return its current state. Terminal ineligible/superseded
results use the existing API conflict/validation conventions.

Both existing `job.completed` and new `job.enrichment.completed` webhook payloads
carry `job_id`, `job_result_id`, `document_id`, `snapshot_id`, `checksum`, and
`status: "completed"`, preserving the existing webhook success spelling. The
first identifies baseline; the second identifies final.
Consumers authenticate and read the job back rather than trusting download
capabilities in webhook payloads. Delivery can duplicate or arrive out of order.

Canonical persisted metadata is `JobResult.document_metadata.progressive`,
containing `enhancement`, `snapshots`, `current_snapshot_id`, and internal
stage/repair claims. Persisted snapshots store `archive_key` instead of a URL.
Workers upload immutable ZIP keys before committing descriptors. No second
JobResult or ingestion charge is created.
