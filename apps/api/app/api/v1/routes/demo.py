"""Notebook demo document catalog routes."""

from __future__ import annotations

from typing import Any

from app.services.demo.document_service import DemoDocumentService
from app.api.dependencies.job_admission import with_current_user
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.core.exceptions.domain_exceptions import NotFoundException

router = APIRouter(tags=["Demo Documents"])

_demo_service = DemoDocumentService()


class DemoMaterializeRequest(BaseModel):
    """Request to copy selected canonical demo sources into a namespace."""

    namespace: str | None = Field(None, description="Target retrieval namespace")
    demo_source_ids: list[str] = Field(
        default_factory=list,
        min_length=1,
        description="Canonical demo source IDs to materialize",
    )


@router.get("/catalog")
async def get_demo_catalog() -> dict[str, Any]:
    """Return API-owned canonical demo source metadata and curated Q/A."""
    return _demo_service.get_catalog()


@router.get("/sources/{demo_source_id}/chunks")
async def list_demo_source_chunks(
    demo_source_id: str,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
) -> dict[str, Any]:
    """Return paginated canonical chunks for a demo source."""
    response = _demo_service.list_chunks(
        demo_source_id=demo_source_id,
        page=page,
        page_size=page_size,
    )
    if response is None:
        raise _demo_source_not_found(demo_source_id)
    return response


@router.get("/sources/{demo_source_id}/chunks/{demo_chunk_id}")
async def get_demo_source_chunk(
    demo_source_id: str,
    demo_chunk_id: str,
) -> dict[str, Any]:
    """Return one canonical demo chunk for citation focusing."""
    response = _demo_service.get_chunk(
        demo_source_id=demo_source_id,
        demo_chunk_id=demo_chunk_id,
    )
    if response is None:
        raise NotFoundException(
            resource="Demo document chunk",
            resource_id=demo_chunk_id,
            internal_message="Demo document chunk not found",
        )
    return response


@router.get("/sources/{demo_source_id}/original")
async def get_demo_source_original(demo_source_id: str) -> FileResponse:
    """Return the canonical original file for preview."""
    file_path = _demo_service.get_original_file_path(
        demo_source_id=demo_source_id,
    )
    if file_path is None:
        raise _demo_source_not_found(demo_source_id)

    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=file_path.name,
        content_disposition_type="inline",
    )


@router.get("/sources/{demo_source_id}/assets/{asset_path:path}")
async def get_demo_source_asset(
    demo_source_id: str,
    asset_path: str,
) -> FileResponse:
    """Return a canonical parsed media or table asset for preview."""
    file_path = _demo_service.get_asset_file_path(
        demo_source_id=demo_source_id,
        asset_path=asset_path,
    )
    if file_path is None:
        raise _demo_source_not_found(demo_source_id)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        content_disposition_type="inline",
    )


@router.post("/materializations")
async def materialize_demo_sources(
    payload: DemoMaterializeRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Copy canonical demo sources into the authenticated user's namespace."""
    namespace = (payload.namespace or "default").strip() or "default"
    try:
        materialized_sources = await _demo_service.materialize_sources(
            db,
            user_id=current_user.user_id,
            namespace=namespace,
            demo_source_ids=payload.demo_source_ids,
        )
    except KeyError as error:
        raise _demo_source_not_found(str(error.args[0])) from error

    return {
        "namespace": namespace,
        "sources": [
            {
                "demo_source_id": source.demo_source_id,
                "document_id": source.document_id,
                "status": source.status,
                "title": source.title,
                "mime_type": source.mime_type,
                "size_bytes": source.size_bytes,
                "chunk_count": source.chunk_count,
                "original_file": {
                    "url": f"/api/v1/demo/sources/{source.demo_source_id}/original",
                    "mime_type": source.mime_type,
                    "size_bytes": source.size_bytes,
                    "can_download": False,
                },
            }
            for source in materialized_sources
        ],
    }


def _demo_source_not_found(demo_source_id: str) -> NotFoundException:
    return NotFoundException(
        resource="Demo document source",
        resource_id=demo_source_id,
        internal_message="Demo document source not found",
    )
