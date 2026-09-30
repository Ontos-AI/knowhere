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

from shared.services.retrieval.agent_explore.evidence_pool import Candidate
from shared.services.retrieval.agent_explore.harness.base import Harness
from shared.services.retrieval.agent_explore.harness.resolve import (
    _HARNESS_ENV,
    resolve_harness,
    resolve_harness_name,
)
from shared.services.retrieval.agent_explore.prompt import AGENT_SYSTEM_PROMPT, LOOP_RULES
from shared.services.retrieval.agent_explore.shared import (
    build_wire_tool_name_map,
    cursor_execute_content,
    https_image_parts,
    model_accepts_images,
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
    assert (
        tool_message_content(result, tool_name="corpus.grep", max_chars=100)
        == "short body"
    )


def test_tool_message_content_caps_long_text_with_note() -> None:
    result = ToolResult(text="x" * 200)
    content = tool_message_content(result, tool_name="corpus.grep", max_chars=100)
    assert content.startswith("x" * 100)
    assert "truncated, 100 more chars" in content
    assert len(content) > 100  # capped body + truncation note, not silently dropped


def test_tool_message_content_surfaces_error_instead_of_text() -> None:
    result = ToolResult(text="ignored", error="bad args: missing document_id")
    assert (
        tool_message_content(result, tool_name="corpus.grep", max_chars=100)
        == "error: bad args: missing document_id"
    )


def test_tool_message_content_empty_text_placeholder() -> None:
    result = ToolResult(text="")
    assert (
        tool_message_content(result, tool_name="corpus.grep", max_chars=100)
        == "(empty result)"
    )


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
                tool_args={"notes": ""},
                observation_text="pool=0 notes=''",
                error=None,
                elapsed_ms=0,
                tokens_used_delta=0,
                tokens_used_total=0,
                pick_requested=["R1.1"],
                picked=["R1.1"],
                pick_rejected=[],
                candidates=None,
            ),
        ]
    )
    assert steps[0].phase == "tool_call"
    assert steps[1].phase == "finish"
    assert steps[1].decision["pick_requested"] == ["R1.1"]
    assert steps[1].result["picked"] == ["R1.1"]


def test_attach_evidence_pool_reuses_finish_step() -> None:
    from shared.services.retrieval.agent_explore.bridge import (
        attach_evidence_pool,
        build_decision_trace,
    )
    from shared.services.retrieval.agent_explore.types import AgentStep

    steps = build_decision_trace(
        [
            AgentStep(
                step_index=0,
                tool_name="finish",
                tool_args={"notes": ""},
                observation_text="pool=1 notes=''",
                error=None,
                elapsed_ms=0,
                tokens_used_delta=0,
                tokens_used_total=0,
            )
        ]
    )
    attached = attach_evidence_pool(
        steps,
        [
            Candidate(
                handle="O1",
                kind="outline",
                document_id="doc_a",
                source_file_name="guide.pdf",
                outline_lines=("  [O1] guide.pdf",),
            )
        ],
    )
    assert len(attached) == 1
    assert attached[0].phase == "finish"
    assert attached[0].result["evidence_pool"] == [
        {
            "handle": "O1",
            "kind": "outline",
            "document_id": "doc_a",
            "section_path": None,
            "chunk_ids": [],
        }
    ]


def test_attach_evidence_pool_appends_finish_when_missing() -> None:
    from shared.services.retrieval.agent_explore.bridge import attach_evidence_pool
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
    attached = attach_evidence_pool(steps, [])
    assert len(attached) == 2
    assert attached[1].phase == "finish"
    assert attached[1].observation["observation_text"] == "finish was not called"
    assert attached[1].result["evidence_pool"] == []


def test_agent_explore_keeps_inventory_tool_for_explicit_inventory_requests() -> None:
    from shared.services.retrieval.agent_explore.harness.openai_harness import (
        _build_openai_tools,
    )

    assert REGISTRY.get("corpus.list_documents") is not None
    tools, name_map = _build_openai_tools()
    wire_names = {tool["function"]["name"] for tool in tools}
    assert "corpus_list_documents" in wire_names
    assert name_map["corpus_list_documents"] == "corpus.list_documents"
    list_doc = REGISTRY.get("corpus.list_documents")
    assert list_doc is not None
    assert "inventory the namespace's documents" in list_doc.description
    assert "pick" in tools[0]["function"]["parameters"]["properties"]


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


def test_loop_rules_keep_dependent_calls_off_the_same_turn() -> None:
    assert "wait for that result instead of" in LOOP_RULES
    assert "issuing both in the same turn" in LOOP_RULES
    assert "finish ends the exploration at once" in LOOP_RULES
    assert "corpus_read = corpus.read" in LOOP_RULES
    assert AGENT_SYSTEM_PROMPT.endswith(LOOP_RULES)


@pytest.mark.asyncio
async def test_openai_harness_rebuilds_two_message_context_and_merges_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget
    from shared.services.retrieval.agent_explore.harness import openai_harness as openai_mod
    from shared.services.retrieval.agent_explore.harness.openai_harness import OpenAIHarness

    class _Fn:
        def __init__(self, name: str, arguments: str) -> None:
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, name: str, arguments: str, call_id: str) -> None:
            self.id = call_id
            self.function = _Fn(name, arguments)

    class _Message:
        def __init__(self, tool_calls: list[_Call]) -> None:
            self.tool_calls = tool_calls
            self.content = ""

    class _Choice:
        def __init__(self, message: _Message) -> None:
            self.message = message

    class _Response:
        def __init__(self, tool_calls: list[_Call]) -> None:
            self.choices = [_Choice(_Message(tool_calls))]

    class _Client:
        def __init__(self) -> None:
            self.seen: list[list[dict[str, object]]] = []
            self._turns = [
                _Response(
                    [_Call("corpus_read", '{"refs": [{"document_id": "doc_a", "section_path": "A"}]}', "c1")]
                ),
                _Response(
                    [
                        _Call("corpus_grep", '{"pattern": "ACEI", "pick": ["R1.1"]}', "c2"),
                        _Call("finish", '{"pick": ["R1.2"], "notes": "done"}', "c3"),
                    ]
                ),
            ]

        def chat_completion_raw_with_usage(self, **kwargs: object) -> tuple[_Response, dict[str, int]]:
            self.seen.append(list(kwargs["messages"]))  # type: ignore[arg-type]
            return self._turns.pop(0), {"total_tokens": 10}

    async def _fake_dispatch(name: str, args: dict[str, object], **_kwargs: object) -> ToolResult:
        assert "pick" not in args
        if name == "corpus.read":
            return ToolResult(
                text="read body",
                payload={
                    "refs": [
                        {
                            "status": "ok",
                            "document_id": "doc_a",
                            "section_path": "A",
                            "chunk_ids": ["c1"],
                        },
                        {
                            "status": "ok",
                            "document_id": "doc_a",
                            "section_path": "B",
                            "chunk_ids": ["c2"],
                        },
                    ],
                    "chunks": [
                        {
                            "chunk_id": "c1",
                            "document_id": "doc_a",
                            "source_file_name": "guide.pdf",
                            "section_path": "A",
                            "chunk_type": "text",
                            "section_summary": "sum A",
                        },
                        {
                            "chunk_id": "c2",
                            "document_id": "doc_a",
                            "source_file_name": "guide.pdf",
                            "section_path": "B",
                            "chunk_type": "text",
                            "section_summary": "sum B",
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected dispatch {name}")

    client = _Client()
    monkeypatch.setattr(openai_mod, "_resolve_client_and_model", lambda: (client, "test-model"))
    monkeypatch.setattr(openai_mod, "dispatch_tool_call", _fake_dispatch)

    episode = await OpenAIHarness().run_episode(
        db_factory=lambda: None,  # type: ignore[arg-type]
        user_id="u",
        namespace="ns",
        query="高血压患者首选什么降压药？",
        budget=EpisodeBudget(token_limit=100000, max_steps=12, wall_clock_seconds=180),
    )

    assert len(client.seen) == 2
    for messages in client.seen:
        assert [item["role"] for item in messages] == ["system", "user"]
        assert messages[0]["content"] == AGENT_SYSTEM_PROMPT
    first_user = str(client.seen[0][1]["content"])
    assert first_user.startswith("User query: 高血压患者首选什么降压药？")
    assert "trace:" not in first_user
    assert "latest results" not in first_user
    assert "evidence pool: empty" in first_user
    assert "budget: steps 0/12" in first_user

    second_user = str(client.seen[1][1]["content"])
    assert second_user.startswith("User query: 高血压患者首选什么降压药？")
    assert "trace:\n  1. corpus_read " in second_user
    assert " -> ok" in second_user
    assert "R1.1" not in second_user.split("latest results", 1)[0]
    assert "latest results (turn 1):\ncorpus_read:" in second_user
    assert "[pick ids for this result: R1.1 = A, R1.2 = B]" in second_user
    assert "evidence pool: empty" in second_user
    assert "budget: steps 1/12" in second_user
    assert "collapsed" not in second_user
    assert "role" not in second_user

    assert [item.handle for item in episode.pool] == ["R1.1", "R1.2"]
    assert episode.notes == "done"
    assert episode.stop_reason == "finished"
    assert episode.steps[0].candidates == ["R1.1", "R1.2"]
    finish_step = episode.steps[-1]
    assert finish_step.tool_name == "finish"
    assert finish_step.pick_requested == ["R1.1", "R1.2"]
    assert finish_step.picked == ["R1.1", "R1.2"]
