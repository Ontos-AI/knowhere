# Document-centric list and history

**Status:** Draft implementation — API decisions below require maintainer review
**Related PRs:** [#232](https://github.com/Ontos-AI/knowhere/pull/232), [#231](https://github.com/Ontos-AI/knowhere/pull/231)
**Maintainer direction:** [namespace ownership](https://github.com/Ontos-AI/knowhere/pull/232#issuecomment-5265960465), [processing ledger](https://github.com/Ontos-AI/knowhere/pull/231#issuecomment-5265855328)
**Implementation base:** `main` @ `f9d61119` (2026-10-07)

Namespaces remain free-form client-owned isolation labels. Documents own the
persisted corpus; jobs preserve processing attempts, billing and provenance.
This implementation adds document-scoped history and explicit cross-namespace
document listing. It does not change #231/#232 or their authors' branches.

## List documents

Existing calls keep their behavior on both `/api/v1` and `/api/v2`:

```http
GET /api/v1/documents?namespace=tenant-42&page=1&page_size=50
```

Omitting `namespace`, passing an empty value, or passing whitespace selects
`default`. Listing returns non-archived documents for that namespace. It does
not enumerate namespaces or represent empty folders.

A reader whose client does not retain labels may now explicitly page through
all of the credential owner's private documents:

```http
GET /api/v1/documents?all_namespaces=true&page=1&page_size=50
```

The existing response envelope remains `namespace`, `documents`, `pagination`.
For `all_namespaces=true`, the envelope's `namespace` is `null`; each document
retains its actual namespace. Documents are ordered by `updated_at` descending,
then `document_id` ascending. `namespace` and `all_namespaces=true` are mutually
exclusive (400 `INVALID_ARGUMENT`), including an explicitly empty namespace value. Page size is
1–200, default 50; page numbers start at 1.

The client may derive labels from the returned documents. Archived-only and
empty labels have no entries. This reads private user-owned corpus rows;
shared demo documents remain available through the existing explicit
`namespace=__knowhere_demo__` query and are not mixed into this traversal.

## Read document processing history

```http
GET /api/v1/documents/{document_id}/jobs?page=1&page_size=50
GET /api/v2/documents/{document_id}/jobs?page=1&page_size=50
```

The document must exist and belong to the credential owner. An archived
private document remains readable. Missing documents, another user's
documents, and initial uploads that have not materialized a document return
404. An existing document with no associated jobs returns an empty page.
Shared demo documents return an empty history; their internal publisher's
processing and billing ledger is not exposed to corpus readers.

Each item summarizes one attempt; a failed or running attempt need not have a
published revision:

```json
{
  "document_id": "doc_example",
  "namespace": "tenant-42",
  "jobs": [
    {
      "job_id": "job_retry",
      "job_type": "document_ingestion",
      "status": "failed",
      "source_type": "file",
      "error_code": "INVALID_ARGUMENT",
      "page_count": null,
      "credits_charged": 0,
      "billing_status": "pending",
      "created_at": "2026-10-07T10:00:00",
      "updated_at": "2026-10-07T10:01:00",
      "job_result_id": null,
      "is_current_revision": false
    }
  ],
  "pagination": {"page": 1, "page_size": 50, "total": 1, "total_pages": 1}
}
```

Jobs are ordered by `created_at` descending, then `job_id` ascending so equal
timestamps paginate consistently. Pagination has the same bounds as document
listing. Counts and pages are separate reads, so concurrent submissions may
change totals between requests; this is offset pagination, not a frozen
snapshot.

Association rules:

1. `job_results.document_id` is the canonical persisted link and takes priority.
2. When both the private and shared-demo canonical result links are absent
   (including no result),
   `jobs.job_metadata.document_id` associates failed, pending and legacy attempts.
   A result published to a demo document cannot enter private history through
   conflicting metadata.
3. The job must independently belong to the requesting user. A namespace match,
   filename or external `data_id` alone does not associate a job.

Every associated result is represented once. `job_result_id` identifies the
attempt's result when present; `is_current_revision` compares it with the
current document pointer. Full job metadata, source URLs, result payloads,
asset URLs and raw error messages are excluded from the summary. Use the
existing `GET /jobs/{job_id}` to inspect a known attempt in detail. A returned
published `job_result_id` can select an older revision through the existing
`GET /documents/{document_id}/chunks?job_result_id=...`, including after private
archive. Private document detail continues to describe the current document.

Archiving removes the document from listing/retrieval and leaves its job ledger
and document-scoped history readable. This endpoint has no deletion or hiding
operation and performs no ledger mutation.

## Pending submissions and namespace ownership

Clients retain the job ID returned at upload and poll `GET /jobs/{job_id}`.
History is only available once the document exists. Durable reconciliation of
abandoned initial uploads, potentially using external `data_id`, remains a
separate design problem.

No namespace registry, `GET /documents/namespaces`, namespace-filtered job
listing or job deletion is introduced. No schema migration or environment
configuration is required. `all_namespaces`, history association precedence,
summary fields and demo behavior are local Draft decisions for review.

## Verification

`apps/api/tests/contract/test_document_history_contract.py` exercises actual
HTTP routes with synthetic PostgreSQL data on v1 and v2: old/current revisions,
failed and running attempts, canonical-only and legacy links, conflicting
links, stable pagination, archive retention, job inspection, unknown/foreign
and unmaterialized documents, empty history, authentication, pagination bounds,
cross-namespace listing with unchanged default/blank behavior, and reading an
older revision's chunks from a history result ID before and after archive.
