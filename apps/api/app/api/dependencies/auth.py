"""FastAPI authentication dependency adapters."""

from app.services.auth.current_user_authentication_service import (
    get_current_user_authentication_service,
)
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db


async def get_current_user_id(
    authorization: str | None = Header(
        default=None,
        description="Bearer <token> OR internal signature auth",
    ),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Authenticate the caller and return the current user ID."""
    return await get_current_user_authentication_service().authenticate_authorization_header(
        db,
        authorization,
    )
