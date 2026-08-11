"""Unit test for AgentStep stamp_step_detail."""

from __future__ import annotations

import time

from shared.services.retrieval.nav.nav_token_budget import (
    nav_token_episode,
    record_episode_tokens,
    stamp_step_detail,
)


def test_stamp_step_detail_tracks_delta_and_elapsed() -> None:
    with nav_token_episode():
        t0 = time.perf_counter()
        first = stamp_step_detail({"action": "a"}, t0=t0)
        assert first["tokens_used_total"] == 0
        assert first["tokens_used_delta"] == 0
        assert first["elapsed_ms"] >= 0
        record_episode_tokens({"total_tokens": 40})
        second = stamp_step_detail({"action": "b"})
        assert second["tokens_used_total"] == 40
        assert second["tokens_used_delta"] == 40
        record_episode_tokens({"total_tokens": 10})
        third = stamp_step_detail({"action": "c"})
        assert third["tokens_used_total"] == 50
        assert third["tokens_used_delta"] == 10
