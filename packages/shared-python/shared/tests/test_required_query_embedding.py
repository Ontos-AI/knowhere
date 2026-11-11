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


@pytest.mark.asyncio
async def test_hybrid_does_not_return_lexical_hits_after_required_visual_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(visual_discovery, "map_unit_discovery", AsyncMock(return_value=DiscoveryResult(
        status="discovery_done", payload={"fused_rows": [{"chunk_id": "lexical-hit"}]},
    )))
    monkeypatch.setattr(visual_discovery, "visual_page_discovery", AsyncMock(side_effect=RequiredQueryEmbeddingException(internal_message="provider unavailable")))
    with pytest.raises(RequiredQueryEmbeddingException):
        await visual_discovery.hybrid_discovery(Mock(), user_id="u", namespace="n", query="q", top_k=1)


@pytest.mark.asyncio
async def test_hybrid_reuses_one_query_vector_across_coarse_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    has_embeddings = Mock(scalar=Mock(return_value=True))
    # Initial visual retrieval locates page 5. The follow-up section searches
    # resolve the evidence pages for four coarse keyword hits.
    database_results: list[Mock] = []
    for page in (5, 1, 2, 3, 4):
        database_results.extend([
            has_embeddings,
            Mock(all=Mock(return_value=[Mock(_mapping={
                "document_id": "d", "chunk_id": f"p{page}", "page_num": page, "similarity": 0.9,
            })])),
        ])
    db = Mock(execute=AsyncMock(side_effect=database_results))
    embed = Mock(return_value=[0.25, 0.5])
    lexical_rows: list[dict[str, object]] = [
        {"document_id": "d", "chunk_id": f"p{page}", "section_path": str(page),
         "chunk_type": "page", "chunk_metadata": {"enrichment_state": "coarse"}}
        for page in range(1, 5)
    ]
    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(visual_discovery, "embed_query", embed)
    monkeypatch.setattr(visual_discovery, "map_unit_discovery", AsyncMock(return_value=DiscoveryResult(
        status="discovery_done", payload={"fused_rows": lexical_rows},
    )))

    result = await visual_discovery.hybrid_discovery(db, user_id="u", namespace="n", query="q", top_k=5)

    assert {row["chunk_id"]: row.get("matched_page_nums") for row in result.payload["fused_rows"]} == {
        f"p{page}": [page] for page in range(1, 6)
    }
    embed.assert_called_once_with("q", usage_task="retrieval.query_embed")


async def test_hybrid_keeps_lexical_pages_when_visual_hit_uses_another_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    lexical_row: dict[str, object] = {
        "document_id": "document",
        "chunk_id": "shared-chunk",
        "build_id": "captured-build",
        "chunk_type": "page",
        "chunk_metadata": {"enrichment_state": "refined"},
        "matched_page_nums": [2],
        "score": 0.7,
    }
    visual_row: dict[str, object] = {
        "document_id": "document",
        "chunk_id": "shared-chunk",
        "visual_build_id": "later-build",
        "matched_page_nums": [7],
    }
    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(
        visual_discovery, "map_unit_discovery",
        AsyncMock(return_value=DiscoveryResult(
            status="discovery_done", payload={"fused_rows": [lexical_row]},
        )),
    )
    monkeypatch.setattr(
        visual_discovery, "visual_page_discovery",
        AsyncMock(return_value=DiscoveryResult(
            status="discovery_done", payload={"fused_rows": [visual_row]},
        )),
    )

    result = await visual_discovery.hybrid_discovery(
        Mock(), user_id="u", namespace="n", query="q", top_k=1,
    )

    assert result.payload["fused_rows"] == [lexical_row]


async def test_section_lighting_does_not_copy_pages_from_another_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    lexical_row = {
        "document_id": "document",
        "chunk_id": "shared-chunk",
        "build_id": "captured-build",
        "chunk_type": "page",
        "section_path": "Root / Section",
        "chunk_metadata": {"enrichment_state": "coarse"},
    }
    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(
        visual_discovery, "visual_page_discovery",
        AsyncMock(return_value=DiscoveryResult(status="discovery_done", payload={
            "fused_rows": [{
                "document_id": "document",
                "chunk_id": "shared-chunk",
                "visual_build_id": "later-build",
                "matched_page_nums": [7],
            }],
        })),
    )

    await visual_discovery.light_unrefined_section_pages(
        Mock(), user_id="u", namespace="n", query="q", rows=[lexical_row],
    )

    assert "matched_page_nums" not in lexical_row


async def test_inline_visual_assets_hydrate_against_the_scored_page_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead

    reader = Mock(
        scope=Mock(namespace="n", ownerId="u"),
        manifestId=None,
        builds={"d": "captured-build"},
    )
    database = Mock(execute=AsyncMock(side_effect=[
        Mock(scalar=Mock(return_value=True)),
        Mock(all=Mock(return_value=[Mock(_mapping={
            "document_id": "d",
            "chunk_id": "page",
            "chunk_type": "page",
            "visual_build_id": "captured-build",
            "chunk_metadata": {"connect_to": [{"target": "chart"}]},
            "page_num": 3,
            "similarity": 0.8,
        })])),
    ]))
    connected = AsyncMock(return_value=[{
        "document_id": "d", "chunk_id": "chart", "chunk_type": "image",
        "build_id": "captured-build",
    }])
    monkeypatch.setattr(PublishedLexicalRead, "current", lambda: reader)
    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(visual_discovery, "embed_query", Mock(return_value=[0.25, 0.5]))
    monkeypatch.setattr(visual_discovery, "hydrate_connected_target_rows", connected)

    result = await visual_discovery.visual_page_discovery(
        database, user_id="u", namespace="n", query="q", top_k=1, chunk_types={"image"},
    )

    page = connected.await_args.kwargs["rows"][0]
    assert page["build_id"] == "captured-build"
    assert page["uses_inline_build"] is True
    assert result.payload["fused_rows"][0]["visual_build_id"] == "captured-build"
