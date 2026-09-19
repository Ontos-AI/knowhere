# Document-centric list and history

**Status:** Draft — RFC only, no implementation in this change
**Related PRs:** [#232](https://github.com/Ontos-AI/knowhere/pull/232) (@gdccyuen), [#231](https://github.com/Ontos-AI/knowhere/pull/231) (@SusannaShu)
**Maintainer review:** [suguanYang on #232](https://github.com/Ontos-AI/knowhere/pull/232#issuecomment-5265960465), [suguanYang on #231](https://github.com/Ontos-AI/knowhere/pull/231#issuecomment-5265855328)
**Observed against:** `main` @ `9489fa2c` (2026-09-14)

This is a unifying API-ownership draft. It does not replace or rewrite
#232 / #231, and it does not include a code patch. Those PRs stay with
their authors.

## Why this is an RFC

Both PRs were blocked as **public API / resource-ownership** questions, not
as local bugs:

- #232 `GET /documents/namespaces` does not list a Knowhere resource.
  Namespace is a client-owned isolation label with no identity, create
  operation, metadata, authz, or lifecycle.
- #231 `GET /jobs?namespace=` and job deletion treat a processing attempt as
  a file registry. A job is the operational ledger and should remain after
  the document is archived.

Those are domain-contract changes. They should not land as drive-by
route additions.

## Resource ownership (from maintainer, restated)

| Concept | Owns | Does not own |
| --- | --- | --- |
| Namespace | Client (tenant / folder / workspace id) | Knowhere registry, empty folders, archived-only labels |
| Document | Persisted corpus: list, get, archive, retrieval scope | Processing attempts |
| Job | One parse attempt: status, billing, errors, provenance | Tenant file listing, delete-from-folder |

## Existing surface (do not duplicate)

On current `main`:

- `GET /v1/documents?namespace=` already lists the credential's documents
  in that namespace (`apps/api/app/api/v1/routes/documents.py`).
- `GET /v1/documents/{document_id}` and chunk listing already exist.
- Removal from the active corpus is `POST /v1/documents/{document_id}/archive`
  (plus a legacy `:archive` alias). There is no `DELETE /documents/{id}`.
- `GET /v1/jobs` lists processing attempts with status/type/time filters.
  It has **no** `namespace` query.
- `GET /v1/jobs/{job_id}` inspects one attempt.

The keyboard-backend / on-prem folder workflow that #231 and #232 were
solving is therefore **mostly already on the document resource**:

1. List a tenant folder: `GET /documents?namespace={label}`
2. Remove a file from that folder: `POST /documents/{document_id}/archive`
3. Poll a known upload: `GET /jobs/{job_id}` (client keeps the id while
   pending)

## Remaining gaps (only these need new API)

### 1. Processing history under a document

Suggested, not implemented:

```http
GET /v1/documents/{document_id}/jobs
```

or, if the product name is "revisions":

```http
GET /v1/documents/{document_id}/revisions
```

Returns the job ledger rows that produced this document (including failed
retries), newest first. Jobs stay immutable. This is not job deletion and
not a namespace filter on `GET /jobs`.

### 2. Pending uploads with no document yet

Clients should keep the job id returned at create time. If a self-hosted
app must reconcile abandoned uploads without that id, design that around
the existing external `data_id`, as a separate RFC. Do not add
`GET /jobs?namespace=` as a file browser.

### 3. "What folders exist?" for an uploader ≠ consumer

#232's on-prem case (gdccyuen, 2026-08-13): one credential, many domain
folders, the reader did not choose the namespace strings.

Do **not** answer that with a Namespace resource.

Options, in order of preference:

1. **Derive from documents.** `GET /documents` (paged) already returns
   each document's namespace. The client unique-sorts labels. Empty
   folders do not exist in Knowhere; that matches "namespace appears only
   after a document is created."
2. If pagination makes (1) too expensive, an **explicitly administrative
   document-aggregation** query (for example distinct `documents.namespace`
   for non-archived rows under this credential) may be considered later. It
   must not be named or documented as `GET /namespaces`, must not imply
   empty namespaces, and must not be mounted as a first-class resource.

Archived-only labels stay out of that aggregation unless a later admin
tool asks for them separately.

## What not to merge from #232 / #231

- `GET /documents/namespaces` (or `/api/v2/documents/namespaces` via the
  v1-mounted-under-v2 router) as a Namespace resource
- `GET /jobs?namespace=` as a document-listing interface
- `DELETE /jobs/{job_id}` / soft-delete of the processing ledger
- Counting "active" documents as `status != archived` while calling them
  "active document counts" without defining `status`

#231's discussion already includes agreement to drop the job-listing /
job-deletion approach. This RFC records that direction so a future
document-history PR does not revive those routes.

## Testing expectations for a later implementation PR

- Contract tests for `GET /documents?namespace=` remain the listing
  interface (already present in `test_documents_contract.py`).
- Any new `/documents/{id}/jobs` (or `/revisions`) test must show: archived
  document still returns job history; deleting/archiving the document does
  not hide jobs.
- No test should encode `GET /jobs?namespace=` as the supported client
  workflow.

## Ask

@suguanYang: confirm (1) listing stays on `GET /documents?namespace=`,
(2) history is document-scoped jobs/revisions, (3) no Namespace resource
without a separate domain-contract change.

@gdccyuen @SusannaShu: this is not a competing implementation of #232/#231.
If you want to continue, the document-history route is the remaining
gap; namespace discovery should be derived from documents or an explicit
admin aggregation, not a namespace resource.
