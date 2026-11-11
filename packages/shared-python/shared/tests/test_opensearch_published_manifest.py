"""Both native search channels can resolve one immutable published list."""

from shared.services.retrieval.lexical.searchplan import NativeSearchPlan


def test_build_lookup_replaces_inline_ids_for_both_channels() -> None:
    lookup = {"index": "knowhere-lexical-manifests", "id": "selected", "path": "buildIds"}

    for channel in ("pathTokens", "contentTokens"):
        payload = NativeSearchPlan.buildPayload(
            channel,
            buildIds=(),
            queryTokens=("receipt",),
            limit=10,
            unitIds=None,
            buildLookup=lookup,
        )
        assert payload["query"] == {
            "bool": {
                "filter": [{"terms": {"buildId": lookup}}],
                "must": [{"match": {channel: {"query": "receipt", "operator": "or"}}}],
            }
        }


def test_manifest_cache_keeps_filter_components_distinct() -> None:
    from shared.services.retrieval.cache_service import _cache_shape_digest

    common = {"query": "receipt", "top_k": 10, "exclude_sections": [],
              "published_manifest_id": "published-list"}
    assert _cache_shape_digest(**common, exclude_document_ids=["a", "b"]) != _cache_shape_digest(
        **common, exclude_document_ids=["a|b"],
    )
    assert _cache_shape_digest(**common, exclude_document_ids=[], signal_paths=["billing,legal"]) != _cache_shape_digest(
        **common, exclude_document_ids=[], signal_paths=["billing", "legal"],
    )
