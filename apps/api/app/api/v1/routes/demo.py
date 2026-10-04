"""Public shared demo directory and immutable revision media routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.demo.shared_document_service import SharedDemoDocumentService
from app.services.documents.lifecycle_service import DocumentService
from shared.core.database import get_db
from shared.core.exceptions.demo_materialization_removed import DemoMaterializationRemovedException

router = APIRouter(tags=["Demo Documents"])
_demo_service: SharedDemoDocumentService = SharedDemoDocumentService()
_document_service: DocumentService = DocumentService()


@router.get("/catalog")
async def get_demo_catalog(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _demo_service.get_catalog(db)


@router.get("/sources/{demo_source_id}/chunks")
async def list_demo_source_chunks(demo_source_id: str, job_result_id: str | None = Query(None, max_length=36), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db)) -> dict[str, Any] | None:
    document, revision = await _demo_service.resolve_source(db, source_id=demo_source_id, job_result_id=job_result_id)
    response = await _document_service.list_document_chunks(db, user_id="", document_id=document.document_id, job_result_id=revision.id, page=page, page_size=page_size, chunk_type=None, include_asset_urls=True)
    return {**(response or {}), "demo_source_id": demo_source_id, "canonical_document_id": document.document_id}


@router.get("/sources/{demo_source_id}/chunks/{demo_chunk_id}")
async def get_demo_source_chunk(demo_source_id: str, demo_chunk_id: str, job_result_id: str | None = Query(None, max_length=36), db: AsyncSession = Depends(get_db)) -> dict[str, Any] | None:
    document, revision = await _demo_service.resolve_source(db, source_id=demo_source_id, job_result_id=job_result_id)
    response = await _document_service.get_document_chunk(db, user_id="", document_id=document.document_id, document_chunk_id=demo_chunk_id, job_result_id=revision.id, include_asset_urls=True)
    if response is None:
        from shared.core.exceptions.domain_exceptions import NotFoundException
        raise NotFoundException(resource="Demo chunk", resource_id=demo_chunk_id)
    return {**response, "demo_source_id": demo_source_id, "canonical_document_id": document.document_id}


@router.get("/sources/{demo_source_id}/original")
async def get_demo_source_original(demo_source_id: str, job_result_id: str | None = Query(None, max_length=36), db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    return RedirectResponse(await _demo_service.get_media_url(db, source_id=demo_source_id, job_result_id=job_result_id), status_code=307, headers={"Cache-Control": "no-store"})


@router.get("/sources/{demo_source_id}/assets/{asset_path:path}")
async def get_demo_source_asset(demo_source_id: str, asset_path: str, job_result_id: str | None = Query(None, max_length=36), db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    return RedirectResponse(await _demo_service.get_media_url(db, source_id=demo_source_id, job_result_id=job_result_id, asset_path=asset_path), status_code=307, headers={"Cache-Control": "no-store"})


@router.post("/materializations", status_code=410)
async def materialize_demo_sources() -> None:
    raise DemoMaterializationRemovedException()
