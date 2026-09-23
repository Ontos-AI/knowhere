"""Unit tests for the Phase 3.5 ``agent_explore`` harness layer.

Covers only the pure functions and the ``AGENT_EXPLORE_HARNESS`` switch
resolution — none of these need a real DB or LLM. Does not cover
``dispatch.dispatch_tool_call`` (needs a DB session) or a full
``Harness.run_episode`` loop (needs an LLM/Cursor SDK backend) — those stay
integration-level, exercised via the debug scripts and
``eval-cursor-harness``, not here.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.agent_explore.config import LOOP_CONTRACT_SUFFIX
from shared.services.retrieval.agent_explore.harness.base import Harness
from shared.services.retrieval.agent_explore.harness.resolve import (
    _HARNESS_ENV,
    resolve_harness,
    resolve_harness_name,
)
from shared.services.retrieval.agent_explore.shared import (
    EVIDENCE_TOOL_NAMES,
    build_wire_tool_name_map,
    cursor_execute_content,
    dedup_refs,
    finish_refs_from_args,
    https_image_parts,
    model_accepts_images,
    normalize_finish_refs,
    select_episode_refs,
    tool_message_content,
    wire_safe_tool_name,
)
from shared.services.retrieval.agent_tools import REGISTRY, ToolResult


# --------------------------------------------------------------------------
# shared.py: wire-safe tool name mapping
# --------------------------------------------------------------------------


def test_wire_safe_tool_name_replaces_dots() -> None:
    assert wire_safe_tool_name("corpus.read") == "corpus_read"
    assert wire_safe_tool_name("corpus.node_filter") == "corpus_node_filter"
    # Already wire-safe names are untouched.
    assert wire_safe_tool_name("finish") == "finish"


def test_build_wire_tool_name_map_round_trips_to_canonical() -> None:
    mapping = build_wire_tool_name_map(["corpus.read", "corpus.recall", "finish"])
    assert mapping == {
        "corpus_read": "corpus.read",
        "corpus_recall": "corpus.recall",
        "finish": "finish",
    }


# --------------------------------------------------------------------------
# shared.py: tool_message_content (max_chars capping)
# --------------------------------------------------------------------------


def test_tool_message_content_passes_through_short_text() -> None:
    result = ToolResult(text="short body")
    assert tool_message_content(result, max_chars=100) == "short body"


def test_tool_message_content_caps_long_text_with_note() -> None:
    result = ToolResult(text="x" * 200)
    content = tool_message_content(result, max_chars=100)
    assert content.startswith("x" * 100)
    assert "truncated, 100 more chars" in content
    assert len(content) > 100  # capped body + truncation note, not silently dropped


def test_tool_message_content_surfaces_error_instead_of_text() -> None:
    result = ToolResult(text="ignored", error="bad args: missing document_id")
    assert tool_message_content(result, max_chars=100) == "error: bad args: missing document_id"


def test_tool_message_content_empty_text_placeholder() -> None:
    result = ToolResult(text="")
    assert tool_message_content(result, max_chars=100) == "(empty result)"


def test_model_accepts_images_only_when_name_contains_vision() -> None:
    assert model_accepts_images("gpt-4-vision") is True
    assert model_accepts_images("deepseek-v4-flash") is False


def test_https_image_parts_keeps_https_and_drops_filesystem() -> None:
    result = ToolResult(
        text="body",
        media=[
            {"type": "image_url", "url": "https://cdn.example/a.png"},
            {"type": "image_url", "url": "filesystem:///tmp/a.png"},
            {"type": "image_url", "url": "http://insecure.example/a.png"},
        ],
    )
    assert https_image_parts(result) == [
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.example/a.png"},
        }
    ]


def test_cursor_execute_content_attaches_https_image_parts() -> None:
    result = ToolResult(
        text="caption",
        media=[{"type": "image_url", "url": "https://cdn.example/a.png"}],
    )
    assert cursor_execute_content(result, text="caption") == [
        {"type": "text", "text": "caption"},
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.example/a.png"},
        },
    ]
    text_only = ToolResult(
        text="caption",
        media=[{"type": "image_url", "url": "filesystem:///tmp/a.png"}],
    )
    assert cursor_execute_content(text_only, text="caption") == "caption"


# --------------------------------------------------------------------------
# shared.py: normalize_finish_refs / dedup_refs
# --------------------------------------------------------------------------


def test_normalize_finish_refs_drops_non_dict_and_empty_document_id() -> None:
    raw = [
        {"document_id": "doc_a", "chunk_id": "c1"},
        {"document_id": "", "chunk_id": "c2"},
        {"chunk_id": "c3"},
        "not a dict",
        None,
        {"document_id": "doc_b"},
    ]
    normalized = normalize_finish_refs(raw)
    assert normalized == [
        {"document_id": "doc_a", "chunk_id": "c1"},
        {"document_id": "doc_b"},
    ]


def test_normalize_finish_refs_non_list_input_is_empty() -> None:
    assert normalize_finish_refs(None) == []
    assert normalize_finish_refs("refs") == []
    assert normalize_finish_refs({"document_id": "doc_a"}) == []


def test_finish_refs_from_args_omitted_is_none_explicit_empty_is_list() -> None:
    assert finish_refs_from_args(None) is None
    assert finish_refs_from_args({}) is None
    assert finish_refs_from_args({"notes": "none"}) is None
    assert finish_refs_from_args({"refs": None}) is None
    assert finish_refs_from_args({"refs": []}) == []
    assert finish_refs_from_args(
        {"refs": [{"document_id": "doc_a", "section_path": "Intro"}]}
    ) == [{"document_id": "doc_a", "section_path": "Intro"}]


def test_select_episode_refs_omitted_finish_uses_trajectory() -> None:
    trajectory = [{"document_id": "doc_a", "chunk_id": "c1"}]
    selection = select_episode_refs(None, trajectory, "notes")
    assert selection.refs == trajectory
    assert selection.agent_selected_refs is None
    assert selection.fallback_refs == trajectory
    assert "finish was not called" in selection.notes


def test_select_episode_refs_keeps_agent_cited_refs() -> None:
    cited = [{"document_id": "doc_a", "section_path": "Intro"}]
    selection = select_episode_refs(
        cited, [{"document_id": "doc_b", "chunk_id": "other"}], ""
    )
    assert selection.refs == cited
    assert selection.agent_selected_refs == cited
    assert selection.fallback_refs == []


def test_build_decision_trace_marks_finish_phase() -> None:
    from shared.services.retrieval.agent_explore.bridge import build_decision_trace
    from shared.services.retrieval.agent_explore.types import AgentStep

    steps = build_decision_trace(
        [
            AgentStep(
                step_index=0,
                tool_name="corpus.read",
                tool_args={},
                observation_text="body",
                error=None,
                elapsed_ms=1,
                tokens_used_delta=0,
                tokens_used_total=0,
            ),
            AgentStep(
                step_index=1,
                tool_name="finish",
                tool_args={"refs": [{"document_id": "doc_a"}]},
                observation_text="refs=1 notes=''",
                error=None,
                elapsed_ms=0,
                tokens_used_delta=0,
                tokens_used_total=0,
            ),
        ]
    )
    assert steps[0].phase == "tool_call"
    assert steps[1].phase == "finish"
    assert steps[1].decision["args"]["refs"] == [{"document_id": "doc_a"}]


def test_attach_ref_provenance_reuses_finish_step() -> None:
    from shared.services.retrieval.agent_explore.bridge import (
        attach_ref_provenance,
        build_decision_trace,
    )
    from shared.services.retrieval.agent_explore.types import AgentStep

    steps = build_decision_trace(
        [
            AgentStep(
                step_index=0,
                tool_name="finish",
                tool_args={"refs": []},
                observation_text="refs=0 notes=''",
                error=None,
                elapsed_ms=0,
                tokens_used_delta=0,
                tokens_used_total=0,
            )
        ]
    )
    attached = attach_ref_provenance(
        steps,
        agent_selected_refs=[],
        fallback_refs=[],
        resolved_refs=[],
        dropped_refs=[{"ref": {"document_id": "doc_a"}, "reason": "unknown document_id: doc_a"}],
    )
    assert len(attached) == 1
    assert attached[0].phase == "finish"
    assert attached[0].result["agent_selected_refs"] == []
    assert attached[0].result["dropped_refs"][0]["reason"] == "unknown document_id: doc_a"


def test_attach_ref_provenance_appends_finish_when_missing() -> None:
    from shared.services.retrieval.agent_explore.bridge import attach_ref_provenance
    from shared.services.retrieval.trace import DecisionTraceStep

    steps = [
        DecisionTraceStep(
            step_index=0,
            agent="agent_explore",
            phase="tool_call",
            observation={"observation_text": "hit"},
            decision={"action": "corpus.read", "args": {}},
            result={"status": "ok", "error": None},
        )
    ]
    attached = attach_ref_provenance(
        steps,
        agent_selected_refs=None,
        fallback_refs=[{"document_id": "doc_a", "chunk_id": "c1"}],
        resolved_refs=[{"document_id": "doc_a", "chunk_id": "c1"}],
        dropped_refs=[],
    )
    assert len(attached) == 2
    assert attached[1].phase == "finish"
    assert attached[1].observation["observation_text"] == "finish was not called"
    assert attached[1].result["fallback_refs"] == [
        {"document_id": "doc_a", "chunk_id": "c1"}
    ]


def test_select_episode_refs_explicit_empty_does_not_fallback() -> None:
    trajectory = [{"document_id": "doc_a", "chunk_id": "c1"}]
    selection = select_episode_refs([], trajectory, "chose none")
    assert selection.refs == []
    assert selection.agent_selected_refs == []
    assert selection.fallback_refs == []
    assert selection.notes == "chose none"


def test_dedup_refs_keeps_first_seen_and_drops_missing_ids() -> None:
    refs = [
        {"document_id": "doc_a", "chunk_id": "c1", "section_path": "first"},
        {"document_id": "doc_a", "chunk_id": "c1", "section_path": "duplicate"},
        {"document_id": "doc_a", "chunk_id": "c2"},
        {"document_id": "doc_a"},  # missing chunk_id -> dropped
        {"chunk_id": "c3"},  # missing document_id -> dropped
        {"document_id": "doc_b", "chunk_id": "c1"},
    ]
    deduped = dedup_refs(refs)
    assert deduped == [
        {"document_id": "doc_a", "chunk_id": "c1", "section_path": "first"},
        {"document_id": "doc_a", "chunk_id": "c2"},
        {"document_id": "doc_b", "chunk_id": "c1"},
    ]


def test_evidence_tool_names_includes_read_assets_and_query_table() -> None:
    assert EVIDENCE_TOOL_NAMES == frozenset(
        {"corpus.read", "corpus.assets", "corpus.query_table"}
    )


def test_agent_explore_keeps_inventory_tool_for_explicit_inventory_requests() -> None:
    from shared.services.retrieval.agent_explore.harness.openai_harness import (
        _build_openai_tools,
    )

    assert REGISTRY.get("corpus.list_documents") is not None
    tools, name_map = _build_openai_tools()
    wire_names = {tool["function"]["name"] for tool in tools}
    assert "corpus_list_documents" in wire_names
    assert name_map["corpus_list_documents"] == "corpus.list_documents"
    assert "only if the user explicitly asks to list or inventory" in LOOP_CONTRACT_SUFFIX


# --------------------------------------------------------------------------
# harness/resolve.py: AGENT_EXPLORE_HARNESS switch
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_HARNESS_ENV, raising=False)


def test_resolve_harness_name_defaults_to_cursor_sdk() -> None:
    assert resolve_harness_name() == "cursor_sdk"


def test_resolve_harness_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_HARNESS_ENV, "cursor_sdk")
    assert resolve_harness_name() == "cursor_sdk"


def test_resolve_harness_name_unknown_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_HARNESS_ENV, "not_a_real_harness")
    assert resolve_harness_name() == "cursor_sdk"


def test_resolve_harness_name_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_HARNESS_ENV, "CURSOR_SDK")
    assert resolve_harness_name() == "cursor_sdk"


def test_resolve_harness_default_builds_cursor_harness() -> None:
    from shared.services.retrieval.agent_explore.harness.cursor_harness import CursorHarness

    harness = resolve_harness()
    assert isinstance(harness, CursorHarness)
    assert isinstance(harness, Harness)


def test_resolve_harness_cursor_sdk_builds_cursor_harness_without_sdk_installed() -> None:
    """Selecting cursor_sdk must not require the optional cursor-sdk package
    to be importable — only actually running an episode does (see
    cursor_harness.py's guarded _require_cursor_sdk, exercised at
    run_episode() call time, not at harness construction time).
    """
    from shared.services.retrieval.agent_explore.harness.cursor_harness import CursorHarness

    harness = resolve_harness("cursor_sdk")
    assert isinstance(harness, CursorHarness)
    assert isinstance(harness, Harness)


def test_resolve_harness_explicit_name_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.agent_explore.harness.openai_harness import OpenAIHarness

    monkeypatch.setenv(_HARNESS_ENV, "cursor_sdk")
    harness = resolve_harness("openai")
    assert isinstance(harness, OpenAIHarness)


def test_resolve_harness_passes_cursor_model_to_cursor_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.agent_explore.harness.cursor_harness import CursorHarness

    monkeypatch.delenv("AGENT_EXPLORE_CURSOR_MODEL", raising=False)
    harness = resolve_harness("cursor_sdk", cursor_model="  grok-4.6  ")
    assert isinstance(harness, CursorHarness)
    assert harness._model == "grok-4.6"

    from shared.services.retrieval.agent_explore.config import AGENT_EXPLORE_CURSOR_MODEL

    default_harness = resolve_harness("cursor_sdk", cursor_model="  ")
    assert default_harness._model == AGENT_EXPLORE_CURSOR_MODEL


def test_loop_contract_keeps_dependent_grep_off_the_same_turn() -> None:
    text = LOOP_CONTRACT_SUFFIX
    assert "same turn" in text
    for name in (
        "corpus.grep",
        "corpus.recall",
        "corpus.read",
        "corpus.list_documents",
        "corpus.outline",
        "corpus.node_filter",
        "corpus.assets",
    ):
        assert name in text
