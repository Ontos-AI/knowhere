"""
Top-level API route registry.
"""

from app.api.v1.api_v1 import api_router as v1_router
from app.api.v2.api_v2 import api_router as v2_router
from fastapi import APIRouter

api_router = APIRouter()

# Register the versioned API routes.
api_router.include_router(v1_router, prefix="/v1")
api_router.include_router(v2_router, prefix="/v2")

__all__ = ["api_router"]
