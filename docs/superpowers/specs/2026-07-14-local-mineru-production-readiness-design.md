# Local MinerU Production Readiness Design

## Status

Approved by standing operator authorization on 2026-07-14. External Windows
Firewall attestation is intentionally deferred as backlog item BL-001 and is
not a release gate for this phase.

## Goal

Make the opt-in local MinerU PDF provider operationally safe for a limited
production canary without changing the cloud default, silently falling back,
or expanding production DOCX routing.

## Considered approaches

1. **Operational hardening and controlled canary (selected).** Add a cheap
   runtime preflight, a process-wide local capacity guard, safe domain-error
   mapping, structured provider observations, and a deployment runbook. This
   addresses the highest current risk: one batch run sampled 5.1 GB peak RSS,
   while independent worker jobs can currently enter local MinerU concurrently.
2. **Expand local production routing to DOCX.** This increases feature coverage
   but also introduces LibreOffice pagination and a second production artifact
   path before the PDF provider has canary evidence.
3. **Tune performance and raise concurrency first.** This may reduce latency,
   but without an admission guard or production observations it increases OOM
   and disk-pressure risk. Optimization should follow canary measurements.

## Scope

### Runtime preflight

Create a side-effect-free validation service that runs only when
`MINERU_PROVIDER=local`. It validates the MinerU checkout, configured `uv`
executable, MinerU virtual-environment Python, adapter entry point, positive
timeouts, writable temporary/output roots, and configured minimum free disk and
available memory. It returns a typed status containing booleans and numeric
capacity only; paths, environment values, model configuration, and subprocess
output are never serialized or emitted.

Worker startup runs the cheap preflight and fails fast when explicit local mode
is unusable. A standalone CLI runs the same checks for deployment probes. It
does not load models or make network requests; a real model-backed smoke remains
a separate canary action.

### Capacity guard

Wrap the local subprocess execution in a process-wide bounded semaphore.
`MINERU_LOCAL_MAX_CONCURRENT_JOBS` defaults to 1. Waiting is bounded by
`MINERU_LOCAL_ADMISSION_TIMEOUT_SECONDS`; timeout produces a typed capacity
failure without launching MinerU. The existing per-document shard concurrency
remains 1 by default. The two controls solve different problems and are both
retained.

The guard releases its lease on success, validation failure, child-process
failure, timeout, cancellation, and materialization failure. No queue content,
filename, or source path enters logs.

### Error boundary and observations

Local configuration, capacity, process, artifact-contract, and publication
failures are converted to `MinerUServiceException` at the provider boundary.
The internal message contains only a bounded error category and sanitized child
status; users receive the existing generic 5xx message. Local mode never calls
the cloud provider after any failure.

Each call emits one structured completion record with allowlisted fields:
`provider=local|cloud`, `backend`, `status=ok|error|rejected`, `elapsed_ms`, and
`error_category` when applicable. Do not add filenames, S3 keys, paths, logs,
document content, table values, environment values, or free-form exception
messages. This phase uses local structured logs and stage timings only; it does
not change the anonymous telemetry schema in ADR-0004.

### Canary rollout

Use a dedicated worker deployment with `MINERU_PROVIDER=local`, worker
concurrency 1, local job concurrency 1, and pre-downloaded models. Do not add
percentage routing to application code. Route only an operator-controlled
queue or deployment to the local worker, which keeps rollback to cloud a
configuration/deployment change.

Canary input is limited to approved non-confidential PDFs of 1–20 pages during
the first stage. Advancement requires zero unexpected failures, no partial
output or temporary directory leakage, no cloud calls, peak RSS below the
configured deployment limit, and p95 extraction duration below the configured
job timeout. Rollback requires draining the local queue and returning traffic
to a cloud-configured worker; never retry an already failed local job against
cloud automatically.

## Interfaces

- `check_local_mineru_runtime(settings, *, run_command, disk_usage,
  virtual_memory) -> LocalMinerURuntimeStatus`
- `require_local_mineru_runtime(settings) -> LocalMinerURuntimeStatus`
- `LocalMinerUCapacityGuard.acquire() -> ContextManager[None]`
- `class LocalMinerUCapacityError(RuntimeError)`
- `parse_pdf(...)` retains its current public signature and cloud-default
  behavior.
- `check_local_mineru_runtime.py` exits 0 only for a ready local runtime and
  prints one content-free JSON status object.

## Configuration

- `MINERU_LOCAL_PREFLIGHT_ON_STARTUP=true`
- `MINERU_LOCAL_PYTHON_EXECUTABLE=`; empty derives
  `<MINERU_LOCAL_PROJECT_PATH>/.venv/Scripts/python.exe` on Windows.
- `MINERU_LOCAL_MAX_CONCURRENT_JOBS=1`
- `MINERU_LOCAL_ADMISSION_TIMEOUT_SECONDS=30`
- `MINERU_LOCAL_MIN_FREE_DISK_GB=10`
- `MINERU_LOCAL_MIN_AVAILABLE_MEMORY_GB=8`

These settings are read only in explicit local mode. Cloud installations retain
their current behavior and do not need a MinerU checkout.

## Testing

- Unit contracts inject all subprocess and system-capacity calls; tests never
  access the network or depend on actual RAM/disk totals.
- Capacity tests prove strict one-at-a-time admission and lease release for
  every failure path.
- Provider tests prove typed error mapping, bounded allowlisted logs, cloud
  default preservation, and no fallback.
- Worker bootstrap tests prove preflight runs only in local mode and prevents
  startup when configured local runtime is invalid.
- An opt-in real smoke uses the existing synthetic PDF and local models.
- Final verification runs the full worker contract suite, Ruff, Pyright, and
  the paired MinerU adapter suite.

## Non-goals

- Production DOCX local routing.
- Automatic cloud fallback or retry.
- Application-level percentage/canary routing.
- Raising local job or shard concurrency above 1.
- Changing anonymous telemetry schemas or emitting customer identifiers.
- Completing BL-001 on behalf of the operator.
