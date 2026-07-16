# 0004 Anonymous Self-Hosted Telemetry

## Status

Accepted

## Context

Self-hosted Knowhere installs already emit anonymous PostHog events so Ontos
operators can understand OSS adoption. The v1 emitter covered instance
lifecycle and coarse aggregates, but lacked:

- a locked privacy and allowlist contract
- real periodic health heartbeats
- SaaS `/usage`-parity KPIs (success rate, p95 duration, source mix)
- document-type and client-type mix without leaking filenames or free-form metadata

Operators need a stable schema and metric catalog before the metrics dashboard
and official clients depend on new events.

## Decision

### Purpose and opt-out

Anonymous self-hosted telemetry exists for Ontos operators measuring OSS /
self-hosted adoption. It is **default-on**. Operators opt out with
`TELEMETRY_ENABLED=false`. Transport remains PostHog; Logfire/OTEL are out of
scope.

### Privacy bounds

Events must never include filenames, prompts, emails, IPs, geo, document
content, or arbitrary customer metadata keys. Only allowlisted scalar property
names may leave the box. Free-form `client_version` is not emitted in aggregate
events (cardinality); app version stays on base properties only.

### Schema version

`schema_version = 2026-07-telemetry-v2`

### Event catalog

Event names use the `oss_` prefix (not `self_hosted_`). Catalog:

| Event | Role |
| --- | --- |
| `oss_instance_started` | Install boot |
| `oss_instance_heartbeat` | Periodic liveness + health |
| `oss_instance_shutdown` | Graceful stop (includes base props) |
| `oss_usage_aggregate` | Fleet usage snapshot (24h window) |
| `oss_retrieval_aggregate` | Retrieval activity |
| `oss_worker_aggregate` | Worker backlog / completion |
| `oss_api_aggregate` | In-process API request counters |
| `oss_provider_aggregate` | Parse/retrieval provider activity |
| `oss_document_type_aggregate` | Per allowlisted document type |
| `oss_client_aggregate` | Per allowlisted created_by_client |

### Allowlists

- `document_type`: `pdf|docx|doc|xlsx|xls|pptx|ppt|csv|txt|md|html|image|other`
  - Extension from `job_metadata->>'source_file_name'` only; map
    `png|jpg|jpeg|gif|webp|tiff` → `image`; everything else → `other`
- `created_by_client`: `cli|node-sdk|notebook|mcp|api|other`
  - No `dashboard` client: the web dashboard does not create parse jobs.
  - From `job_metadata #>> '{document_metadata,created_by_client}'`
- `source_type`: `file|url|other`

### Success rate

`success_rate_24h = done / (done + failed)` over the same 24h window, as a
float in **0–1**. Exclude non-terminal statuses. When `(done + failed) = 0`,
emit `0.0`.

### Metric IDs (dashboard catalog)

| Metric ID | Source |
| --- | --- |
| `oss.active_installs_7d` / `oss.active_installs_30d` | heartbeat distinct installs |
| `oss.new_installs_*` | started |
| `oss.retention.w0_w1` / `oss.retention.w0_w4` | started ∩ heartbeat |
| `oss.usage.jobs_created_24h` | usage aggregate |
| `oss.usage.jobs_done_24h` / `oss.usage.jobs_failed_24h` | usage / worker |
| `oss.usage.success_rate_24h` | usage aggregate |
| `oss.usage.pages_processed_24h` | usage aggregate |
| `oss.usage.job_duration_avg_seconds_24h` | worker aggregate |
| `oss.usage.job_duration_p95_seconds_24h` | usage aggregate |
| `oss.usage.backlog_*` | worker pending/running/… |
| `oss.usage.source_*` | `source_{file,url,other}_jobs_24h` |
| `oss.client_*` | client aggregate |
| `oss.document_type_*` | document type aggregate |
| `oss.health.*` | heartbeat health fields |
| `oss.fleet.version_*` / `oss.fleet.flags_*` | base props last-known |

### Capability flags and buckets

Usage aggregate may emit:

- `has_webhooks_24h`, `has_retrieval_24h` (bool)
- `jobs_created_bucket`, `pages_processed_bucket` as
  `0|1-10|11-100|100+`

## Consequences

- Emitter code must bump `schema_version`, extend property allowlists, emit
  real heartbeats, and add document-type / client aggregates before the
  metrics-dashboard Telemetry UI can rely on them.
- Official clients should populate `document_metadata.created_by_client`
  (separate decision surface); until then dashboards must tolerate
  `other`-heavy client mix.
- Changing allowlists or the success-rate formula requires a new ADR or an
  explicit schema_version bump.
