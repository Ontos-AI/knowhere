from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS_ROOT))

from _debug_token_ledger import (
    aggregate_stage_deltas,
    empty_token_usage,
    token_usage_delta,
)

_TEXT_TRACK_STAGES = ("profile", "mineru", "hierarchy", "full")


def test_token_usage_delta_and_reuse_merge_match_stage_costs_semantics() -> None:
    prev = empty_token_usage()
    after_profile = {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "calls": 1,
        "by_model": {"vlm": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "calls": 1}},
        "by_task": {
            "document_agent.coarse_profile": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "calls": 1,
            }
        },
    }
    profile_delta = token_usage_delta(prev, after_profile)
    assert profile_delta["total_tokens"] == 14
    assert profile_delta["by_task"]["document_agent.coarse_profile"]["calls"] == 1

    # reuse-profile starts a fresh tracker; only the new stage exists in this run.
    reuse_run = token_usage_delta(empty_token_usage(), {
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "total_tokens": 10,
        "calls": 1,
        "by_model": {
            "text": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10, "calls": 1}
        },
        "by_task": {
            "parser.heading_hierarchy": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
                "calls": 1,
            }
        },
    })
    ledger = {
        "stages": {
            "profile": {"token_usage": profile_delta},
            "hierarchy": {"token_usage": reuse_run},
        }
    }
    merged = aggregate_stage_deltas(ledger, _TEXT_TRACK_STAGES)
    assert merged["total_tokens"] == 24
    assert merged["calls"] == 2
    assert merged["by_task"]["document_agent.coarse_profile"]["total_tokens"] == 14
    assert merged["by_task"]["parser.heading_hierarchy"]["total_tokens"] == 10
