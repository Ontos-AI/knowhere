# API Test Layout

This directory is the new home for API tests.

## Structure

- `contract/`: HTTP-surface specifications
- `support/`: fixtures, environment bootstrap, seeds, reset helpers
- `migrations/`: Alembic and schema behavior checks

## Rules

- New API behavior tests go here.
- This tree is the default-discovered API test suite.
- Contract tests must call the API through HTTP.
- Contract tests must assert response contract and observable side effects.
- Contract tests must not patch route handlers, repositories, or service internals for the behavior under test.

## Commands

- `uv run python apps/api/scripts/ensure_test_environment.py --install`: install and verify local test prerequisites
- `uv run python apps/api/scripts/ensure_test_environment.py`: verify local test prerequisites only
- `uv run pytest apps/api/tests/contract -q`: fast API HTTP contract checks
- `uv run pytest apps/api/tests/migrations -q`: migration and schema guarantees
- `uv run pytest apps/api/tests -q`: full API test tree under the new taxonomy

## Local Prerequisites

- PostgreSQL server binaries available to `pytest-postgresql`
- PostgreSQL contrib extension files for `uuid-ossp`, `pg_trgm`, and `vector`
- No running local PostgreSQL service is required; `pytest-postgresql` starts an isolated process
- No running local Redis service is required; contract tests use `fakeredis`
- Set `PYTEST_POSTGRESQL_EXECUTABLE` to the `pg_ctl` path if discovery cannot find it
