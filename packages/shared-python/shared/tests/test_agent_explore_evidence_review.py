"""Policy tests for final-visible evidence review, without a database or LLM."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.agent_explore import evidence_review as review_module
from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.evidence_pool import Candidate
from shared.services.retrieval.agent_explore.evidence_review import (
    EvidenceItem,
    EvidenceReviewSession,
    EvidenceSnapshot,
    MAX_REVIEW_INPUT_BYTES,
    MAX_REVIEW_OUTPUT_TOKENS,
)


def _snapshot(
    text: str = "A costs $5. B costs $7.", fingerprint: str = "packet-1"
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        items=(
            EvidenceItem(
                evidence_id="E1",
                document_id="doc-1",
                chunk_id="chunk-1",
                section_path="Prices",
                source_file_name="prices.txt",
                text=text,
                revision="rev-1",
                page_nums=(2,),
            ),
        ),
        fingerprint=fingerprint,
    )


def _facet(
    facet_id: str = "F1",
    requirement: str = "Price of A",
    status: str = "supported",
    evidence_id: str = "E1",
    quote: str = "A costs $5.",
) -> dict[str, Any]:
    return {
        "facet_id": facet_id,
        "requirement": requirement,
        "status": status,
        "citations": []
        if status == "missing"
        else [{"evidence_id": evidence_id, "quote": quote}],
    }


def _verdict(
    *facets: dict[str, Any],
    status: str = "sufficient",
    reason: str = "Selected evidence supports the request.",
) -> str:
    return json.dumps(
        {"status": status, "reason": reason, "coverage": list(facets or (_facet(),))}
    )


class _Script:
    def __init__(self, *responses: str, usage: dict[str, Any] | None = None) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, Any]]] = []
        self.usage = {"total_tokens": 13} if usage is None else usage

    async def __call__(self, messages: list[dict[str, Any]], max_tokens: int):
        assert max_tokens == MAX_REVIEW_OUTPUT_TOKENS
        self.messages.append(messages)
        return self.responses.pop(0), self.usage


class _Loader:
    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def __call__(self, *, pool, queried_tables):
        self.calls += 1
        return self.snapshot


async def _finish(session: EvidenceReviewSession, budget: EpisodeBudget | None = None):
    return await session.on_finish(
        pool=[], queried_tables={}, budget=budget or EpisodeBudget()
    )


@pytest.mark.asyncio
async def test_complete_evidence_is_reviewed_once_with_isolated_original_query():
    snapshot = _snapshot()
    loader, reviewer = _Loader(snapshot), _Script(_verdict())
    session = EvidenceReviewSession("What is the price of A?", loader, reviewer)
    budget = EpisodeBudget(tokens_used=20, steps_used=2)
    decision = await session.on_finish(
        pool=[
            Candidate(
                handle="R1.1",
                kind="read",
                document_id="doc-1",
                source_file_name="prices.txt",
                summary="SECRET EXECUTOR SUMMARY",
            )
        ],
        queried_tables={},
        budget=budget,
    )
    assert not decision.continue_retrieval
    assert session.report["status"] == "sufficient"
    assert session.report["attempts"] == 1
    assert session.report["reviewer_tokens"] == 13
    assert session.report["usage_complete"] is True
    assert budget.tokens_used == 33
    assert budget.steps_used == 3
    assert len(reviewer.messages) == 1
    messages = reviewer.messages[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "UNTRUSTED SOURCE DATA" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["original_query"] == "What is the price of A?"
    assert payload["frozen_requirements"] is None
    assert "".join(span["text"] for span in payload["evidence"][0]["citation_spans"]) == snapshot.items[0].text
    assert "SECRET EXECUTOR SUMMARY" not in json.dumps(messages)
    assert payload["evidence"][0]["revision"] == "rev-1"
    assert payload["evidence"][0]["page_nums"] == [2]
    # Reports are snapshots, not mutable access to internal policy state.
    session.report["coverage"][0]["citations"].clear()
    assert session.report["coverage"][0]["citations"]
    session.on_stop("finished")
    session.finalize(snapshot)
    assert session.report["status"] == "sufficient"
    await _finish(session, budget)
    assert len(reviewer.messages) == 1


@pytest.mark.asyncio
async def test_single_repair_uses_remaining_budget_and_frozen_requirements():
    first = _verdict(
        _facet(), _facet("F2", "Price of B", "missing"), status="insufficient"
    )
    second = _verdict(_facet("F2", "Price of B", quote="B costs $7."), _facet())
    reviewer, loader = _Script(first, second), _Loader(_snapshot("A costs $5."))
    session = EvidenceReviewSession("Compare the prices of A and B", loader, reviewer)
    budget = EpisodeBudget(max_steps=15, steps_used=3, tokens_used=100)
    started_at = budget._started_at
    decision = await _finish(session, budget)
    assert decision.continue_retrieval
    assert "Price of B" in decision.instruction
    assert "Price of A" not in decision.instruction
    assert budget.steps_used == 4
    assert budget.max_steps == 8
    assert budget._started_at == started_at
    assert session.report["repairs"] == 1
    loader.snapshot = _snapshot(fingerprint="packet-2")
    budget.record_step()  # A corrective exploration turn, not a fresh budget.
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "sufficient"
    assert session.report["attempts"] == 2
    assert session.report["reviewer_tokens"] == 26
    assert budget.tokens_used == 126
    assert budget.steps_used == 6
    payload = json.loads(reviewer.messages[1][1]["content"])
    assert payload["frozen_requirements"] == [
        {"facet_id": "F1", "requirement": "Price of A"},
        {"facet_id": "F2", "requirement": "Price of B"},
    ]
    assert [item["facet_id"] for item in session.report["coverage"]] == ["F1", "F2"]
    await _finish(session, budget)
    assert len(reviewer.messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        _verdict(_facet()),
        _verdict(_facet(), _facet("F2", "Mention of B", quote="B costs $7.")),
        _verdict(_facet(), _facet("F3", "Price of B", quote="B costs $7.")),
    ],
)
async def test_repair_cannot_drop_relax_or_rename_original_requirements(second):
    reviewer = _Script(
        _verdict(
            _facet(), _facet("F2", "Price of B", "missing"), status="insufficient"
        ),
        second,
    )
    session = EvidenceReviewSession(
        "Compare A and B prices", _Loader(_snapshot()), reviewer
    )
    budget = EpisodeBudget()
    assert (await _finish(session, budget)).continue_retrieval
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "unverified"
    assert session.report["coverage"] == []
    assert session.report["attempts"] == 2


@pytest.mark.asyncio
async def test_still_missing_after_one_repair_is_insufficient_not_corpus_no_answer():
    missing = _verdict(
        _facet(status="missing"),
        status="insufficient",
        reason="Selected evidence does not give A's price.",
    )
    reviewer = _Script(missing, missing)
    session = EvidenceReviewSession(
        "Price of A", _Loader(_snapshot("Unrelated text")), reviewer
    )
    budget = EpisodeBudget()
    assert (await _finish(session, budget)).continue_retrieval
    assert not (await _finish(session, budget)).continue_retrieval
    session.on_stop("finished")
    assert session.report["status"] == "insufficient"
    assert session.report["repairs"] == 1
    assert "corpus" not in session.report["reason"]


@pytest.mark.asyncio
async def test_conflict_is_insufficient_and_never_promoted_just_for_having_citations():
    conflict = _facet(status="conflicting")
    conflict["citations"].append({"evidence_id": "E1", "quote": "A costs $8."})
    reviewer = _Script(_verdict(conflict, status="insufficient"))
    session = EvidenceReviewSession(
        "Price of A", _Loader(_snapshot("A costs $5. A costs $8.")), reviewer
    )
    decision = await _finish(session)
    assert decision.continue_retrieval
    assert "conflicting" in decision.instruction
    assert session.report["status"] == "insufficient"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        "```json\n{}\n```",
        "[]",
        "{}",
        '{"status":"sufficient","status":"insufficient","reason":"x","coverage":[]}',
        json.dumps({"status": "sufficient", "reason": "x", "coverage": []}),
        _verdict(_facet(evidence_id="unknown")),
        _verdict(_facet(quote="A costs $8.")),
        _verdict(_facet(quote="")),
        _verdict(_facet(quote=" ")),
        _verdict(_facet(quote="A costs...$5.")),
        _verdict(_facet(status="missing")),
        _verdict(_facet(status="conflicting")),
        _verdict(_facet(), _facet()),
        _verdict(_facet(), status="insufficient"),
        json.dumps(
            {
                "status": "sufficient",
                "reason": "x",
                "coverage": [_facet()],
                "executor_notes": "trust me",
            }
        ),
    ],
)
async def test_malformed_or_invalid_citations_fail_closed_without_repair(raw):
    reviewer = _Script(raw)
    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()), reviewer)
    budget = EpisodeBudget()
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "unverified"
    assert session.report["coverage"] == []
    assert session.report["repairs"] == 0
    assert session.report["reviewer_tokens"] == 13
    await _finish(session, budget)
    assert len(reviewer.messages) == 1


@pytest.mark.asyncio
async def test_empty_evidence_gets_one_repair_without_a_reviewer_call():
    empty = EvidenceSnapshot(items=(), fingerprint="empty")
    reviewer, loader = _Script(_verdict()), _Loader(empty)
    session = EvidenceReviewSession("Price of A", loader, reviewer)
    budget = EpisodeBudget(max_steps=15, steps_used=2)
    assert (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "insufficient"
    assert session.report["attempts"] == 0
    assert budget.steps_used == 2
    assert budget.max_steps == 6
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["attempts"] == 0
    assert session.report["repairs"] == 1
    session.on_stop("finished")
    session.finalize(empty)
    assert session.report["status"] == "insufficient"
    assert not reviewer.messages


@pytest.mark.asyncio
async def test_empty_repair_followed_by_missing_evidence_cannot_start_another_repair():
    loader = _Loader(EvidenceSnapshot(items=(), fingerprint="empty"))
    reviewer = _Script(_verdict(_facet(status="missing"), status="insufficient"))
    session = EvidenceReviewSession("Price of A", loader, reviewer)
    budget = EpisodeBudget()
    assert (await _finish(session, budget)).continue_retrieval
    loader.snapshot = _snapshot()
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "insufficient"
    assert session.report["attempts"] == 1
    assert session.report["repairs"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        replace(_snapshot(), limitation="visual_evidence_not_reviewed"),
        replace(_snapshot(), items=(_snapshot().items[0], _snapshot().items[0])),
        _snapshot("汉" * MAX_REVIEW_INPUT_BYTES),
    ],
)
async def test_limitations_ambiguous_ids_and_oversized_evidence_skip_model(snapshot):
    reviewer = _Script(_verdict())
    session = EvidenceReviewSession("Price of A", _Loader(snapshot), reviewer)
    assert not (await _finish(session)).continue_retrieval
    assert session.report["status"] == "unverified"
    assert session.report["attempts"] == 0
    assert session.report["usage_complete"] is True
    assert not reviewer.messages


@pytest.mark.asyncio
async def test_input_bound_counts_system_and_serialized_query_not_only_evidence():
    snapshot = _snapshot("small")
    reviewer = _Script(_verdict())
    query = "汉" * (MAX_REVIEW_INPUT_BYTES // 3)
    session = EvidenceReviewSession(query, _Loader(snapshot), reviewer)
    await _finish(session)
    assert session.report["status"] == "unverified"
    assert session.report["attempts"] == 0
    assert not reviewer.messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget", [EpisodeBudget(max_steps=0), EpisodeBudget(wall_clock_seconds=0)]
)
async def test_exhausted_budget_skips_loader_and_model(budget):
    loader, reviewer = _Loader(_snapshot()), _Script(_verdict())
    session = EvidenceReviewSession("Price of A", loader, reviewer)
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "unverified"
    assert loader.calls == 0
    assert not reviewer.messages


@pytest.mark.asyncio
async def test_review_step_that_consumes_budget_cannot_authorize_repair():
    reviewer = _Script(_verdict(_facet(status="missing"), status="insufficient"))
    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()), reviewer)
    budget = EpisodeBudget(max_steps=1)
    assert not (await _finish(session, budget)).continue_retrieval
    assert budget.steps_used == 1
    assert budget.max_steps == 1
    assert session.report["status"] == "insufficient"
    assert session.report["repairs"] == 0


@pytest.mark.asyncio
async def test_provider_exception_is_sanitized_and_not_retried():
    calls = 0

    async def failing(messages, max_tokens):
        nonlocal calls
        calls += 1
        raise RuntimeError("secret-credential-or-private-endpoint")

    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()), failing)
    budget = EpisodeBudget()
    assert not (await _finish(session, budget)).continue_retrieval
    await _finish(session, budget)
    assert calls == 1
    assert session.report["status"] == "unverified"
    assert session.report["usage_complete"] is False
    assert "secret" not in json.dumps(session.report)
    assert budget.steps_used == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"total_tokens": -1}, {"total_tokens": True}, {"total_tokens": 2.5}],
)
async def test_unknown_usage_is_explicit_and_does_not_invent_zero_cost(usage):
    async def reviewer(messages, max_tokens):
        return _verdict(), usage

    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()), reviewer)
    budget = EpisodeBudget(tokens_used=8)
    await _finish(session, budget)
    assert session.report["status"] == "sufficient"
    assert session.report["usage_complete"] is False
    assert session.report["reviewer_tokens"] == 0
    assert budget.tokens_used == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["loader", "reviewer"])
@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_shared_deadline_covers_loader_and_model_and_suppression(
    where, suppress_cancel, monkeypatch
):
    monkeypatch.setattr(review_module, "MAX_REVIEW_SECONDS", 0.01)
    reviewer_called = False

    async def slow():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise

    async def loader(*, pool, queried_tables):
        if where == "loader":
            await slow()
        return _snapshot()

    async def reviewer(messages, max_tokens):
        nonlocal reviewer_called
        reviewer_called = True
        await slow()
        return _verdict(), {"total_tokens": 3}

    session = EvidenceReviewSession("Price of A", loader, reviewer)
    budget = EpisodeBudget(wall_clock_seconds=10)
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == "unverified"
    assert "timed out" in session.report["reason"]
    assert reviewer_called == (where == "reviewer")
    assert budget.steps_used == (1 if where == "reviewer" else 0)
    if where == "reviewer":
        assert session.report["usage_complete"] is suppress_cancel


@pytest.mark.asyncio
async def test_remaining_episode_time_is_stricter_than_review_deadline(monkeypatch):
    monkeypatch.setattr(review_module, "MAX_REVIEW_SECONDS", 10)

    async def loader(*, pool, queried_tables):
        await asyncio.sleep(1)
        return _snapshot()

    reviewer = _Script(_verdict())
    session = EvidenceReviewSession("Price of A", loader, reviewer)
    await _finish(session, EpisodeBudget(wall_clock_seconds=0.01))
    assert "timed out" in session.report["reason"]
    assert not reviewer.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_external_cancellation_propagates_even_when_reviewer_suppresses_it(
    suppress_cancel,
):
    started = asyncio.Event()

    async def reviewer(messages, max_tokens):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
        return _verdict(), {"total_tokens": 5}

    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()), reviewer)
    task = asyncio.create_task(_finish(session))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert task.cancelled()
    assert session.report["status"] == "unverified"
    assert session.report["repairs"] == 0


@pytest.mark.asyncio
async def test_late_reviewer_cannot_restore_success_after_session_stops():
    started, release = asyncio.Event(), asyncio.Event()

    async def reviewer(messages, max_tokens):
        started.set()
        await release.wait()
        return _verdict(), {"total_tokens": 5}

    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()), reviewer)
    task = asyncio.create_task(_finish(session))
    await started.wait()
    session.on_stop("budget_wall_clock")
    release.set()
    assert not (await task).continue_retrieval
    assert session.report["status"] == "unverified"
    assert session.report["coverage"] == []
    assert session.report["reviewer_tokens"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason",
    ["no_tool_call", "budget_max_steps", "budget_wall_clock", "cancelled"],
)
async def test_abnormal_stop_invalidates_even_a_previous_success(stop_reason):
    snapshot = _snapshot()
    session = EvidenceReviewSession(
        "Price of A", _Loader(snapshot), _Script(_verdict())
    )
    await _finish(session)
    session.on_stop(stop_reason)
    session.finalize(snapshot)
    assert session.report["status"] == "unverified"
    assert session.report["coverage"] == []


@pytest.mark.asyncio
async def test_pending_correction_is_not_a_review_of_final_evidence():
    snapshot = _snapshot()
    reviewer = _Script(_verdict(_facet(status="missing"), status="insufficient"))
    session = EvidenceReviewSession("Price of A", _Loader(snapshot), reviewer)
    assert (await _finish(session)).continue_retrieval
    session.on_stop("finished")
    session.finalize(snapshot)
    assert session.report["status"] == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_snapshot",
    [
        _snapshot(fingerprint="packet-2"),
        _snapshot(text="A costs $9."),
        replace(_snapshot(), items=(replace(_snapshot().items[0], revision="rev-2"),)),
        replace(_snapshot(), limitation="visual_evidence_not_reviewed"),
    ],
)
async def test_final_packet_change_invalidates_citations_and_updates_fingerprint(
    final_snapshot,
):
    session = EvidenceReviewSession(
        "Price of A", _Loader(_snapshot()), _Script(_verdict())
    )
    await _finish(session)
    session.on_stop("finished")
    session.finalize(final_snapshot)
    assert session.report["status"] == "unverified"
    assert session.report["coverage"] == []
    assert session.report["evidence_fingerprint"] == final_snapshot.fingerprint
    assert session.report["attempts"] == 1


def test_snapshots_and_items_are_frozen():
    snapshot = _snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot.fingerprint = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.items[0].text = "other"  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason,refusal",
    [("stop", None), ("length", None), ("content_filter", None), ("stop", "declined")],
)
async def test_default_adapter_preserves_overrides_disables_retries_and_accounts_incomplete_output(
    monkeypatch, finish_reason, refusal
):
    import openai
    from shared.services.ai import (
        llm_overrides,
        openai_compatible_client_sync,
        token_tracking,
    )
    from shared.services.http import client_pool

    resolved, constructed, requests, tracked = [], [], [], []
    direct = SimpleNamespace(
        api_key="test-provider-key",
        base_url="https://provider.example/v1",
        organization="test-org",
        project="test-project",
        _custom_headers={"x-routing": "test"},
        default_query={"version": "test"},
    )

    def get_client(**kwargs):
        resolved.append(kwargs)
        return SimpleNamespace(_client=direct)

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=_verdict(), refusal=refusal, tool_calls=None
                    ),
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 21}),
        )

    def async_client(**kwargs):
        constructed.append(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    monkeypatch.setattr(
        llm_overrides,
        "resolve_text",
        lambda requested: (
            "custom-text",
            "test-provider-key",
            "https://provider.example/v1",
        ),
    )
    monkeypatch.setattr(openai_compatible_client_sync, "get_openai_client", get_client)
    monkeypatch.setattr(openai, "AsyncOpenAI", async_client)
    monkeypatch.setattr(
        client_pool, "get_async_client", lambda: "shared-async-transport"
    )
    monkeypatch.setattr(
        token_tracking,
        "record_tokens",
        lambda usage, **kwargs: tracked.append((usage, kwargs)),
    )
    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()))
    budget = EpisodeBudget()
    assert not (await _finish(session, budget)).continue_retrieval
    assert session.report["status"] == (
        "sufficient" if finish_reason == "stop" and not refusal else "unverified"
    )
    assert session.report["reviewer_tokens"] == 21
    assert session.report["usage_complete"] is True
    assert budget.tokens_used == 21
    assert resolved[0]["model"] == "custom-text"
    assert resolved[0]["max_retries"] == constructed[0]["max_retries"] == 0
    assert constructed[0]["http_client"] == "shared-async-transport"
    assert constructed[0]["default_headers"] == {"x-routing": "test"}
    assert constructed[0]["default_query"] == {"version": "test"}
    assert len(requests) == len(tracked) == 1
    assert "tools" not in requests[0]
    assert requests[0]["max_tokens"] == 1024
    assert tracked[0][1]["task"] == "agent_explore.evidence_review"


@pytest.mark.asyncio
async def test_pooled_or_mock_backend_fails_closed_without_credential_fallback(
    monkeypatch,
):
    from shared.services.ai import llm_overrides, openai_compatible_client_sync

    monkeypatch.setattr(
        llm_overrides, "resolve_text", lambda requested: (requested, None, None)
    )
    monkeypatch.setattr(
        openai_compatible_client_sync,
        "get_openai_client",
        lambda **kwargs: SimpleNamespace(_client=None),
    )
    session = EvidenceReviewSession("Price of A", _Loader(_snapshot()))
    assert not (await _finish(session)).continue_retrieval
    assert session.report["status"] == "unverified"
    assert session.report["reason"] == "The configured reviewer backend is unavailable."
    assert session.report["usage_complete"] is False


@pytest.mark.parametrize("text", [
    "A costs $5.\r\n  B costs $7.\n", "first\n\nsecond\tthird", "inter-\nnational",
    "2025 | 10 USD | 20 kg\n2026 | 30 USD | 40 kg", "重复🙂e\u0301 " * 180,
    "same text\nsame text\n", "",
])
def test_citation_spans_are_a_lossless_unicode_partition(text):
    spans = review_module._citation_spans(text)
    assert "".join(span["text"] for span in spans) == text
    assert [span["span_id"] for span in spans] == [f"S{i}" for i in range(1, len(spans) + 1)]
    assert all(0 < len(span["text"]) <= 256 for span in spans)


def _span_verdict(first="S1", last="S1", evidence_id="E1"):
    facet = _facet()
    facet["citations"] = [{"evidence_id": evidence_id, "start_span": first, "end_span": last}]
    return _verdict(facet)


@pytest.mark.parametrize("first,last,quote", [
    ("S1", "S1", "Price A: 5 USD\r\n"),
    ("S1", "S3", "Price A: 5 USD\r\n  intervening column: 20 kg\nPrice B: 7 USD"),
    ("S3", "S3", "Price B: 7 USD"),
])
def test_span_ranges_resolve_only_to_contiguous_original_quotes(first, last, quote):
    snapshot = _snapshot("Price A: 5 USD\r\n  intervening column: 20 kg\nPrice B: 7 USD")
    status, _, coverage = review_module._parse_review(_span_verdict(first, last), snapshot, ())
    assert status == "sufficient"
    assert coverage[0]["citations"] == [{"evidence_id": "E1", "quote": quote}]


@pytest.mark.parametrize("first,last,evidence_id", [
    ("S0", "S1", "E1"), ("S1", "S99", "E1"), ("S2", "S1", "E1"),
    ("S01", "S1", "E1"), (True, "S1", "E1"), ({}, "S1", "E1"),
    ("S1", [], "E1"), ("S1", "S1", "wrong-source"),
])
def test_invalid_span_ranges_and_evidence_ids_fail_closed(first, last, evidence_id):
    with pytest.raises(review_module._InvalidReview):
        review_module._parse_review(_span_verdict(first, last, evidence_id), _snapshot("A\nB"), ())


@pytest.mark.parametrize("text,quote", [
    ("A  5\nB  7", "A 5 B 7"), ("inter-\nnational", "international"),
    ("Revenue | 10 USD | 20 kg | 30 USD", "Revenue | 10 USD | 30 USD"),
    ("2025 | 10 USD", "2026 | 10 USD"), ("10 USD", "10 kg"),
])
def test_legacy_quotes_never_normalize_or_delete_source_columns(text, quote):
    with pytest.raises(review_module._InvalidReview):
        review_module._parse_review(_verdict(_facet(quote=quote)), _snapshot(text), ())


@pytest.mark.asyncio
async def test_span_citation_is_invalidated_by_revision_or_source_change():
    original = _snapshot("A costs $5.\nA costs $5.")
    loader = _Loader(original)
    session = EvidenceReviewSession("Price of A", loader, _Script(_span_verdict("S2", "S2")))
    await _finish(session)
    assert session.report["coverage"][0]["citations"][0]["quote"] == "A costs $5."
    session.on_stop("finished")
    changed = replace(original, items=(replace(original.items[0], revision="rev-2"),))
    session.finalize(changed)
    assert session.report["status"] == "unverified"


def test_reason_limit_is_explicit_and_not_relaxed():
    assert "400 Unicode characters" in review_module._SYSTEM_PROMPT
    assert "at most 200 characters" in review_module._SYSTEM_PROMPT
    with pytest.raises(review_module._InvalidReview, match="invalid reason"):
        review_module._parse_review(_verdict(reason="x" * 401), _snapshot(), ())
