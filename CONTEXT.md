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

### Job Transition Outcome

The typed result of a Job state-machine transition. It preserves whether the
transition succeeded, the target state, previous state when known, attempt
count, and rejection reason while keeping older boolean facades available.

### Job Post-Commit Effect

A post-transaction side effect planned during terminal Job finalization and run
only after the database commit succeeds. Current effects include retrieval cache
invalidation and outbound webhook publication.

### Job Read

The workflow that lists a User's Jobs and projects one Job into the public Job
Result response shape.

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

### Worker Document Parsing

The worker-side workflow that turns a source file into parsed DataFrame rows,
parsed assets, and parser debug artifacts before chunk conversion and result
packaging.

### Parse Output

The stable parser adapter result with an output directory and optional parsed
DataFrame. It owns legacy tuple compatibility for older parser callers.

### Parse Artifact

The ingestion-side parsed content artifact. It validates parser output before
chunk conversion and result packaging.

### Generated Result Package

The generated ZIP bundle metadata used by terminal Job finalization, including
ZIP path, checksum, statistics, and byte size.

### Workload Estimate

The worker-side estimate used for billing and processing metadata. It records
page count, estimation method, and any fallback reason.

### Parser Input

The typed worker-side parse request assembled from Job metadata, parser options,
source-file identity, output naming, and storage transform keys.

### Document Format Routing

The Worker Document Parsing module that selects one concrete parser adapter for
the source document format while keeping format-specific conversion details out
of the stable parser entrypoint.

### Rendered PDF Transform

The Worker Document Parsing module that reuses or creates rendered PDF artifacts
for PDF-backed parsing paths, including PPTX-to-PDF fallback handling, image-only
PDF rendering, temporary PDF materialization, MinerU handoff, and cleanup.

### Heading Hierarchy

The Worker Document Parsing module that predicts section levels from Markdown
lines, DOCX blocks, TOC context, layout metadata, heuristics, and optional LLM
inference.

### Job Admission

The policy checks that must pass before a new Job is created: authentication,
guest scope, system limits, billing RPM, concurrent job limits, and daily
quota.

### Job Admission Route Policy

The route-aware part of Job Admission that enforces guest API key scope and
system limits from plain route-admission context built by HTTP dependency
adapters.

### Job Admission Capacity

The quota-aware part of Job Admission that enforces billing RPM, concurrent
jobs, and daily quota.

### Publication

The shared workflow that turns parsed chunks into Documents, Document Sections,
Document Chunks, and document graph state.

### Retrieval

The query workflow that returns cited evidence from published documents.

### Retrieval Query

The typed retrieval request that owns cache-shaping fields and route policy:
scope, filters, data type, channels, ranking options, and agentic toggle.

### Workflow Run Request

The agentic Retrieval request passed through planning and step execution. It
preserves user scope, filters, channel policy, internal recall, and explicit
ranking policy fields for the workflow path.

### Workflow Step Request

The per-step projection of a Workflow Run Request. It applies step-level query,
top-k, and data-type overrides while preserving the request policy.

### Demo Source

An API-owned canonical document shipped with the repository for demo and guest
flows.

### Demo Source Materialization

The workflow that copies a Demo Source into a user's Namespace as normal Job,
Job Result, Document, and Document Chunk records.

### Billing Workflow

The credits purchase, checkout, webhook handling, refund reconciliation, and
tier refresh flows.

### API Key Authentication

The auth-time workflow that validates API keys, reads and writes the API-key
cache, and schedules best-effort last-used updates.

### API Key Management

The user-facing workflow that creates, lists, reads, revokes, and toggles API
keys.

### Stripe Purchase

The Billing Workflow adapter that creates Stripe payment intents and checkout
sessions for credits purchases.

### Stripe Credits Settlement

The Billing Workflow adapter that settles successful Stripe checkout and
payment-intent events into credits, payment records, and tier refreshes.

### Stripe Webhook Reconciliation

The Billing Workflow adapter that verifies Stripe events and reconciles credits,
payment records, and refunds.

### Guest API Key

A guest-tier API key with a restricted route surface.

### Webhook Management

The user-facing workflow for storing outbound webhook configuration and reading
delivery logs.

### QStash Callback

The verified async callback used to continue background work after external
delivery.

### Public URL Policy

The shared URL safety workflow used before Knowhere reaches user-provided or
third-party HTTP targets. It validates public HTTP/HTTPS URLs, pins resolved
addresses for outbound requests, blocks unsafe redirects, and detects URL file
types for Document Ingestion.

### Redis State

The shared Redis-backed runtime state used by background work, rate limits,
state-machine progress, distributed locks, and job metadata. It owns Redis key
language and Redis retry policy.

### Quota Token Pool

The shared Redis-backed token leasing workflow used by provider-specific quota
managers such as Ali, iLoveAPI, and MinerU.

## apps/api Module Map

### HTTP Adapters

`apps/api/app/api/v1/routes/*`
`apps/api/app/api/dependencies/*`

These modules translate HTTP requests and dependency context into application
workflow calls.

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
exceptions. Shared Job lifecycle finalization lives under
`packages/shared-python/shared/services/jobs/lifecycle/*`.

## apps/api Workflow Ownership

### Document Ingestion

- `app/api/v1/routes/jobs.py`
- `app/services/document_ingestion/service.py`
- `app/services/document_ingestion/creation_service.py`
- `app/services/document_ingestion/confirmation_service.py`
- `app/services/document_ingestion/scope_service.py`
- `app/repositories/job_repository.py`

### Job Read

- `app/api/v1/routes/jobs.py`
- `app/services/jobs/read_service.py`
- `app/services/jobs/result_projection.py`
- `app/repositories/job_repository.py`

### Job Admission

- `app/api/dependencies/auth.py`
- `app/api/dependencies/job_admission.py`
- `app/services/auth/*`
- `app/services/rate_limit/*`

### Document Lifecycle

- `app/api/v1/routes/documents.py`
- `app/services/documents/lifecycle_service.py`
- `app/repositories/document_repository.py`

### Retrieval

- `app/api/v1/routes/retrieval.py`
- shared retrieval modules in `packages/shared-python/shared/services/retrieval/*`

### Demo Source Materialization

- `app/api/v1/routes/demo.py`
- `app/services/demo/*`

### Billing Workflow

- `app/api/v1/routes/billing.py`
- `app/services/billing/*`
- `app/repositories/payment_record_repository.py`
- shared billing modules in `packages/shared-python/shared/services/billing/*`

### API Key Management

- `app/api/v1/routes/api_key.py`
- `app/services/auth/*`
- `app/repositories/api_key_repository.py`

### Webhook Management

- `app/api/v1/routes/webhook.py`
- `app/api/v1/routes/webhook_secrets.py`
- `app/services/webhook/*`
- `app/repositories/webhook_repository.py`

### Internal Storage Events

- `app/api/v1/routes/s3_events.py`
- `app/services/s3_events/*`

### Storage Event Intake

The internal workflow that decodes S3-compatible storage events, sanitizes
headers, acknowledges malformed or unsafe events, and triggers upload handoff.

### Async Callbacks

- `app/api/v1/routes/qstash_callbacks.py`
- `app/services/webhook/qstash_callback_service.py`

The route owns QStash HTTP signature verification and HTTP response projection.
The workflow owns callback parsing, event status resolution, and webhook log
side effects.

## Shared Workflow Ownership

### Job Lifecycle Finalization

- `shared/services/jobs/lifecycle/service.py`
- `shared/services/jobs/lifecycle/success_finalizer.py`
- `shared/services/jobs/lifecycle/failure_finalizer.py`
- `shared/services/jobs/lifecycle/result_writer.py`
- `shared/services/jobs/lifecycle/publication.py`
- `shared/services/jobs/lifecycle/post_commit_effects.py`
- `shared/services/jobs/lifecycle/webhook_outbox.py`

## apps/worker Workflow Ownership

### Worker Document Parsing

- `app/services/document_parser/parse_service.py`
- `app/services/document_parser/orchestration/parse_input.py`
- `app/services/document_parser/orchestration/parse_session.py`
- `app/services/document_parser/orchestration/route_parse.py`
- `app/services/document_parser/orchestration/format_router.py`
- `app/services/document_parser/orchestration/format_adapters.py`

### Rendered PDF Transform

- `app/services/document_parser/rendered_pdf_transform.py`
- `app/services/document_parser/pptx_pdf_rendering.py`
- `app/services/document_parser/pdf_parser.py`
- `app/services/document_parser/pptx_parser.py`

### Heading Hierarchy

- `app/services/document_parser/heading_hierarchy.py`
- `app/services/document_parser/layout_parser.py`
- `app/services/document_parser/md_parser.py`
- `app/services/document_parser/doc_parser.py`

## Invariants

- `apps/api` coordinates workflows. Parsing, publication, retrieval internals,
  storage mechanics, and state-machine implementation mostly live outside the
  route modules.
- Worker Document Parsing exposes `checkerboard_inject_parse` as the stable
  legacy parser entrypoint; parser option shaping, format routing, rendered PDF
  transforms, typed Parse Output, and heading inference stay behind that
  entrypoint.
- A Job and a Document are not the same thing. Jobs track intake and processing;
  Documents track retrieval-visible knowledge state.
- Terminal Job finalization should plan post-commit effects with primitive
  identifiers and run them after the database transaction commits.
- State-machine callers that need diagnostics should consume Job Transition
  Outcome; boolean state-machine methods remain compatibility facades.
- `current_job_result_id` selects the active revision of a Document.
- Namespace is part of the retrieval contract, not a UI-only label.
- Demo Sources should behave like normal Documents after materialization.
- Billing Workflow and Job Admission shape whether work is allowed to start;
  they are not worker-only concerns.
