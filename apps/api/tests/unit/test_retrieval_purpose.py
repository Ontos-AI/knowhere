"""The retrieval request's purpose reaches the retrieval plan unchanged."""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.v1.routes import retrieval
from app.api.v2.routes.retrieval import RetrievalQueryRequestV2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [({}, "explicit"), ({"purpose": "explicit"}, "explicit"), ({"purpose": "automatic"}, "automatic")],
)
async def test_purpose_reaches_retrieval(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, str], expected: str
) -> None:
    received: dict[str, Any] = {}

    async def run(**keywords: Any) -> dict[str, Any]:
        received.update(keywords)
        return {}

    monkeypatch.setattr(retrieval, "run_retrieval_query", run)

    await retrieval.execute_retrieval_query(
        RetrievalQueryRequestV2(namespace="ns", query="topic", **body),
        SimpleNamespace(user_id="user"),
        SimpleNamespace(in_transaction=lambda: False),
    )

    assert received["purpose"] == expected


def test_unknown_purpose_is_rejected() -> None:
    with pytest.raises(ValidationError):
        retrieval.RetrievalQueryRequest(query="topic", purpose="background")
