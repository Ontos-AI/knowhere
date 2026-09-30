"""Strict section-subtree clause for map-unit discovery (recall scope)."""

from __future__ import annotations

from shared.services.retrieval.search.map_unit_discovery import (
    _build_section_subtree_clause,
)


def test_section_subtree_clause_empty_when_no_named_path() -> None:
    assert _build_section_subtree_clause(None) == ("", {})
    assert _build_section_subtree_clause([]) == ("", {})
    assert _build_section_subtree_clause([("doc_a", None)]) == ("", {})


def test_section_subtree_clause_is_exact_path_or_children() -> None:
    clause, params = _build_section_subtree_clause(
        [("doc_a", "guide.pdf / 1 Overview"), ("doc_b", None)]
    )
    assert clause.startswith("AND (")
    assert "ds.section_path = :_sec_path_0" in clause
    assert "ds.section_path LIKE :_sec_pathlike_0" in clause
    assert params["_sec_doc_0"] == "doc_a"
    assert params["_sec_path_0"] == "guide.pdf / 1 Overview"
    assert params["_sec_pathlike_0"] == "guide.pdf / 1 Overview / %"
    assert "(dmu.document_id = :_sec_doc_1)" in clause
    assert params["_sec_doc_1"] == "doc_b"
    assert "%" not in params["_sec_path_0"]
