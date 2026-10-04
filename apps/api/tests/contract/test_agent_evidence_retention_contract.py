"""Provider/harness contract: complementary reads remain visible until finish."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import pytest
from openai.types.chat import ChatCompletion
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.harness import openai_harness
from shared.services.retrieval.agent_tools import ToolBudget, ToolResult


class EvidenceProvider:
    """Read what is missing from the current context; finish when both facts exist."""

    def __init__(self) -> None:
        self.calls: int = 0
        self.hasStaleDiscovery: bool = False

    def chat_completion_raw_with_usage(
        self, *, messages: list[dict[str, object]], **options: object
    ) -> tuple[ChatCompletion, dict[str, int]]:
        self.calls += 1
        visibleText: str = "\n".join(
            str(message["content"])
            for message in messages
            if message.get("role") == "tool"
        )
        name: str = "corpus_read"
        arguments: dict[str, object]
        if self.calls == 1:
            name = "corpus_recall"
            arguments = {"query": "launch capabilities"}
        elif self.calls == 2:
            arguments = {
                "refs": [{"section_path": "Launch"}, {"section_path": "Heavy"}]
            }
        elif "Falcon 9: 23 tonnes" not in visibleText:
            arguments = {"refs": [{"section_path": "Launch"}]}
        elif "Falcon Heavy: 64 tonnes" not in visibleText:
            arguments = {"refs": [{"section_path": "Heavy"}]}
        else:
            name = "finish"
            arguments = {
                "refs": [
                    {"document_id": "ddoc_launch", "section_path": "Launch"},
                    {"document_id": "ddoc_launch", "section_path": "Heavy"},
                ]
            }
            self.hasStaleDiscovery = "DISCOVERY_LIST" in visibleText
        if options.get("tool_choice") != "auto":
            name = "finish"
            arguments = {"refs": []}
        response: ChatCompletion = ChatCompletion.model_validate(
            {
                "id": f"completion-{self.calls}",
                "object": "chat.completion",
                "created": 0,
                "model": "contract-provider",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": f"call-{self.calls}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        )
        return response, {"total_tokens": 100}


@pytest.mark.asyncio
async def test_complementary_reads_survive_discovery_compaction(
    monkeypatch: MonkeyPatch,
) -> None:
    provider: EvidenceProvider = EvidenceProvider()
    monkeypatch.setattr(
        openai_harness,
        "_resolve_client_and_model",
        lambda: (provider, "contract-provider"),
    )

    async def dispatchTool(
        name: str, arguments: dict[str, object], **context: object
    ) -> ToolResult:
        if name == "corpus.recall":
            return ToolResult(text="DISCOVERY_LIST: Launch, Heavy")
        requested: str = json.dumps(arguments)
        sections: list[str] = [
            section for section in ("Launch", "Heavy") if section in requested
        ]
        # A multi-section read exposes the first fact but cuts off the second.
        text: str = ""
        if "Launch" in sections:
            text += "Falcon 9: 23 tonnes\n" + "x" * ToolBudget().max_chars
        if "Heavy" in sections:
            text += "Falcon Heavy: 64 tonnes\n"
        return ToolResult(
            text=text,
            refs=[
                {"document_id": "ddoc_launch", "section_path": section}
                for section in sections
            ],
        )

    monkeypatch.setattr(openai_harness, "dispatch_tool_call", dispatchTool)

    @asynccontextmanager
    async def createDatabase() -> AsyncIterator[AsyncSession]:
        raise AssertionError("The provider contract must not access the database")
        yield AsyncSession()

    result = await openai_harness.OpenAIHarness().run_episode(
        db_factory=createDatabase,
        user_id="actual-reader",
        namespace="__knowhere_demo__",
        query="Compare Falcon 9 and Falcon Heavy launch capabilities",
        budget=EpisodeBudget(max_steps=8, wall_clock_seconds=30),
    )
    assert result.stop_reason == "finished", [step.tool_name for step in result.steps]
    assert provider.calls == 4
    assert not provider.hasStaleDiscovery
    assert {reference["section_path"] for reference in result.refs} == {
        "Launch",
        "Heavy",
    }
    assert "truncated" in result.steps[1].observation_text
