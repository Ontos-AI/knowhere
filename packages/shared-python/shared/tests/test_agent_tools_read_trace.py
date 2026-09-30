"""read per-ref status is what the episode trace records."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_explore.bridge import build_decision_trace
from shared.services.retrieval.agent_explore.shared import tool_message_content
from shared.services.retrieval.agent_explore.types import AgentStep
from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.tools.read import read

USER_ID = "user_trace"
NAMESPACE = "default"
DOC_A = "doc_a"
REV_A = "jr_a"
FILE_A = "guide.pdf"
PATH_INTRO = f"{FILE_A} / Intro"
CHUNK_INTRO = "chunk_intro"


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, statement):  # noqa: ANN001
        return self._session.execute(statement)


@asynccontextmanager
async def _unused_db_factory():
    raise AssertionError("read-trace test should not open a second session")
    yield  # pragma: no cover


@pytest.fixture
def read_ctx() -> ToolContext:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    now = datetime(2026, 1, 1)
    JobResult.__table__.create(engine)
    Document.__table__.create(engine)
    DocumentSection.__table__.create(engine)
    DocumentChunk.__table__.create(engine)
    session = Session(engine)
    session.add_all(
        [
            JobResult(
                id=REV_A, job_id="job_a", delivery_mode="inline", created_at=now, updated_at=now
            ),
            Document(
                document_id=DOC_A,
                user_id=USER_ID,
                namespace=NAMESPACE,
                status="active",
                current_job_result_id=REV_A,
                source_file_name=FILE_A,
                parse_track="chunk",
                created_at=now,
                updated_at=now,
            ),
            DocumentSection(
                section_id="sec_intro",
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_A,
                job_result_id=REV_A,
                section_path=PATH_INTRO,
                section_level=1,
                sort_order=0,
                created_at=now,
            ),
            DocumentChunk(
                id="row_intro",
                chunk_id=CHUNK_INTRO,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_A,
                job_result_id=REV_A,
                section_id="sec_intro",
                chunk_type="text",
                content="intro body",
                source_chunk_path=CHUNK_INTRO,
                created_at=now,
            ),
        ]
    )
    session.commit()
    return ToolContext(
        db=_AsyncSessionAdapter(session),  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        db_factory=_unused_db_factory,
    )


@pytest.mark.asyncio
async def test_read_partial_failure_status_is_in_payload_and_trace(
    read_ctx: ToolContext,
) -> None:
    result = await read(
        read_ctx,
        {
            "refs": [
                {"document_id": DOC_A, "section_path": PATH_INTRO},
                {"document_id": DOC_A, "chunk_id": "missing_chunk"},
            ],
            "mode": "self",
            "include_assets": False,
            "resolve_same_as": False,
        },
    )
    assert result.error is None
    statuses = result.payload["refs"]
    assert statuses[0]["status"] == "ok"
    assert statuses[1]["status"] == "failed"
    assert "unknown chunk_id" in statuses[1]["reason"]
    assert result.refs == [{"document_id": DOC_A, "chunk_id": CHUNK_INTRO}]

    observation = tool_message_content(
        result, tool_name="corpus.read", max_chars=12_000
    )
    assert "[ok]" in observation
    assert "[failed: unknown chunk_id: missing_chunk in doc_a]" in observation

    trace = build_decision_trace(
        [
            AgentStep(
                step_index=0,
                tool_name="corpus.read",
                tool_args={"refs": [{"document_id": DOC_A, "section_path": PATH_INTRO}]},
                observation_text=observation,
                error=result.error,
                elapsed_ms=1,
                tokens_used_delta=0,
                tokens_used_total=0,
                ref_status=result.payload["refs"],
            )
        ]
    )
    recorded = trace[0].observation["observation_text"]
    assert "[ok]" in recorded
    assert "[failed: unknown chunk_id: missing_chunk in doc_a]" in recorded
    assert trace[0].observation["ref_status"] == result.payload["refs"]
    assert trace[0].result["status"] == "ok"
    assert trace[0].result["error"] is None
