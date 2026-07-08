"""API v2 route registry."""

from app.api.v1.routes import documents as v1_documents
from app.api.v2.routes import documents, jobs, retrieval
from fastapi import APIRouter

api_router = APIRouter()

api_router.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
api_router.include_router(retrieval.router, prefix="/retrieval", tags=["Retrieval"])
api_router.include_router(documents.router, prefix="/documents", tags=["Documents"])
api_router.include_router(v1_documents.router, prefix="/documents", tags=["Documents"])

__all__ = ["api_router"]
