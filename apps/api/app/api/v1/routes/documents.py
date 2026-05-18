"""Document API routes for canonical document lifecycle."""

from __future__ import annotations

from typing import Literal

from app.services.document_service import DocumentService
from app.api.dependencies.job_admission import with_current_user
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.core.exceptions.domain_exceptions import NotFoundException

router = APIRouter(tags=["Documents"])

_document_service = DocumentService()
DocumentChunkType = Literal["text", "image", "table"]


async def _archive_document_response(
    *,
    document_id: str,
    current_user: CurrentUser,
    db: AsyncSession,
):
    document = await _document_service.archive_document(
        db,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise NotFoundException(
            resource="Document",
            resource_id=document_id,
            internal_message="Document not found",
        )
    return document


@router.get("")
async def list_documents(
    namespace: str | None = Query(None),
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    effective_namespace = namespace or "default"
    documents = await _document_service.list_documents(
        db,
        user_id=current_user.user_id,
        namespace=effective_namespace,
    )
    return {
        "namespace": effective_namespace,
        "documents": documents,
    }


@router.get("/{document_id}")
async def get_document(
    document_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    document = await _document_service.get_document(
        db,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise NotFoundException(
            resource="Document",
            resource_id=document_id,
            internal_message="Document not found",
        )
    return document


@router.get("/{document_id}/chunks")
async def list_document_chunks(
    document_id: str,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    chunk_type: DocumentChunkType | None = Query(None, description="Chunk type filter"),
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    response = await _document_service.list_document_chunks(
        db,
        user_id=current_user.user_id,
        document_id=document_id,
        page=page,
        page_size=page_size,
        chunk_type=chunk_type,
    )
    if response is None:
        raise NotFoundException(
            resource="Document",
            resource_id=document_id,
            internal_message="Document not found",
        )
    return response


@router.get("/{document_id}/chunks/{document_chunk_id}")
async def get_document_chunk(
    document_id: str,
    document_chunk_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    response = await _document_service.get_document_chunk(
        db,
        user_id=current_user.user_id,
        document_id=document_id,
        document_chunk_id=document_chunk_id,
    )
    if response is None:
        raise NotFoundException(
            resource="Document chunk",
            resource_id=document_chunk_id,
            internal_message="Document chunk not found",
        )
    return response


@router.post("/{document_id}/archive")
async def archive_document(
    document_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _archive_document_response(
        document_id=document_id,
        current_user=current_user,
        db=db,
    )


@router.post("/{document_id}:archive", include_in_schema=False)
async def archive_document_legacy(
    document_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _archive_document_response(
        document_id=document_id,
        current_user=current_user,
        db=db,
    )
