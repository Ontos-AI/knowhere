"""Current-user authentication workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.services.auth.api_key_authentication_service import (
    APIKeyAuthenticationService,
)
from app.services.auth.dashboard_jwt_authentication_service import (
    DashboardJWTAuthenticationService,
    FULL_ACCESS_PERMISSION,
    Permission,
    get_dashboard_jwt_authentication_service,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import AuthException
from shared.models.database.user import User
from shared.utils.api_keys import is_api_key_token

AuthSource = Literal["api_key", "jwt"]


@dataclass(frozen=True)
class AuthenticatedIdentity:
    user_id: str
    permission: Permission
    source: AuthSource


class CurrentUserAuthenticationService:
    """Authenticate API keys or Dashboard JWTs and return the current user ID."""

    def __init__(
        self,
        *,
        api_key_authentication_service: APIKeyAuthenticationService | None = None,
        dashboard_jwt_authentication_service: (
            DashboardJWTAuthenticationService | None
        ) = None,
    ) -> None:
        self._api_key_authentication_service = (
            api_key_authentication_service or APIKeyAuthenticationService()
        )
        self._dashboard_jwt_authentication_service = (
            dashboard_jwt_authentication_service
            or get_dashboard_jwt_authentication_service()
        )

    async def authenticate_authorization_header(
        self,
        session: AsyncSession,
        authorization: str | None,
    ) -> str:
        """Authenticate an Authorization header and return the owning user ID."""
        identity = await self.authenticate_authorization_header_with_identity(
            session,
            authorization,
        )
        return identity.user_id

    async def authenticate_authorization_header_with_identity(
        self,
        session: AsyncSession,
        authorization: str | None,
    ) -> AuthenticatedIdentity:
        """Authenticate an Authorization header and return identity details."""
        token = self._extract_bearer_token(authorization)

        if is_api_key_token(token):
            user_id = await self._api_key_authentication_service.validate_api_key(
                session,
                token,
            )
            if user_id:
                return AuthenticatedIdentity(
                    user_id=user_id,
                    permission=FULL_ACCESS_PERMISSION,
                    source="api_key",
                )
            raise AuthException(user_message="Invalid API Key")

        identity = self._dashboard_jwt_authentication_service.decode_identity(token)
        await self._ensure_authenticated_user_exists(session, identity.user_id)
        return AuthenticatedIdentity(
            user_id=identity.user_id,
            permission=identity.permission,
            source="jwt",
        )

    @staticmethod
    def _extract_bearer_token(authorization: str | None) -> str:
        if not authorization:
            raise AuthException(
                user_message="Authentication required. Provide Authorization header."
            )

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthException(user_message="Invalid Authorization header format")

        return token

    @staticmethod
    async def _ensure_authenticated_user_exists(
        session: AsyncSession,
        user_id: str,
    ) -> None:
        result = await session.execute(
            select(User.id).where(User.id == user_id).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return

        raise AuthException(
            user_message="Invalid authentication credentials",
            internal_message=(
                "Authenticated user id is not present in the user table: "
                f"user_id={user_id}"
            ),
        )


_current_user_authentication_service = CurrentUserAuthenticationService()


def get_current_user_authentication_service() -> CurrentUserAuthenticationService:
    """Return the process-wide current-user authentication service."""
    return _current_user_authentication_service
