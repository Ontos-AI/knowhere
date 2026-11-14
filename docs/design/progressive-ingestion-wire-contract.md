# Progressive ingestion wire contract

This additive contract is shared by the API, Node SDK and Hipo implementation.
Existing full ingestion fields and behavior remain valid.

New progressive revisions retain the baseline retrieval content until all required
enhancement work and the final archive are durable. Scope outputs are private
intermediate artifacts. One final database transaction publishes the complete
content/index and admits the final snapshot and event. This does not add a public
parameter or change the existing BM25/visual fusion or Agent retrieval routes.
The internal publication protocol marker allows already-running revisions to
finish using their original scope-publication protocol.

`GET /v2/jobs/{job_id}` adds `enhancement`, `snapshots` and
`current_snapshot_id`. These fields are absent for legacy/full results.
It also exposes the known `page_count` so status consumers do not download a
result archive to recover a count already persisted by ingestion.

Both full and progressive job reads expose a top-level `source_size_bytes`
integer once ingestion records the source size. It counts the bytes in the
directly uploaded file or the file downloaded from the source URL before
normalization. Subsequent reads return the stored count. The field is omitted
when no count was recorded, including older jobs that predate source measurement.

```json
{
  "source_size_bytes": 7654,
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

## Verified source uploads

`POST /v2/jobs` accepts optional `source_integrity` for `source_type: "file"`.
Pass `size_bytes` as a nonnegative integer up to the configured upload limit
and `sha256` as the 64 lowercase hexadecimal digits of the original file's
SHA-256 digest. Existing unguarded file uploads and URL imports keep their
current contracts.

Persist the returned `job_id` before sending bytes. Use a single PUT to the
returned `upload_url`, with all returned `upload_headers`. The S3 Signature
Version 4 URL signs `Content-Type` determined from `file_name`, `Content-Length`
as a decimal byte count, `If-None-Match: *`, and `x-amz-checksum-sha256` as
standard base64 of the raw 32-byte digest.

Send the Knowhere API bearer only to Knowhere. If a PUT result is unknown or
S3 returns a conditional `409` or `412`, check the saved job and confirm it
before retrying the PUT. `POST /v2/jobs/{job_id}/upload-url` renews the same
guarded key for its owner while the job is `waiting-file` and within its upload
lifetime. `expires_in` can be shorter than that lifetime because the signature
is also limited by the storage credential's expiry.

`POST /v2/jobs/{job_id}/confirm-upload` takes an empty body or `{}`. Manual
confirmation, storage notifications, and expiry recovery verify length and
SHA-256 and use the S3 object's `LastModified` to check the upload deadline.
A verified object stored by the deadline can start the original job after the
deadline. The worker verifies the downloaded original again before parsing.

An object missing before the deadline returns `400 INVALID_ARGUMENT` with a
`file` violation. An in-window object with a mismatched length, SHA-256, or
missing SHA-256 returns `400 INVALID_ARGUMENT` with a `source_integrity`
violation and marks the job `failed` with `UPLOAD_INTEGRITY_MISMATCH`. An
object stored after the deadline, or confirmed absent after it, returns
`409 ABORTED` and marks the waiting job `failed` with `UPLOAD_EXPIRED`. A
replay after the job advances can return `200` even for a failed job, so poll
the saved job for the outcome.

When `JOB_WAITING_EXPIRE_SECONDS` is zero or negative, verified uploads use
the default two-hour upload lifetime. Once confirmed, a guarded upload has
at least two minutes for parse dispatch and worker admission.

This mode requires S3 with conditional PUT and checksum-enabled HEAD. The
API and worker roles need access to read object checksums and identify an
absent key.
For S3, a missing object returns `404` when the caller also has `s3:ListBucket`.
KMS-encrypted buckets also need the permissions required for checksum retrieval.
Temporary storage verification failures return retryable `503` responses to
manual confirmation and leave the waiting job eligible for recovery.
[Amazon S3 PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)
and [Amazon S3 HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html)
describe these storage requirements.

Use the saved job ID as the authority for retry. A lost create response can
leave a waiting job until expiry. An explicit existing document can return an
active-ingestion `409 ABORTED`. Its `error.details` holds `reason`,
`resource: "Document"`, the document `id`, and `active_job_id` when the API
found the caller's active job:

```json
{"reason": "ABORTED", "resource": "Document", "id": "doc_123", "active_job_id": "job_456"}
```

A client that lost the original create response can continue with
`active_job_id`. A conflict caught only by the database unique index, when two
creates race, omits `active_job_id`. Retrying the create reports it while that
job stays active.
