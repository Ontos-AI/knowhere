"""Unit tests for map-nav AgentStep → decision_trace mapping."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.trace.mapnav import (
    build_decision_trace,
    episode_selected_doc_ids,
    episode_selected_paths,
    episode_token_count,
    episode_workflow_plan,
)


def _step(action: str, detail: dict, step_idx: int = 1) -> SimpleNamespace:
    return SimpleNamespace(step_idx=step_idx, action=action, detail=detail)


def test_build_decision_trace_maps_three_layers_and_terminal() -> None:
    episode = SimpleNamespace(
        stop_reason="completed",
        evidence_chars_actual=1200,
        steps=[
            _step(
                "query_plan",
                {
                    "reason": "plan it",
                    "n_subgoals": 1,
                    "fallback": False,
                    "plan": {
                        "subgoals": [{"id": "s1"}],
                        "coverage_checklist": [{"id": "c1", "fact": "x"}],
                        "map_coverage": "sufficient",
                        "raw": '{"ok":true}',
                        "reason": "plan it",
                    },
                    "token_limit": 100000,
                    "tokens_used_total": 100,
                    "tokens_used_delta": 100,
                    "elapsed_ms": 10,
                },
            ),
            _step(
                "harvest",
                {
                    "subgoal_id": "s1",
                    "scope": "sec_a",
                    "depth": 0,
                    "collect_ids": ["C1"],
                    "dispatch_ids": [],
                    "search_assets": [],
                    "confidence": {"C1": 0.9},
                    "reason": "hit",
                    "projection_chars": 200,
                    "legal_actions_preview": ["C1 collect"],
                    "visible_section_ids": ["sec_a"],
                    "model": "deepseek-v4-flash",
                    "raw": "{}",
                    "token_limit": 100000,
                    "tokens_used_total": 250,
                    "tokens_used_delta": 150,
                    "elapsed_ms": 20,
                },
                step_idx=2,
            ),
            _step(
                "harvest",
                {
                    "subgoal_id": "s1",
                    "scope": "sec_b",
                    "depth": 1,
                    "collect_ids": ["C2"],
                    "dispatch_ids": [],
                    "search_assets": [],
                    "reason": "child",
                    "token_limit": 100000,
                    "tokens_used_total": 300,
                    "tokens_used_delta": 50,
                    "elapsed_ms": 5,
                },
                step_idx=3,
            ),
            _step(
                "plan_control",
                {
                    "global": "done",
                    "reason": "covered",
                    "subgoals": {"s1": {"decision": "accept", "note": "ok"}},
                    "token_limit": 100000,
                    "tokens_used_total": 400,
                    "tokens_used_delta": 100,
                    "elapsed_ms": 8,
                },
                step_idx=4,
            ),
        ],
    )
    steps = build_decision_trace(episode, evidence_char_budget=12000, n_refs=2)
    phases = [s.phase for s in steps]
    assert phases[0] == "plan"
    assert phases[1] == "harvest"
    assert phases[2] == "harvest"
    assert phases[3] == "plan_control"
    assert phases[-1] == "terminal"
    assert steps[0].agent == "planner"
    assert steps[0].decision["action"] == "plan_query"
    assert steps[0].result["coverage_checklist"][0]["id"] == "c1"
    assert steps[0].budget["tokens_used_delta"] == 100
    assert steps[1].observation["projection_chars"] == 200
    assert steps[1].decision["collect_ids"] == ["C1"]
    assert steps[2].parent_step_index == steps[1].step_index
    assert steps[3].decision["action"] == "done"
    assert steps[-1].result["n_refs"] == 2
    assert steps[-1].result["layer_llm_steps"]["planner"] >= 1
    assert steps[-1].result["layer_llm_steps"]["control"] >= 1


def test_episode_helpers() -> None:
    episode = SimpleNamespace(
        steps=[
            _step(
                "query_plan",
                {
                    "plan": {"subgoals": [{"id": "s1"}], "raw": "x"},
                    "tokens_used_total": 12,
                },
            ),
            _step("harvest", {"tokens_used_total": 40}),
        ]
    )
    refs = [
        {"document_id": "d1", "section_path": "A / B", "chunk_id": "c1"},
        {"document_id": "d1", "section_path": "A / B", "chunk_id": "c2"},
        {"document_id": "d2", "section_path": "C", "chunk_id": "c3"},
    ]
    assert episode_workflow_plan(episode)["subgoals"][0]["id"] == "s1"
    assert episode_token_count(episode) == 40
    assert episode_selected_paths(episode, refs) == ["A / B", "C"]
    assert episode_selected_doc_ids(refs) == ["d1", "d2"]
