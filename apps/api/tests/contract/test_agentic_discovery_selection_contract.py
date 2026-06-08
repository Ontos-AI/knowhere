from shared.services.retrieval.agentic.navigation.actions import build_legal_actions


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
        rejected_paths=set(),
        rejected_collect_paths=set(),
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
        rejected_paths=set(),
        rejected_collect_paths=set(),
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )

    assert action_set.collect == []


def test_discovery_hint_under_rejected_collect_path_is_not_repeated() -> None:
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
        rejected_paths=set(),
        rejected_collect_paths={"1、2016：机构行为助推行情演绎"},
        total_images=0,
        total_tables=0,
        budget_snapshot=None,
    )

    assert action_set.collect == []
