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
            "Provide text and/or vision provider configs; each slot overrides "
            "only its own channel (missing slots keep server defaults). "
            "To use one multimodal model for both, set text and vision to the "
            "same credentials. When omitted, server defaults are used."
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
