"""Contract checks for isolated, redacted pool reuse evidence."""

# ruff: noqa: E402

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    get_contract_database_url,
)

from tests.support.publication_benchmark_support import (
    benchmark_layout,
    ensure_benchmark_import_path,
    write_listed_clone,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.check_pool_reuse import (  # noqa: E402
    POOL_REUSE_SCHEMA_VERSION,
    _case_passes,
    _validate_target,
    check_pool_reuse,
)
from scripts.publication_benchmark.guards import BenchmarkGuardError  # noqa: E402

RUN_ID = "pool-reuse-contract"
SOURCE_CLONE_RUN_ID = "source-clone-contract"
CLONE_URL = "postgresql://postgres:secret@127.0.0.1:55450/knowhere_benchmark"


def _probe_case(owner: str) -> dict[str, Any]:
    return {
        "owner": owner,
        "driver": "psycopg2" if owner == "sync" else "asyncpg",
        "checkout_count": 3,
        "same_backend_reused": True,
        "residual_trace_checkouts": 0,
        "first_trace_statement_count": 1,
        "first_trace_statement_count_after_untraced": 1,
        "first_trace_statement_count_after_reuse": 1,
        "second_trace_statement_count": 1,
    }


@pytest.mark.parametrize(
    "target",
    [
        "postgresql://postgres:secret@127.0.0.1:55433/knowhere",
        CLONE_URL,
        "postgresql://postgres:secret@db.example.com:55499/"
        "knowhere_publication_pool_test_contract",
        "postgresql://postgres:secret@127.0.0.1:55499/knowhere",
    ],
)
def test_pool_reuse_refuses_source_clone_and_nonisolated_targets(
    tmp_path: Path, target: str
) -> None:
    layout = benchmark_layout(tmp_path)
    write_listed_clone(layout, run_id=SOURCE_CLONE_RUN_ID, database_url=CLONE_URL)

    with pytest.raises(BenchmarkGuardError):
        _validate_target(
            layout=layout,
            source_clone_run_id=SOURCE_CLONE_RUN_ID,
            database_url=target,
        )

    assert not (layout.report_directory(RUN_ID) / "pool-reuse.json").exists()


def test_pool_reuse_rejects_residual_trace_evidence() -> None:
    sync_case = _probe_case("sync")
    sync_case["residual_trace_checkouts"] = 1

    assert not _case_passes(sync_case, owner="sync", driver="psycopg2")


def test_pool_reuse_discards_stale_evidence_if_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = benchmark_layout(tmp_path)
    write_listed_clone(layout, run_id=SOURCE_CLONE_RUN_ID, database_url=CLONE_URL)
    artifact_path = layout.report_directory(RUN_ID) / "pool-reuse.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text('{"status":"pass"}', encoding="utf-8")
    digests = iter(("sha256:" + "a" * 64, "sha256:" + "b" * 64))
    monkeypatch.setattr(
        "scripts.publication_benchmark.check_pool_reuse.resolve_source_content_digest",
        lambda repository_root: next(digests),
    )
    monkeypatch.setattr(
        "scripts.publication_benchmark.check_pool_reuse._probe_sync",
        lambda database_url: _probe_case("sync"),
    )

    async def probe_async(database_url: str) -> dict[str, Any]:
        return _probe_case("async")

    monkeypatch.setattr(
        "scripts.publication_benchmark.check_pool_reuse._probe_async",
        probe_async,
    )

    with pytest.raises(BenchmarkGuardError, match="source changed"):
        check_pool_reuse(
            layout=layout,
            run_id=RUN_ID,
            source_clone_run_id=SOURCE_CLONE_RUN_ID,
            database_url=(
                "postgresql://postgres:secret@127.0.0.1:55499/"
                "knowhere_publication_pool_test_contract"
            ),
        )

    assert not artifact_path.exists()


def test_pool_reuse_checks_both_real_postgresql_drivers(
    postgresql_proc: PostgreSQLProcess,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one empty database inside pytest's isolated PostgreSQL process."""
    database_name = f"knowhere_publication_pool_test_{uuid4().hex[:12]}"
    admin_url = make_url(get_contract_database_url(postgresql_proc)).set(
        drivername="postgresql+psycopg2", database="postgres"
    )
    test_url = admin_url.set(database=database_name)
    rendered_test_url = test_url.render_as_string(hide_password=False)
    layout = benchmark_layout(tmp_path)
    write_listed_clone(layout, run_id=SOURCE_CLONE_RUN_ID, database_url=CLONE_URL)
    monkeypatch.setattr(
        "scripts.publication_benchmark.check_pool_reuse.resolve_source_content_digest",
        lambda repository_root: "sha256:" + "a" * 64,
    )

    admin = psycopg2.connect(
        host=admin_url.host,
        port=admin_url.port,
        user=admin_url.username,
        password=admin_url.password,
        dbname="postgres",
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )
        artifact = check_pool_reuse(
            layout=layout,
            run_id=RUN_ID,
            source_clone_run_id=SOURCE_CLONE_RUN_ID,
            database_url=rendered_test_url,
        )
        assert artifact["status"] == "pass"
        assert artifact["schema_version"] == POOL_REUSE_SCHEMA_VERSION
        assert artifact["source_content_digest"] == "sha256:" + "a" * 64
        assert all(case["same_backend_reused"] for case in artifact["cases"])
        assert all(case["residual_trace_checkouts"] == 0 for case in artifact["cases"])
        saved = (layout.report_directory(RUN_ID) / "pool-reuse.json").read_text(
            encoding="utf-8"
        )
        assert rendered_test_url not in saved
        assert json.loads(saved) == artifact
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )
        admin.close()
