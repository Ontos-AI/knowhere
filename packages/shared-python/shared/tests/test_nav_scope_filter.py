"""Bounded WHERE scope-filter loop tests (mocked policy LLM)."""

from __future__ import annotations

import json
from typing import Any, List

from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    NamespaceKnowhereProvider,
    SectionRow,
)
from shared.services.retrieval.nav.nav_node_filter import field_predicate, node_filter
from shared.services.retrieval.nav.nav_scope_filter import run_scope_filter
from shared.services.retrieval.nav.nav_types import NavConfig


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
                summary="Apple quarterly profit",
                order=1,
            ),
        ],
        units=(),
    )
    filler = KnowhereProvider(
        doc_id="doc_other",
        sections=[
            _section("sec_root_o", None, "Root", "Root", level=0, order=0),
            _section(
                "sec_misc",
                "sec_root_o",
                "Notes",
                "Notes",
                level=1,
                summary="unrelated notes",
                order=1,
            ),
        ],
        units=(),
    )
    return ProviderToolSpace(
        NamespaceKnowhereProvider(
            [apple, filler],
            titles={"doc_apple": "AAPL 10-K.pdf", "doc_other": "Misc.docx"},
        )
    )


def _cfg(**kwargs: Any) -> NavConfig:
    data = {
        "enable_node_filter": True,
        "filter_max_rounds": 3,
        "filter_min_hits": 1,
        "filter_max_hits": 40,
        "llm_model": "test-model",
        "llm_max_tokens": 256,
    }
    data.update(kwargs)
    return NavConfig.from_dict(data)


def _install_script(monkeypatch: Any, replies: List[dict[str, Any]]) -> None:
    queue = list(replies)

    def fake_nav_chat(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        obj = queue.pop(0) if queue else {"action": "fallback", "reason": "empty"}
        return {"content": json.dumps(obj)}

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_llm.nav_chat",
        fake_nav_chat,
    )


def test_widen_relooks_full_map(monkeypatch: Any) -> None:
    seen_users: List[str] = []
    queue: List[dict[str, Any]] = [
        {"action": "widen", "reason": "sub-map too narrow"},
        {"action": "done", "decision": "collect_all", "reason": "ok"},
    ]

    def fake_nav_chat(**kwargs: Any) -> dict[str, Any]:
        messages = kwargs.get("messages") or []
        seen_users.append(str(messages[-1].get("content") or ""))
        obj = queue.pop(0) if queue else {"action": "fallback", "reason": "empty"}
        return {"content": json.dumps(obj)}

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_llm.nav_chat",
        fake_nav_chat,
    )
    out = run_scope_filter(
        _ts(),
        _cfg(),
        query="apple q3 profit",
        doc_ids=["doc_apple", "doc_other"],
        seed_filter=node_filter([field_predicate("path", ["Q3"])]),
    )
    assert out.decision == "collect_all"
    assert len(seen_users) == 2
    assert "Full map" not in seen_users[0]
    assert "Sub-map" in seen_users[0]
    assert "Full map" in seen_users[1]
    assert "Last filter" in seen_users[1]


def test_zero_hits_widen_then_done(monkeypatch: Any) -> None:
    _install_script(
        monkeypatch,
        [
            {
                "action": "filter",
                "predicates": [
                    {"field": "path", "terms": ["AAPL"], "match": "substring"}
                ],
            },
            {"action": "done", "decision": "collect_all", "reason": "ok"},
        ],
    )
    out = run_scope_filter(
        _ts(),
        _cfg(),
        query="apple q3 profit",
        doc_ids=["doc_apple", "doc_other"],
        seed_filter=node_filter([field_predicate("path", ["zzz-missing"])]),
    )
    assert out.decision == "collect_all"
    assert "sec_q3" in out.settled_section_ids
    assert out.rounds == 2


def test_zero_hits_policy_fallback(monkeypatch: Any) -> None:
    _install_script(
        monkeypatch,
        [{"action": "fallback", "reason": "cannot widen"}],
    )
    out = run_scope_filter(
        _ts(),
        _cfg(),
        query="missing topic",
        doc_ids=["doc_apple", "doc_other"],
        seed_filter=node_filter([field_predicate("path", ["zzz-missing"])]),
    )
    assert out.decision == "fallback"
    assert out.reason == "cannot widen"
    assert out.rounds == 1


def test_too_many_hits_tighten(monkeypatch: Any) -> None:
    _install_script(
        monkeypatch,
        [
            {
                "action": "filter",
                "predicates": [
                    {"field": "summary", "terms": ["profit"], "match": "substring"}
                ],
            },
            {"action": "done", "decision": "collect_all", "reason": "tight"},
        ],
    )
    out = run_scope_filter(
        _ts(),
        _cfg(filter_max_hits=1),
        query="everything",
        doc_ids=["doc_apple", "doc_other"],
        seed_filter=node_filter([field_predicate("path", ["Root"])]),
    )
    assert out.decision == "collect_all"
    assert out.settled_section_ids == ["sec_q3"]
    assert out.rounds == 2


def test_max_rounds_hard_stop(monkeypatch: Any) -> None:
    _install_script(
        monkeypatch,
        [
            {
                "action": "filter",
                "predicates": [
                    {"field": "path", "terms": ["zzz"], "match": "substring"}
                ],
            },
            {
                "action": "filter",
                "predicates": [
                    {"field": "path", "terms": ["still-missing"], "match": "substring"}
                ],
            },
        ],
    )
    out = run_scope_filter(
        _ts(),
        _cfg(filter_max_rounds=2),
        query="no hits",
        doc_ids=["doc_apple"],
        seed_filter=node_filter([field_predicate("path", ["absent"])]),
    )
    assert out.decision == "fallback"
    assert out.rounds == 2
    assert out.reason == "max_rounds_out_of_band"


def test_cardinality_drives_scoped_harvest(monkeypatch: Any) -> None:
    _install_script(
        monkeypatch,
        [{"action": "done", "reason": "keep"}],
    )
    out = run_scope_filter(
        _ts(),
        _cfg(filter_min_hits=1, filter_max_hits=40),
        query="apple",
        doc_ids=["doc_apple", "doc_other"],
        seed_filter=node_filter([field_predicate("path", ["AAPL"])]),
    )
    # filename + Root + Q3 → more than min_hits → scoped_harvest
    assert out.decision == "scoped_harvest"
    assert out.rounds == 1
    assert "sec_q3" in out.settled_section_ids


def test_disabled_skips_policy(monkeypatch: Any) -> None:
    called = {"n": 0}

    def boom(**kwargs: Any) -> dict[str, Any]:
        called["n"] += 1
        raise AssertionError("policy should not run")

    monkeypatch.setattr(
        "shared.services.retrieval.nav.nav_llm.nav_chat",
        boom,
    )
    out = run_scope_filter(
        _ts(),
        _cfg(enable_node_filter=False),
        query="apple",
        doc_ids=["doc_apple"],
        seed_filter=node_filter([field_predicate("path", ["AAPL"])]),
    )
    assert out.decision == "fallback"
    assert out.reason == "disabled"
    assert called["n"] == 0
