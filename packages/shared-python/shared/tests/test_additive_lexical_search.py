"""Verify native namespace search sends no build roster."""

from shared.services.retrieval.lexical.searchplan import NativeSearchPlan


def test_additive_search_uses_scoped_native_filters_and_a_live_cursor() -> None:
    first = NativeSearchPlan.buildPayload(
        "contentTokens", buildIds=None, queryTokens=("heading",), limit=100,
        unitIds=None, extraFilters=({"term": {"documentId": "doc-allowed"}},),
    )
    assert first["query"] == {"bool": {
        "filter": [{"term": {"documentId": "doc-allowed"}}],
        "must": [{"match": {"contentTokens": {"query": "heading", "operator": "or"}}}],
    }}
    assert first["sort"][-1] == {"buildId": "asc"}
    nextPage = NativeSearchPlan.buildPayload(
        "contentTokens", buildIds=None, queryTokens=("heading",), limit=100,
        unitIds=None, searchAfter=(3.0, "doc-allowed", 0, "section", "attempt"),
    )
    assert nextPage["search_after"] == (3.0, "doc-allowed", 0, "section", "attempt")
