from shared.services.retrieval.agentic.navigation.actions import build_legal_actions
from shared.services.retrieval.agentic.navigation.state import RejectionRecord


def _rejected(paths: dict[str, str]) -> dict[str, RejectionRecord]:
    """Build a rejection ledger from {path: reason}."""
    return {
        path: RejectionRecord(path=path, reason=reason, step=1, detail="")
        for path, reason in paths.items()
    }


def test_discovery_hint_is_projected_as_collect_action() -> None:
    action_set = build_legal_actions(
        items=[],
        current_scope=None,
        collected_paths=[],
        expanded_scopes=set(),
        discovery_hints=[
            {
                "section_path": "2 阶段性调整还是牛熊切换？ / 2.1 牛熊切换缘何开启?",
                "discovery_score": 0.82,
                "chunk_type": "text",
            }
        ],
        rejected={},
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )

    assert len(action_set.collect) == 1
    action = action_set.collect[0]
    assert action.id == "D1"
    assert action.action == "COLLECT"
    assert action.source == "discovery"
    assert action.path == "2 阶段性调整还是牛熊切换？ / 2.1 牛熊切换缘何开启?"
    assert action.score == 0.82


def test_discovery_hint_under_collected_path_is_not_repeated() -> None:
    action_set = build_legal_actions(
        items=[],
        current_scope=None,
        collected_paths=[
            {
                "path": "2 阶段性调整还是牛熊切换？",
                "hydrate_mode": "chunks",
            }
        ],
        expanded_scopes=set(),
        discovery_hints=[
            {
                "section_path": "2 阶段性调整还是牛熊切换？ / 2.1 牛熊切换缘何开启?",
                "discovery_score": 0.82,
            }
        ],
        rejected={},
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )

    assert action_set.collect == []


def test_discovery_hint_under_tool_adjudicated_path_is_not_repeated() -> None:
    """tool_adjudicated rejections are not revived by discovery this round."""
    action_set = build_legal_actions(
        items=[],
        current_scope=None,
        collected_paths=[],
        expanded_scopes=set(),
        discovery_hints=[
            {
                "section_path": "1、2016：机构行为助推行情演绎 / 二是英国“脱欧”影响下",
                "discovery_score": 0.7,
            }
        ],
        rejected=_rejected({
            "1、2016：机构行为助推行情演绎": "tool_adjudicated",
        }),
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )

    assert action_set.collect == []


# ─── NavigationState ledger (Phase 0) ───────────────────────────────────────


def test_mark_rejected_collect_records_tool_adjudicated_reason() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.mark_rejected_collect("Chapter 1", step=3, detail="no matching asset")

    assert "Chapter 1" in state.rejected
    record = state.rejected["Chapter 1"]
    assert record.reason == "tool_adjudicated"
    assert record.step == 3
    assert record.detail == "no matching asset"


def test_mark_rejected_if_unproductive_records_navigational_abandon() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.mark_rejected_if_unproductive("Chapter 2", step=5, detail="back_from_unproductive")

    assert state.rejected["Chapter 2"].reason == "navigational_abandon"


def test_tool_adjudicated_overrides_weak_abandon_record() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.mark_rejected_if_unproductive("Chapter 3", step=2)
    state.mark_rejected_collect("Chapter 3", step=4, detail="asset mismatch")

    # Stronger reason wins.
    assert state.rejected["Chapter 3"].reason == "tool_adjudicated"
    assert state.rejected["Chapter 3"].step == 4


def test_weak_abandon_does_not_overwrite_strong_record() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.mark_rejected_collect("Chapter 4", step=1)
    state.mark_rejected_if_unproductive("Chapter 4", step=5)

    assert state.rejected["Chapter 4"].reason == "tool_adjudicated"
    assert state.rejected["Chapter 4"].step == 1


def test_coverage_helpers_derive_from_collected_paths() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.add_collected(
        {"path": "A", "hydrate_mode": "chunks", "confidence": 0.9},
        step=1,
        scope_context=None,
    )
    state.add_collected(
        {"path": "B", "hydrate_mode": "outline", "confidence": 0.6},
        step=2,
        scope_context=None,
    )
    # A path upgraded from outline to full counts as covered, not outline.
    state.add_collected(
        {"path": "C", "hydrate_mode": "outline", "confidence": 0.5},
        step=3,
        scope_context=None,
    )
    state.add_collected(
        {"path": "C", "hydrate_mode": "chunks", "confidence": 0.8},
        step=4,
        scope_context=None,
    )

    assert state.covered_paths() == {"A", "C"}
    assert state.outline_paths() == {"B"}


def test_snapshot_delta_records_rejection_reasons() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.step_count = 2
    state.mark_rejected_if_unproductive("X", step=2)

    state.step_count = 3
    state.mark_rejected_collect("Y", step=3, detail="asset mismatch")

    delta = state.snapshot_delta(
        before_scope=None,
        expanded_before=set(),
        rejected_before={},
        collected_before_count=0,
    )
    rejected_added = {item["path"]: item["reason"] for item in delta["rejected_added"]}
    assert rejected_added == {"X": "navigational_abandon", "Y": "tool_adjudicated"}


def test_rejected_paths_with_reason_partitions_by_label() -> None:
    from shared.services.retrieval.agentic.navigation.state import NavigationState

    state = NavigationState(
        document_id="d1",
        document_name="doc.pdf",
        job_result_id="j1",
    )
    state.mark_rejected_collect("A", step=1)
    state.mark_rejected_if_unproductive("B", step=1)
    state.mark_rejected_collect("C", step=1)

    assert state.rejected_paths_with_reason("tool_adjudicated") == {"A", "C"}
    assert state.rejected_paths_with_reason("navigational_abandon") == {"B"}


# ─── Reason-aware action filtering (T7-style regression) ────────────────────


def test_tool_adjudicated_rejection_blocks_collect_even_with_discovery() -> None:
    """A tool-adjudicated path stays out of COLLECT even when discovery hints it."""
    action_set = build_legal_actions(
        items=[],
        current_scope=None,
        collected_paths=[],
        expanded_scopes=set(),
        discovery_hints=[{"section_path": "X", "discovery_score": 0.95}],
        rejected=_rejected({"X": "tool_adjudicated"}),
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )
    assert action_set.collect == []


def test_navigational_abandon_is_revived_by_discovery_for_collect() -> None:
    """A soft-abandoned path CAN still be COLLECTed when discovery signals it."""
    action_set = build_legal_actions(
        items=[],
        current_scope=None,
        collected_paths=[],
        expanded_scopes=set(),
        discovery_hints=[{"section_path": "Y", "discovery_score": 0.9}],
        rejected=_rejected({"Y": "navigational_abandon"}),
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )
    assert any(action.path == "Y" for action in action_set.collect)


def test_navigational_abandon_suppresses_expand_without_discovery_signal() -> None:
    """EXPAND is suppressed for soft-abandoned scopes lacking any discovery signal."""
    items = [{"path": "Z", "level": 1, "is_leaf": False, "chunk_count": 5}]
    action_set = build_legal_actions(
        items=items,
        current_scope=None,
        collected_paths=[],
        expanded_scopes=set(),
        discovery_hints=[],
        rejected=_rejected({"Z": "navigational_abandon"}),
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )
    assert any(action.path == "Z" for action in action_set.collect)
    assert not any(action.path == "Z" for action in action_set.expand)


def test_navigational_abandon_revives_expand_with_discovery_signal() -> None:
    """EXPAND is offered for soft-abandoned scopes when a discovery signal exists."""
    items = [{"path": "Z", "level": 1, "is_leaf": False, "chunk_count": 5}]
    action_set = build_legal_actions(
        items=items,
        current_scope=None,
        collected_paths=[],
        expanded_scopes=set(),
        discovery_hints=[{"section_path": "Z / child", "discovery_score": 0.7}],
        rejected=_rejected({"Z": "navigational_abandon"}),
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )
    assert any(action.path == "Z" for action in action_set.expand)


def test_covered_path_excluded_from_actions() -> None:
    """Regression: a path already collected as full evidence is not re-offered."""
    items = [{"path": "A", "level": 1, "is_leaf": False, "chunk_count": 3}]
    action_set = build_legal_actions(
        items=items,
        current_scope=None,
        collected_paths=[{"path": "A", "hydrate_mode": "chunks"}],
        expanded_scopes=set(),
        discovery_hints=[{"section_path": "A", "discovery_score": 0.9}],
        rejected={},
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )
    assert not any(action.path == "A" for action in action_set.collect)
    assert not any(action.path == "A" for action in action_set.expand)

