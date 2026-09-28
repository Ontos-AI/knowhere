"""Record connection-local trace cleanup on an isolated PostgreSQL test DB.

The target must be an explicitly supplied, loopback-only, dedicated empty test
database. This probe executes SELECT statements only and does not use the
publication source or benchmark clone database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.clone_state import read_clone_record  # noqa: E402
from scripts.publication_benchmark.guards import (  # noqa: E402
    BenchmarkGuardError,
    assert_not_source_database,
    read_database_url_file,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.report import assert_report_is_redacted  # noqa: E402
from scripts.publication_benchmark.run_record import (  # noqa: E402
    resolve_source_content_digest,
)

POOL_REUSE_SCHEMA_VERSION: str = "publication-pool-reuse/1"
TEST_DATABASE_PREFIX: str = "knowhere_publication_pool_test_"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


class _CountingTrace:
    """Retain only the count most recently emitted by one trace stage."""

    def __init__(self) -> None:
        self.statement_count: int = 0

    def record_sql(
        self,
        *,
        stage: str,
        statement_count: int,
        total_sql_ms: float,
        max_statement_ms: float,
        batch_count: int | None = None,
    ) -> None:
        del stage, total_sql_ms, max_statement_ms, batch_count
        self.statement_count = statement_count


def _validate_target(
    *, layout: BenchmarkLayout, source_clone_run_id: str, database_url: str
) -> None:
    """Refuse nonlocal, source, clone, or nondedicated database targets."""
    clone_record = read_clone_record(layout.clone_record_path(source_clone_run_id))
    if clone_record.run_id != source_clone_run_id:
        raise BenchmarkGuardError("pool reuse: source record run ID does not match")
    assert_not_source_database(
        database_url, source=clone_record.source, purpose="pool reuse"
    )
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise BenchmarkGuardError("pool reuse: target must be PostgreSQL")
    if url.host not in LOOPBACK_HOSTS or url.port is None:
        raise BenchmarkGuardError(
            "pool reuse: target must specify a loopback host and port"
        )
    if not str(url.database or "").startswith(TEST_DATABASE_PREFIX):
        raise BenchmarkGuardError(
            "pool reuse: target must be a dedicated pool test database"
        )
    if int(url.port) == clone_record.source.database_url_port:
        raise BenchmarkGuardError("pool reuse: target uses the source database port")
    if int(url.port) == clone_record.port:
        raise BenchmarkGuardError("pool reuse: target uses the benchmark clone port")


def _probe_sync(database_url: str) -> dict[str, Any]:
    from shared.services.jobs.lifecycle.publication_trace_sql import (
        attach_publication_trace,
        clear_publication_trace,
        install_publication_trace_sql_instrumentation,
        read_publication_trace,
    )

    url = make_url(database_url).set(drivername="postgresql+psycopg2")
    engine = create_engine(url, pool_size=1, max_overflow=0)
    install_publication_trace_sql_instrumentation(engine)
    first_trace = _CountingTrace()
    second_trace = _CountingTrace()
    residual_checkouts = 0
    try:
        with engine.connect() as first:
            first_backend = first.execute(text("SELECT pg_backend_pid()")).scalar_one()
            attach_publication_trace(first, first_trace)
            first.execute(text("SELECT 1"))
            # Deliberately omit clear: the pool checkin hook must clean up.
        first_count = first_trace.statement_count

        with engine.connect() as second:
            second_backend = second.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            residual_checkouts += int(read_publication_trace(second) is not None)
            second.execute(text("SELECT 2"))
            after_untraced_count = first_trace.statement_count
            attach_publication_trace(second, second_trace)
            try:
                second.execute(text("SELECT 3"))
            finally:
                clear_publication_trace(second)

        with engine.connect() as third:
            third_backend = third.execute(text("SELECT pg_backend_pid()")).scalar_one()
            residual_checkouts += int(read_publication_trace(third) is not None)
        return {
            "owner": "sync",
            "driver": "psycopg2",
            "checkout_count": 3,
            "same_backend_reused": first_backend == second_backend == third_backend,
            "residual_trace_checkouts": residual_checkouts,
            "first_trace_statement_count": first_count,
            "first_trace_statement_count_after_reuse": first_trace.statement_count,
            "first_trace_statement_count_after_untraced": after_untraced_count,
            "second_trace_statement_count": second_trace.statement_count,
        }
    finally:
        engine.dispose()


async def _probe_async(database_url: str) -> dict[str, Any]:
    from shared.services.jobs.lifecycle.publication_trace_sql import (
        attach_publication_trace,
        clear_publication_trace,
        install_publication_trace_sql_instrumentation,
        read_publication_trace,
    )

    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    install_publication_trace_sql_instrumentation(engine)
    first_trace = _CountingTrace()
    second_trace = _CountingTrace()
    residual_checkouts = 0
    try:
        async with engine.connect() as first:
            first_backend = (
                await first.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            attach_publication_trace(first, first_trace)
            await first.execute(text("SELECT 1"))
        first_count = first_trace.statement_count

        async with engine.connect() as second:
            second_backend = (
                await second.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            residual_checkouts += int(read_publication_trace(second) is not None)
            await second.execute(text("SELECT 2"))
            after_untraced_count = first_trace.statement_count
            attach_publication_trace(second, second_trace)
            try:
                await second.execute(text("SELECT 3"))
            finally:
                clear_publication_trace(second)

        async with engine.connect() as third:
            third_backend = (
                await third.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            residual_checkouts += int(read_publication_trace(third) is not None)
        return {
            "owner": "async",
            "driver": "asyncpg",
            "checkout_count": 3,
            "same_backend_reused": first_backend == second_backend == third_backend,
            "residual_trace_checkouts": residual_checkouts,
            "first_trace_statement_count": first_count,
            "first_trace_statement_count_after_reuse": first_trace.statement_count,
            "first_trace_statement_count_after_untraced": after_untraced_count,
            "second_trace_statement_count": second_trace.statement_count,
        }
    finally:
        await engine.dispose()


def _case_passes(case: Mapping[str, Any], *, owner: str, driver: str) -> bool:
    return (
        case.get("owner") == owner
        and case.get("driver") == driver
        and case.get("checkout_count") == 3
        and case.get("same_backend_reused") is True
        and case.get("residual_trace_checkouts") == 0
        and case.get("first_trace_statement_count") == 1
        and case.get("first_trace_statement_count_after_untraced") == 1
        and case.get("first_trace_statement_count_after_reuse") == 1
        and case.get("second_trace_statement_count") == 1
    )


def check_pool_reuse(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    source_clone_run_id: str,
    database_url: str,
) -> dict[str, Any]:
    """Probe both real drivers and write a redacted reuse evidence artifact."""
    resolved_run_id = validate_run_id(run_id)
    resolved_source_clone_run_id = validate_run_id(source_clone_run_id)
    _validate_target(
        layout=layout,
        source_clone_run_id=resolved_source_clone_run_id,
        database_url=database_url,
    )
    artifact_path = layout.report_directory(resolved_run_id) / "pool-reuse.json"
    artifact_path.unlink(missing_ok=True)
    source_content_digest = resolve_source_content_digest(layout.repository_root)
    sync_case = _probe_sync(database_url)
    async_case = asyncio.run(_probe_async(database_url))
    if resolve_source_content_digest(layout.repository_root) != source_content_digest:
        raise BenchmarkGuardError("benchmark source changed during pool reuse check")
    cases = [sync_case, async_case]
    expected = (("sync", "psycopg2"), ("async", "asyncpg"))
    valid = all(
        _case_passes(case, owner=owner, driver=driver)
        for case, (owner, driver) in zip(cases, expected, strict=True)
    )
    artifact = {
        "schema_version": POOL_REUSE_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "code_commit": cli_support.resolve_code_commit(layout.repository_root),
        "dependency_lock_digest": cli_support.resolve_dependency_lock_digest(
            layout.repository_root
        ),
        "source_content_digest": source_content_digest,
        "status": "pass" if valid else "fail",
        "cases": cases,
    }
    assert_report_is_redacted(artifact)
    cli_support.write_json(artifact_path, artifact)
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    """Probe a dedicated local PostgreSQL test database from the command line."""
    parser = argparse.ArgumentParser(description="Check publication trace pool reuse")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-clone-run-id", required=True)
    parser.add_argument("--db-url-file", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        database_url = read_database_url_file(
            arguments.db_url_file, purpose="pool reuse"
        )
        result = check_pool_reuse(
            layout=BenchmarkLayout.from_path(),
            run_id=arguments.run_id,
            source_clone_run_id=arguments.source_clone_run_id,
            database_url=database_url,
        )
    except (BenchmarkGuardError, OSError, ValueError) as error:
        cli_support.print_result(
            {"command": "check_pool_reuse", "status": "failed", "error": str(error)}
        )
        return cli_support.EXIT_GUARD_REFUSED
    cli_support.print_result({"command": "check_pool_reuse", **result})
    return (
        cli_support.EXIT_OK
        if result["status"] == "pass"
        else cli_support.EXIT_GATE_FAILED
    )


if __name__ == "__main__":
    raise SystemExit(main())
