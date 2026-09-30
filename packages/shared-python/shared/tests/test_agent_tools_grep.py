"""Regression coverage for ``corpus.grep`` scope and exact-count query."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from sqlalchemy.dialects import postgresql

from shared.services.retrieval.agent_tools.registry import REGISTRY, ToolContext
from shared.services.retrieval.agent_tools.snippet import build_row, format_row
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
        None,
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


class _ScalarsResult:
    """Fakes ``(await db.execute(...)).scalars().all()`` for ORM lookups
    (``resolve_scope``'s ``Document`` query)."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> "_ScalarsResult":
        return self

    def all(self) -> list[Any]:
        return self._items


class _SequencedDb:
    """Replays one canned result per ``execute()`` call, in order.

    ``resolve_scope`` (when a call passes ``scope``) issues its own
    ``Document`` lookup before grep's own rows query — this fake replaces
    the single-result ``_RecordingDb`` so each call gets the result meant
    for it.
    """

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.execute_count = 0
        self.statements: list[Any] = []

    async def execute(self, statement: Any):  # noqa: ANN001
        self.execute_count += 1
        self.statements.append(statement)
        return self.results[self.execute_count - 1]

    @property
    def statement(self) -> Any:
        return self.statements[0] if self.statements else None

    @property
    def rows_statement(self) -> Any:
        """The final (rows) statement — last one issued."""
        return self.statements[-1] if self.statements else None


def _scoped_document(document_id: str, job_result_id: str = "jr_a") -> SimpleNamespace:
    return SimpleNamespace(document_id=document_id, current_job_result_id=job_result_id)


@pytest.mark.asyncio
async def test_grep_runs_one_scoped_query_with_exact_total() -> None:
    db = _SequencedDb([_RowsResult()])

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
    result = await grep(ctx, {"pattern": "HFrEF"})

    assert result.error is None
    assert result.payload["details"]["total_matches"] == 2
    assert [row["document_id"] for row in result.payload["rows"]] == ["doc_a", "doc_a"]
    assert [row["chunk_id"] for row in result.payload["rows"]] == [None, None]
    assert result.refs == [
        {"document_id": "doc_a", "chunk_id": "chunk_a"},
        {"document_id": "doc_a", "chunk_id": "chunk_b"},
    ]
    assert db.execute_count == 1
    sql = str(
        db.rows_statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    matched_sql = sql.split(")\n SELECT", 1)[0]
    assert "count(*) OVER ()" in sql
    assert "term_search_text" in matched_sql
    assert "document_chunks.user_id = 'user_grep'" in matched_sql
    assert "document_chunks.namespace = 'default'" in matched_sql
    assert "lower(" in matched_sql.lower()
    assert "coalesce" in matched_sql.lower()
    assert " like " in matched_sql.lower()
    assert "ilike" not in matched_sql.lower()
    assert "~*" not in matched_sql
    assert "%hfref%" in matched_sql.lower()
    assert result.text.startswith("total_matches=2 returned=2")
    assert "- [text] guide.pdf | document_id=doc_a section_path=guide.pdf / Intro" in result.text
    assert "chunk_id=" not in result.text


@pytest.mark.asyncio
async def test_grep_scope_narrows_to_document_and_subtree() -> None:
    db = _SequencedDb([_ScalarsResult([_scoped_document("doc_a")]), _RowsResult()])

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
    result = await grep(
        ctx, {"pattern": "HFrEF", "scope": [{"document_id": "doc_a"}]}
    )

    assert result.error is None
    assert db.execute_count == 2
    sql = str(
        db.rows_statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    matched_sql = sql.split(")\n SELECT", 1)[0]
    assert "document_chunks.document_id = 'doc_a'" in matched_sql


@pytest.mark.asyncio
async def test_grep_unknown_scope_document_fails_the_call() -> None:
    db = _SequencedDb([_ScalarsResult([])])

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
    result = await grep(
        ctx, {"pattern": "HFrEF", "scope": [{"document_id": "doc_missing"}]}
    )

    assert result.error is not None
    assert "unknown document_id: doc_missing" in result.error
    assert db.execute_count == 1


@pytest.mark.asyncio
async def test_grep_exact_total_survives_row_limit() -> None:
    db = _SequencedDb([_LimitedRowsResult()])

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
    result = await grep(ctx, {"pattern": "HFrEF", "limit": 1})

    assert result.payload["details"]["total_matches"] == 2
    assert len(result.payload["rows"]) == 1
    assert result.text.startswith("total_matches=2 returned=1")


@pytest.mark.asyncio
async def test_grep_empty_result_reports_zero_total() -> None:
    db = _SequencedDb([_EmptyRowsResult()])

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

    assert result.payload["rows"] == []
    assert result.payload["details"]["total_matches"] == 0
    assert result.refs == []
    assert result.text == "total_matches=0 returned=0"


@pytest.mark.asyncio
async def test_grep_requires_pattern_or_patterns() -> None:
    @asynccontextmanager
    async def rows_factory():
        yield _SequencedDb([_RowsResult()])

    ctx = ToolContext(
        db=_SequencedDb([_RowsResult()]),  # type: ignore[arg-type]
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
    assert spec.json_schema["additionalProperties"] is False
    assert "document_ids" not in spec.json_schema["properties"]
    assert "max_results" not in spec.json_schema["properties"]
    assert "limit" in spec.json_schema["properties"]
    assert "scope" in spec.json_schema["properties"]


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


def test_term_search_single_literal_uses_indexed_lower_like() -> None:
    compiled, predicate = _term_search(["HFrEF"])
    assert compiled.search("alpha HFrEF body")
    sql = _sql(predicate)
    assert "lower(" in sql.lower()
    assert "coalesce" in sql.lower()
    assert " like " in sql.lower()
    assert "ilike" not in sql.lower()
    assert "term_search_text" in sql
    assert "%hfref%" in sql.lower()
    assert "~*" not in sql


def test_term_search_ors_literal_terms_in_sql_and_matcher() -> None:
    compiled, predicate = _term_search(["foo", "bar"])
    assert compiled.search("xxx foo yyy")
    assert compiled.search("xxx bar yyy")
    assert compiled.search("xxx BAR yyy")
    assert compiled.search("xxx baz yyy") is None
    sql = _sql(predicate)
    lowered = sql.lower()
    assert lowered.count(" like ") == 2
    assert "ilike" not in lowered
    assert " or " in lowered
    assert "term_search_text" in sql
    assert "%foo%" in lowered
    assert "%bar%" in lowered
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
                None,
                "guide.pdf / Root",
                "guide.pdf",
                1,
            )
        ]


@pytest.mark.asyncio
async def test_grep_table_hit_text_includes_chunk_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _SequencedDb([_TableRowsResult(), _EmptyRowsResult()])

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
        "- [table] guide.pdf | document_id=doc_a section_path=guide.pdf / Root "
        "(no host section) chunk_id=chunk_table"
        in result.text
    )
    assert "<table>" in result.text
    assert "30 mg" in result.text
    assert "tables/dose.html" not in result.text.split("\n", 1)[-1]


@pytest.mark.asyncio
async def test_grep_table_download_failure_does_not_fail_whole_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table download error must not crash the grep call — grep still
    returns its total_matches/text; only that hit's rendering becomes a
    warning note instead of raising."""
    from shared.services.retrieval.hydration.table_grid import TableDownloadError

    db = _SequencedDb([_TableRowsResult(), _EmptyRowsResult()])

    @asynccontextmanager
    async def unused_factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    def _raise_download_error(_row):
        raise TableDownloadError("S3 301: PermanentRedirect")

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.table_grid.load_table_html",
        _raise_download_error,
    )
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=unused_factory,
    )
    result = await grep(ctx, {"pattern": "30 mg"})

    assert result.error is None
    assert result.payload["details"]["total_matches"] == 1
    assert "table unavailable" in result.text
    assert "download failed" in result.text


@pytest.mark.asyncio
async def test_grep_matches_table_via_term_search_text_not_content_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _SequencedDb([_TableRowsResult(), _EmptyRowsResult()])

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
    assert result.payload["details"]["total_matches"] == 1
    assert result.payload["rows"][0]["chunk_id"] == "chunk_table"
    assert "dose table summary" in result.text


def test_format_row_body_omits_chunk_id() -> None:
    assert format_row(
        build_row(
            kind="text",
            document_id="doc_a",
            section_path="guide.pdf / Intro",
            title="guide.pdf",
            snippet="alpha",
        )
    ) == (
        "- [text] guide.pdf | document_id=doc_a section_path=guide.pdf / Intro\n"
        "  snippet: 'alpha'"
    )


def test_format_row_omits_empty_snippet() -> None:
    assert (
        format_row(
            build_row(
                kind="table",
                document_id="doc_a",
                section_path="guide.pdf / Root",
                title="guide.pdf",
                chunk_id="chunk_table",
            )
        )
        == "- [table] guide.pdf | document_id=doc_a section_path=guide.pdf / Root chunk_id=chunk_table"
    )


def test_format_row_asset_includes_chunk_id_and_score() -> None:
    assert format_row(
        build_row(
            kind="table",
            document_id="doc_a",
            section_path="guide.pdf / Root",
            title="guide.pdf",
            chunk_id="chunk_table",
            snippet="30 mg",
            score=1.5,
        )
    ) == (
        "- [table] guide.pdf | document_id=doc_a section_path=guide.pdf / Root "
        "chunk_id=chunk_table score=1.5\n  snippet: '30 mg'"
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
                    None,
                    "guide.pdf / Root",
                    "guide.pdf",
                    1,
                )
            ]

    db = _SequencedDb([_ImageRows(), _EmptyRowsResult()])

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
    assert result.payload["rows"][0]["chunk_id"] == "chunk_image"
    assert "chart of dose" in result.text
    assert "[Image:" in result.text


@pytest.mark.asyncio
async def test_grep_scope_does_not_scan_table_cells() -> None:
    db = _SequencedDb([_ScalarsResult([_scoped_document("doc_a")]), _EmptyRowsResult()])

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
    result = await grep(
        ctx, {"pattern": "30 mg", "scope": [{"document_id": "doc_a"}]}
    )
    assert db.execute_count == 2
    assert result.payload["details"]["total_matches"] == 0
    assert "cell=" not in result.text
