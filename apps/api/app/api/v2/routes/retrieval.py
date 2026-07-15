"""Retrieval API v2 routes."""

from __future__ import annotations

from app.api.dependencies.current_user import with_current_user
from app.api.v1.routes.retrieval import (
    RetrievalQueryRequest,
    RetrievalQueryResponse,
    execute_retrieval_query,
)
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.llm_config import LLMConfig

router = APIRouter(tags=["Retrieval"])


class RetrievalQueryRequestV2(RetrievalQueryRequest):
    """v2 retrieval query request with optional BYOK LLM credentials."""

    llm_config: LLMConfig | None = Field(
        None,
        description=(
            "Optional bring-your-own-key OpenAI-compatible LLM credentials. "
            "Use `provider` for a single multimodal model (both channels). "
            "Use `text` / `vision` to override one channel (or both). "
            "Missing channels without `provider` keep server defaults. "
            "When omitted, server defaults are used."
        ),
    )


@router.post("/query", response_model=RetrievalQueryResponse)
async def query_retrieval(
    payload: RetrievalQueryRequestV2,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await execute_retrieval_query(
        payload,
        current_user,
        db,
        llm_config=payload.llm_config,
    )
