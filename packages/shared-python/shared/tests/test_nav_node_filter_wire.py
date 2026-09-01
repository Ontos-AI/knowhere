"""Orchestrate pre-pass + allowed-scope projection tests."""

from __future__ import annotations

from typing import Any

from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    NamespaceKnowhereProvider,
    SectionRow,
)
from shared.services.retrieval.nav.nav_orchestrate import _execute_subgoal_harvest_once
from shared.services.retrieval.nav.nav_plan import RetrievalPlan, Subgoal
from shared.services.retrieval.nav.nav_projection import build_projection
from shared.services.retrieval.nav.nav_scope_filter import ScopeFilterOutcome
from shared.services.retrieval.nav.nav_types import NavConfig, NavState


def _section(
    section_id: str,
    parent: str | None,
    path: str,
    title: str,
    *,
    level: int,
    summary: str = "",
    order: int = 0,
) -> SectionRow:
    return SectionRow(
        section_id=section_id,
        parent_section_id=parent,
        section_path=path,
        section_title=title,
        section_level=level,
        summary=summary,
        sort_order=order,
    )


def _ts() -> ProviderToolSpace:
    apple = KnowhereProvider(
        doc_id="doc_apple",
        sections=[
            _section("sec_root_a", None, "Root", "Root", level=0, order=0),
            _section(
                "sec_q3",
                "sec_root_a",
                "Q3 Results",
                "Q3 Results",
                level=1,
                summary="profit",
                order=1,
            ),
            _section(
                "sec_hw",
                "sec_root_a",
                "Hardware",
                "Hardware",
                level=1,
                summary="iphone",
                order=2,
            ),
        ],
        units=(),
    )
    return ProviderToolSpace(
        NamespaceKnowhereProvider([apple], titles={"doc_apple": "AAPL 10-K.pdf"})
    )


def _cfg(**kwargs: Any) -> NavConfig:
    data = {
        "enable_node_filter": True,
        "mode": "checklist",
        "map_mode": True,
        "llm_model": "test-model",
    }
    data.update(kwargs)
    return NavConfig.from_dict(data)


def _subgoal(*, use_filter: bool = True) -> Subgoal:
    return Subgoal(
        id="s1",
        need="apple profit",
        retrieval_query="apple profit",
        use_node_filter=use_filter,
    )


def test_collect_all_skips_harvest(monkeypatch: Any) -> None:
    harvest_calls: list[dict[str, Any]] = []
    collect_calls: list[list[str]] = []

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_scope_filter.run_scope_filter",
        lambda *args, **kwargs: ScopeFilterOutcome(
            decision="collect_all",
            settled_section_ids=["sec_q3"],
            settled_doc_ids=["doc_apple"],
            rounds=1,
            reason="small",
        ),
    )

    def fake_harvest(*args: Any, **kwargs: Any) -> Any:
        harvest_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("harvest should not run")

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_harvest.harvest",
        fake_harvest,
    )

    def fake_collect(ts: Any, state: NavState, chosen: Any, config: Any) -> dict[str, Any]:
        del ts, config
        batch = list((chosen.metadata or {}).get("batch_actions") or [chosen])
        sids = [str(a.section_id) for a in batch]
        collect_calls.append(sids)
        state.explicit_collect_ids.update(sids)
        state.collected_section_ids.update(sids)
        return {"collect_section_ids": sids}

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_navigate._apply_collect",
        fake_collect,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._relit_map",
        lambda *args, **kwargs: _nullcontext(),
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._wave_subgoal_result",
        lambda *args, **kwargs: type("Sig", (), {"chars_used": 0})(),
    )

    out = _execute_subgoal_harvest_once(
        _ts(),
        NavState(doc_id="doc_apple", query="apple profit"),
        _cfg(),
        RetrievalPlan(subgoals=[_subgoal()]),
        _subgoal(),
        steps_out=[],
    )
    assert harvest_calls == []
    assert collect_calls == [["sec_q3"]]
    assert out["harvest"]["reason"] == "node_filter_collect_all"


def test_scoped_harvest_passes_allowed_ids(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_scope_filter.run_scope_filter",
        lambda *args, **kwargs: ScopeFilterOutcome(
            decision="scoped_harvest",
            settled_section_ids=["sec_q3"],
            settled_doc_ids=["doc_apple"],
            rounds=1,
            reason="medium",
        ),
    )

    def fake_harvest(*args: Any, **kwargs: Any) -> Any:
        seen["allowed"] = kwargs.get("allowed_section_ids")
        return type(
            "HR",
            (),
            {
                "n_policy_calls": 1,
                "visited_section_ids": [],
                "max_depth_hit": False,
                "reason": "harvested",
            },
        )()

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_harvest.harvest",
        fake_harvest,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._relit_map",
        lambda *args, **kwargs: _nullcontext(),
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._wave_subgoal_result",
        lambda *args, **kwargs: type("Sig", (), {"chars_used": 0})(),
    )

    _execute_subgoal_harvest_once(
        _ts(),
        NavState(doc_id="doc_apple", query="apple profit"),
        _cfg(),
        RetrievalPlan(subgoals=[_subgoal()]),
        _subgoal(),
        steps_out=[],
    )
    assert seen["allowed"] == {"sec_q3"}


def test_fallback_matches_today(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_scope_filter.run_scope_filter",
        lambda *args, **kwargs: ScopeFilterOutcome(
            decision="fallback",
            reason="policy_fallback",
        ),
    )

    def fake_harvest(*args: Any, **kwargs: Any) -> Any:
        seen["allowed"] = kwargs.get("allowed_section_ids")
        return type(
            "HR",
            (),
            {
                "n_policy_calls": 1,
                "visited_section_ids": [],
                "max_depth_hit": False,
                "reason": "",
            },
        )()

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_harvest.harvest",
        fake_harvest,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._relit_map",
        lambda *args, **kwargs: _nullcontext(),
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._wave_subgoal_result",
        lambda *args, **kwargs: type("Sig", (), {"chars_used": 0})(),
    )

    _execute_subgoal_harvest_once(
        _ts(),
        NavState(doc_id="doc_apple", query="apple profit"),
        _cfg(),
        RetrievalPlan(subgoals=[_subgoal()]),
        _subgoal(),
        steps_out=[],
    )
    assert seen["allowed"] is None


def test_flag_off_skips_pre_pass(monkeypatch: Any) -> None:
    called = {"n": 0}

    def boom(*args: Any, **kwargs: Any) -> Any:
        called["n"] += 1
        raise AssertionError("scope filter should not run")

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_scope_filter.run_scope_filter",
        boom,
    )

    def fake_harvest(*args: Any, **kwargs: Any) -> Any:
        return type(
            "HR",
            (),
            {
                "n_policy_calls": 1,
                "visited_section_ids": [],
                "max_depth_hit": False,
                "reason": "",
            },
        )()

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_harvest.harvest",
        fake_harvest,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._relit_map",
        lambda *args, **kwargs: _nullcontext(),
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_orchestrate._wave_subgoal_result",
        lambda *args, **kwargs: type("Sig", (), {"chars_used": 0})(),
    )
    _execute_subgoal_harvest_once(
        _ts(),
        NavState(doc_id="doc_apple", query="apple profit"),
        _cfg(),
        RetrievalPlan(subgoals=[_subgoal(use_filter=False)]),
        _subgoal(use_filter=False),
        steps_out=[],
    )
    assert called["n"] == 0


def test_projection_keeps_allowed_and_ancestors() -> None:
    ts = _ts()
    cfg = _cfg()
    projection = build_projection(
        ts,
        doc_id="",
        query="profit",
        scope=None,
        config=cfg,
        allowed_section_ids={"sec_q3"},
    )
    ids = {v.section_id for v in projection.tree_sections}
    assert "sec_q3" in ids
    assert "sec_hw" not in ids
    assert "sec_root_a" in ids or "doc_apple" in ids


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> None:
        return None
