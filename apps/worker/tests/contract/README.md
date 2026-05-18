# Worker Contract Tests

Put worker surface specifications here.

These tests should verify:

- task entrypoint behavior
- durable side effects
- queue-facing or task-facing contracts

These tests should avoid:

- asserting helper call sequences
- patching internal services that define the behavior under test
- turning task tests into unit tests disguised as contracts

## Current Surface

- `app.core.tasks.stale_job_sweeper.expire_stale_jobs`
  verifies stale-job expiration, durable failure state, audit logging, and the Redis-backed duplicate-Beat lock
- `app.core.tasks.document_ingestion_tasks.upload_url_file_task`
  verifies the cached storage target, Redis progress publication, and the stable `waiting-file` job state while mocking only outbound URL download and S3 boundaries
- `app.core.tasks.document_ingestion_tasks.parse_task`
  verifies success publication, terminal skip handling, and failure cleanup/refund behavior while keeping billing, finalization, and retrieval publication real
- `app.core.tasks.webhook_tasks.recover_orphaned_webhooks`
  verifies orphaned pending webhook recovery, durable QStash delivery-state persistence, and the Redis-backed duplicate-Beat lock

## Command

- `uv run python apps/api/scripts/ensure_test_environment.py --install`
- `uv run python apps/api/scripts/ensure_test_environment.py`
- `uv run pytest apps/worker/tests/contract -q`

## Local Prerequisites

- PostgreSQL server binaries available to `pytest-postgresql`
- PostgreSQL contrib extension files for `uuid-ossp` and `pg_trgm`
- No running local PostgreSQL service is required; `pytest-postgresql` starts an isolated process
- No running local Redis service is required; contract tests use `fakeredis`
- Set `PYTEST_POSTGRESQL_EXECUTABLE` to the `pg_ctl` path if discovery cannot find it
