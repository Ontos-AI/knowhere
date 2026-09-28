"""Benchmark snapshots keep one database version during concurrent publication."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from typing import Any, Mapping
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.testing.contract_runtime import (
    CONTRACT_DEVELOPER_USER_ID,
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark import state_snapshot  # noqa: E402
from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationExecutionRequest,
    PublicationScope,
    capture_benchmark_state,
    executor_for,
)


def test_snapshot_ignores_a_document_committed_after_its_first_read(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    database_url = (
        make_url(get_contract_database_url())
        .set(drivername="postgresql+psycopg2")
        .render_as_string(hide_password=False)
    )
    identifier = uuid4().hex[:10]
    scope = PublicationScope(
        user_ref=CONTRACT_DEVELOPER_USER_ID,
        namespace_ref=f"snapshot-isolation-{identifier}",
        source_file_name="first.pdf",
        job_ref=f"snapshot-job-{identifier}",
        revision_ref=f"snapshot-result-{identifier}",
    )
    result = executor_for("sync").execute(
        PublicationExecutionRequest(
            database_url=database_url,
            scope=scope,
            chunks=[
                {
                    "chunk_id": f"snapshot-chunk-{identifier}",
                    "type": "text",
                    "content": "snapshot isolation evidence",
                    "path": "first.pdf/Root/body",
                    "order": 0,
                    "metadata": {},
                }
            ],
            owner="sync",
            mode="cold",
            redis_namespace="publication-benchmark:snapshot-isolation",
        )
    )
    assert result.outcome == "committed"

    first_read_done = Event()
    writer_committed = Event()
    original_rows = state_snapshot._rows

    def pause_after_document_list(
        db: Session, statement: str, parameters: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        rows = original_rows(db, statement, parameters)
        if current_thread().name.startswith("snapshot-reader") and statement.startswith(
            "SELECT document_id, source_file_name, status, current_job_result_id "
        ):
            first_read_done.set()
            assert writer_committed.wait(15)
        return rows

    monkeypatch.setattr(state_snapshot, "_rows", pause_after_document_list)
    engine = create_engine(database_url)

    def read_snapshot() -> dict[str, Any]:
        with Session(engine) as session:
            snapshot, _activity = capture_benchmark_state(session, scope)
            return snapshot

    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="snapshot-reader"
        ) as pool:
            reader = pool.submit(read_snapshot)
            assert first_read_done.wait(15)
            document_id = f"doc_{uuid4().hex[:12]}"
            with Session(engine) as session:
                session.execute(
                    text(
                        "INSERT INTO documents (document_id, user_id, namespace, status, "
                        "source_file_name, parse_track, created_at, updated_at) VALUES "
                        "(:document_id, :user_id, :namespace, 'active', 'second.pdf', "
                        "'chunk', now(), now())"
                    ),
                    {
                        "document_id": document_id,
                        "user_id": scope.user_ref,
                        "namespace": scope.namespace_ref,
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO graph_nodes (node_id, user_id, namespace, "
                        "node_kind, owner_document_id, job_result_id, created_at, "
                        "updated_at) VALUES (:node_id, :user_id, :namespace, "
                        "'document', :document_id, :revision_id, now(), now())"
                    ),
                    {
                        "node_id": f"doc:{document_id}",
                        "user_id": scope.user_ref,
                        "namespace": scope.namespace_ref,
                        "document_id": document_id,
                        "revision_id": scope.revision_ref,
                    },
                )
                session.commit()
            writer_committed.set()
            snapshot = reader.result(timeout=15)
    finally:
        writer_committed.set()
        engine.dispose()

    assert snapshot["relation_counts"]["documents_active"] == 1
