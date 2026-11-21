"""Visual search contributes independently of textual hits."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from shared.services.retrieval.search import visual_discovery


@pytest.mark.asyncio
async def test_visual_channel_runs_while_text_is_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visual_discovery.settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setattr(visual_discovery.settings, "VISUAL_RETRIEVAL_ENABLED", True)
    ready = asyncio.Event()

    async def text(db: AsyncSession, **kwargs: object) -> visual_discovery.DiscoveryResult:
        await asyncio.wait_for(ready.wait(), timeout=0.5)
        return visual_discovery.DiscoveryResult(status="discovery_done", payload={"fused_rows": []}, latency_ms=1)

    async def canSearch(db: AsyncSession, **kwargs: object) -> bool:
        return True

    async def visual(db: AsyncSession, **kwargs: object) -> visual_discovery.DiscoveryResult:
        assert db is not session
        ready.set()
        return visual_discovery.DiscoveryResult(status="discovery_done",
            payload={"fused_rows": [{"document_id": "scan", "chunk_id": "matching-page"}]}, latency_ms=1)

    monkeypatch.setattr(visual_discovery, "map_unit_discovery", text)
    monkeypatch.setattr(visual_discovery, "has_visual_query_embeddings", canSearch)
    monkeypatch.setattr(visual_discovery, "visual_page_discovery", visual)
    engine = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    try:
        async with AsyncSession(engine) as session:
            result = await visual_discovery.hybrid_discovery(session, user_id="user",
                namespace="parallel-content", query="a chart", top_k=5)
            assert [row["chunk_id"] for row in result.payload["fused_rows"]] == ["matching-page"]
    finally:
        await engine.dispose()
