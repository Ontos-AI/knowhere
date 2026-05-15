"""
API key management endpoints.
"""

from app.services.auth.api_key_management_service import APIKeyManagementService
from app.services.rate_limit.dependencies import (
    CurrentUser,
    with_current_user,
)
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.core.exceptions.domain_exceptions import (
    APIKeyOperationException,
    NotFoundException,
    ValidationException,
)
from shared.models.schemas.api_key import (
    APIKeyListResponse,
    APIKeyResponse,
    CreateAPIKeyRequest,
    CreateAPIKeyResponse,
    RevokeAPIKeyRequest,
)

router = APIRouter(tags=["API Key Management"])
_api_key_management_service = APIKeyManagementService()


@router.post("/create", summary="Create an API key")
async def create_api_key(
    request: CreateAPIKeyRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an API key."""
    try:
        api_key = await _api_key_management_service.create_api_key(
            session=db,
            user_id=current_user.user_id,
            name=request.name,
            enabled_modules=request.enabled_modules,
            expires_at=request.expires_at,
        )

        return CreateAPIKeyResponse(
            api_key=api_key,
            name=request.name,
            enabled_modules=request.enabled_modules,
            expires_at=request.expires_at,
        )

    except ValidationException:
        raise
    except NotFoundException:
        raise
    except Exception as e:
        raise APIKeyOperationException(
            internal_message=f"Failed to create API Key: {str(e)}"
        )


@router.get("/list", summary="List API keys")
async def list_api_keys(
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List API keys for the current user."""
    try:
        api_keys_data = await _api_key_management_service.list_user_api_keys(
            db,
            user_id=current_user.user_id,
        )

        api_keys = [
            APIKeyResponse(
                id=key["id"],
                name=key["name"],
                api_key=key["api_key"],
                enabled_modules=key["enabled_modules"],
                is_active=key["is_active"],
                created_at=key["created_at"],
                last_used_at=key["last_used_at"],
                expires_at=key["expires_at"],
            )
            for key in api_keys_data
        ]

        return APIKeyListResponse(api_keys=api_keys, total=len(api_keys))

    except Exception as e:
        raise APIKeyOperationException(
            internal_message=f"Failed to list API Keys: {str(e)}"
        )


@router.post("/revoke", summary="Revoke an API key")
async def revoke_api_key(
    request: RevokeAPIKeyRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key."""
    try:
        await _api_key_management_service.revoke_api_key(
            session=db,
            api_key_id=request.api_key_id,
            user_id=current_user.user_id,
        )
        return {"message": "API key revoked"}

    except NotFoundException:
        raise
    except APIKeyOperationException:
        raise
    except Exception as e:
        raise APIKeyOperationException(
            internal_message=f"Failed to revoke API Key: {str(e)}"
        )


@router.get("/{api_key_id}", summary="Get API key details")
async def get_api_key(
    api_key_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get details for a single API key."""
    try:
        api_key = await _api_key_management_service.get_api_key(
            db,
            user_id=current_user.user_id,
            api_key_id=api_key_id,
        )
        if not api_key:
            raise NotFoundException(
                resource="APIKey",
                resource_id=api_key_id,
                internal_message="API Key not found",
            )

        return {
            "id": str(api_key.id),
            "name": api_key.name,
            "enabled_modules": api_key.enabled_modules,
            "is_active": api_key.is_active,
            "created_at": api_key.created_at,
            "last_used_at": api_key.last_used_at,
            "expires_at": api_key.expires_at,
        }

    except NotFoundException:
        raise
    except Exception as e:
        raise APIKeyOperationException(
            internal_message=f"Failed to get API Key: {str(e)}"
        )


@router.put("/{api_key_id}/toggle", summary="Enable or disable an API key")
async def toggle_api_key(
    api_key_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable an API key."""
    try:
        success = await _api_key_management_service.toggle_api_key(
            db,
            user_id=current_user.user_id,
            api_key_id=api_key_id,
        )
        if success:
            return {"message": "API key status updated"}
        else:
            raise APIKeyOperationException(
                internal_message="Failed to toggle API Key status"
            )
    except APIKeyOperationException:
        raise
    except Exception as e:
        raise APIKeyOperationException(
            internal_message=f"Failed to toggle API Key: {str(e)}"
        )
