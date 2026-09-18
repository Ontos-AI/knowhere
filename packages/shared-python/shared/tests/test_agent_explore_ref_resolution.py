"""``resolve_finish_refs`` keeps resolved refs and records drop reasons."""

from __future__ import annotations

import os
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
from shared.services.retrieval.agent_explore.ref_resolution import resolve_finish_refs

USER_ID = "user_resolve"
NAMESPACE = "default"
DOC_ID = "doc_a"
REV_ID = "jr_a"
PATH_INTRO = "guide.pdf / Intro"
CHUNK_INTRO = "chunk_intro"


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, statement):  # noqa: ANN001
        return self._session.execute(statement)


@pytest.fixture
def resolve_db() -> _AsyncSessionAdapter:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    JobResult.__table__.create(engine)
    Document.__table__.create(engine)
    DocumentSection.__table__.create(engine)
    DocumentChunk.__table__.create(engine)
    session = Session(engine)
    now = datetime(2026, 1, 1)
    session.add_all(
        [
            JobResult(
                id=REV_ID,
                job_id="job_a",
                delivery_mode="inline",
                created_at=now,
                updated_at=now,
            ),
            Document(
                document_id=DOC_ID,
                user_id=USER_ID,
                namespace=NAMESPACE,
                status="active",
                current_job_result_id=REV_ID,
                source_file_name="guide.pdf",
                parse_track="chunk",
                created_at=now,
                updated_at=now,
            ),
            DocumentSection(
                section_id="sec_intro",
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_path=PATH_INTRO,
                section_title="Intro",
                section_level=1,
                sort_order=0,
                created_at=now,
            ),
            DocumentChunk(
                id="row_intro",
                chunk_id=CHUNK_INTRO,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_id="sec_intro",
                chunk_type="text",
                content="intro body",
                source_chunk_path=CHUNK_INTRO,
                sort_order=0,
                created_at=now,
            ),
        ]
    )
    session.commit()
    return _AsyncSessionAdapter(session)


@pytest.mark.asyncio
async def test_resolve_finish_refs_records_drop_reasons(
    resolve_db: _AsyncSessionAdapter,
) -> None:
    resolution = await resolve_finish_refs(
        resolve_db,  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        refs=[
            {"document_id": DOC_ID, "section_path": PATH_INTRO},
            {"document_id": DOC_ID, "section_path": "no such path"},
            {"section_path": PATH_INTRO},
            {"document_id": "doc_missing", "section_path": PATH_INTRO},
        ],
    )
    assert resolution.resolved == [{"document_id": DOC_ID, "chunk_id": CHUNK_INTRO}]
    reasons = [item["reason"] for item in resolution.dropped]
    assert "missing document_id" in reasons
    assert any("unknown section_path" in reason for reason in reasons)
    assert "unknown document_id: doc_missing" in reasons
