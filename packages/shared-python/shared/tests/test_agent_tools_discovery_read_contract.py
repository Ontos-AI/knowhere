"""Discovery text identifiers must be copyable into ``corpus.read``.

Locks the live outline/assets/grep observation shape: every identifier the
model can see in ``ToolResult.text`` is passed as-is to ``read``. File paths
are not chunk ids.
"""

from __future__ import annotations

import os
import re
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
from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.tools.assets import assets
from shared.services.retrieval.agent_tools.tools.grep import grep
from shared.services.retrieval.agent_tools.tools.outline import outline
from shared.services.retrieval.agent_tools.tools.read import read

USER_ID = "user_discovery"
NAMESPACE = "default"
DOC_ID = "doc_a"
REV_ID = "jr_a"
JOB_ID = "job_a"
FILE_NAME = "guide.pdf"
PATH_INTRO = f"{FILE_NAME} / Intro"
PATH_ROOT = f"{FILE_NAME} / Root"
CHUNK_INTRO = "chunk_intro"
CHUNK_TABLE = "chunk_table"
TABLE_FILE = "tables/dose.html"


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, statement):  # noqa: ANN001
        return self._session.execute(statement)


class _GrepRows:
    def all(self) -> list[tuple[object, ...]]:
        return [
            (
                CHUNK_TABLE,
                DOC_ID,
                "table",
                "dose table 30 mg",
                "<table><tr><td>30 mg</td></tr></table>",
                TABLE_FILE,
                {"summary": "dose table"},
                REV_ID,
                JOB_ID,
                PATH_ROOT,
                FILE_NAME,
                1,
            )
        ]


class _RecordingDb:
    def __init__(self, result: object) -> None:
        self.result = result

    async def execute(self, statement):  # noqa: ANN001
        return self.result


@asynccontextmanager
async def _unused_db_factory():
    raise AssertionError("discovery/read contract should not open a second session")
    yield  # pragma: no cover


def _seed_session(session: Session) -> None:
    now = datetime(2026, 1, 1)
    session.add_all(
        [
            JobResult(
                id=REV_ID,
                job_id=JOB_ID,
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
                source_file_name=FILE_NAME,
                parse_track="chunk",
                created_at=now,
                updated_at=now,
            ),
            DocumentSection(
                section_id="sec_root",
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_path=PATH_ROOT,
                section_title="Root",
                section_level=1,
                sort_order=0,
                created_at=now,
            ),
            DocumentSection(
                section_id="sec_intro",
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_path=PATH_INTRO,
                section_title="Intro",
                section_level=2,
                sort_order=1,
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
                content="intro body 30 mg mention",
                source_chunk_path=CHUNK_INTRO,
                sort_order=0,
                created_at=now,
            ),
            DocumentChunk(
                id="row_table",
                chunk_id=CHUNK_TABLE,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_id="sec_root",
                chunk_type="table",
                content="<table><tr><td>30 mg</td></tr></table>",
                file_path=TABLE_FILE,
                source_chunk_path=TABLE_FILE,
                chunk_metadata={"summary": "dose table"},
                sort_order=1,
                created_at=now,
            ),
        ]
    )
    session.commit()


@pytest.fixture
def discovery_ctx() -> ToolContext:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    JobResult.__table__.create(engine)
    Document.__table__.create(engine)
    DocumentSection.__table__.create(engine)
    DocumentChunk.__table__.create(engine)
    session = Session(engine)
    _seed_session(session)
    return ToolContext(
        db=_AsyncSessionAdapter(session),  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        db_factory=_unused_db_factory,
    )


def _read_kwargs(refs: list[dict[str, str]]) -> dict:
    return {
        "refs": refs,
        "mode": "self",
        "include_assets": False,
        "resolve_same_as": False,
    }


def _section_paths_from_outline_text(text: str) -> list[str]:
    return re.findall(r"\| section_path=(.+) \(chunks=", text)


def _asset_refs_from_text(text: str) -> list[dict[str, str]]:
    return [
        {"document_id": document_id, "chunk_id": chunk_id}
        for document_id, chunk_id in re.findall(
            r"document_id=(\S+) chunk_id=(\S+)", text
        )
    ]


@pytest.mark.asyncio
async def test_outline_visible_section_path_reads(discovery_ctx: ToolContext) -> None:
    listed = await outline(discovery_ctx, {"document_id": DOC_ID})
    assert listed.error is None
    paths = _section_paths_from_outline_text(listed.text)
    assert PATH_INTRO in paths
    result = await read(
        discovery_ctx,
        _read_kwargs([{"document_id": DOC_ID, "section_path": PATH_INTRO}]),
    )
    assert result.error is None
    assert result.payload["errors"] == []
    assert result.refs == [{"document_id": DOC_ID, "chunk_id": CHUNK_INTRO}]


@pytest.mark.asyncio
async def test_assets_visible_chunk_id_reads(discovery_ctx: ToolContext) -> None:
    listed = await assets(discovery_ctx, {"document_ids": [DOC_ID], "type": "table"})
    assert listed.error is None
    assert TABLE_FILE in listed.text
    refs = _asset_refs_from_text(listed.text)
    assert refs == [{"document_id": DOC_ID, "chunk_id": CHUNK_TABLE}]
    result = await read(discovery_ctx, _read_kwargs(refs))
    assert result.error is None
    assert result.payload["errors"] == []
    assert result.refs == [{"document_id": DOC_ID, "chunk_id": CHUNK_TABLE}]


@pytest.mark.asyncio
async def test_grep_table_visible_chunk_id_reads(discovery_ctx: ToolContext) -> None:
    grep_ctx = ToolContext(
        db=_RecordingDb(_GrepRows()),  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        db_factory=_unused_db_factory,
    )
    listed = await grep(grep_ctx, {"pattern": "30 mg"})
    match = re.search(r"\((\S+)\) chunk_id=(\S+) /", listed.text)
    assert match is not None
    result = await read(
        discovery_ctx,
        _read_kwargs(
            [{"document_id": match.group(1), "chunk_id": match.group(2)}]
        ),
    )
    assert result.error is None
    assert result.payload["errors"] == []
    assert result.refs == [{"document_id": DOC_ID, "chunk_id": CHUNK_TABLE}]


@pytest.mark.asyncio
async def test_file_path_is_not_a_readable_chunk_id(
    discovery_ctx: ToolContext,
) -> None:
    result = await read(
        discovery_ctx,
        _read_kwargs([{"document_id": DOC_ID, "chunk_id": TABLE_FILE}]),
    )
    assert result.error is not None
    assert f"unknown chunk_id: {TABLE_FILE}" in result.error
