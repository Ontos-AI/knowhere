"""Contract for measurement-only publication traces shared by both owners."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from loguru import logger

from shared.services.jobs.lifecycle.publication_trace import PublicationTrace


@pytest.fixture
def terminal_events() -> Iterator[list[dict[str, object]]]:
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


def create_trace(owner: str = "sync") -> PublicationTrace:
    return PublicationTrace.start(
        attempt_ref="attempt_123",
        owner=cast("str", owner),  # type: ignore[arg-type]
        job_id="job_123",
        job_result_id="result_123",
        scope_fingerprint="scope-0123456789abcdef",
        job_type="document_ingestion",
        parse_track="chunk",
    )


@pytest.mark.parametrize("owner", ["sync", "async"])
def test_trace_emits_one_terminal_event_with_aggregated_stage_and_sql_counts(
    owner: str,
    terminal_events: list[dict[str, object]],
) -> None:
    trace = create_trace(owner)
    trace.record_count("input_text_chunks", 2)
    trace.record_count("chunks", 2)
    with trace.stage("sections_persist"):
        assert trace.active_stage == "sections_persist"
        trace.record_sql(None, 2, 8.0, 5.0, batch_count=1)
    trace.record_stage("commit", 1.5)
    payload = trace.finish(outcome="success")

    assert trace.is_finished
    assert len(terminal_events) == 1
    assert terminal_events[0] == payload
    assert payload["owner"] == owner
    assert payload["counts"] == {"input_text_chunks": 2, "chunks": 2}
    assert payload["sql"] == {
        "statement_count": 2,
        "batch_count": 1,
        "total_sql_ms": 8.0,
        "max_statement_ms": 5.0,
    }
    stages = cast(dict[str, dict[str, float | int]], payload["stages"])
    assert stages["sections_persist"]["statement_count"] == 2
    assert stages["sections_persist"]["total_sql_ms"] == 8.0
    assert stages["commit"]["duration_ms"] == 1.5

    with pytest.raises(RuntimeError, match="already finished"):
        trace.finish(outcome="success")
    assert len(terminal_events) == 1


def test_rollback_emits_terminal_event_without_error_payload(
    terminal_events: list[dict[str, object]],
) -> None:
    trace = create_trace()
    trace.record_stage("rollback", 3.0)
    payload = trace.finish(outcome="rollback")

    assert len(terminal_events) == 1
    assert payload["outcome"] == "rollback"
    assert "error" not in payload
    assert payload["stages"] == {
        "rollback": {
            "duration_ms": 3.0,
            "statement_count": 0,
            "total_sql_ms": 0.0,
            "max_statement_ms": 0.0,
        }
    }


def test_trace_rejects_business_payload_keys_and_unsafe_values(
    terminal_events: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError, match="opaque ASCII"):
        PublicationTrace.start(
            attempt_ref="attempt_123",
            owner="sync",
            job_id="source file.pdf",
            job_result_id="result_123",
        )
    with pytest.raises(ValueError, match="opaque digest"):
        PublicationTrace.start(
            attempt_ref="attempt_123",
            owner="sync",
            job_id="job_123",
            job_result_id="result_123",
            scope_fingerprint="customer-a",
        )

    trace = create_trace()
    with pytest.raises(ValueError, match="unsupported publication stage"):
        trace.record_stage("namespace/customer-a", 1.0)
    with pytest.raises(ValueError, match="unsupported publication counter"):
        trace.record_count("source_file_name", 3)
    with pytest.raises(ValueError, match="nonnegative integer"):
        trace.record_count("chunks", -1)
    with pytest.raises(ValueError, match="maximum SQL"):
        trace.record_sql("chunks_persist", 1, 2.0, 3.0)
    payload = trace.finish(outcome="success")

    assert len(terminal_events) == 1
    assert "source file.pdf" not in str(payload)
    assert "customer-a" not in str(payload)
    assert "source_file_name" not in str(payload)


def test_stage_sql_is_attributed_to_innermost_stage(
    terminal_events: list[dict[str, object]],
) -> None:
    trace = create_trace()
    with trace.stage("serving_index_prepare"):
        trace.record_sql(None, 1, 2.0, 2.0)
        with trace.stage("map_units_persist"):
            trace.record_sql(None, 1, 5.0, 5.0)
    payload = trace.finish(outcome="success")

    stages = cast(dict[str, dict[str, float | int]], payload["stages"])
    assert stages["serving_index_prepare"]["statement_count"] == 1
    assert stages["map_units_persist"]["statement_count"] == 1
    assert cast(dict[str, object], payload["sql"])["statement_count"] == 2
    assert len(terminal_events) == 1


def test_cumulative_sql_hook_updates_are_counted_once() -> None:
    trace = create_trace()
    trace.record_sql(
        stage="chunks_persist",
        statement_count=1,
        total_sql_ms=2.0,
        max_statement_ms=2.0,
    )
    trace.record_sql(
        stage="chunks_persist",
        statement_count=2,
        total_sql_ms=5.0,
        max_statement_ms=3.0,
    )
    payload = trace.finish(outcome="success")

    assert cast(dict[str, object], payload["sql"])["statement_count"] == 2
    assert cast(dict[str, object], payload["sql"])["total_sql_ms"] == 5.0
    stages = cast(dict[str, dict[str, float | int]], payload["stages"])
    assert stages["chunks_persist"]["statement_count"] == 2
    assert stages["chunks_persist"]["duration_ms"] == 0.0
