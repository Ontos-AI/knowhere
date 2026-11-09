"""Required visual retrieval failures cannot become successful partial answers."""

import asyncio
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


@pytest.mark.asyncio
async def test_hybrid_does_not_return_lexical_hits_after_required_visual_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(visual_discovery, "map_unit_discovery", AsyncMock(return_value=DiscoveryResult(
        status="discovery_done", payload={"fused_rows": [{"chunk_id": "lexical-hit"}]},
    )))
    db = Mock()
    db.execute = AsyncMock()
    db.execute.return_value = Mock()
    db.execute.return_value.scalar.return_value = True
    monkeypatch.setattr(visual_discovery, "embed_query", Mock(side_effect=RuntimeError("provider unavailable")))

    with pytest.raises(RequiredQueryEmbeddingException):
        await visual_discovery.hybrid_discovery(db, user_id="u", namespace="n", query="q", top_k=1)


@pytest.mark.asyncio
async def test_hybrid_requests_query_embedding_during_lexical_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    db = Mock()
    db.execute = AsyncMock()
    db.execute.return_value = Mock()
    db.execute.return_value.scalar.return_value = True
    db.execute.return_value.all.return_value = []
    loop = asyncio.get_running_loop()
    embedding_started = asyncio.Event()

    def embed(*args: object, **kwargs: object) -> list[float]:
        loop.call_soon_threadsafe(embedding_started.set)
        return [0.25, 0.5]

    async def discover_lexically(*args: object, **kwargs: object) -> DiscoveryResult:
        await asyncio.wait_for(embedding_started.wait(), timeout=2)
        assert db.execute.await_count == 1
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": [{"chunk_id": "lexical-hit"}]})

    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(visual_discovery, "embed_query", embed)
    monkeypatch.setattr(visual_discovery, "map_unit_discovery", discover_lexically)

    result = await visual_discovery.hybrid_discovery(db, user_id="u", namespace="n", query="q", top_k=1)

    assert result.payload["fused_rows"] == [{"chunk_id": "lexical-hit"}]
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_hybrid_reuses_one_query_vector_across_coarse_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    db = Mock()
    db.execute = AsyncMock(return_value=Mock())
    db.execute.return_value.scalar.return_value = True
    embed = Mock(return_value=[0.25, 0.5])
    lexical_rows: list[dict[str, object]] = [
        {"document_id": "d", "chunk_id": f"p{page}", "section_path": str(page),
         "chunk_type": "page", "chunk_metadata": {"enrichment_state": "coarse"}}
        for page in range(1, 5)
    ]

    async def rank_pages(*args: object, **kwargs: object) -> DiscoveryResult:
        targets = kwargs.get("section_targets")
        page = int(targets[0][1]) if isinstance(targets, list) else 5
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": [
            {"document_id": "d", "chunk_id": f"p{page}", "matched_page_nums": [page]}
        ]})

    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(visual_discovery, "embed_query", embed)
    monkeypatch.setattr(visual_discovery, "_search_visual_pages", rank_pages)
    monkeypatch.setattr(visual_discovery, "map_unit_discovery", AsyncMock(return_value=DiscoveryResult(
        status="discovery_done", payload={"fused_rows": lexical_rows},
    )))

    result = await visual_discovery.hybrid_discovery(db, user_id="u", namespace="n", query="q", top_k=5)

    assert {row["chunk_id"]: row.get("matched_page_nums") for row in result.payload["fused_rows"]} == {
        f"p{page}": [page] for page in range(1, 6)
    }
    assert embed.call_count == 1
