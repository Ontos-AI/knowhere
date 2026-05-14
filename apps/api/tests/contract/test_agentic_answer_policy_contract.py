from __future__ import annotations

import pytest

from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.policy import attempt_answer
from shared.services.retrieval.agentic.types import AgentRunConfig, AgentState


async def _malformed_json_wrapper(_prompt: str) -> str:
    return '{"status": "DONE", "answer": "truncated"'


async def _raise_runtime_error(_prompt: str) -> str:
    raise RuntimeError("simulated LLM failure")


async def _raise_budget_exceeded(_prompt: str) -> str:
    raise BudgetExceeded("budget exhausted")


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


@pytest.mark.asyncio
async def test_attempt_answer_text_llm_error_returns_not_found() -> None:
    """Text LLM failures should return NOT_FOUND instead of crashing."""
    status, answer, reason = await attempt_answer(
        _raise_runtime_error,
        query="What changed?",
        evidence_text="┈ evidence",
        state=AgentState(),
        config=AgentRunConfig(),
    )

    assert status == "NOT_FOUND"
    assert answer == ""
    assert "LLM error" in reason
    assert "simulated LLM failure" in reason


@pytest.mark.asyncio
async def test_attempt_answer_vlm_and_fallback_both_fail_returns_not_found() -> None:
    """When VLM fails and the text LLM fallback also fails, return NOT_FOUND."""
    status, answer, reason = await attempt_answer(
        _raise_runtime_error,  # llm_fn — used as the text fallback
        query="What changed?",
        evidence_text="┈ evidence",
        state=AgentState(),
        config=AgentRunConfig(),
        vlm_fn=_raise_runtime_error,
        image_urls=["https://example.com/test.png"],
    )

    assert status == "NOT_FOUND"
    assert answer == ""
    assert "both failed" in reason


@pytest.mark.asyncio
async def test_attempt_answer_budget_exceeded_in_fallback_propagates() -> None:
    """BudgetExceeded during the text LLM fallback must propagate, not be swallowed."""
    with pytest.raises(BudgetExceeded):
        await attempt_answer(
            _raise_budget_exceeded,  # llm_fn — used as the text fallback
            query="What changed?",
            evidence_text="┈ evidence",
            state=AgentState(),
            config=AgentRunConfig(),
            vlm_fn=_raise_runtime_error,
            image_urls=["https://example.com/test.png"],
        )
