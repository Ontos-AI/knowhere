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
                round_index=2,
            ),
            AgentStep(
                step_index=1,
                tool_name="corpus.pick",
                tool_args={"pick": ["R1.1"]},
                observation_text="picked: R1.1",
                error=None,
                elapsed_ms=0,
                round_index=3,
                picked=["R1.1"],
                pick_rejected=[],
            ),
            AgentStep(
                step_index=2,
                tool_name="finish",
                tool_args={"notes": ""},
                observation_text="pool=1 notes=''",
                error=None,
                elapsed_ms=0,
                round_index=4,
            ),
        ]
    )
    assert [step.phase for step in steps] == ["tool_call", "tool_call", "finish"]
    assert [step.decision["round_index"] for step in steps] == [2, 3, 4]
    assert all(step.budget == {} for step in steps)
    assert steps[1].result["picked"] == ["R1.1"]
    assert "picked" not in steps[2].result


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
                round_index=1,
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
    assert "corpus_pick" not in wire_names
    for tool in tools:
        assert "pick" not in tool["function"]["parameters"]["properties"]


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


def _grep_hit_result() -> ToolResult:
    return ToolResult(
        text="hits",
        payload={
            "rows": [
                {
                    "document_id": "doc_a",
                    "section_path": "guide.pdf / A",
                    "chunk_id": None,
                    "mounted_chunk_ids": [],
                },
                {
                    "document_id": "doc_a",
                    "section_path": "guide.pdf / B",
                    "chunk_id": None,
                    "mounted_chunk_ids": [],
                },
            ]
        },
    )


def _read_ab_result() -> ToolResult:
    return ToolResult(
        text="read body",
        payload={
            "refs": [
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "guide.pdf / A",
                    "chunk_ids": ["c1"],
                },
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "guide.pdf / B",
                    "chunk_ids": ["c2"],
                },
            ],
            "chunks": [
                {
                    "chunk_id": "c1",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "guide.pdf / A",
                    "chunk_type": "text",
                    "section_summary": "sum A",
                },
                {
                    "chunk_id": "c2",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "guide.pdf / B",
                    "chunk_type": "text",
                    "section_summary": "sum B",
                },
            ],
        },
    )


@pytest.mark.asyncio
async def test_openai_harness_runs_pick_phase_after_read_without_counting_it(
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

    read_args = (
        '{"refs": [{"document_id": "doc_a", "section_path": "guide.pdf / A"}, '
        '{"document_id": "doc_a", "section_path": "guide.pdf / B"}]}'
    )

    class _Client:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []
            self._turns = [
                _Response([_Call("corpus_grep", '{"pattern": "ACEI"}', "c1")]),
                _Response([_Call("corpus_read", read_args, "c2")]),
                _Response([_Call("corpus_pick", '{"pick": ["R1.1"]}', "c3")]),
                _Response([_Call("finish", '{"notes": "done"}', "c4")]),
            ]

        def chat_completion_raw_with_usage(self, **kwargs: object) -> tuple[_Response, dict[str, int]]:
            self.seen.append(dict(kwargs))
            return self._turns.pop(0), {"total_tokens": 10}

    readable_seen: list[object] = []

    async def _fake_dispatch(name: str, args: dict[str, object], **kwargs: object) -> ToolResult:
        readable_seen.append(kwargs["readable"])
        assert kwargs["decided"] == {}
        if name == "corpus.grep":
            return _grep_hit_result()
        if name == "corpus.read":
            return _read_ab_result()
        raise AssertionError(f"unexpected dispatch {name}")

    client = _Client()
    monkeypatch.setattr(openai_mod, "_resolve_client_and_model", lambda: (client, "test-model"))
    monkeypatch.setattr(openai_mod, "dispatch_tool_call", _fake_dispatch)

    budget = EpisodeBudget(max_steps=12, wall_clock_seconds=180)
    episode = await OpenAIHarness().run_episode(
        db_factory=lambda: None,  # type: ignore[arg-type]
        user_id="u",
        namespace="ns",
        query="高血压患者首选什么降压药？",
        budget=budget,
    )

    assert len(client.seen) == 4
    user_texts: list[str] = []
    for call in client.seen:
        messages = call["messages"]
        assert isinstance(messages, list)
        assert [item["role"] for item in messages] == ["system", "user"]
        assert messages[0]["content"] == AGENT_SYSTEM_PROMPT
        user_texts.append(str(messages[1]["content"]))

    assert readable_seen[0] == frozenset()
    assert readable_seen[1] == {
        ("doc_a", "guide.pdf / A"),
        ("doc_a", "guide.pdf / B"),
    }

    first_user = user_texts[0]
    assert first_user.startswith("User query: 高血压患者首选什么降压药？")
    assert "trace:" not in first_user
    assert "latest results" not in first_user
    assert "evidence pool: empty" in first_user
    assert "budget: steps 0/12" in first_user

    pick_call = client.seen[2]
    tools = pick_call["tools"]
    assert isinstance(tools, list)
    assert [tool["function"]["name"] for tool in tools] == ["corpus_pick"]
    assert pick_call["tool_choice"] == {"type": "function", "function": {"name": "corpus_pick"}}
    pick_user = user_texts[2]
    assert "latest results (turn 2):\ncorpus_read:" in pick_user
    assert "[pick ids for this result: R1.1 = guide.pdf / A, R1.2 = guide.pdf / B]" in pick_user
    assert pick_user.endswith(
        "Pick phase: you can only call corpus_pick now. Pick the ids worth "
        "keeping, or pass an empty list."
    )
    assert "budget: steps 2/12" in pick_user

    finish_call = client.seen[3]
    finish_tools = finish_call["tools"]
    assert isinstance(finish_tools, list)
    assert "corpus_pick" not in {tool["function"]["name"] for tool in finish_tools}
    finish_user = user_texts[3]
    assert "latest results (turn 2):\ncorpus_read:" in finish_user
    assert '3. corpus_pick {"pick": ["R1.1"]} -> picked: R1.1' in finish_user
    assert "evidence pool (1):" in finish_user
    assert "budget: steps 2/12" in finish_user
    assert "Pick phase" not in finish_user

    assert budget.steps_used == 3
    assert episode.tokens_used == 40
    assert [item.handle for item in episode.pool] == ["R1.1"]
    assert episode.stop_reason == "finished"
    assert [step.tool_name for step in episode.steps] == [
        "corpus.grep",
        "corpus.read",
        "corpus.pick",
        "finish",
    ]
    assert [step.round_index for step in episode.steps] == [1, 2, 3, 4]
    assert episode.steps[1].candidates == ["R1.1", "R1.2"]
    assert episode.steps[2].picked == ["R1.1"]
    assert episode.steps[2].pick_rejected == []


class _FakeStarted:
    type = "tool-call-started"

    def __init__(self, call_id: str, model_call_id: str) -> None:
        self.call_id = call_id
        self.model_call_id = model_call_id


class _FakeCtx:
    def __init__(self, tool_call_id: str) -> None:
        self.tool_call_id = tool_call_id


def _fake_cursor_sdk(
    script: list[tuple[str, list[tuple[str, str, dict[str, object]]]]],
    outputs: dict[str, object],
) -> object:
    """Minimal ``cursor_sdk`` stand-in whose run executes ``script`` round by round.

    Every callback of a round starts before that round's ``tool-call-started``
    updates are delivered, so callbacks must wait for their own tag.
    """
    import asyncio
    import types

    class _Options:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class _Usage:
        total_tokens = 77

    class _Result:
        usage = _Usage()

    class _Run:
        def __init__(self, tools: dict[str, object], on_delta: object) -> None:
            self._tools = tools
            self._on_delta = on_delta

        async def wait(self) -> _Result:
            loop = asyncio.get_running_loop()
            for model_call_id, calls in script:
                futures = [
                    loop.run_in_executor(
                        None,
                        self._tools[wire_name].execute,  # type: ignore[attr-defined]
                        args,
                        _FakeCtx(call_id),
                    )
                    for call_id, wire_name, args in calls
                ]
                await asyncio.sleep(0.05)
                self._on_delta(types.SimpleNamespace(type="text-delta"))  # type: ignore[operator]
                for call_id, _wire_name, _args in calls:
                    self._on_delta(_FakeStarted(call_id, model_call_id))  # type: ignore[operator]
                results = await asyncio.gather(*futures)
                for (call_id, _wire_name, _args), result in zip(calls, results):
                    outputs[call_id] = result
            return _Result()

    class _Agent:
        def __init__(self, options: _Options) -> None:
            self._tools = options.local.custom_tools  # type: ignore[attr-defined]

        async def __aenter__(self) -> "_Agent":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def send(self, _prompt: str, options: _Options) -> _Run:
            return _Run(self._tools, options.on_delta)  # type: ignore[attr-defined]

    class _Agents:
        async def create(self, options: _Options) -> _Agent:
            return _Agent(options)

    class _Client:
        agents = _Agents()

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class _AsyncClient:
        @staticmethod
        async def launch_bridge(**_kwargs: object) -> _Client:
            return _Client()

    return types.SimpleNamespace(
        AsyncClient=_AsyncClient,
        AgentOptions=_Options,
        LocalAgentOptions=_Options,
        SendOptions=_Options,
        CustomTool=_Options,
    )


@pytest.mark.asyncio
async def test_cursor_harness_rounds_follow_model_call_id_and_pick_phase_rejects_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget
    from shared.services.retrieval.agent_explore.harness import cursor_harness as cursor_mod
    from shared.services.retrieval.agent_explore.harness.cursor_harness import CursorHarness

    read_a = {"refs": [{"document_id": "doc_a", "section_path": "guide.pdf / A"}]}
    script: list[tuple[str, list[tuple[str, str, dict[str, object]]]]] = [
        (
            "m1",
            [
                ("c1", "corpus_grep", {"pattern": "ACEI"}),
                ("c2", "corpus_read", read_a),
            ],
        ),
        (
            "m2",
            [
                ("c3", "corpus_read", read_a),
                ("c4", "corpus_pick", {"pick": ["R1.1"]}),
                ("c5", "finish", {"notes": "early"}),
            ],
        ),
        ("m3", [("c6", "finish", {"notes": "done"})]),
    ]
    outputs: dict[str, object] = {}
    dispatched: list[tuple[str, object]] = []

    async def _fake_dispatch(name: str, args: dict[str, object], **kwargs: object) -> ToolResult:
        dispatched.append((name, kwargs["readable"]))
        if name == "corpus.grep":
            return _grep_hit_result()
        if name == "corpus.read":
            return _read_ab_result()
        raise AssertionError(f"unexpected dispatch {name}")

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(cursor_mod, "_require_cursor_sdk", lambda: _fake_cursor_sdk(script, outputs))
    monkeypatch.setattr(cursor_mod, "dispatch_tool_call", _fake_dispatch)

    budget = EpisodeBudget(max_steps=12, wall_clock_seconds=30)
    episode = await CursorHarness(model="test-model").run_episode(
        db_factory=lambda: None,  # type: ignore[arg-type]
        user_id="u",
        namespace="ns",
        query="q",
        budget=budget,
    )

    assert sorted(name for name, _readable in dispatched) == ["corpus.grep", "corpus.read"]
    assert all(readable == frozenset() for _name, readable in dispatched)
    assert (
        "Pick phase: you can only call corpus_pick now. Pick the ids worth "
        "keeping, or pass an empty list."
    ) in str(outputs["c2"])
    assert outputs["c3"] == (
        "Pick phase: only corpus_pick is accepted now. Pick from R/O ids above, "
        "or pass an empty list."
    )
    assert "Pick phase" in str(outputs["c5"])
    assert str(outputs["c4"]).startswith("picked: R1.1")
    assert '"status": "finished"' in str(outputs["c6"])

    assert budget.steps_used == 2
    assert episode.tokens_used == 77
    assert [item.handle for item in episode.pool] == ["R1.1"]
    by_round: dict[int, list[str]] = {}
    for step in episode.steps:
        by_round.setdefault(step.round_index, []).append(step.tool_name)
    assert sorted(by_round[1]) == ["corpus.grep", "corpus.read"]
    assert sorted(by_round[2]) == ["corpus.pick", "corpus.read", "finish"]
    assert by_round[3] == ["finish"]
