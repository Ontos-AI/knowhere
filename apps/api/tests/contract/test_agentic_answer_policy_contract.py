from __future__ import annotations

import pytest

from shared.services.retrieval.agentic.policy import attempt_answer
from shared.services.retrieval.agentic.types import AgentRunConfig, AgentState
from shared.services.retrieval.llm_adapter import LLMFnInput


async def _malformed_json_wrapper(_prompt: LLMFnInput) -> str:
    return '{"status": "DONE", "answer": "truncated"'


@pytest.mark.asyncio
async def test_attempt_answer_should_not_expose_malformed_json_wrapper() -> None:
    status, answer, reason = await attempt_answer(
        _malformed_json_wrapper,
        query="What changed?",
        evidence_text="┈ evidence",
        state=AgentState(),
        config=AgentRunConfig(),
    )

    assert status == "NOT_FOUND"
    assert answer == ""
    assert reason == "attempt_answer returned malformed JSON"
