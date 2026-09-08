from __future__ import annotations

from shared.services.retrieval.nav.nav_orchestrate import _relit_map
from shared.services.retrieval.nav.nav_types import NavState


def test_relit_map_reuses_same_query_within_episode(monkeypatch) -> None:
    calls: list[str] = []

    def fake_relight_map_for_query(*_args, **kwargs):
        calls.append(str(kwargs["query"]))
        return {"section": 1.0}, {"unit": 2.0}, ["section"]

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_map_scores.relight_map_for_query",
        fake_relight_map_for_query,
    )
    state = NavState(doc_id="", query="retrieval")
    config = type("Config", (), {"collect_top_k": 6})()

    with _relit_map(None, state, config, query="retrieval"):
        pass
    with _relit_map(None, state, config, query="retrieval"):
        pass

    assert calls == ["retrieval"]
    assert state.relit_map_cache["retrieval"][0] == {"section": 1.0}
