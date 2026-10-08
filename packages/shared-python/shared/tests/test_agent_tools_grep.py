"""Regression coverage for ``corpus.grep`` scope, term blocks, and folding."""

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

from shared.services.retrieval.agent_explore.evidence_pool import EvidencePool
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
    sort_order: int = 0,
) -> tuple[object, ...]:
    return (
        chunk_id,
        document_id,
        "text",
        term,
        sort_order,
        section_path,
        source_file_name,
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
                sort_order=1,
            ),
            _text_row(
                chunk_id="chunk_b",
                document_id="doc_a",
                term="other HFrEF note",
                section_path="notes.pdf / Leaf",
                source_file_name="notes.pdf",
                sort_order=2,
            ),
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


def _unused_factory():
    @asynccontextmanager
    async def factory():
        raise AssertionError("grep must not open a second database connection")
        yield  # pragma: no cover

    return factory


@pytest.mark.asyncio
async def test_grep_one_term_lists_section_rows() -> None:
    db = _SequencedDb([_RowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "HFrEF"})

    assert result.error is None
    assert result.payload["details"] == {}
    assert [row["document_id"] for row in result.payload["rows"]] == ["doc_a", "doc_a"]
    assert [row["section_path"] for row in result.payload["rows"]] == [
        "guide.pdf / Intro",
        "notes.pdf / Leaf",
    ]
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
    assert "count(*) OVER ()" not in sql
    assert " LIMIT " not in sql
    assert "term_search_text" in matched_sql
    assert "document_chunks.user_id = 'user_grep'" in matched_sql
    assert "document_chunks.namespace = 'default'" in matched_sql
    assert "lower(" in matched_sql.lower()
    assert "coalesce" in matched_sql.lower()
    assert " like " in matched_sql.lower()
    assert "ilike" not in matched_sql.lower()
    assert "~*" not in matched_sql
    assert "%hfref%" in matched_sql.lower()
    assert result.text.startswith("HFrEF\n")
    assert "total_matches=" not in result.text
    assert "- [text] guide.pdf | document_id=doc_a section_path=guide.pdf / Intro" in result.text
    assert "chunk_id=" not in result.text
    assert "还有" not in result.text


@pytest.mark.asyncio
async def test_grep_scope_narrows_to_document_and_subtree() -> None:
    db = _SequencedDb([_ScalarsResult([_scoped_document("doc_a")]), _RowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
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
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(
        ctx, {"pattern": "HFrEF", "scope": [{"document_id": "doc_missing"}]}
    )

    assert result.error is not None
    assert "unknown document_id: doc_missing" in result.error
    assert db.execute_count == 1


@pytest.mark.asyncio
async def test_grep_limit_folds_leftover_sections_per_document() -> None:
    db = _SequencedDb([_RowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "HFrEF", "limit": 1})

    assert [row["section_path"] for row in result.payload["rows"]] == [
        "guide.pdf / Intro"
    ]
    assert result.refs == [{"document_id": "doc_a", "chunk_id": "chunk_a"}]
    assert result.text.startswith("HFrEF\n")
    assert "notes.pdf / Leaf" not in result.text
    assert "notes.pdf 还有 1 节命中 — scope=[{document_id: doc_a}]" in result.text


@pytest.mark.asyncio
async def test_grep_empty_result_is_blank() -> None:
    db = _SequencedDb([_EmptyRowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "missing"})

    assert result.payload["rows"] == []
    assert result.payload["details"] == {}
    assert result.refs == []
    assert result.text == ""


@pytest.mark.asyncio
async def test_grep_requires_pattern_or_patterns() -> None:
    ctx = ToolContext(
        db=_SequencedDb([_RowsResult()]),  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
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
                1,
                "guide.pdf / Root",
                "guide.pdf",
            )
        ]


@pytest.mark.asyncio
async def test_grep_table_hit_is_address_and_snippet() -> None:
    db = _SequencedDb([_TableRowsResult(), _EmptyRowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "30 mg"})

    assert result.error is None
    assert (
        "- [table] guide.pdf | document_id=doc_a (not in any section) chunk_id=chunk_table"
        in result.text
    )
    assert "30 mg" in result.text
    assert "<table>" not in result.text
    assert result.text.startswith("30 mg\n")


@pytest.mark.asyncio
async def test_grep_matches_table_via_term_search_text_not_content_path() -> None:
    db = _SequencedDb([_TableRowsResult(), _EmptyRowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "dose table summary"})

    assert result.error is None
    assert result.payload["rows"][0]["chunk_id"] == "chunk_table"
    assert "dose table summary" in result.text
    assert "<table>" not in result.text


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
                    1,
                    "guide.pdf / Root",
                    "guide.pdf",
                )
            ]

    db = _SequencedDb([_ImageRows(), _EmptyRowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "chart of dose"})
    assert result.error is None
    assert result.payload["rows"][0]["chunk_id"] == "chunk_image"
    assert "chart of dose" in result.text
    assert "[Image:" not in result.text


@pytest.mark.asyncio
async def test_grep_scope_does_not_scan_table_cells() -> None:
    db = _SequencedDb([_ScalarsResult([_scoped_document("doc_a")]), _EmptyRowsResult()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(
        ctx, {"pattern": "30 mg", "scope": [{"document_id": "doc_a"}]}
    )
    assert db.execute_count == 2
    assert result.payload["rows"] == []
    assert result.text == ""
    assert "cell=" not in result.text


class _DrownRows:
    def all(self) -> list[tuple[object, ...]]:
        generic = [
            _text_row(
                chunk_id=f"chunk_g{i}",
                document_id="doc_bbb",
                term=f"body 血管紧张素 {i}",
                section_path=f"HTN_Guide.pdf / {i}",
                source_file_name="HTN_Guide.pdf",
                sort_order=i,
            )
            for i in range(4)
        ]
        rare = [
            _text_row(
                chunk_id="chunk_rare",
                document_id="doc_aaa",
                term="Surgery TGFBR2 pathogenic variant",
                section_path="ESC_Marfan.pdf / 4.7.1 Marfan syndrome",
                source_file_name="ESC_Marfan.pdf",
                sort_order=1,
            )
        ]
        return generic + rare


@pytest.mark.asyncio
async def test_grep_rare_term_is_listed_before_generic_and_generic_folds() -> None:
    db = _SequencedDb([_DrownRows()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(
        ctx, {"patterns": ["TGFBR2", "血管紧张素"], "limit": 2}
    )

    assert result.error is None
    assert result.text.startswith("TGFBR2\n")
    assert "ESC_Marfan.pdf / 4.7.1 Marfan syndrome" in result.text
    assert [row["section_path"] for row in result.payload["rows"]] == [
        "ESC_Marfan.pdf / 4.7.1 Marfan syndrome",
        "HTN_Guide.pdf / 0",
    ]
    assert "HTN_Guide.pdf / 1" not in result.text
    assert "HTN_Guide.pdf 还有 3 节命中 — scope=[{document_id: doc_bbb}]" in result.text
    tgf_at = result.text.index("TGFBR2")
    ang_at = result.text.index("血管紧张素")
    assert tgf_at < ang_at


@pytest.mark.asyncio
async def test_grep_visible_rows_are_readable_folded_rows_are_not() -> None:
    db = _SequencedDb([_DrownRows()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(
        ctx, {"patterns": ["TGFBR2", "血管紧张素"], "limit": 2}
    )
    pool = EvidencePool()
    assert pool.issue("corpus.grep", result) == []
    pool.begin_round()
    readable = pool.readable()
    assert ("doc_aaa", "ESC_Marfan.pdf / 4.7.1 Marfan syndrome") in readable
    assert ("doc_bbb", "HTN_Guide.pdf / 0") in readable
    assert ("doc_bbb", "HTN_Guide.pdf / 1") not in readable
    assert ("doc_bbb", "HTN_Guide.pdf / 3") not in readable


@pytest.mark.asyncio
async def test_grep_same_section_two_chunks_keeps_first() -> None:
    class _DupSection:
        def all(self) -> list[tuple[object, ...]]:
            return [
                _text_row(
                    chunk_id="chunk_first",
                    document_id="doc_a",
                    term="first TGFBR2 line",
                    section_path="guide.pdf / Intro",
                    source_file_name="guide.pdf",
                    sort_order=1,
                ),
                _text_row(
                    chunk_id="chunk_second",
                    document_id="doc_a",
                    term="second TGFBR2 line",
                    section_path="guide.pdf / Intro",
                    source_file_name="guide.pdf",
                    sort_order=2,
                ),
            ]

    db = _SequencedDb([_DupSection()])
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        user_id="user_grep",
        namespace="default",
        db_factory=_unused_factory(),
    )
    result = await grep(ctx, {"pattern": "TGFBR2"})
    assert len(result.payload["rows"]) == 1
    assert result.refs == [{"document_id": "doc_a", "chunk_id": "chunk_first"}]
    assert "first TGFBR2 line" in result.text
    assert "second TGFBR2 line" not in result.text
