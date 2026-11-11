"""PG maintenance/backend gates precede cache and small-corpus execution."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from tests.contract.test_lexical_maintenance_contract import publishedInput as publishedInput  # noqa: F401


@pytest.mark.parametrize("version", ("v1", "v2"))
async def test_postgresql_query_releases_admission_read_transaction(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    async with developer_api_client_factory() as api_client:
        from app.api.v1.routes import retrieval
        from app.services.rate_limit.tier_service import TierService

        run_query = retrieval.run_retrieval_query
        transactions_at_query_start: list[bool] = []

        async def miss_tier_cache(*_arguments: object) -> None:
            return None

        async def check_request_transaction(
            *, db: AsyncSession, **arguments: Any,
        ) -> dict[str, Any]:
            transactions_at_query_start.append(db.in_transaction())
            return await run_query(db=db, **arguments)

        monkeypatch.setattr(TierService, "_get_cached_tier", staticmethod(miss_tier_cache))
        monkeypatch.setattr(retrieval, "run_retrieval_query", check_request_transaction)

        response = await api_client.post(
            f"/api/{version}/retrieval/query",
            json={"query": "empty local corpus", "use_agentic": False},
        )

    assert response.status_code == 200
    assert response.json()["results"] == []
    assert transactions_at_query_start == [False]


@pytest.mark.parametrize("useAgentic", [False, True])
def test_pg_read_gate_rejects_before_cached_or_small_corpus_success(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str], useAgentic: bool) -> None:
    from shared.core.config import settings
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.execution.query_request import RetrievalQuery

    engine, _, namespace, _ = publishedInput
    with Session(engine) as db:
        control = db.get(LexicalBackendControl, 1)
        assert control is not None
        control.backend = "postgresql"
        control.mode = "gated"
        db.commit()
    touched: list[str] = []
    async def cached(**arguments: object) -> tuple[int, dict[str, object]]:
        touched.append("cache")
        return 1, {"results": [], "query": "query", "namespace": namespace}
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
    monkeypatch.setattr(plan, "_read_cached_response", cached)
    async def exercise() -> None:
        asyncEngine = create_async_engine(make_url(engine.url).set(drivername="postgresql+asyncpg"))
        try:
            async with AsyncSession(asyncEngine) as db:
                request: RetrievalQuery = RetrievalQuery(db=db, user_id="maintenance-user", namespace=namespace, query="query", top_k=100, exclude_document_ids=[], exclude_sections=[], use_agentic=useAgentic)
                with pytest.raises(UnavailableException):
                    await plan.RetrievalExecutionPlan(request).execute()
        finally:
            await asyncEngine.dispose()
    asyncio.run(exercise())
    assert touched == []
