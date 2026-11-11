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
