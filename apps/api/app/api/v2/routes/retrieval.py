"""Retrieval API v2 routes."""

from __future__ import annotations

import asyncio
from typing import Any

from app.api.dependencies.current_user import with_current_user
from app.api.v1.routes.retrieval import (
    RetrievalQueryRequest,
    RetrievalQueryResponse,
    execute_retrieval_query,
)
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db, get_db_context
from shared.models.schemas.llm_config import LLMConfig
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.agent_tools import REGISTRY, ToolContext

router = APIRouter(tags=["Retrieval"])

KNOWHERE_NAMESPACE_HEADER = "x-knowhere-namespace"
INVOKE_CORPUS_TOOLS = frozenset(
    {
        "corpus.recall",
        "corpus.grep",
        "corpus.outline",
        "corpus.read",
        "corpus.query_table",
        "corpus.assets",
    }
)


class RetrievalQueryRequestV2(RetrievalQueryRequest):
    """v2 retrieval query request with optional BYOK LLM credentials."""

    llm_config: LLMConfig | None = Field(
        None,
        description=(
            "Optional bring-your-own-key OpenAI-compatible LLM credentials. "
            "Flat {api_key, model, base_url} applies to both channels. "
            "Use models.{text,vision} for different model ids on the same "
            "endpoint, or text/vision objects for different endpoints. "
            "When omitted, server defaults are used."
        ),
    )


async def wait_for_client_disconnect(request: Request) -> None:
    # FastAPI has already consumed the request body to validate the payload.
    while (await request.receive())["type"] != "http.disconnect":
        pass


@router.post("/query", response_model=RetrievalQueryResponse)
async def query_retrieval(
    payload: RetrievalQueryRequestV2,
    request: Request,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = asyncio.create_task(
        execute_retrieval_query(
            payload, current_user, db, llm_config=payload.llm_config
        )
    )
    disconnect = asyncio.create_task(wait_for_client_disconnect(request))
    try:
        completed, _ = await asyncio.wait(
            (query, disconnect), return_when=asyncio.FIRST_COMPLETED
        )
        if query in completed:
            return await query
        await disconnect
        raise HTTPException(status_code=499, detail="Client disconnected")
    finally:
        for task in (query, disconnect):
            if not task.done():
                task.cancel()
        # Join before FastAPI releases the request-owned database session.
        await asyncio.gather(query, disconnect, return_exceptions=True)


class RetrievalToolInvokeRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class RetrievalToolInvokeResponse(BaseModel):
    text: str
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


@router.post("/tools/invoke", response_model=RetrievalToolInvokeResponse)
async def invoke_retrieval_tool(
    payload: RetrievalToolInvokeRequest,
    request: Request,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.tool not in INVOKE_CORPUS_TOOLS:
        raise HTTPException(status_code=400, detail="tool is not supported")
    namespace = normalize_retrieval_namespace(
        request.headers.get(KNOWHERE_NAMESPACE_HEADER)
    )
    result = await REGISTRY.dispatch(
        payload.tool,
        ToolContext(
            db=db,
            user_id=current_user.user_id,
            namespace=namespace,
            db_factory=get_db_context,
        ),
        payload.arguments,
    )
    return RetrievalToolInvokeResponse(
        text=result.text,
        payload=result.payload,
        refs=result.refs,
        error=result.error,
    )
