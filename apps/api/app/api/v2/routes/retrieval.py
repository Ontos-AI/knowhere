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
from shared.services.retrieval.stats.recorder import (
    schedule_active_day_update,
    schedule_document_usage_update,
)
from shared.services.retrieval.stats.service import record_document_usage

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
    records_usage: bool = Field(
        False,
        description=(
            "The caller's Agent named the documents in arguments, so they count "
            "as used. Leave false when the caller filled the document list itself."
        ),
    )


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
    # A corpus tool call is an Agent working in the namespace, so the day is active.
    schedule_active_day_update(user_id=current_user.user_id, namespace=namespace)
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
    if payload.records_usage and result.error is None:
        named = documents_named_by_tool_arguments(payload.arguments)
        if named:
            schedule_document_usage_update(
                user_id=current_user.user_id, namespace=namespace, items=named
            )
    return RetrievalToolInvokeResponse(
        text=result.text,
        payload=result.payload,
        refs=result.refs,
        error=result.error,
    )


def documents_named_by_tool_arguments(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """The documents, and chunks where given, a corpus tool call was pointed at.

    A call that names a document (read refs, a search scope, a table's
    document_id) is the Agent looking into that file, so it counts as a use.
    """
    named: list[dict[str, Any]] = []
    for key in ("refs", "scope"):
        items = arguments.get(key)
        if isinstance(items, list):
            named.extend(
                {"document_id": item.get("document_id"), "chunk_id": item.get("chunk_id")}
                for item in items
                if isinstance(item, dict) and item.get("document_id")
            )
    if arguments.get("document_id"):
        named.append(
            {"document_id": arguments.get("document_id"), "chunk_id": arguments.get("chunk_id")}
        )
    return named


class RetrievalUsageItem(BaseModel):
    document_id: str = Field(..., min_length=1)
    chunk_id: str | None = None


class RetrievalUsageRequest(BaseModel):
    namespace: str | None = None
    items: list[RetrievalUsageItem] = Field(..., min_length=1)


class RetrievalUsageResponse(BaseModel):
    recorded: int


@router.post("/usage", response_model=RetrievalUsageResponse)
async def record_retrieval_usage(
    payload: RetrievalUsageRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record documents the caller's Agent actually used.

    Items outside the caller's documents in this namespace are ignored.
    """
    recorded = await record_document_usage(
        db,
        user_id=current_user.user_id,
        namespace=normalize_retrieval_namespace(payload.namespace),
        items=[item.model_dump() for item in payload.items],
    )
    await db.commit()
    return RetrievalUsageResponse(recorded=recorded)
