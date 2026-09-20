"""Explore-phase table HTML / large-table window / query_table / image media."""

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
from shared.services.retrieval.agent_tools import REGISTRY
from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.explore_mount import mount_explore_hits
from shared.services.retrieval.agent_tools.tools.grep import grep
from shared.services.retrieval.agent_tools.tools.query_table import query_table
from shared.services.retrieval.agent_tools.tools.read import read
from shared.services.retrieval.agent_tools.tools.recall import recall
from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult
from shared.services.retrieval.settings import LARGE_TABLE_AXIS

USER_ID = "user_explore"
NAMESPACE = "default"
DOC_ID = "doc_a"
REV_ID = "jr_a"
JOB_ID = "job_a"
FILE_NAME = "guide.pdf"
PATH_ROOT = f"{FILE_NAME} / Root"
PATH_INTRO = f"{FILE_NAME} / Intro"
CHUNK_SMALL = "chunk_small_table"
CHUNK_LARGE = "chunk_large_table"
CHUNK_IMAGE = "chunk_image"
CHUNK_TEXT = "chunk_intro"

SMALL_HTML = (
    "<table><tr><th>Dose</th><th>Unit</th></tr>"
    "<tr><td>30 mg</td><td>tablet</td></tr></table>"
)


def _large_table_html(*, rows: int, cols: int) -> str:
    parts = ["<table>"]
    parts.append("<tr>" + "".join(f"<th>H{col}</th>" for col in range(cols)) + "</tr>")
    for row in range(1, rows):
        cells = [f"<td>R{row}C{col}</td>" for col in range(cols)]
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, statement):  # noqa: ANN001
        return self._session.execute(statement)


@asynccontextmanager
async def _unused_db_factory():
    raise AssertionError("explore table/image tests should not open a second session")
    yield  # pragma: no cover


def _seed(
    session: Session,
    *,
    small_html: str = SMALL_HTML,
    large_html: str | None = None,
    text_connect: dict | None = None,
    image_path: str = "images/x.png",
) -> None:
    now = datetime(2026, 1, 1)
    large_html = large_html or _large_table_html(
        rows=LARGE_TABLE_AXIS, cols=3
    )
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
                id="row_small",
                chunk_id=CHUNK_SMALL,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_id="sec_root",
                chunk_type="table",
                content=small_html,
                file_path="tables/small.html",
                source_chunk_path="tables/small.html",
                chunk_metadata={"summary": "dose table summary"},
                sort_order=0,
                created_at=now,
            ),
            DocumentChunk(
                id="row_large",
                chunk_id=CHUNK_LARGE,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_id="sec_root",
                chunk_type="table",
                content=large_html,
                file_path="tables/large.html",
                source_chunk_path="tables/large.html",
                chunk_metadata={"summary": "large table summary"},
                sort_order=1,
                created_at=now,
            ),
            DocumentChunk(
                id="row_image",
                chunk_id=CHUNK_IMAGE,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_id="sec_root",
                chunk_type="image",
                content="a chart of dose",
                file_path=image_path,
                source_chunk_path=image_path,
                sort_order=2,
                created_at=now,
            ),
            DocumentChunk(
                id="row_intro",
                chunk_id=CHUNK_TEXT,
                user_id=USER_ID,
                namespace=NAMESPACE,
                document_id=DOC_ID,
                job_result_id=REV_ID,
                section_id="sec_intro",
                chunk_type="text",
                content="intro body [tables/small.html]",
                source_chunk_path=CHUNK_TEXT,
                chunk_metadata=text_connect
                or {
                    "connect_to": [
                        {
                            "target": CHUNK_SMALL,
                            "relation": "embeds",
                            "ref": "[tables/small.html]",
                        }
                    ]
                },
                sort_order=3,
                created_at=now,
            ),
        ]
    )
    session.commit()


@pytest.fixture
def explore_ctx() -> ToolContext:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    JobResult.__table__.create(engine)
    Document.__table__.create(engine)
    DocumentSection.__table__.create(engine)
    DocumentChunk.__table__.create(engine)
    session = Session(engine)
    _seed(session)
    return ToolContext(
        db=_AsyncSessionAdapter(session),  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        db_factory=_unused_db_factory,
    )


def _read_table(chunk_id: str, **extra: object) -> dict:
    return {
        "refs": [{"document_id": DOC_ID, "chunk_id": chunk_id}],
        "mode": "self",
        "include_assets": False,
        "resolve_same_as": False,
        **extra,
    }


def test_query_table_is_registered() -> None:
    spec = REGISTRY.get("corpus.query_table")
    assert spec is not None
    assert spec.json_schema["required"] == ["document_id", "chunk_id", "sql"]


def test_corpus_schema_documents_explore_table_rules() -> None:
    from shared.services.retrieval.agent_tools import load_corpus_schema_text

    text = load_corpus_schema_text()
    assert "query_table" in text
    assert "Grep does not scan table-cell HTML" in text
    assert "Table **cells** are searched only when `document_ids` is set" not in text
    assert "focus" not in text.lower()


@pytest.mark.asyncio
async def test_read_small_table_returns_html_not_summary(
    explore_ctx: ToolContext,
) -> None:
    result = await read(explore_ctx, _read_table(CHUNK_SMALL))
    assert result.error is None
    assert "<table" in result.text
    assert "30 mg" in result.text
    assert "[Table:" not in result.text
    assert "dose table summary" not in result.text


@pytest.mark.asyncio
async def test_read_large_table_has_headers_not_full_html(
    explore_ctx: ToolContext,
) -> None:
    """No focus/window: GREP/recall never scan table-cell HTML, so there is
    no real "hit cell" to center a window on — large tables always get
    headers plus a corpus.query_table pointer, nothing else."""
    result = await read(explore_ctx, _read_table(CHUNK_LARGE))
    assert result.error is None
    assert "too large" in result.text
    assert "Column headers:" in result.text
    assert "Row headers:" in result.text
    assert "corpus.query_table" in result.text
    assert "Window around" not in result.text
    assert result.text.count("<table") == 0


@pytest.mark.asyncio
async def test_read_inlines_connected_small_table_html(
    explore_ctx: ToolContext,
) -> None:
    result = await read(
        explore_ctx,
        {
            "refs": [{"document_id": DOC_ID, "section_path": PATH_INTRO}],
            "mode": "self",
            "include_assets": True,
            "resolve_same_as": False,
        },
    )
    assert result.error is None
    assert "intro body" in result.text
    assert "<table" in result.text
    assert "30 mg" in result.text
    assert "[Table:" not in result.text


@pytest.mark.asyncio
async def test_read_connected_table_download_failure_keeps_body_content(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected table that fails to download must not take the body
    chunk down with it — the body's own content still comes back, with a
    warning note where the table would have been."""
    from shared.services.retrieval.hydration.table_grid import TableDownloadError

    def _raise_download_error(_row):
        raise TableDownloadError("S3 301: PermanentRedirect")

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.table_grid.load_table_html",
        _raise_download_error,
    )
    result = await read(
        explore_ctx,
        {
            "refs": [{"document_id": DOC_ID, "section_path": PATH_INTRO}],
            "mode": "self",
            "include_assets": True,
            "resolve_same_as": False,
        },
    )
    assert result.error is None
    assert "intro body" in result.text
    assert "table unavailable" in result.text
    assert "download failed" in result.text
    assert "<table" not in result.text


@pytest.mark.asyncio
async def test_query_table_select_returns_html_rows(
    explore_ctx: ToolContext,
) -> None:
    result = await query_table(
        explore_ctx,
        {
            "document_id": DOC_ID,
            "chunk_id": CHUNK_SMALL,
            "sql": "SELECT row_header, Unit FROM t",
        },
    )
    assert result.error is None
    assert "<table>" in result.text
    assert "30 mg" in result.text
    assert result.refs == [{"document_id": DOC_ID, "chunk_id": CHUNK_SMALL}]


@pytest.mark.asyncio
async def test_query_table_rejects_writes_and_multiple_statements(
    explore_ctx: ToolContext,
) -> None:
    write = await query_table(
        explore_ctx,
        {
            "document_id": DOC_ID,
            "chunk_id": CHUNK_SMALL,
            "sql": "DELETE FROM t",
        },
    )
    assert write.error is not None
    assert "SELECT" in write.error

    multi = await query_table(
        explore_ctx,
        {
            "document_id": DOC_ID,
            "chunk_id": CHUNK_SMALL,
            "sql": "SELECT * FROM t; SELECT * FROM t",
        },
    )
    assert multi.error is not None


@pytest.mark.asyncio
async def test_query_table_download_failure_returns_error_not_raise(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.hydration.table_grid import TableDownloadError

    def _raise_download_error(_row):
        raise TableDownloadError("S3 301: PermanentRedirect")

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.query_table.load_table_html",
        _raise_download_error,
    )
    result = await query_table(
        explore_ctx,
        {
            "document_id": DOC_ID,
            "chunk_id": CHUNK_SMALL,
            "sql": "SELECT * FROM t",
        },
    )
    assert result.error is not None
    assert "download failed" in result.error


@pytest.mark.asyncio
async def test_read_https_image_fills_media(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _enrich(rows, *, log_context):  # noqa: ANN001
        enriched = []
        for row in rows:
            item = dict(row)
            if item.get("chunk_type") == "image":
                item["asset_url"] = "https://cdn.example/chart.png"
            enriched.append(item)
        return enriched

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.read.enrich_rows_with_retrieval_asset_url",
        _enrich,
    )
    result = await read(explore_ctx, _read_table(CHUNK_IMAGE))
    assert result.error is None
    assert result.media == [
        {"type": "image_url", "url": "https://cdn.example/chart.png"}
    ]
    assert "[Image: https://cdn.example/chart.png]" in result.text


@pytest.mark.asyncio
async def test_read_filesystem_image_does_not_fill_media(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _enrich(rows, *, log_context):  # noqa: ANN001
        enriched = []
        for row in rows:
            item = dict(row)
            if item.get("chunk_type") == "image":
                item["asset_url"] = "filesystem:///tmp/chart.png"
            enriched.append(item)
        return enriched

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.read.enrich_rows_with_retrieval_asset_url",
        _enrich,
    )
    result = await read(explore_ctx, _read_table(CHUNK_IMAGE))
    assert result.error is None
    assert result.media == []
    assert "[Image: filesystem:///tmp/chart.png]" in result.text


@pytest.mark.asyncio
async def test_mount_table_hit_renders_html_not_path(
    explore_ctx: ToolContext,
) -> None:
    hits, media = await mount_explore_hits(
        explore_ctx,
        [
            {
                "document_id": DOC_ID,
                "chunk_id": CHUNK_SMALL,
                "chunk_type": "table",
                "content": SMALL_HTML,
                "file_path": "tables/small.html",
                "chunk_metadata": {"summary": "dose table summary"},
                "job_result_id": REV_ID,
                "job_id": JOB_ID,
                "section_path": PATH_ROOT,
                "source_file_name": FILE_NAME,
                "snippet": "dose table summary",
            }
        ],
        char_budget=explore_ctx.budget.max_chars,
    )
    assert media == []
    assert "<table" in hits[0]["rendered"]
    assert "30 mg" in hits[0]["rendered"]
    assert "tables/small.html" not in hits[0]["rendered"]


@pytest.mark.asyncio
async def test_mount_body_hit_includes_connected_table(
    explore_ctx: ToolContext,
) -> None:
    hits, media = await mount_explore_hits(
        explore_ctx,
        [
            {
                "document_id": DOC_ID,
                "chunk_id": CHUNK_TEXT,
                "chunk_type": "text",
                "content": "intro body [tables/small.html]",
                "chunk_metadata": {
                    "connect_to": [
                        {
                            "target": CHUNK_SMALL,
                            "relation": "embeds",
                            "ref": "[tables/small.html]",
                        }
                    ]
                },
                "job_result_id": REV_ID,
                "job_id": JOB_ID,
                "section_path": PATH_INTRO,
                "source_file_name": FILE_NAME,
                "snippet": "intro body",
            }
        ],
        char_budget=explore_ctx.budget.max_chars,
    )
    assert media == []
    assert f"mounted table chunk_id={CHUNK_SMALL}:" in hits[0]["rendered"]
    assert "<table" in hits[0]["rendered"]
    assert "30 mg" in hits[0]["rendered"]


@pytest.mark.asyncio
async def test_mount_body_hit_includes_connected_https_image_media(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _enrich(rows, *, log_context):  # noqa: ANN001
        enriched = []
        for row in rows:
            item = dict(row)
            if item.get("chunk_type") == "image":
                item["asset_url"] = "https://cdn.example/chart.png"
            enriched.append(item)
        return enriched

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.explore_mount.enrich_rows_with_retrieval_asset_url",
        _enrich,
    )
    hits, media = await mount_explore_hits(
        explore_ctx,
        [
            {
                "document_id": DOC_ID,
                "chunk_id": CHUNK_TEXT,
                "chunk_type": "text",
                "content": "intro body",
                "chunk_metadata": {
                    "connect_to": [
                        {
                            "target": CHUNK_IMAGE,
                            "relation": "embeds",
                            "ref": "[images/x.png]",
                        }
                    ]
                },
                "job_result_id": REV_ID,
                "job_id": JOB_ID,
                "section_path": PATH_INTRO,
                "source_file_name": FILE_NAME,
                "snippet": "intro body",
            }
        ],
        char_budget=explore_ctx.budget.max_chars,
    )
    assert f"mounted image chunk_id={CHUNK_IMAGE}:" in hits[0]["rendered"]
    assert media == [{"type": "image_url", "url": "https://cdn.example/chart.png"}]


@pytest.mark.asyncio
async def test_recall_mounts_connected_table(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _term_rows(*_args, **_kwargs):  # noqa: ANN001
        return [
            {
                "chunk_id": CHUNK_TEXT,
                "document_id": DOC_ID,
                "section_id": "sec_intro",
                "section_path": PATH_INTRO,
                "source_file_name": FILE_NAME,
                "chunk_type": "text",
                "snippet": "intro body",
                "content": "intro body [tables/small.html]",
                "file_path": None,
                "chunk_metadata": {
                    "connect_to": [
                        {
                            "target": CHUNK_SMALL,
                            "relation": "embeds",
                            "ref": "[tables/small.html]",
                        }
                    ]
                },
                "job_result_id": REV_ID,
                "job_id": JOB_ID,
            }
        ]

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.recall._term_channel_rows",
        _term_rows,
    )
    result = await recall(explore_ctx, {"query": "intro", "channels": ["term"]})
    assert result.error is None
    assert f"mounted table chunk_id={CHUNK_SMALL}:" in result.text
    assert "<table" in result.text
    assert "30 mg" in result.text


@pytest.mark.asyncio
async def test_recall_mounts_table_hit(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _term_rows(*_args, **_kwargs):  # noqa: ANN001
        return [
            {
                "chunk_id": CHUNK_SMALL,
                "document_id": DOC_ID,
                "section_id": "sec_root",
                "section_path": PATH_ROOT,
                "source_file_name": FILE_NAME,
                "chunk_type": "table",
                "snippet": "dose table summary",
                "content": SMALL_HTML,
                "file_path": "tables/small.html",
                "chunk_metadata": {"summary": "dose table summary"},
                "job_result_id": REV_ID,
                "job_id": JOB_ID,
            }
        ]

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.recall._term_channel_rows",
        _term_rows,
    )
    result = await recall(explore_ctx, {"query": "dose", "channels": ["term"]})
    assert result.error is None
    assert f"chunk_id={CHUNK_SMALL}" in result.text
    assert "<table" in result.text
    assert "30 mg" in result.text
    assert "tables/small.html" not in result.text


def _table_path_hit(*, summary: str | None) -> dict:
    metadata = {"summary": summary} if summary is not None else {}
    return {
        "chunk_id": CHUNK_SMALL,
        "document_id": DOC_ID,
        "section_id": "sec_root",
        "section_path": PATH_ROOT,
        "source_file_name": FILE_NAME,
        "chunk_type": "table",
        "content": "tables/small.html",
        "file_path": "tables/small.html",
        "chunk_metadata": metadata,
        "job_result_id": REV_ID,
        "job_id": JOB_ID,
        "score": 1.0,
    }


@pytest.mark.asyncio
async def test_recall_path_content_table_snippet_uses_summary_not_path(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _discovery(*_args, **_kwargs):  # noqa: ANN001
        return DiscoveryResult(
            status="discovery_done",
            payload={"fused_rows": [_table_path_hit(summary="dose table summary")]},
        )

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.recall.map_unit_discovery",
        _discovery,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.table_grid.load_table_html",
        lambda _row: SMALL_HTML,
    )
    result = await recall(explore_ctx, {"query": "dose", "channels": ["path_content"]})
    assert result.error is None
    assert "dose table summary" in result.text
    assert "<table" in result.text
    assert "tables/small.html" not in result.text


@pytest.mark.asyncio
async def test_recall_path_content_table_snippet_empty_without_summary(
    explore_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _discovery(*_args, **_kwargs):  # noqa: ANN001
        return DiscoveryResult(
            status="discovery_done",
            payload={"fused_rows": [_table_path_hit(summary=None)]},
        )

    monkeypatch.setattr(
        "shared.services.retrieval.agent_tools.tools.recall.map_unit_discovery",
        _discovery,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.table_grid.load_table_html",
        lambda _row: SMALL_HTML,
    )
    result = await recall(explore_ctx, {"query": "dose", "channels": ["path_content"]})
    assert result.error is None
    assert "<table" in result.text
    assert "tables/small.html" not in result.text
    assert ": ''" not in result.text


class _GrepConnectedRow:
    def all(self) -> list[tuple[object, ...]]:
        return [
            (
                CHUNK_TEXT,
                DOC_ID,
                "text",
                "intro body 30 mg mention",
                "intro body [tables/small.html]",
                None,
                {
                    "connect_to": [
                        {
                            "target": CHUNK_SMALL,
                            "relation": "embeds",
                            "ref": "[tables/small.html]",
                        }
                    ]
                },
                REV_ID,
                JOB_ID,
                PATH_INTRO,
                FILE_NAME,
                1,
            )
        ]


class _GrepThenSession:
    def __init__(self, session: Session, first_result: object) -> None:
        self._session = session
        self._first = first_result
        self.execute_count = 0

    async def execute(self, statement):  # noqa: ANN001
        self.execute_count += 1
        if self.execute_count == 1:
            return self._first
        return self._session.execute(statement)


@pytest.mark.asyncio
async def test_grep_mounts_connected_table(explore_ctx: ToolContext) -> None:
    db = _GrepThenSession(explore_ctx.db._session, _GrepConnectedRow())
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        db_factory=_unused_db_factory,
    )
    result = await grep(ctx, {"pattern": "intro body"})
    assert result.error is None
    assert f"mounted table chunk_id={CHUNK_SMALL}:" in result.text
    assert "<table" in result.text
    assert "30 mg" in result.text
    assert db.execute_count == 2

