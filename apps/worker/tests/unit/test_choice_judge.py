from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest  # noqa: E402

from app.services.document_parser.support.choice_judge import (  # noqa: E402
    FallbackChoiceJudge,
    TextLlmChoiceJudge,
)

_QUESTIONS = {"q": {"type": "choice", "criteria": {"merge": "m", "separate": "s"}}}
_ALLOWED = {"q": frozenset({"merge", "separate"})}


class _StaticJudge:
    def __init__(self, answer: dict[str, str] | None) -> None:
        self.answer = answer
        self.calls = 0

    def choose(self, **_kwargs: object) -> dict[str, str] | None:
        self.calls += 1
        return self.answer


def test_fallback_uses_second_judge_only_when_first_has_no_answer() -> None:
    first = _StaticJudge(None)
    second = _StaticJudge({"q": "merge"})
    third = _StaticJudge({"q": "separate"})
    chosen = FallbackChoiceJudge((first, second, third)).choose(
        state={}, questions=_QUESTIONS, required_ids=("q",), allowed=_ALLOWED
    )
    assert chosen == {"q": "merge"}
    assert (first.calls, second.calls, third.calls) == (1, 1, 0)


def test_fallback_returns_none_when_every_judge_fails() -> None:
    assert (
        FallbackChoiceJudge((_StaticJudge(None), _StaticJudge(None))).choose(
            state={}, questions=_QUESTIONS, required_ids=("q",), allowed=_ALLOWED
        )
        is None
    )


class _FakeTextClient:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.messages: list[dict[str, str]] = []
        self.usage_task = ""

    def chat_completion(self, *, messages: list[dict[str, str]], usage_task: str) -> str:
        self.messages = messages
        self.usage_task = usage_task
        if isinstance(self.reply, Exception):
            raise self.reply
        return json.dumps(self.reply)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"q": "merge"}, {"q": "merge"}),
        ({"q": "maybe"}, None),
        ({}, None),
        (["merge"], None),
        (RuntimeError("down"), None),
    ],
)
def test_text_llm_judge_validates_choices(
    monkeypatch: pytest.MonkeyPatch,
    reply: object,
    expected: dict[str, str] | None,
) -> None:
    client = _FakeTextClient(reply)
    monkeypatch.setattr(
        "shared.services.ai.llm_overrides.get_text_client",
        lambda: (client, "model"),
    )
    chosen = TextLlmChoiceJudge(usage_task="parser.test.choice").choose(
        state={"previous": "a"},
        questions=_QUESTIONS,
        required_ids=("q",),
        allowed=_ALLOWED,
    )
    assert chosen == expected
    assert client.usage_task == "parser.test.choice"
    payload = json.loads(client.messages[1]["content"])
    assert payload == {"state": {"previous": "a"}, "questions": _QUESTIONS}
