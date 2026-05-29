"""Retrieval API routes for lexical + graph-routing baseline."""

from __future__ import annotations

from typing import Literal

from app.api.dependencies.current_user import with_current_user
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.app_service import run_retrieval_query

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
    top_k: int = 10
    exclude_document_ids: list[str] = Field(default_factory=list)
    exclude_sections: list[ExcludeSection] = Field(default_factory=list)
    data_type: int = Field(
        1,
        ge=1,
        le=6,
        description="Chunk type filter: 1=all, 2=text, 3=image, 4=table, 5=text+image, 6=text+table",
    )
    signal_paths: list[str] = Field(
        default_factory=list, description="Path keywords for include/exclude filtering"
    )
    filter_mode: Literal["delete", "keep"] = Field(
        "delete", description="Signal path filter mode"
    )
    channels: list[str] = Field(
        default_factory=list,
        description="Channels to run (empty=all). Options: path, content, term",
    )
    channel_weights: dict[str, float] = Field(
        default_factory=dict, description="Per-channel weight overrides"
    )
    rerank: bool = Field(False, description="Enable LLM reranking after RRF fusion")
    threshold: float = Field(0.0, ge=0.0, description="Minimum RRF score threshold")
    internal_recall_k: int | None = Field(
        None, ge=1, description="Override per-channel recall count"
    )
    use_agentic: bool | None = Field(
        None,
        description="Per-request agentic mode toggle. true=force agentic, false=force legacy, null=use server default.",
    )

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, v: list[str]) -> list[str]:
        valid = {"path", "content", "term"}
        for ch in v:
            if ch not in valid:
                raise ValueError(f"Invalid channel: {ch}. Must be one of {valid}")
        return v

    @field_validator("namespace")
    @classmethod
    def normalize_namespace(cls, namespace: str | None) -> str:
        return normalize_retrieval_namespace(namespace)


class RetrievalQueryResponse(BaseModel):
    namespace: str
    query: str
    router_used: str
    evidence_text: str = Field(
        default="",
        description="Hierarchical evidence text. Primary output for downstream agents.",
    )
    answer_text: str = Field(
        default="",
        description=(
            "DEPRECATED. Always empty; KNOWHERE no longer generates answers. "
            "Use evidence_text and synthesize answers downstream."
        ),
    )
    referenced_chunks: list[dict] = Field(default_factory=list)
    results: list[dict] = Field(default_factory=list)
    stop_reason: str | None = None
    failure_reason: str | None = None
    decision_trace: list[dict] | None = Field(
        default=None,
        description=(
            "Per-step navigation decisions from agentic retrieval. "
            "Each entry has phase, document, action, reason, stop_type, "
            "selected_paths, and hydrated_count. Use this to understand "
            "why KNOWHERE stopped or made specific navigation choices."
        ),
    )


@router.post("/query", response_model=RetrievalQueryResponse)
async def query_retrieval(
    payload: RetrievalQueryRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await run_retrieval_query(
        db=db,
        user_id=current_user.user_id,
        namespace=normalize_retrieval_namespace(payload.namespace),
        query=payload.query,
        top_k=payload.top_k,
        exclude_document_ids=payload.exclude_document_ids,
        exclude_sections=[item.model_dump() for item in payload.exclude_sections],
        data_type=payload.data_type,
        signal_paths=payload.signal_paths or None,
        filter_mode=payload.filter_mode,
        channels=payload.channels or None,
        channel_weights=payload.channel_weights or None,
        rerank=payload.rerank,
        threshold=payload.threshold,
        internal_recall_k=payload.internal_recall_k,
        use_agentic=payload.use_agentic,
    )
