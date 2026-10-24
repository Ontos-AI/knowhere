"""Picked complementary reads stay in the evidence pool after later discovery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import pytest
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.harness import openai_harness
from shared.services.retrieval.agent_tools import ToolResult


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


def _read_result() -> ToolResult:
    return ToolResult(
        text="Falcon 9: 23 tonnes\nFalcon Heavy: 64 tonnes\n",
        payload={
            "refs": [
                {
                    "status": "ok",
                    "document_id": "ddoc_launch",
                    "section_path": "Launch",
                    "chunk_ids": ["launch"],
                },
                {
                    "status": "ok",
                    "document_id": "ddoc_launch",
                    "section_path": "Heavy",
                    "chunk_ids": ["heavy"],
                },
            ],
            "chunks": [
                {
                    "chunk_id": "launch",
                    "document_id": "ddoc_launch",
                    "source_file_name": "spacex.pdf",
                    "section_path": "Launch",
                    "chunk_type": "text",
                    "section_summary": "Falcon 9: 23 tonnes",
                },
                {
                    "chunk_id": "heavy",
                    "document_id": "ddoc_launch",
                    "source_file_name": "spacex.pdf",
                    "section_path": "Heavy",
                    "chunk_type": "text",
                    "section_summary": "Falcon Heavy: 64 tonnes",
                },
            ],
        },
        refs=[
            {"document_id": "ddoc_launch", "section_path": "Launch"},
            {"document_id": "ddoc_launch", "section_path": "Heavy"},
        ],
    )


@pytest.mark.asyncio
async def test_complementary_reads_stay_in_pool_after_later_discovery(
    monkeypatch: MonkeyPatch,
) -> None:
    class ScriptedClient:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []
            self._turns = [
                _Response([_Call("corpus_recall", '{"query": "launch capabilities"}', "c1")]),
                _Response(
                    [
                        _Call(
                            "corpus_read",
                            '{"refs": [{"section_path": "Launch"}, {"section_path": "Heavy"}]}',
                            "c2",
                        )
                    ]
                ),
                _Response([_Call("corpus_pick", '{"pick": ["R1.1", "R1.2"]}', "c3")]),
                _Response([_Call("corpus_recall", '{"query": "more launch facts"}', "c4")]),
                _Response([_Call("finish", '{"notes": "both facts kept"}', "c5")]),
            ]

        def chat_completion_raw_with_usage(
            self, **kwargs: object
        ) -> tuple[_Response, dict[str, int]]:
            self.seen.append(dict(kwargs))
            return self._turns.pop(0), {"total_tokens": 10}

    async def dispatch_tool(
        name: str, arguments: dict[str, object], **context: object
    ) -> ToolResult:
        if name == "corpus.recall":
            return ToolResult(text="DISCOVERY_LIST: Launch, Heavy")
        if name == "corpus.read":
            return _read_result()
        raise AssertionError(f"unexpected dispatch {name}")

    client = ScriptedClient()
    monkeypatch.setattr(
        openai_harness,
        "_resolve_client_and_model",
        lambda: (client, "contract-provider"),
    )
    monkeypatch.setattr(openai_harness, "dispatch_tool_call", dispatch_tool)

    @asynccontextmanager
    async def create_database() -> AsyncIterator[AsyncSession]:
        raise AssertionError("The provider contract must not access the database")
        yield AsyncSession()

    result = await openai_harness.OpenAIHarness().run_episode(
        db_factory=create_database,
        user_id="actual-reader",
        namespace="__knowhere_demo__",
        query="Compare Falcon 9 and Falcon Heavy launch capabilities",
        budget=EpisodeBudget(max_steps=8, wall_clock_seconds=30),
    )

    assert result.stop_reason == "finished", [step.tool_name for step in result.steps]
    assert {item.section_path for item in result.pool} == {"Launch", "Heavy"}
    finish_user = str(client.seen[-1]["messages"][1]["content"])  # type: ignore[index]
    assert "evidence pool (2):" in finish_user
    assert "Launch" in finish_user
    assert "Heavy" in finish_user
