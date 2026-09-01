"""Unit tests for deterministic WHERE node filter (in-memory hierarchy)."""

from __future__ import annotations

from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    NamespaceKnowhereProvider,
    SectionRow,
)
from shared.services.retrieval.nav.nav_node_filter import (
    apply_node_filter,
    field_predicate,
    node_filter,
    render_submap_observation,
)


def _section(
    section_id: str,
    parent: str | None,
    path: str,
    title: str,
    *,
    level: int,
    summary: str = "",
    order: int = 0,
) -> SectionRow:
    return SectionRow(
        section_id=section_id,
        parent_section_id=parent,
        section_path=path,
        section_title=title,
        section_level=level,
        summary=summary,
        sort_order=order,
    )


def _namespace_ts() -> ProviderToolSpace:
    apple = KnowhereProvider(
        doc_id="doc_apple",
        sections=[
            _section("sec_root_a", None, "Root", "Root", level=0, order=0),
            _section(
                "sec_q3",
                "sec_root_a",
                "Q3 Results",
                "Q3 Results",
                level=1,
                summary="Apple quarterly profit and revenue",
                order=1,
            ),
            _section(
                "sec_hw",
                "sec_root_a",
                "Hardware",
                "Hardware",
                level=1,
                summary="iPhone unit sales",
                order=2,
            ),
        ],
        units=(),
    )
    orange = KnowhereProvider(
        doc_id="doc_orange",
        sections=[
            _section("sec_root_o", None, "Root", "Root", level=0, order=0),
            _section(
                "sec_crop",
                "sec_root_o",
                "Crop Report",
                "Crop Report",
                level=1,
                summary="orange harvest yield",
                order=1,
            ),
        ],
        units=(),
    )
    provider = NamespaceKnowhereProvider(
        [apple, orange],
        titles={
            "doc_apple": "AAPL 10-K.pdf",
            "doc_orange": "Citrus Outlook.docx",
        },
    )
    return ProviderToolSpace(provider)


def test_path_filter_returns_complete_set_across_documents() -> None:
    ts = _namespace_ts()
    result = apply_node_filter(
        ts,
        ["doc_apple", "doc_orange"],
        node_filter([field_predicate("path", ["AAPL", "Apple"])]),
    )

    assert result.truncated is False
    assert result.failed_predicates == []
    assert result.matched_doc_ids == ["doc_apple"]
    assert "sec_q3" in result.matched_section_ids
    assert "sec_hw" in result.matched_section_ids
    assert "sec_crop" not in result.matched_section_ids
    assert result.cardinality == len(result.matched_section_ids)
    assert result.cardinality >= 2


def test_summary_filter_and_path_and_summary() -> None:
    ts = _namespace_ts()
    summary_only = apply_node_filter(
        ts,
        ["doc_apple", "doc_orange"],
        node_filter([field_predicate("summary", ["profit"])]),
    )
    assert summary_only.matched_section_ids == ["sec_q3"]

    both = apply_node_filter(
        ts,
        ["doc_apple", "doc_orange"],
        node_filter(
            [
                field_predicate("path", ["AAPL", "Apple"]),
                field_predicate("summary", ["profit"]),
            ]
        ),
    )
    assert both.matched_section_ids == ["sec_q3"]
    assert both.matched_doc_ids == ["doc_apple"]


def test_zero_hits_and_invalid_regex_recorded() -> None:
    ts = _namespace_ts()
    empty = apply_node_filter(
        ts,
        ["doc_apple", "doc_orange"],
        node_filter([field_predicate("path", ["zzz-not-present"])]),
    )
    assert empty.cardinality == 0
    assert empty.matched_section_ids == []
    assert empty.matched_doc_ids == []

    bad = apply_node_filter(
        ts,
        ["doc_apple", "doc_orange"],
        node_filter([field_predicate("path", ["(unclosed"], match="regex")]),
    )
    assert bad.cardinality == 0
    assert bad.matched_section_ids == []
    assert bad.failed_predicates == ["path:regex:invalid"]


def test_regex_or_terms_and_preview_budget() -> None:
    ts = _namespace_ts()
    result = apply_node_filter(
        ts,
        ["doc_apple", "doc_orange"],
        node_filter(
            [field_predicate("summary", ["profit|yield"], match="regex")]
        ),
    )
    assert set(result.matched_section_ids) == {"sec_q3", "sec_crop"}
    assert result.cardinality == 2

    preview = render_submap_observation(ts, result)
    assert preview.startswith("hits=2")
    assert "sec_q3" not in preview  # paths shown, not ids
    assert "Q3 Results" in preview
