"""PG maintenance/backend gates precede cache and small-corpus execution."""

import asyncio

import pytest
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from tests.contract.test_lexical_maintenance_contract import publishedInput as publishedInput  # noqa: F401


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
