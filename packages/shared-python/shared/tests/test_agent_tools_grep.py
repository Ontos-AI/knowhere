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
    _content_search,
    _terms_from_args,
    grep,
)


class _RowsResult:
    def all(self) -> list[tuple[str, str, str, str, str, str, int]]:
        return [
            (
                "chunk_a",
                "doc_a",
                "text",
                "alpha HFrEF body",
                "guide.pdf / Intro",
                "guide.pdf",
                2,
            ),
            (
                "chunk_b",
                "doc_a",
                "text",
                "other HFrEF note",
                "notes.pdf / Leaf",
                "notes.pdf",
                2,
            ),
        ]


class _LimitedRowsResult:
    def all(self) -> list[tuple[str, str, str, str, str, str, int]]:
        return [
            (
                "chunk_a",
                "doc_a",
                "text",
                "alpha HFrEF body",
                "guide.pdf / Intro",
                "guide.pdf",
                2,
            )
        ]


class _EmptyRowsResult:
    def all(self) -> list[tuple[str, str, str, str, str, str, int]]:
        return []


class _RecordingDb:
    def __init__(self, result: object) -> None:
        self.result = result
        self.execute_count = 0
        self.statement = None

    async def execute(self, statement):  # noqa: ANN001
        self.execute_count += 1
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
    assert "document_chunks.user_id = 'user_grep'" in matched_sql
    assert "document_chunks.namespace = 'default'" in matched_sql
    assert "document_chunks.document_id IN ('doc_a')" in matched_sql
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


def test_content_search_single_literal_uses_ilike() -> None:
    compiled, predicate = _content_search(["HFrEF"], is_regex=False)
    assert compiled.search("alpha HFrEF body")
    sql = _sql(predicate)
    assert "ilike" in sql.lower()
    assert "HFrEF" in sql
    assert "~*" not in sql


def test_content_search_ors_literal_terms_in_sql_and_matcher() -> None:
    compiled, predicate = _content_search(["foo", "bar"], is_regex=False)
    assert compiled.search("xxx foo yyy")
    assert compiled.search("xxx bar yyy")
    assert compiled.search("xxx BAR yyy")
    assert compiled.search("xxx baz yyy") is None
    sql = _sql(predicate)
    assert "~*" in sql
    assert "foo" in sql
    assert "bar" in sql
    assert "ilike" not in sql.lower()


class _TableRowsResult:
    def all(self) -> list[tuple[str, str, str, str, str, str, int]]:
        return [
            (
                "chunk_table",
                "doc_a",
                "table",
                "<table><tr><td>30 mg</td></tr></table>",
                "guide.pdf / Root",
                "guide.pdf",
                1,
            )
        ]


@pytest.mark.asyncio
async def test_grep_table_hit_text_includes_chunk_id() -> None:
    db = _RecordingDb(_TableRowsResult())

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
    result = await grep(ctx, {"pattern": "30 mg"})

    assert result.error is None
    assert (
        "- [table] guide.pdf (doc_a) chunk_id=chunk_table / guide.pdf / Root:"
        in result.text
    )


def test_format_search_hit_line_body_omits_chunk_id() -> None:
    assert format_search_hit_line(
        source_file_name="guide.pdf",
        document_id="doc_a",
        section_path="guide.pdf / Intro",
        snippet="alpha",
        chunk_type="text",
    ) == "- [text] guide.pdf (doc_a) / guide.pdf / Intro: 'alpha'"


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
