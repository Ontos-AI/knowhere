"""Documents API v2 routes."""

from __future__ import annotations

from typing import Any

from app.api.dependencies.current_user import with_current_user
from app.services.documents.lifecycle_service import DocumentService
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.core.exceptions.domain_exceptions import NotFoundException

router = APIRouter(tags=["Documents"])

_document_service = DocumentService()


@router.get("/{document_id}/files/page-citation-source")
async def get_document_page_citation_source(
    document_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    response = await _document_service.get_document_page_citation_source(
        db,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if response is None:
        raise NotFoundException(
            resource="Document page citation source",
            resource_id=document_id,
            internal_message="Document page citation source not found",
        )
    return response


@router.get("/{document_id}/files/mineru-raw")
async def get_document_mineru_raw(
    document_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    response = await _document_service.get_document_mineru_raw(
        db,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if response is None:
        raise NotFoundException(
            resource="Document MinerU raw output",
            resource_id=document_id,
            internal_message="Document MinerU raw output not found",
        )
    return response

__all__ = ["router"]
