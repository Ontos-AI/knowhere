"""Retrieval API routes for evidence-only agentic retrieval."""

from __future__ import annotations

from typing import Any, Literal

from app.api.dependencies.current_user import with_current_user
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.llm_config import LLMConfig
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.app_service import run_retrieval_query
from shared.services.retrieval.settings import DEFAULT_TOP_K, VALID_CHUNK_TYPES, normalize_chunk_types

router = APIRouter(tags=["Retrieval"])


class ExcludeSection(BaseModel):
    document_id: str
    section_path: str


class RetrievalQueryRequest(BaseModel):
    namespace: str | None = Field(
        None,
        max_length=255,
        description="Effective namespace; defaults to default",
    )
    query: str
    top_k: int = DEFAULT_TOP_K
    include_document_ids: list[str] | None = Field(
        None, description="Document allowlist: null means unrestricted, [] means empty; exclusions win."
    )
    exclude_document_ids: list[str] = Field(
        default_factory=list,
        description="Documents excluded from every retrieval path. Exclusions override include_document_ids.",
    )
    exclude_sections: list[ExcludeSection] = Field(default_factory=list)
    data_type: int = Field(
        1,
        ge=1,
        le=8,
        description=(
            "Chunk type filter: 1=all, 2=text, 3=image, 4=table, "
            "5=text+image, 6=text+table, 7=page, 8=text+image+table"
        ),
        deprecated=True,
    )
    chunk_types: list[str] | None = Field(
        None,
        description=(
            "Allowed chunk types for retrieval. "
            "Options: text, image, table, page. "
            "None or empty means all types. Overrides data_type if provided."
        ),
    )
    signal_paths: list[str] = Field(
        default_factory=list, description="Path keywords for include/exclude filtering"
    )
    filter_mode: Literal["delete", "keep"] = Field(
        "delete", description="Signal path filter mode"
    )
    rerank: bool = Field(False, description="Enable LLM reranking after RRF fusion")
    threshold: float = Field(0.0, ge=0.0, description="Minimum RRF score threshold")
    internal_recall_k: int | None = Field(
        None, ge=1, description="Override per-channel recall count"
    )
    use_agentic: bool | None = Field(
        None,
        description=(
            "Turns agent retrieval on or off. False uses classic map-unit "
            "BM25. Unset or true turns agent retrieval on; "
            "AGENT_EXPLORE_HARNESS then selects Cursor (cursor_sdk) or "
            "Knowhere's own harness (openai)."
        ),
    )
    agent_explore_model: str | None = Field(
        None,
        max_length=255,
        description=(
            "Optional Cursor model when agent retrieval is on and "
            "AGENT_EXPLORE_HARNESS=cursor_sdk. Omit or leave empty to use "
            "AGENT_EXPLORE_CURSOR_MODEL (default composer-2.5). Ignored when "
            "agent retrieval is off or the harness is openai."
        ),
    )
    conversation_id: str | None = Field(
        None,
        max_length=255,
        description=(
            "Caller-supplied conversation identifier, threaded through for "
            "retrieval tracing. Does not affect caching or result content."
        ),
    )

    @field_validator("chunk_types")
    @classmethod
    def validate_chunk_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for ct in v:
            if ct not in VALID_CHUNK_TYPES:
                raise ValueError(
                    f"Invalid chunk type: {ct}. Must be one of {sorted(VALID_CHUNK_TYPES)}"
                )
        return v

    @field_validator("namespace")
    @classmethod
    def normalize_namespace(cls, namespace: str | None) -> str:
        return normalize_retrieval_namespace(namespace)


class RetrievalQueryResponse(BaseModel):
    namespace: str
    query: str
    router_used: str
    evidence: list[dict] = Field(
        default_factory=list,
        description=(
            "Grouped evidence blocks. Each group starts with "
            "[E1] [§ source_file_name / parent_path]. Text and table HTML "
            "stay as text parts; images stay as image parts at their "
            "placeholder positions."
        ),
    )
    evidence_text: str = Field(
        default="",
        description=(
            "Text projection of the grouped evidence blocks. Tables stay as "
            "HTML. Images stay on evidence as image parts and are marked "
            "here as [image: see evidence]."
        ),
    )
    answer_text: str = Field(
        default="",
        description=(
            "DEPRECATED. Always empty; KNOWHERE no longer generates answers. "
            "Use evidence_text and synthesize answers downstream."
        ),
    )
    referenced_chunks: list[dict] = Field(default_factory=list)
    results: list[dict] = Field(
        default_factory=list,
        description="Raw path chunks for debug. Content keeps placeholders; composed parts live on evidence.",
    )
    stop_reason: str | None = None
    failure_reason: str | None = None
    decision_trace: list[dict] | None = Field(
        default=None,
        description=(
            "Per-step agentic retrieval trace. Each entry follows the "
            "observation/decision/result schema."
        ),
    )


async def execute_retrieval_query(
    payload: RetrievalQueryRequest,
    current_user: CurrentUser,
    db: AsyncSession,
    *,
    llm_config: LLMConfig | None = None,
) -> dict[str, Any]:
    """Shared retrieval execution used by v1 and v2 route handlers."""
    # Resolve chunk_types: explicit field takes precedence over legacy data_type
    if payload.chunk_types is not None:
        resolved_chunk_types = normalize_chunk_types(payload.chunk_types)
    elif payload.data_type != 1:
        # Legacy fallback: convert integer to chunk_types set
        _DATA_TYPE_MAP: dict[int, set[str] | None] = {
            1: None, 2: {"text"}, 3: {"image"}, 4: {"table"},
            5: {"text", "image"}, 6: {"text", "table"},
            7: {"page"}, 8: {"text", "image", "table"},
        }
        resolved_chunk_types = _DATA_TYPE_MAP.get(payload.data_type)
    else:
        resolved_chunk_types = None

    query = str(payload.query or "").strip()
    if not query:
        return {
            "namespace": normalize_retrieval_namespace(payload.namespace),
            "query": query,
            "router_used": "empty_query_filtered",
            "failure_reason": "empty query — retrieval was not run",
            "evidence": [],
            "evidence_text": "",
            "answer_text": "",
            "referenced_chunks": [],
            "results": [],
        }

    return await run_retrieval_query(
        db=db,
        user_id=current_user.user_id,
        namespace=normalize_retrieval_namespace(payload.namespace),
        query=query,
        top_k=payload.top_k,
        include_document_ids=payload.include_document_ids,
        exclude_document_ids=payload.exclude_document_ids,
        exclude_sections=[item.model_dump() for item in payload.exclude_sections],
        chunk_types=resolved_chunk_types,
        signal_paths=payload.signal_paths or None,
        filter_mode=payload.filter_mode,
        rerank=payload.rerank,
        threshold=payload.threshold,
        internal_recall_k=payload.internal_recall_k,
        use_agentic=payload.use_agentic,
        agent_explore_model=(
            str(payload.agent_explore_model).strip()
            if payload.agent_explore_model
            else None
        ),
        conversation_id=payload.conversation_id,
        llm_config=llm_config,
    )


@router.post("/query", response_model=RetrievalQueryResponse)
async def query_retrieval(
    payload: RetrievalQueryRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await execute_retrieval_query(
        payload,
        current_user,
        db,
        llm_config=None,
    )
