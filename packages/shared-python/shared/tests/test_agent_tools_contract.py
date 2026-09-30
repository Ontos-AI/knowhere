"""Schema contract: closed objects, field descriptions, unknown/old names error."""

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

from shared.services.retrieval.agent_explore.config import FINISH_TOOL_SCHEMA
from shared.services.retrieval.agent_tools import REGISTRY, ToolContext, load_corpus_overview_text
from shared.services.retrieval.agent_tools.snippet import build_row, format_row


@asynccontextmanager
async def _unused_db_factory():
    raise AssertionError("schema validation must not open a database")
    yield  # pragma: no cover


def _ctx() -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user_id="user_contract",
        namespace="default",
        db_factory=_unused_db_factory,
    )


def _walk_objects(schema: object, path: str) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    if not isinstance(schema, dict):
        return found
    if schema.get("type") == "object" or "properties" in schema:
        found.append((path, schema))
        for name, child in (schema.get("properties") or {}).items():
            found.extend(_walk_objects(child, f"{path}.{name}"))
    if "items" in schema:
        found.extend(_walk_objects(schema["items"], f"{path}[]"))
    for key in ("anyOf", "oneOf", "allOf"):
        for index, child in enumerate(schema.get(key) or []):
            found.extend(_walk_objects(child, f"{path}/{key}[{index}]"))
    return found


def test_every_schema_object_closes_unknown_keys() -> None:
    for spec in REGISTRY.all():
        for path, obj in _walk_objects(spec.json_schema, spec.name):
            assert obj.get("additionalProperties") is False, path
    for path, obj in _walk_objects(FINISH_TOOL_SCHEMA, "finish"):
        assert obj.get("additionalProperties") is False, path


def test_every_schema_property_has_a_description() -> None:
    for spec in REGISTRY.all():
        for path, obj in _walk_objects(spec.json_schema, spec.name):
            for name, child in (obj.get("properties") or {}).items():
                if not isinstance(child, dict):
                    continue
                if child.get("type") == "object" or "properties" in child:
                    continue
                assert child.get("description"), f"{path}.{name}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "args", "legal_names"),
    [
        (
            "corpus.grep",
            {"pattern": "x", "document_ids": ["doc_a"]},
            ["chunk_types", "context_chars", "limit", "pattern", "patterns", "scope"],
        ),
        (
            "corpus.grep",
            {"pattern": "x", "max_results": 3},
            ["chunk_types", "context_chars", "limit", "pattern", "patterns", "scope"],
        ),
        (
            "corpus.recall",
            {"query": "x", "top_k": 5},
            ["chunk_types", "limit", "query", "scope"],
        ),
        (
            "corpus.recall",
            {"query": "x", "channels": ["term"]},
            ["chunk_types", "limit", "query", "scope"],
        ),
        (
            "corpus.outline",
            {"scope": [{"document_id": "doc_a"}], "document_id": "doc_a"},
            ["depth", "scope"],
        ),
        (
            "corpus.outline",
            {"scope": [{"document_id": "doc_a"}], "path_prefix": "A"},
            ["depth", "scope"],
        ),
        (
            "corpus.assets",
            {"document_ids": ["doc_a"]},
            ["host_of", "query", "scope", "type"],
        ),
        (
            "corpus.read",
            {
                "refs": [{"document_id": "doc_a", "section_path": "A / B"}],
                "section_paths": ["A / B"],
            },
            ["include_assets", "mode", "refs"],
        ),
        (
            "corpus.node_filter",
            {
                "scope": [{"document_id": "doc_a"}],
                "predicates": [{"field": "path", "terms": ["A"]}],
                "document_ids": ["doc_a"],
            },
            ["chunk_types", "predicates", "scope"],
        ),
    ],
)
async def test_unknown_or_old_names_fail_with_legal_parameter_list(
    tool_name: str, args: dict, legal_names: list[str]
) -> None:
    result = await REGISTRY.dispatch(tool_name, _ctx(), args)
    assert result.error is not None
    assert "unknown argument" in result.error
    for name in legal_names:
        assert name in result.error


@pytest.mark.asyncio
async def test_nested_unknown_key_names_the_path() -> None:
    result = await REGISTRY.dispatch(
        "corpus.grep",
        _ctx(),
        {"pattern": "x", "scope": [{"document_id": "doc_a", "bogus": 1}]},
    )
    assert result.error is not None
    assert "scope[0]" in result.error
    assert "document_id" in result.error
    assert "section_path" in result.error


def test_old_parameter_names_are_absent_from_docs_and_descriptions() -> None:
    schema_text = load_corpus_overview_text()
    for stale in ("document_ids", "path_prefix", "max_results"):
        assert stale not in schema_text
        for spec in REGISTRY.all():
            assert stale not in spec.description
            assert stale not in spec.json_schema.get("properties", {})


_ROW_KEYS = {
    "kind",
    "title",
    "document_id",
    "section_path",
    "chunk_id",
    "summary",
    "snippet",
    "score",
    "is_hit",
    "depth",
    "hosted",
}


def test_format_row_is_the_shared_search_line() -> None:
    row = build_row(
        kind="text",
        document_id="doc_a",
        section_path="guide.pdf / Intro",
        title="guide.pdf",
        snippet="hello",
    )
    assert set(row) == _ROW_KEYS
    line = format_row(row)
    assert line.startswith("- [text] guide.pdf | document_id=doc_a section_path=")
    assert "snippet:" in line


@pytest.mark.asyncio
async def test_chunk_types_enum_rejects_old_names() -> None:
    result = await REGISTRY.dispatch(
        "corpus.grep",
        _ctx(),
        {"pattern": "x", "chunk_types": ["body"]},
    )
    assert result.error is not None
    assert "chunk_types" in result.error
    assert "unknown argument" not in result.error


@pytest.mark.asyncio
async def test_limit_below_minimum_is_rejected() -> None:
    result = await REGISTRY.dispatch(
        "corpus.grep",
        _ctx(),
        {"pattern": "x", "limit": 0},
    )
    assert result.error is not None
    assert "limit" in result.error


@pytest.mark.asyncio
async def test_recall_blank_query_is_rejected() -> None:
    result = await REGISTRY.dispatch("corpus.recall", _ctx(), {"query": "   "})
    assert result.error == "recall requires query"


@pytest.mark.asyncio
async def test_read_ref_requires_exactly_one_address() -> None:
    both = await REGISTRY.dispatch(
        "corpus.read",
        _ctx(),
        {
            "refs": [
                {
                    "document_id": "doc_a",
                    "section_path": "guide.pdf / Intro",
                    "chunk_id": "chunk_a",
                }
            ]
        },
    )
    assert both.error is not None
    assert "section_path" in both.error
    assert "chunk_id" in both.error

    neither = await REGISTRY.dispatch(
        "corpus.read",
        _ctx(),
        {"refs": [{"document_id": "doc_a"}]},
    )
    assert neither.error is not None
    assert "section_path" in neither.error or "chunk_id" in neither.error


def test_chunk_types_and_bounds_are_in_schema() -> None:
    grep = REGISTRY.get("corpus.grep")
    recall = REGISTRY.get("corpus.recall")
    outline = REGISTRY.get("corpus.outline")
    assert grep is not None and recall is not None and outline is not None
    for spec in (grep, recall):
        items = spec.json_schema["properties"]["chunk_types"]["items"]
        assert items["enum"] == ["text", "page", "image", "table"]
        assert spec.json_schema["properties"]["limit"]["minimum"] == 1
    assert grep.json_schema["properties"]["context_chars"]["minimum"] == 1
    assert outline.json_schema["properties"]["depth"]["minimum"] == 0
