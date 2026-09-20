"""Regression coverage for ``corpus.grep`` scope and exact-count query."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from sqlalchemy.dialects import postgresql

from shared.services.retrieval.agent_tools.registry import REGISTRY, ToolContext
from shared.services.retrieval.agent_tools.snippet import format_search_hit_line
from shared.services.retrieval.agent_tools.tools.grep import (
    _term_search,
    _terms_from_args,
    grep,
)


def _text_row(
    *,
    chunk_id: str,
    document_id: str,
    term: str,
    section_path: str,
    source_file_name: str,
    total: int,
) -> tuple[object, ...]:
    return (
        chunk_id,
        document_id,
        "text",
        term,
        term,
        None,
        None,
        "jr_a",
        "job_a",
        section_path,
        source_file_name,
        total,
    )


class _RowsResult:
    def all(self) -> list[tuple[object, ...]]:
        return [
            _text_row(
                chunk_id="chunk_a",
                document_id="doc_a",
                term="alpha HFrEF body",
                section_path="guide.pdf / Intro",
                source_file_name="guide.pdf",
                total=2,
            ),
            _text_row(
                chunk_id="chunk_b",
                document_id="doc_a",
                term="other HFrEF note",
                section_path="notes.pdf / Leaf",
                source_file_name="notes.pdf",
                total=2,
            ),
        ]


class _LimitedRowsResult:
    def all(self) -> list[tuple[object, ...]]:
        return [
            _text_row(
                chunk_id="chunk_a",
                document_id="doc_a",
                term="alpha HFrEF body",
                section_path="guide.pdf / Intro",
                source_file_name="guide.pdf",
                total=2,
            )
        ]


class _EmptyRowsResult:
    def all(self) -> list[tuple[object, ...]]:
        return []


class _RecordingDb:
    def __init__(self, result: object) -> None:
        self.result = result
        self.execute_count = 0
        self.statement = None

    async def execute(self, statement):  # noqa: ANN001
        self.execute_count += 1
        if self.statement is None:
            self.statement = statement
        return self.result


@pytest.mark.asyncio
async def test_grep_runs_one_scoped_query_with_exact_total() -> None:
    db = _RecordingDb(_RowsResult())

    @asynccontextmanager
    async def rows_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=rows_factory,
    )
    result = await grep(ctx, {"pattern": "HFrEF", "document_ids": ["doc_a"]})

    assert result.error is None
    assert result.payload["total_matches"] == 2
    assert [row["chunk_id"] for row in result.payload["results"]] == ["chunk_a", "chunk_b"]
    assert result.refs == [
        {"document_id": "doc_a", "chunk_id": "chunk_a"},
        {"document_id": "doc_a", "chunk_id": "chunk_b"},
    ]
    assert db.execute_count == 1
    assert db.statement is not None
    sql = str(
        db.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    matched_sql = sql.split(")\n SELECT", 1)[0]
    assert "count(*) OVER ()" in sql
    assert "term_search_text" in matched_sql
    assert "document_chunks.user_id = 'user_grep'" in matched_sql
    assert "document_chunks.namespace = 'default'" in matched_sql
    assert "document_chunks.document_id IN ('doc_a')" in matched_sql
    assert "ilike" in matched_sql.lower()
    assert "~*" not in matched_sql
    assert result.text.startswith("total_matches=2 returned=2")
    assert "- [text] guide.pdf (doc_a) / guide.pdf / Intro:" in result.text
    assert "chunk_id=" not in result.text


@pytest.mark.asyncio
async def test_grep_exact_total_survives_row_limit() -> None:
    db = _RecordingDb(_LimitedRowsResult())

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "HFrEF", "max_results": 1})

    assert result.payload["total_matches"] == 2
    assert len(result.payload["results"]) == 1
    assert result.text.startswith("total_matches=2 returned=1")


@pytest.mark.asyncio
async def test_grep_empty_result_reports_zero_total() -> None:
    db = _RecordingDb(_EmptyRowsResult())

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "missing"})

    assert result.payload == {"total_matches": 0, "results": []}
    assert result.refs == []
    assert result.text == "total_matches=0 returned=0"


@pytest.mark.asyncio
async def test_grep_requires_pattern_or_patterns() -> None:
    # Error path returns before any db access, so a never-entered factory is
    # enough here — mirrors the other tests' db_factory shape for consistency.
    @asynccontextmanager
    async def rows_factory():
        yield _RecordingDb(_RowsResult())

    ctx = ToolContext(
        db=_RecordingDb(_RowsResult()),  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=rows_factory,
    )
    result = await grep(ctx, {})
    assert result.error == "grep requires pattern or patterns"

    result_empty_list = await grep(ctx, {"patterns": ["", "  "]})
    assert result_empty_list.error == "grep requires pattern or patterns"


def test_grep_schema_requires_pattern_or_patterns() -> None:
    spec = REGISTRY.get("corpus.grep")
    assert spec is not None
    assert spec.json_schema.get("required") in (None, [])
    assert spec.json_schema["anyOf"] == [
        {"required": ["pattern"]},
        {"required": ["patterns"]},
    ]
    assert "is_regex" not in spec.json_schema["properties"]


def test_terms_from_args_merges_pattern_and_patterns() -> None:
    assert _terms_from_args({}) == []
    assert _terms_from_args({"pattern": "  "}) == []
    assert _terms_from_args({"patterns": ["", "  "]}) == []
    assert _terms_from_args({"pattern": "HFrEF"}) == ["HFrEF"]
    assert _terms_from_args({"patterns": ["NT-proBNP", "BNP"]}) == ["NT-proBNP", "BNP"]
    assert _terms_from_args({"pattern": "BNP", "patterns": ["BNP", "NT-proBNP"]}) == [
        "BNP",
        "NT-proBNP",
    ]


def _sql(predicate: object) -> str:
    return str(
        predicate.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_term_search_single_literal_uses_ilike() -> None:
    compiled, predicate = _term_search(["HFrEF"])
    assert compiled.search("alpha HFrEF body")
    sql = _sql(predicate)
    assert "ilike" in sql.lower()
    assert "term_search_text" in sql
    assert "HFrEF" in sql
    assert "~*" not in sql


def test_term_search_ors_literal_terms_in_sql_and_matcher() -> None:
    compiled, predicate = _term_search(["foo", "bar"])
    assert compiled.search("xxx foo yyy")
    assert compiled.search("xxx bar yyy")
    assert compiled.search("xxx BAR yyy")
    assert compiled.search("xxx baz yyy") is None
    sql = _sql(predicate)
    assert sql.lower().count("ilike") == 2
    assert " or " in sql.lower()
    assert "term_search_text" in sql
    assert "foo" in sql
    assert "bar" in sql
    assert "~*" not in sql


class _TableRowsResult:
    def all(self) -> list[tuple[object, ...]]:
        return [
            (
                "chunk_table",
                "doc_a",
                "table",
                "dose table summary 30 mg",
                "tables/dose.html",
                "tables/dose.html",
                {"summary": "dose table summary 30 mg"},
                "jr_a",
                "job_a",
                "guide.pdf / Root",
                "guide.pdf",
                1,
            )
        ]


@pytest.mark.asyncio
async def test_grep_table_hit_text_includes_chunk_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RecordingDb(_TableRowsResult())

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.table_grid.load_table_html",
        lambda _row: "<table><tr><td>30 mg</td></tr></table>",
    )
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "30 mg"})

    assert result.error is None
    assert (
        "- [table] guide.pdf (doc_a) chunk_id=chunk_table / guide.pdf / Root:"
        in result.text
    )
    assert "<table>" in result.text
    assert "30 mg" in result.text
    assert "tables/dose.html" not in result.text.split("\n", 1)[-1]


@pytest.mark.asyncio
async def test_grep_matches_table_via_term_search_text_not_content_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RecordingDb(_TableRowsResult())

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.table_grid.load_table_html",
        lambda _row: "<table><tr><td>30 mg</td></tr></table>",
    )
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "dose table summary"})

    assert result.error is None
    assert result.payload["total_matches"] == 1
    assert result.payload["results"][0]["chunk_id"] == "chunk_table"
    assert "dose table summary" in result.text


def test_format_search_hit_line_body_omits_chunk_id() -> None:
    assert format_search_hit_line(
        source_file_name="guide.pdf",
        document_id="doc_a",
        section_path="guide.pdf / Intro",
        snippet="alpha",
        chunk_type="text",
    ) == "- [text] guide.pdf (doc_a) / guide.pdf / Intro: 'alpha'"


def test_format_search_hit_line_omits_empty_snippet() -> None:
    assert format_search_hit_line(
        source_file_name="guide.pdf",
        document_id="doc_a",
        section_path="guide.pdf / Root",
        snippet="",
        chunk_type="table",
        chunk_id="chunk_table",
    ) == "- [table] guide.pdf (doc_a) chunk_id=chunk_table / guide.pdf / Root"


def test_format_search_hit_line_asset_includes_chunk_id() -> None:
    assert format_search_hit_line(
        source_file_name="guide.pdf",
        document_id="doc_a",
        section_path="guide.pdf / Root",
        snippet="30 mg",
        chunk_type="table",
        chunk_id="chunk_table",
        score=1.5,
    ) == (
        "- [table] guide.pdf (doc_a) chunk_id=chunk_table / "
        "guide.pdf / Root score=1.5: '30 mg'"
    )


@pytest.mark.asyncio
async def test_grep_matches_image_description() -> None:
    class _ImageRows:
        def all(self) -> list[tuple[object, ...]]:
            return [
                (
                    "chunk_image",
                    "doc_a",
                    "image",
                    "a chart of dose",
                    "a chart of dose\n[images/x.png]",
                    "images/x.png",
                    None,
                    "jr_a",
                    "job_a",
                    "guide.pdf / Root",
                    "guide.pdf",
                    1,
                )
            ]

    db = _RecordingDb(_ImageRows())

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "chart of dose"})
    assert result.error is None
    assert result.payload["results"][0]["chunk_id"] == "chunk_image"
    assert "chart of dose" in result.text
    assert "[Image:" in result.text


@pytest.mark.asyncio
async def test_grep_document_ids_does_not_scan_table_cells() -> None:
    db = _RecordingDb(_EmptyRowsResult())

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "30 mg", "document_ids": ["doc_a"]})
    assert db.execute_count == 1
    assert result.payload["total_matches"] == 0
    assert "cell=" not in result.text
