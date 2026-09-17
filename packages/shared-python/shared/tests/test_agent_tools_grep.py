"""Regression for ``corpus.grep`` concurrent COUNT + row fetch.

COUNT stays on the tool's call session; rows use a second session from the
same ``db_factory`` so both queries can run at once. Output must stay the
same as sequential execution.
"""

from __future__ import annotations

import asyncio
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
from shared.services.retrieval.agent_tools.tools.grep import (
    _content_search,
    _terms_from_args,
    grep,
)


class _CountResult:
    def scalar_one(self) -> int:
        return 2


class _RowsResult:
    def all(self) -> list[tuple[str, str, str, str, str, str]]:
        return [
            ("chunk_a", "doc_a", "text", "alpha HFrEF body", "guide.pdf / Intro", "guide.pdf"),
            ("chunk_b", "doc_b", "text", "other HFrEF note", "notes.pdf / Leaf", "notes.pdf"),
        ]


class _RecordingDb:
    def __init__(self, result: object, *, delay: float) -> None:
        self.result = result
        self.delay = delay
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.execute_count = 0

    async def execute(self, _statement):  # noqa: ANN001
        self.execute_count += 1
        self.started_at = asyncio.get_running_loop().time()
        await asyncio.sleep(self.delay)
        self.finished_at = asyncio.get_running_loop().time()
        return self.result


@pytest.mark.asyncio
async def test_grep_runs_count_and_rows_on_two_connections() -> None:
    count_db = _RecordingDb(_CountResult(), delay=0.05)
    rows_db = _RecordingDb(_RowsResult(), delay=0.05)

    @asynccontextmanager
    async def rows_factory():
        yield rows_db

    ctx = ToolContext(
        db=count_db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=rows_factory,
    )
    result = await grep(ctx, {"pattern": "HFrEF"})

    assert result.error is None
    assert result.payload["total_matches"] == 2
    assert [row["chunk_id"] for row in result.payload["results"]] == ["chunk_a", "chunk_b"]
    assert result.refs == [
        {"document_id": "doc_a", "chunk_id": "chunk_a"},
        {"document_id": "doc_b", "chunk_id": "chunk_b"},
    ]
    assert count_db.execute_count == 1
    assert rows_db.execute_count == 1
    assert count_db.started_at is not None and rows_db.started_at is not None
    assert count_db.finished_at is not None and rows_db.finished_at is not None
    assert count_db.started_at < rows_db.finished_at
    assert rows_db.started_at < count_db.finished_at
    assert result.text.startswith("total_matches=2 returned=2")


@pytest.mark.asyncio
async def test_grep_requires_pattern_or_patterns() -> None:
    # Error path returns before any db access, so a never-entered factory is
    # enough here — mirrors the other tests' db_factory shape for consistency.
    @asynccontextmanager
    async def rows_factory():
        yield _RecordingDb(_RowsResult(), delay=0.0)

    ctx = ToolContext(
        db=_RecordingDb(_CountResult(), delay=0.0),  # type: ignore[arg-type]
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
