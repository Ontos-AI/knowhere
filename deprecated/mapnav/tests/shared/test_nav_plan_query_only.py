"""Planner is query-only; map lighting happens after the plan exists."""

from __future__ import annotations

from typing import Any

from shared.services.retrieval.nav.nav_orchestrate import seed_episode_map_scores_from_plan
from shared.services.retrieval.nav.nav_plan import (
    RetrievalPlan,
    Subgoal,
    _planner_system_prompt,
    fallback_plan,
)
from shared.services.retrieval.nav.nav_types import NavConfig, NavState


def test_planner_system_prompt_is_query_only() -> None:
    text = _planner_system_prompt(max_subgoals=3)
    assert "only the user query" in text.lower()
    assert "no document map" in text.lower()
    assert "folded" not in text.lower()
    assert "Hit" not in text
    assert "collect=C" not in text
    assert "whether the user query alone is enough" in text.lower()


def test_seed_episode_scores_plan_retrieval_queries(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_relight(ts: Any, *, doc_id: str, queries: list[str], top_k: int):
        seen["queries"] = list(queries)
        seen["top_k"] = top_k
        out = {}
        for i, q in enumerate(queries):
            out[q] = ({f"n{i}": 1.0}, {f"u{i}": 2.0}, [f"u{i}"])
        return out

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_map_scores.relight_maps_for_queries",
        fake_relight,
    )
    state = NavState(doc_id="", query="user original", task_type="unknown")
    state.relit_map_cache = {"stale": ({}, {}, [])}
    plan = RetrievalPlan(
        subgoals=[
            Subgoal(id="s1", need="a", retrieval_query="alpha terms"),
            Subgoal(id="s2", need="b", retrieval_query="beta terms"),
            Subgoal(id="s3", need="c", retrieval_query="alpha terms"),
        ]
    )
    n = seed_episode_map_scores_from_plan(
        object(), state, NavConfig(collect_top_k=7), plan
    )
    assert n == 2
    assert seen["queries"] == ["alpha terms", "beta terms"]
    assert seen["top_k"] == 7
    assert "stale" not in state.relit_map_cache
    assert state.map_scores == {"n0": 1.0}
    assert state.unit_scores == {"u0": 2.0}
    assert state.highlight_ids == ["u0"]
    assert "alpha terms" in state.relit_map_cache
    assert "beta terms" in state.relit_map_cache


def test_seed_episode_fallback_uses_user_query(monkeypatch: Any) -> None:
    seen: list[str] = []

    def fake_relight(ts: Any, *, doc_id: str, queries: list[str], top_k: int):
        seen.extend(queries)
        return {queries[0]: ({"n": 1.0}, {"u": 1.0}, ["u"])}

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_map_scores.relight_maps_for_queries",
        fake_relight,
    )
    state = NavState(doc_id="doc1", query="心血管", task_type="unknown")
    plan = RetrievalPlan(
        subgoals=[Subgoal(id="s1", need="should not score", retrieval_query="")]
    )
    n = seed_episode_map_scores_from_plan(
        object(), state, NavConfig(collect_top_k=5), plan
    )
    assert n == 1
    assert seen == ["心血管"]
    assert state.map_scores == {"n": 1.0}


def test_seed_skips_unbound_slot_queries(monkeypatch: Any) -> None:
    seen: list[str] = []

    def fake_relight(ts: Any, *, doc_id: str, queries: list[str], top_k: int):
        seen.extend(queries)
        return {q: ({f"n:{q}": 1.0}, {f"u:{q}": 1.0}, [f"u:{q}"]) for q in queries}

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_map_scores.relight_maps_for_queries",
        fake_relight,
    )
    state = NavState(doc_id="", query="user original", task_type="unknown")
    plan = RetrievalPlan(
        subgoals=[
            Subgoal(id="s1", need="a", retrieval_query="alpha"),
            Subgoal(
                id="s2",
                need="b",
                retrieval_query="beta {{s1.entity}}",
                depends_on=["s1"],
            ),
        ]
    )
    n = seed_episode_map_scores_from_plan(object(), state, NavConfig(), plan)
    assert n == 1
    assert seen == ["alpha"]
    assert "alpha" in state.relit_map_cache
    assert not any("beta" in q for q in state.relit_map_cache)


def test_wave_uses_seeded_cache_and_scores_missing_once(monkeypatch: Any) -> None:
    score_calls: list[list[str]] = []

    def fake_relight(ts: Any, *, doc_id: str, queries: list[str], top_k: int):
        score_calls.append(list(queries))
        return {
            q: ({f"n:{q}": 1.0}, {f"u:{q}": 1.0}, [f"u:{q}"])
            for q in queries
        }

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_map_scores.relight_maps_for_queries",
        fake_relight,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._apply_plan_control",
        lambda *args, **kwargs: {"done": True},
    )

    harvest_queries: list[str] = []

    def fake_harvest(ts: Any, state: NavState, config: NavConfig, **kwargs: Any):
        harvest_queries.append(str(kwargs.get("query") or ""))
        return type(
            "HR",
            (),
            {
                "n_policy_calls": 0,
                "visited_section_ids": [],
                "max_depth_hit": False,
                "reason": "test",
            },
        )()

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._harvest_after_node_filter",
        fake_harvest,
    )

    state = NavState(doc_id="", query="user original", task_type="unknown")
    plan = RetrievalPlan(
        subgoals=[
            Subgoal(id="s1", need="a", retrieval_query="alpha"),
            Subgoal(id="s2", need="b", retrieval_query="beta"),
        ]
    )
    state.retrieval_plan = plan
    seed_episode_map_scores_from_plan(object(), state, NavConfig(), plan)
    assert score_calls == [["alpha", "beta"]]

    from shared.services.retrieval.nav.nav_orchestrate import execute_plan

    execute_plan(object(), state, NavConfig(), episode_query="user original")
    # Wave relight must hit the seeded cache; no second full-corpus scoring.
    assert score_calls == [["alpha", "beta"]]
    assert harvest_queries == ["alpha", "beta"]


def test_run_nav_episode_scores_after_plan(monkeypatch: Any) -> None:
    order: list[str] = []

    class _TS:
        def sections_for_doc(self, _doc_id: str) -> list[str]:
            return ["sec_root"]

    monkeypatch.setattr(
        "shared.services.retrieval.nav._compat.load_llm_env",
        lambda: None,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav._compat.require_llm_env",
        lambda *_args, **_kwargs: None,
    )

    def fake_plan_query(state: NavState, config: NavConfig, **_kwargs: Any):
        order.append("plan")
        assert state.map_scores == {}
        assert state.unit_scores == {}
        return fallback_plan(state.query, reason="test_plan")

    def fake_seed(ts: Any, state: NavState, config: NavConfig, plan: RetrievalPlan):
        order.append("score")
        assert plan.subgoals
        state.map_scores = {"sec_root": 1.0}
        state.unit_scores = {"u1": 1.0}
        state.highlight_ids = ["u1"]
        return 1

    def fake_execute(ts: Any, state: NavState, config: NavConfig, **_kwargs: Any):
        order.append("orch")
        assert state.map_scores == {"sec_root": 1.0}
        return {"waves": []}

    class _Fill:
        scored_chunks = []
        kept_chunks = []
        evidence_text = ""
        evidence_chars_actual = 0
        n_chunks_kept = 0
        truncated_last = False

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_plan.plan_query",
        fake_plan_query,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate.seed_episode_map_scores_from_plan",
        fake_seed,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate.execute_plan",
        fake_execute,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_agent.pack_nav_evidence",
        lambda *_args, **_kwargs: _Fill(),
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_agent.uses_document_nodes",
        lambda _ts: False,
    )

    from shared.services.retrieval.nav.nav_agent import _run_nav_episode_body

    result = _run_nav_episode_body(
        None,
        "心血管",
        doc_id="doc1",
        budget_chars=2000,
        compose_answer=False,
        policy="llm",
        config=NavConfig(policy="llm"),
        toolspace=_TS(),
    )
    assert order == ["plan", "score", "orch"]
    assert result.section_ids == ["sec_root"]
