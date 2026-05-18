"""API key management workflow."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from app.repositories.api_key_repository import APIKeyRepository
from app.services.auth.api_key_authentication_service import (
    APIKeyAuthenticationService,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    APIKeyOperationException,
    KnowhereException,
    NotFoundException,
    ValidationException,
)
from shared.models.database.api_key import APIKey
from shared.services.auth.api_key_tokens import generate_api_key, hash_api_key, mask_api_key


class APIKeyListItem(TypedDict):
    id: str
    name: str
    api_key: str
    enabled_modules: list[str] | None
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class APIKeyManagementService:
    """Create, list, read, revoke, and toggle API keys."""

    def __init__(
        self,
        *,
        repository: APIKeyRepository | None = None,
        authentication_service: APIKeyAuthenticationService | None = None,
    ) -> None:
        self._repository = repository or APIKeyRepository()
        self._authentication_service = (
            authentication_service or APIKeyAuthenticationService()
        )

    async def create_api_key(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        name: str,
        enabled_modules: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        key_count = await self._repository.count_by_user(session, user_id)
        if key_count >= 10:
            raise ValidationException(
                user_message="Maximum API Key limit reached (10)",
                violations=[
                    {
                        "field": "api_keys",
                        "description": "User has reached the maximum API Key limit",
                    }
                ],
            )

        existing_key = await self._repository.get_by_user_and_name(
            session,
            user_id,
            name,
        )
        if existing_key:
            raise ValidationException(
                user_message="API Key name already exists",
                violations=[
                    {
                        "field": "name",
                        "description": f"An API Key with name '{name}' already exists",
                    }
                ],
            )

        api_key = generate_api_key()
        api_key_record = APIKey(
            user_id=user_id,
            key_hash=hash_api_key(api_key),
            key_mask=mask_api_key(api_key),
            name=name,
            enabled_modules=enabled_modules or ["all"],
            expires_at=expires_at,
        )
        await self._repository.create(session, api_key_record)
        return api_key

    async def revoke_api_key(
        self,
        session: AsyncSession,
        *,
        api_key_id: str,
        user_id: str,
    ) -> bool:
        logger.info(f"Revoking API key: api_key_id={api_key_id}, user_id={user_id}")
        api_key = await self._repository.get_by_id(session, api_key_id)

        if not api_key:
            logger.warning("API key does not exist")
            raise NotFoundException(
                resource="APIKey",
                resource_id=api_key_id,
                internal_message="API Key not found",
            )

        if str(api_key.user_id) != user_id:
            logger.warning(
                f"User ID mismatch: api_key.user_id={api_key.user_id}, user_id={user_id}"
            )
            raise NotFoundException(
                resource="APIKey",
                resource_id=api_key_id,
                internal_message="API Key not found or does not belong to user",
            )

        success = await self._repository.delete_by_id(session, api_key_id)
        logger.info(f"Delete result: {success}")

        if success:
            await session.commit()
            logger.info("Transaction committed")
            await self._authentication_service.invalidate_api_key_user_cache(
                user_id=user_id,
                api_key_hash=api_key.key_hash,
            )

        return success

    async def list_user_api_keys(
        self,
        session: AsyncSession,
        *,
        user_id: str,
    ) -> list[APIKeyListItem]:
        api_keys = await self._repository.get_unexpired_by_user_id(session, user_id)
        return [
            {
                "id": str(api_key.id),
                "name": api_key.name,
                "api_key": api_key.key_mask
                or f"sk_{api_key.id[:8]}••••••••••••••••••••••••••••••••••••••••",
                "enabled_modules": api_key.enabled_modules,
                "is_active": api_key.is_active,
                "created_at": api_key.created_at,
                "last_used_at": api_key.last_used_at,
                "expires_at": api_key.expires_at,
            }
            for api_key in api_keys
        ]

    async def get_api_key(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        api_key_id: str,
    ) -> APIKey | None:
        try:
            api_key = await self._repository.get(session, api_key_id)
            if api_key and str(api_key.user_id) == user_id:
                return api_key
            return None
        except KnowhereException:
            raise
        except Exception as exc:
            logger.error(f"Failed to get API key: {exc}")
            raise APIKeyOperationException(
                internal_message=f"Failed to get API key: {str(exc)}",
                original_exception=exc,
            )

    async def toggle_api_key(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        api_key_id: str,
    ) -> bool:
        try:
            api_key = await self._repository.get(session, api_key_id)
            if not api_key or str(api_key.user_id) != user_id:
                return False

            api_key.is_active = not api_key.is_active
            await session.commit()
            await session.refresh(api_key)

            if not api_key.is_active:
                await self._authentication_service.invalidate_api_key_user_cache(
                    user_id=user_id,
                    api_key_hash=api_key.key_hash,
                )

            logger.info(
                f"API key status toggled successfully: {api_key_id}, new_status={api_key.is_active}"
            )
            return True
        except KnowhereException:
            raise
        except Exception as exc:
            logger.error(f"Failed to toggle API key status: {exc}")
            await session.rollback()
            raise APIKeyOperationException(
                internal_message=f"Failed to toggle API key status: {str(exc)}",
                original_exception=exc,
            )
