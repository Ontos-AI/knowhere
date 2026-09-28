"""Contract for the worker owner's first traced SQL connection."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

from loguru import logger
from pytest import MonkeyPatch
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace


def test_worker_installs_sql_hook_before_first_connection(
    monkeypatch: MonkeyPatch,
    tmp_path,
) -> None:
    import shared.services.jobs.lifecycle.service as lifecycle_service

    engine = create_engine(f"sqlite:///{tmp_path / 'first-worker-connection.db'}")
    monkeypatch.setattr(
        lifecycle_service,
        "settings",
        SimpleNamespace(KNOWHERE_PUBLICATION_TRACE_ENABLED=True),
    )

    @contextmanager
    def open_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(lifecycle_service, "get_sync_db_context", open_session)
    terminal_events: list[dict[str, object]] = []

    def capture_terminal(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["extra"].get("event") == "publication.trace.terminal":
            terminal_events.append(
                cast(dict[str, object], record["extra"]["publication_trace"])
            )

    def finalize(session: Session, trace: PublicationTrace | None) -> str:
        assert trace is not None
        with trace.stage("chunks_persist"):
            session.execute(text("SELECT 1"))
        return "completed"

    sink_id = logger.add(capture_terminal, level="INFO")
    try:
        result = lifecycle_service._run_lifecycle_transaction(
            job_id="job_123",
            label="success",
            finalize=finalize,
            should_commit=lambda finalization: finalization == "completed",
            build_response=lambda finalization: finalization,
            build_effect_plan=lambda finalization: PostCommitEffectPlan.none(),
            run_after_commit_effects=lambda plan: None,
            trace_publication=True,
        )
    finally:
        logger.remove(sink_id)
        engine.dispose()

    assert result == "completed"
    assert len(terminal_events) == 1
    assert cast(dict[str, object], terminal_events[0]["sql"])["statement_count"] == 1
