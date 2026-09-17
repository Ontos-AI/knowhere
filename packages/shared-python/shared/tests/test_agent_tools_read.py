"""Baseline and regression for ``corpus.read`` multi-ref resolution.

Locks the live ``read()`` return (text, payload errors, refs) for mixed
section_path / chunk_id refs, suffix resolution, and ambiguity errors.
FIX3 must not change these outputs.
"""

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
from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.tools.read import read

USER_ID = "user_read"
NAMESPACE = "default"
DOC_A = "doc_a"
DOC_B = "doc_b"
REV_A = "jr_a"
REV_B = "jr_b"
REV_OLD = "jr_old"
JOB_A = "job_a"
JOB_B = "job_b"
FILE_A = "guide.pdf"
FILE_B = "notes.pdf"

PATH_OVERVIEW = f"{FILE_A} / 1 Overview"
PATH_FINDINGS = f"{FILE_A} / 1 Overview / 1.1 Findings"
PATH_TREATMENT = f"{FILE_A} / 2 Treatment"
PATH_ANNEX_FINDINGS = f"{FILE_A} / Annex / 1.1 Findings"
PATH_INTRO = f"{FILE_B} / Intro"
PATH_UNIQUE = f"{FILE_B} / Special / Unique Leaf"

CHUNK_OVERVIEW = "chunk_overview"
CHUNK_FINDINGS = "chunk_findings"
CHUNK_TREATMENT = "chunk_treatment"
CHUNK_ANNEX = "chunk_annex"
CHUNK_INTRO = "chunk_intro"
CHUNK_UNIQUE = "chunk_unique"
CHUNK_OLD = "chunk_old"
CHUNK_IMAGE = "chunk_image"


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.execute_count = 0

    async def execute(self, statement):  # noqa: ANN001
        self.execute_count += 1
        return self._session.execute(statement)


@asynccontextmanager
async def _unused_db_factory():
    raise AssertionError("corpus.read should not open a second session")
    yield  # pragma: no cover


def _seed_session(session: Session) -> None:
    now = datetime(2026, 1, 1)
    session.add_all(
        [
            JobResult(
                id=REV_A,
                job_id=JOB_A,
                delivery_mode="inline",
                created_at=now,
                updated_at=now,
            ),
            JobResult(
                id=REV_B,
                job_id=JOB_B,
                delivery_mode="inline",
                created_at=now,
                updated_at=now,
            ),
            JobResult(
                id=REV_OLD,
                job_id="job_old",
                delivery_mode="inline",
                created_at=now,
                updated_at=now,
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
            Document(
                document_id=DOC_B,
                user_id=USER_ID,
                namespace=NAMESPACE,
                status="active",
                current_job_result_id=REV_B,
                source_file_name=FILE_B,
                parse_track="chunk",
                created_at=now,
                updated_at=now,
            ),
        ]
    )

    def section(
        section_id: str,
        document_id: str,
        job_result_id: str,
        path: str,
        sort_order: int,
    ) -> DocumentSection:
        return DocumentSection(
            section_id=section_id,
            user_id=USER_ID,
            namespace=NAMESPACE,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=path,
            section_level=1,
            sort_order=sort_order,
            created_at=now,
        )

    def chunk(
        row_id: str,
        chunk_id: str,
        document_id: str,
        job_result_id: str,
        section_id: str,
        content: str,
        *,
        chunk_type: str = "text",
        sort_order: int = 0,
        source_chunk_path: str | None = None,
    ) -> DocumentChunk:
        return DocumentChunk(
            id=row_id,
            chunk_id=chunk_id,
            user_id=USER_ID,
            namespace=NAMESPACE,
            document_id=document_id,
            job_result_id=job_result_id,
            section_id=section_id,
            chunk_type=chunk_type,
            content=content,
            sort_order=sort_order,
            source_chunk_path=source_chunk_path or chunk_id,
            created_at=now,
        )

    session.add_all(
        [
            section("sec_overview", DOC_A, REV_A, PATH_OVERVIEW, 1),
            section("sec_findings", DOC_A, REV_A, PATH_FINDINGS, 2),
            section("sec_treatment", DOC_A, REV_A, PATH_TREATMENT, 3),
            section("sec_annex", DOC_A, REV_A, PATH_ANNEX_FINDINGS, 4),
            section("sec_overview_old", DOC_A, REV_OLD, PATH_OVERVIEW, 1),
            section("sec_intro", DOC_B, REV_B, PATH_INTRO, 0),
            section("sec_unique", DOC_B, REV_B, PATH_UNIQUE, 1),
            section("sec_root_a", DOC_A, REV_A, f"{FILE_A} / Root", 0),
            chunk("row_overview", CHUNK_OVERVIEW, DOC_A, REV_A, "sec_overview", "overview body"),
            chunk("row_findings", CHUNK_FINDINGS, DOC_A, REV_A, "sec_findings", "findings body"),
            chunk("row_treatment", CHUNK_TREATMENT, DOC_A, REV_A, "sec_treatment", "treatment body"),
            chunk("row_annex", CHUNK_ANNEX, DOC_A, REV_A, "sec_annex", "annex findings"),
            chunk("row_old", CHUNK_OLD, DOC_A, REV_OLD, "sec_overview_old", "OLD overview"),
            chunk("row_intro", CHUNK_INTRO, DOC_B, REV_B, "sec_intro", "intro body"),
            chunk("row_unique", CHUNK_UNIQUE, DOC_B, REV_B, "sec_unique", "unique body"),
            chunk(
                "row_image",
                CHUNK_IMAGE,
                DOC_A,
                REV_A,
                "sec_root_a",
                "image bytes",
                chunk_type="image",
                source_chunk_path="images/x.png",
            ),
        ]
    )
    session.commit()


@pytest.fixture
def read_ctx() -> ToolContext:
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


def _read_kwargs(refs: list[dict[str, str]], *, mode: str = "self") -> dict:
    return {
        "refs": refs,
        "mode": mode,
        "include_assets": False,
        "resolve_same_as": False,
    }


def _assert_result(
    ctx: ToolContext,
    result,
    *,
    text: str,
    errors: list[str],
    refs: list[dict[str, str]],
    executes: int,
) -> None:
    assert result.error is None
    assert result.text == text
    assert result.payload["errors"] == errors
    assert result.refs == refs
    assert [row["chunk_id"] for row in result.payload["chunks"]] == [
        ref["chunk_id"] for ref in refs
    ]
    assert ctx.db.execute_count == executes  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_multi_exact_section_paths_preserve_ref_order(read_ctx: ToolContext) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [
                {"document_id": DOC_A, "section_path": PATH_TREATMENT},
                {"document_id": DOC_A, "section_path": PATH_OVERVIEW},
            ]
        ),
    )
    _assert_result(
        read_ctx,
        result,
        text=(
            f"### {FILE_A} ({DOC_A}) / {PATH_TREATMENT} [text]\n"
            "treatment body\n"
            f"### {FILE_A} ({DOC_A}) / {PATH_OVERVIEW} [text]\n"
            "overview body"
        ),
        errors=[],
        refs=[
            {"document_id": DOC_A, "chunk_id": CHUNK_TREATMENT},
            {"document_id": DOC_A, "chunk_id": CHUNK_OVERVIEW},
        ],
        executes=6,
    )


@pytest.mark.asyncio
async def test_read_suffix_unique_and_cross_document(read_ctx: ToolContext) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [
                {"document_id": DOC_B, "section_path": "Unique Leaf"},
                {"document_id": DOC_A, "section_path": PATH_TREATMENT},
            ]
        ),
    )
    _assert_result(
        read_ctx,
        result,
        text=(
            f"### {FILE_B} ({DOC_B}) / {PATH_UNIQUE} [text]\n"
            "unique body\n"
            f"### {FILE_A} ({DOC_A}) / {PATH_TREATMENT} [text]\n"
            "treatment body"
        ),
        errors=[],
        refs=[
            {"document_id": DOC_B, "chunk_id": CHUNK_UNIQUE},
            {"document_id": DOC_A, "chunk_id": CHUNK_TREATMENT},
        ],
        executes=7,
    )


@pytest.mark.asyncio
async def test_read_ambiguous_suffix_does_not_contaminate_other_refs(
    read_ctx: ToolContext,
) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [
                {"document_id": DOC_A, "section_path": "1.1 Findings"},
                {"document_id": DOC_B, "section_path": PATH_INTRO},
            ]
        ),
    )
    assert "ambiguous section_path '1.1 Findings'" in result.payload["errors"][0]
    assert PATH_FINDINGS in result.payload["errors"][0]
    assert PATH_ANNEX_FINDINGS in result.payload["errors"][0]
    _assert_result(
        read_ctx,
        result,
        text=(
            f"errors: {result.payload['errors'][0]}\n"
            f"### {FILE_B} ({DOC_B}) / {PATH_INTRO} [text]\n"
            "intro body"
        ),
        errors=result.payload["errors"],
        refs=[{"document_id": DOC_B, "chunk_id": CHUNK_INTRO}],
        executes=7,
    )


@pytest.mark.asyncio
async def test_read_unknown_path_and_unknown_document_keep_valid_ref(
    read_ctx: ToolContext,
) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [
                {"document_id": DOC_A, "section_path": "no such path"},
                {"document_id": "doc_missing", "section_path": PATH_INTRO},
                {"document_id": DOC_B, "section_path": PATH_INTRO},
            ]
        ),
    )
    _assert_result(
        read_ctx,
        result,
        text=(
            f"errors: unknown section_path for {DOC_A}: no such path; "
            "unknown document_id: doc_missing\n"
            f"### {FILE_B} ({DOC_B}) / {PATH_INTRO} [text]\n"
            "intro body"
        ),
        errors=[
            f"unknown section_path for {DOC_A}: no such path",
            "unknown document_id: doc_missing",
        ],
        refs=[{"document_id": DOC_B, "chunk_id": CHUNK_INTRO}],
        executes=7,
    )


@pytest.mark.asyncio
async def test_read_chunk_id_interleaved_with_section_path(read_ctx: ToolContext) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [
                {"document_id": DOC_A, "chunk_id": CHUNK_FINDINGS},
                {"document_id": DOC_B, "section_path": PATH_INTRO},
                {"document_id": DOC_A, "chunk_id": "missing_chunk"},
            ]
        ),
    )
    _assert_result(
        read_ctx,
        result,
        text=(
            f"errors: unknown chunk_id: missing_chunk in {DOC_A}\n"
            f"### {FILE_A} ({DOC_A}) / {PATH_FINDINGS} [text]\n"
            "findings body\n"
            f"### {FILE_B} ({DOC_B}) / {PATH_INTRO} [text]\n"
            "intro body"
        ),
        errors=[f"unknown chunk_id: missing_chunk in {DOC_A}"],
        refs=[
            {"document_id": DOC_A, "chunk_id": CHUNK_FINDINGS},
            {"document_id": DOC_B, "chunk_id": CHUNK_INTRO},
        ],
        executes=7,
    )


@pytest.mark.asyncio
async def test_read_descendants_does_not_include_sibling_suffix_or_old_revision(
    read_ctx: ToolContext,
) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [{"document_id": DOC_A, "section_path": PATH_OVERVIEW}],
            mode="descendants",
        ),
    )
    _assert_result(
        read_ctx,
        result,
        text=(
            f"### {FILE_A} ({DOC_A}) / {PATH_FINDINGS} [text]\n"
            "findings body\n"
            f"### {FILE_A} ({DOC_A}) / {PATH_OVERVIEW} [text]\n"
            "overview body"
        ),
        errors=[],
        refs=[
            {"document_id": DOC_A, "chunk_id": CHUNK_FINDINGS},
            {"document_id": DOC_A, "chunk_id": CHUNK_OVERVIEW},
        ],
        executes=5,
    )
    assert "OLD overview" not in result.text
    assert "annex findings" not in result.text
    assert CHUNK_IMAGE not in {ref["chunk_id"] for ref in result.refs}


@pytest.mark.asyncio
async def test_read_duplicate_section_refs_emit_twice(read_ctx: ToolContext) -> None:
    result = await read(
        read_ctx,
        _read_kwargs(
            [
                {"document_id": DOC_A, "section_path": PATH_TREATMENT},
                {"document_id": DOC_A, "section_path": PATH_TREATMENT},
            ]
        ),
    )
    _assert_result(
        read_ctx,
        result,
        text=(
            f"### {FILE_A} ({DOC_A}) / {PATH_TREATMENT} [text]\n"
            "treatment body\n"
            f"### {FILE_A} ({DOC_A}) / {PATH_TREATMENT} [text]\n"
            "treatment body"
        ),
        errors=[],
        refs=[
            {"document_id": DOC_A, "chunk_id": CHUNK_TREATMENT},
            {"document_id": DOC_A, "chunk_id": CHUNK_TREATMENT},
        ],
        executes=6,
    )
