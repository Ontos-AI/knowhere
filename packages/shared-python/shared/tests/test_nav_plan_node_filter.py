"""Planner parse compat for use_node_filter."""

from __future__ import annotations

from shared.services.retrieval.nav.nav_plan import (
    _planner_system_prompt,
    parse_retrieval_plan,
)


def test_parse_missing_filter_flag_defaults_false() -> None:
    plan = parse_retrieval_plan(
        {
            "reason": "old plan",
            "map_coverage": "sufficient",
            "subgoals": [
                {
                    "id": "s1",
                    "need": "profit",
                    "retrieval_query": "apple profit",
                }
            ],
        },
        query="apple profit",
    )
    assert len(plan.subgoals) == 1
    assert plan.subgoals[0].use_node_filter is False
    assert plan.subgoals[0].node_filter_predicates == []


def test_parse_use_node_filter_and_seed() -> None:
    plan = parse_retrieval_plan(
        {
            "reason": "enum tickers",
            "map_coverage": "sufficient",
            "subgoals": [
                {
                    "id": "s1",
                    "need": "apple profit",
                    "retrieval_query": "AAPL profit",
                    "use_node_filter": True,
                    "node_filter": [
                        {
                            "field": "path",
                            "terms": ["AAPL", "Apple"],
                            "match": "substring",
                        }
                    ],
                }
            ],
        },
        query="apple profit",
    )
    sg = plan.subgoals[0]
    assert sg.use_node_filter is True
    assert sg.node_filter_predicates == [
        {"field": "path", "terms": ["AAPL", "Apple"], "match": "substring"}
    ]


def test_planner_prompt_mentions_where_vs_fuzzy() -> None:
    text = _planner_system_prompt(max_subgoals=0)
    assert "use_node_filter" in text
    assert "fallback" in text
