"""Required visual retrieval failures cannot become successful partial answers."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from shared.services.retrieval.required_query_embedding import (
    RequiredQueryEmbeddingException,
)
from shared.services.retrieval.agent_explore.dispatch import dispatch_tool_call
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolRegistry,
    ToolSpec,
)
from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext
from shared.services.retrieval.search import visual_discovery


@pytest.mark.asyncio
async def test_visual_discovery_marks_required_embedding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Mock()
    db.execute = AsyncMock()
    db.execute.return_value = Mock()
    db.execute.return_value.scalar.return_value = True
    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)

    def fail_embedding(*args: object, **kwargs: object) -> list[float]:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(visual_discovery, "embed_query", fail_embedding)
    with pytest.raises(RequiredQueryEmbeddingException):
        await visual_discovery.visual_page_discovery(
            db, user_id="u", namespace="n", query="q", top_k=1
        )


@pytest.mark.asyncio
async def test_registry_and_dispatch_propagate_required_embedding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.agent_explore import dispatch

    registry = ToolRegistry()

    async def fail_tool(ctx: ToolContext, args: dict[str, object]):
        raise RequiredQueryEmbeddingException(internal_message="provider unavailable")

    registry.register(
        ToolSpec(
            name="test.recall",
            description="test",
            json_schema={"type": "object"},
            run=fail_tool,
        )
    )
    monkeypatch.setattr(dispatch, "REGISTRY", registry)

    @asynccontextmanager
    async def open_db():
        yield Mock()

    with CorpusRevisionContext.bind({"d": "r"}):
        with pytest.raises(RequiredQueryEmbeddingException):
            await dispatch_tool_call(
                "test.recall", {}, db_factory=open_db, user_id="u", namespace="n"
            )


@pytest.mark.asyncio
async def test_invalid_tool_arguments_still_return_normal_error() -> None:
    registry = ToolRegistry()
    result = await registry.dispatch("unknown", Mock(), {})
    assert result.error == "unknown tool: unknown"
