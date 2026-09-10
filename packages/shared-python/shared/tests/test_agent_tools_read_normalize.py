"""Pure tests for ``corpus.read``'s ``_normalize_read_refs`` argument shim.

Covers the live-observed malformed shape (flat ``document_id`` +
``section_paths``/``chunk_ids`` instead of nested ``refs``) alongside the
canonical shape, without touching the DB-backed ``read`` tool function
itself.
"""

from __future__ import annotations

from shared.services.retrieval.agent_tools.tools.read import _normalize_read_refs


def test_normalize_read_refs_passes_through_canonical_refs() -> None:
    args = {"refs": [{"document_id": "doc_1", "section_path": "A / B"}]}
    assert _normalize_read_refs(args) == args["refs"]


def test_normalize_read_refs_ignores_flat_shape_when_refs_present() -> None:
    args = {
        "refs": [{"document_id": "doc_1", "chunk_id": "chunk_1"}],
        "document_id": "doc_2",
        "section_paths": ["should not be used"],
    }
    assert _normalize_read_refs(args) == args["refs"]


def test_normalize_read_refs_flat_document_id_with_section_paths_list() -> None:
    # The exact malformed shape observed live (debug_agent_explore_episode.py,
    # cursor_sdk harness, 2026-09-10): document_id hoisted to the top level
    # alongside a plural section_paths array instead of nested refs.
    args = {
        "document_id": "doc_1cec16fef768",
        "section_paths": [
            "基层心血管病综合管理实践指南2020 / 3 危险因素干预",
            "基层心血管病综合管理实践指南2020 / 4 疾病干预",
        ],
    }
    assert _normalize_read_refs(args) == [
        {
            "document_id": "doc_1cec16fef768",
            "section_path": "基层心血管病综合管理实践指南2020 / 3 危险因素干预",
        },
        {
            "document_id": "doc_1cec16fef768",
            "section_path": "基层心血管病综合管理实践指南2020 / 4 疾病干预",
        },
    ]


def test_normalize_read_refs_flat_document_id_with_singular_section_path() -> None:
    args = {"document_id": "doc_1", "section_path": "A / B"}
    assert _normalize_read_refs(args) == [{"document_id": "doc_1", "section_path": "A / B"}]


def test_normalize_read_refs_flat_document_id_with_chunk_ids_list() -> None:
    args = {"document_id": "doc_1", "chunk_ids": ["c1", "c2"]}
    assert _normalize_read_refs(args) == [
        {"document_id": "doc_1", "chunk_id": "c1"},
        {"document_id": "doc_1", "chunk_id": "c2"},
    ]


def test_normalize_read_refs_flat_document_id_with_singular_chunk_id() -> None:
    args = {"document_id": "doc_1", "chunk_id": "c1"}
    assert _normalize_read_refs(args) == [{"document_id": "doc_1", "chunk_id": "c1"}]


def test_normalize_read_refs_combines_all_flat_variants() -> None:
    args = {
        "document_id": "doc_1",
        "section_path": "A",
        "section_paths": ["B"],
        "chunk_id": "c1",
        "chunk_ids": ["c2"],
    }
    assert _normalize_read_refs(args) == [
        {"document_id": "doc_1", "section_path": "A"},
        {"document_id": "doc_1", "section_path": "B"},
        {"document_id": "doc_1", "chunk_id": "c1"},
        {"document_id": "doc_1", "chunk_id": "c2"},
    ]


def test_normalize_read_refs_no_document_id_returns_empty() -> None:
    assert _normalize_read_refs({"section_paths": ["A"]}) == []


def test_normalize_read_refs_empty_args_returns_empty() -> None:
    assert _normalize_read_refs({}) == []


def test_normalize_read_refs_blank_strings_are_skipped() -> None:
    args = {
        "document_id": "doc_1",
        "section_path": "   ",
        "section_paths": ["", "  ", "A"],
    }
    assert _normalize_read_refs(args) == [{"document_id": "doc_1", "section_path": "A"}]
