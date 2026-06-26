from __future__ import annotations

from typing import Any, cast

from app.services.documents.namespace_migration import (
    NAMESPACE_MODELS,
    NamespaceMigrationConflictError,
    migrate_namespace,
)
from shared.models.database.job import Job
from sqlalchemy.orm import Session


def do_nothing_cache_invalidator(
    user_id: str,
    source_namespace: str,
    target_namespace: str,
) -> None:
    del user_id, source_namespace, target_namespace


class FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value

    def scalars(self) -> "FakeScalarResult":
        return self

    def all(self) -> list[str]:
        if isinstance(self._value, list):
            return self._value
        return []


class FakeSession:
    def __init__(self, *, conflict_count: int = 0) -> None:
        self.conflict_count = conflict_count
        self.executed: list[Any] = []
        self.jobs: dict[str, Job] = {
            "job_active": Job(
                job_id="job_active",
                user_id="user_1",
                job_type="document_ingestion",
                status="running",
                source_type="file",
                webhook_enabled=False,
                job_metadata={
                    "namespace": "notebook-old",
                    "document_id": "doc_1",
                    "original_request": {
                        "namespace": "notebook-old",
                        "source_type": "file",
                    },
                },
            )
        }

    def execute(self, statement: Any) -> FakeScalarResult:
        self.executed.append(statement)
        statement_text = str(statement)
        if "jobs.job_id" in statement_text and "count" not in statement_text.lower():
            return FakeScalarResult(["job_active"])
        if "JOIN" in statement_text:
            return FakeScalarResult(self.conflict_count)
        return FakeScalarResult(3)

    def get(self, model: type, key: str) -> Job | None:
        if model is not Job:
            return None
        return self.jobs.get(key)


def test_namespace_migration_dry_run_counts_rows_without_mutating_jobs() -> None:
    session = FakeSession()

    summary = migrate_namespace(
        cast(Session, session),
        user_id="user_1",
        source_namespace="notebook-old",
        target_namespace="default",
        dry_run=True,
        cache_invalidator=do_nothing_cache_invalidator,
    )

    assert summary.dry_run is True
    assert summary.job_count == 1
    assert summary.row_counts == {
        model.__tablename__: 3 for model in NAMESPACE_MODELS
    }
    assert all(count == 0 for count in summary.conflict_counts.values())
    job_metadata = cast(dict[str, Any], session.jobs["job_active"].job_metadata)

    assert job_metadata["namespace"] == "notebook-old"


def test_namespace_migration_apply_updates_job_metadata() -> None:
    session = FakeSession()

    summary = migrate_namespace(
        cast(Session, session),
        user_id="user_1",
        source_namespace="notebook-old",
        target_namespace="default",
        dry_run=False,
        cache_invalidator=do_nothing_cache_invalidator,
    )

    job_metadata = cast(dict[str, Any], session.jobs["job_active"].job_metadata)
    original_request = job_metadata["original_request"]

    assert summary.dry_run is False
    assert summary.job_count == 1
    assert all(count == 0 for count in summary.conflict_counts.values())
    assert job_metadata["namespace"] == "default"
    assert isinstance(original_request, dict)
    assert original_request["namespace"] == "default"


def test_namespace_migration_dry_run_reports_target_conflicts() -> None:
    session = FakeSession(conflict_count=2)

    summary = migrate_namespace(
        cast(Session, session),
        user_id="user_1",
        source_namespace="notebook-old",
        target_namespace="default",
        dry_run=True,
        cache_invalidator=do_nothing_cache_invalidator,
    )

    assert summary.conflict_counts == {
        "retrieval_hit_stats.document_key": 2,
        "retrieval_hit_stats.chunk_key": 2,
        "demo_materializations.scope_source": 2,
    }
    job_metadata = cast(dict[str, Any], session.jobs["job_active"].job_metadata)

    assert job_metadata["namespace"] == "notebook-old"


def test_namespace_migration_apply_aborts_on_target_conflicts() -> None:
    session = FakeSession(conflict_count=1)

    try:
        migrate_namespace(
            cast(Session, session),
            user_id="user_1",
            source_namespace="notebook-old",
            target_namespace="default",
            dry_run=False,
            cache_invalidator=do_nothing_cache_invalidator,
        )
    except NamespaceMigrationConflictError as exc:
        assert exc.summary.conflict_counts == {
            "retrieval_hit_stats.document_key": 1,
            "retrieval_hit_stats.chunk_key": 1,
            "demo_materializations.scope_source": 1,
        }
    else:
        raise AssertionError("Expected namespace migration conflict")

    job_metadata = cast(dict[str, Any], session.jobs["job_active"].job_metadata)

    assert job_metadata["namespace"] == "notebook-old"


def test_namespace_migration_apply_invalidates_source_and_target_cache() -> None:
    session = FakeSession()
    invalidated: list[tuple[str, str, str]] = []

    migrate_namespace(
        cast(Session, session),
        user_id="user_1",
        source_namespace="notebook-old",
        target_namespace="default",
        dry_run=False,
        cache_invalidator=lambda user_id, source, target: invalidated.append(
            (user_id, source, target)
        ),
    )

    assert invalidated == [("user_1", "notebook-old", "default")]
