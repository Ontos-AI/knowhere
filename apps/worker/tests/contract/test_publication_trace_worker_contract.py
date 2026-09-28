"""Sync worker transaction owner's publication trace contract."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.services.jobs.lifecycle import service as lifecycle_service
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
from shared.services.jobs.lifecycle.publication_trace_sql import (
    PUBLICATION_TRACE_INFO_KEY,
)


@dataclass(frozen=True)
class Finalization:
    should_commit: bool
    response: str


@pytest.fixture
def trace_events() -> Iterator[list[dict[str, object]]]:
    events: list[dict[str, object]] = []

    def capture_event(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["extra"].get("event") == "publication.trace.terminal":
            events.append(cast(dict[str, object], record["extra"]["publication_trace"]))

    sink_id = logger.add(capture_event, level="INFO")
    try:
        yield events
    finally:
        logger.remove(sink_id)


@pytest.fixture
def publication_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    database_path = tmp_path / "publication-owner.db"
    engine = create_engine(
        f"sqlite:///{database_path}", pool_size=1, max_overflow=0
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE publication_rows (value TEXT NOT NULL)"))

    @contextmanager
    def get_database_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(lifecycle_service, "get_sync_db_context", get_database_session)
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_TRACE_ENABLED", True)
    try:
        yield engine
    finally:
        engine.dispose()


def test_sync_owner_emits_success_before_effects_and_clears_connection(
    publication_database: Engine,
    trace_events: list[dict[str, object]],
) -> None:
    effect_event_counts: list[int] = []

    def finalize(db: Session, trace: PublicationTrace | None) -> Finalization:
        assert trace is not None
        trace.bind_result_id("result_123")
        with trace.stage("chunks_persist"):
            db.execute(text("INSERT INTO publication_rows (value) VALUES ('saved')"))
        return Finalization(should_commit=True, response="saved")

    result = lifecycle_service._run_lifecycle_transaction(
        job_id="job_123",
        label="success",
        trace_publication=True,
        finalize=finalize,
        should_commit=lambda finalization: finalization.should_commit,
        build_response=lambda finalization: finalization.response,
        build_effect_plan=lambda _finalization: PostCommitEffectPlan.none(),
        run_after_commit_effects=lambda _plan: effect_event_counts.append(
            len(trace_events)
        ),
    )

    assert result == "saved"
    assert effect_event_counts == [1]
    assert len(trace_events) == 1
    assert trace_events[0]["outcome"] == "success"
    assert trace_events[0]["job_result_id"] == "result_123"
    with publication_database.connect() as connection:
        assert PUBLICATION_TRACE_INFO_KEY not in connection.info
        assert connection.execute(text("SELECT count(*) FROM publication_rows")).scalar_one() == 1


@pytest.mark.parametrize("raises", [False, True])
def test_sync_owner_emits_one_rollback_and_skips_effects(
    publication_database: Engine,
    trace_events: list[dict[str, object]],
    raises: bool,
) -> None:
    effects: list[str] = []

    def finalize(db: Session, trace: PublicationTrace | None) -> Finalization:
        assert trace is not None
        with trace.stage("chunks_persist"):
            db.execute(text("INSERT INTO publication_rows (value) VALUES ('discard')"))
        if raises:
            raise RuntimeError("publication failed")
        return Finalization(should_commit=False, response="skipped")

    def run_owner() -> str:
        return lifecycle_service._run_lifecycle_transaction(
            job_id="job_123",
            label="success",
            trace_publication=True,
            finalize=finalize,
            should_commit=lambda finalization: finalization.should_commit,
            build_response=lambda finalization: finalization.response,
            build_effect_plan=lambda _finalization: PostCommitEffectPlan.none(),
            run_after_commit_effects=lambda _plan: effects.append("ran"),
        )

    if raises:
        with pytest.raises(RuntimeError, match="publication failed"):
            run_owner()
    else:
        assert run_owner() == "skipped"

    assert effects == []
    assert len(trace_events) == 1
    assert trace_events[0]["outcome"] == "rollback"
    with publication_database.connect() as connection:
        assert PUBLICATION_TRACE_INFO_KEY not in connection.info
        assert connection.execute(text("SELECT count(*) FROM publication_rows")).scalar_one() == 0


def test_disabled_trace_keeps_publication_write_and_emits_no_event(
    publication_database: Engine,
    trace_events: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_TRACE_ENABLED", False)

    def finalize(db: Session, trace: PublicationTrace | None) -> Finalization:
        assert trace is None
        db.execute(text("INSERT INTO publication_rows (value) VALUES ('saved')"))
        return Finalization(should_commit=True, response="saved")

    assert lifecycle_service._run_lifecycle_transaction(
        job_id="job_123",
        label="success",
        trace_publication=True,
        finalize=finalize,
        should_commit=lambda finalization: finalization.should_commit,
        build_response=lambda finalization: finalization.response,
        build_effect_plan=lambda _finalization: PostCommitEffectPlan.none(),
        run_after_commit_effects=lambda _plan: None,
    ) == "saved"

    assert trace_events == []
    with publication_database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM publication_rows")).scalar_one() == 1
