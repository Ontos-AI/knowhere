# CONTEXT

## Purpose

Knowhere API turns authenticated requests into document ingestion, document
lifecycle, retrieval, billing, and webhook workflows.

Within this repository, `apps/api` is the coordination layer between HTTP
adapters and the shared implementations in `packages/shared-python/shared`.

## Core Terms

### User

The authenticated owner of jobs, documents, credits, API keys, and webhooks.

### Namespace

The isolation scope for retrieval-visible data. The default namespace is
`default`.

### Job

The API-side intake and execution handle for a workflow such as file parsing,
URL ingestion, or demo source materialization.

### Job Result

The terminal artifact record attached to a Job. It stores delivery metadata,
result bundle references, and the revision that publication uses.

### Document

The retrieval-visible knowledge object produced from a Job Result after
publication.

### Document Section

The hierarchical navigation node derived from parsed headings and section paths.

### Document Chunk

The retrieval-visible text, image, or table row attached to a Document Section.

### Document Ingestion

The workflow that creates a Job, accepts a file or URL source, confirms upload
state, and starts parsing work.

### Job Admission

The policy checks that must pass before a new Job is created: authentication,
guest scope, system limits, billing RPM, concurrent job limits, and daily
quota.

### Publication

The shared workflow that turns parsed chunks into Documents, Document Sections,
Document Chunks, and document graph state.

### Retrieval

The query workflow that returns cited evidence from published documents.

### Demo Source

An API-owned canonical document shipped with the repository for demo and guest
flows.

### Demo Source Materialization

The workflow that copies a Demo Source into a user's Namespace as normal Job,
Job Result, Document, and Document Chunk records.

### Billing Workflow

The credits purchase, checkout, webhook handling, refund reconciliation, and
tier refresh flows.

### Guest API Key

A guest-tier API key with a restricted route surface.

### Webhook Management

The user-facing workflow for storing outbound webhook configuration and reading
delivery logs.

### QStash Callback

The verified async callback used to continue background work after external
delivery.

## apps/api Module Map

### HTTP Adapters

`apps/api/app/api/v1/routes/*`

These modules translate HTTP requests into application workflow calls.

### Application Workflows

`apps/api/app/services/*`

These modules coordinate Job Admission, Document Ingestion, document lifecycle,
Billing Workflow, Demo Source Materialization, webhook handling, and internal
callbacks.

### Persistence Adapters

`apps/api/app/repositories/*`

These modules own database reads and writes for API-side workflows.

### Shared Implementations

`packages/shared-python/shared/*`

These modules own the lower-level implementations for publication, retrieval,
state machines, storage, Redis-backed metadata, billing primitives, and core
exceptions.

## apps/api Workflow Ownership

### Document Ingestion

- `app/api/v1/routes/jobs.py`
- `app/services/job_creation_service.py`
- `app/services/job_upload_confirmation_service.py`
- `app/services/job_document_scope_service.py`
- `app/repositories/job_repository.py`

### Job Admission

- `app/services/rate_limit/*`
- `app/core/dependencies.py`

### Document Lifecycle

- `app/api/v1/routes/documents.py`
- `app/services/document_service.py`
- `app/repositories/document_repository.py`

### Retrieval

- `app/api/v1/routes/retrieval.py`
- shared retrieval modules in `packages/shared-python/shared/services/retrieval/*`

### Demo Source Materialization

- `app/api/v1/routes/demo.py`
- `app/services/demo_document_service.py`

### Billing Workflow

- `app/api/v1/routes/billing.py`
- `app/services/billing/*`
- `app/repositories/payment_record_repository.py`
- shared billing modules in `packages/shared-python/shared/services/billing/*`

### Webhook Management

- `app/api/v1/routes/webhook.py`
- `app/api/v1/routes/webhook_secrets.py`
- `app/services/webhook_service.py`
- `app/repositories/webhook_repository.py`

### Internal Storage Events

- `app/api/v1/routes/s3_events.py`
- `app/services/s3_events/*`

### Async Callbacks

- `app/api/v1/routes/qstash_callbacks.py`
- `app/services/qstash_callback_service.py`

## Invariants

- `apps/api` coordinates workflows. Parsing, publication, retrieval internals,
  storage mechanics, and state-machine implementation mostly live outside the
  route modules.
- A Job and a Document are not the same thing. Jobs track intake and processing;
  Documents track retrieval-visible knowledge state.
- `current_job_result_id` selects the active revision of a Document.
- Namespace is part of the retrieval contract, not a UI-only label.
- Demo Sources should behave like normal Documents after materialization.
- Billing Workflow and Job Admission shape whether work is allowed to start;
  they are not worker-only concerns.
