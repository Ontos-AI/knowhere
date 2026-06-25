"""FastAPI authentication dependency adapters."""

from app.services.auth.current_user_authentication_service import (
    get_current_user_authentication_service,
)
from app.services.auth.dashboard_jwt_authentication_service import (
    FULL_ACCESS_PERMISSION,
    READ_ONLY_PERMISSION,
)
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.core.exceptions.domain_exceptions import PermissionDeniedException


async def get_current_user_id(
    request: Request,
    authorization: str | None = Header(
        default=None,
        description="Bearer <token> OR internal signature auth",
    ),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Authenticate the caller and return the current user ID."""
    identity = await get_current_user_authentication_service().authenticate_authorization_header_with_identity(
        db,
        authorization,
    )
    request.state.user_id = identity.user_id
    request.state.permission = identity.permission
    request.state.auth_source = identity.source
    return identity.user_id


async def require_write_permission(
    request: Request,
    _user_id: str = Depends(get_current_user_id),
) -> None:
    """Reject write operations for read-only Dashboard tokens."""
    permission = getattr(request.state, "permission", FULL_ACCESS_PERMISSION)
    if permission != READ_ONLY_PERMISSION:
        return

    raise PermissionDeniedException(
        user_message="This token is read only.",
        required_permission=FULL_ACCESS_PERMISSION,
    )
