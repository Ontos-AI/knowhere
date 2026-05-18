"""Dashboard JWT authentication workflow."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import jwt
from jwt import PyJWKClient
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import AuthException

JWKS_ENDPOINT_PATH = "/api/auth/jwks"
JWKS_CACHE_TTL_SECONDS = 60 * 60


class DashboardJWTAuthenticationService:
    """Validate Dashboard-issued JWTs through the configured JWKS endpoint."""

    def __init__(self) -> None:
        self._jwks_client: PyJWKClient | None = None
        self._jwks_client_lock = threading.Lock()

    def decode_user_id(self, token: str) -> str:
        """Decode and validate a JWT, returning its authenticated user ID."""
        try:
            key = self._get_verification_key(token)
            payload: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["HS256", "RS256", "EdDSA"],
                leeway=timedelta(seconds=30),
                options={"verify_aud": False},
            )
            user_id = payload.get("id")
            if not isinstance(user_id, str) or not user_id:
                raise AuthException(user_message="Token missing 'id' claim")
            return user_id
        except jwt.ExpiredSignatureError:
            raise AuthException(user_message="Token has expired")
        except jwt.InvalidTokenError as exc:
            logger.warning(f"Invalid JWT token: {exc}")
            raise AuthException(user_message="Invalid token")

    def _get_verification_key(self, token: str) -> Any:
        """Resolve the JWT verification key from the Dashboard JWKS endpoint."""
        try:
            jwks_client = self._get_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            return signing_key.key
        except jwt.PyJWKClientError as exc:
            logger.error(f"Failed to fetch JWKS: {exc}")
            raise AuthException(
                internal_message=(
                    "Failed to fetch verification key from JWKS endpoint: "
                    f"{exc}"
                )
            )
        except jwt.PyJWKSetError as exc:
            logger.error(f"Invalid JWKS format: {exc}")
            raise AuthException(internal_message=f"Invalid JWKS format: {exc}")

    def _get_jwks_client(self) -> PyJWKClient:
        """Return a cached JWKS client for Dashboard token verification."""
        if self._jwks_client is None:
            with self._jwks_client_lock:
                if self._jwks_client is None:
                    jwks_url = (
                        f"{settings.INTERNAL_DASHBOARD_ENDPOINT}"
                        f"{JWKS_ENDPOINT_PATH}"
                    )
                    self._jwks_client = PyJWKClient(
                        jwks_url,
                        cache_jwk_set=True,
                        lifespan=JWKS_CACHE_TTL_SECONDS,
                        timeout=30,
                    )
                    logger.info(f"Initialized JWKS client with endpoint: {jwks_url}")

        return self._jwks_client


_dashboard_jwt_authentication_service = DashboardJWTAuthenticationService()


def get_dashboard_jwt_authentication_service() -> DashboardJWTAuthenticationService:
    """Return the process-wide Dashboard JWT authentication service."""
    return _dashboard_jwt_authentication_service
