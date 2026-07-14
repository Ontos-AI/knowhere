"""Dashboard JWT authentication workflow."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError
from jwt.algorithms import AllowedPublicKeys

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import AuthException

JWKS_ENDPOINT_PATH = "/api/auth/jwks"
JWKS_CACHE_TTL_SECONDS = 60 * 60
JWT_KEY_ID_MAX_LENGTH = 64
JWT_KEY_ID_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9._:-]")
JWT_ALGORITHMS: tuple[str, ...] = ("HS256", "RS256", "EdDSA")
READ_ONLY_PERMISSION: Literal["read_only"] = "read_only"
FULL_ACCESS_PERMISSION: Literal["full_access"] = "full_access"
Permission = Literal["read_only", "full_access"]
JWTFailureReason = Literal[
    "jwt_missing_key_id",
    "jwt_unknown_key_id",
    "jwt_expired",
    "jwt_invalid",
    "jwks_unavailable",
    "jwks_invalid",
]
VerificationKey = AllowedPublicKeys | str | bytes


@dataclass(frozen=True)
class DashboardJWTIdentity:
    user_id: str
    permission: Permission


class DashboardJWTAuthenticationService:
    """Validate Dashboard-issued JWTs through the configured JWKS endpoint."""

    def __init__(self) -> None:
        self._jwks_client: PyJWKClient | None = None
        self._jwks_client_lock = threading.Lock()

    def decode_user_id(self, token: str) -> str:
        """Decode and validate a JWT, returning its authenticated user ID."""
        return self.decode_identity(token).user_id

    def decode_identity(self, token: str) -> DashboardJWTIdentity:
        """Decode and validate a JWT, returning the user ID and permission."""
        try:
            unverified_header = cast(dict[str, object], jwt.get_unverified_header(token))
        except jwt.InvalidTokenError:
            raise _create_auth_exception(
                user_message="Invalid token",
                failure_reason="jwt_invalid",
                exception_context=_build_exception_context(
                    algorithm=None,
                    key_id=None,
                ),
            ) from None

        algorithm = unverified_header.get("alg")
        key_id_value = unverified_header.get("kid")
        key_id = key_id_value if isinstance(key_id_value, str) else None
        exception_context = _build_exception_context(
            algorithm=algorithm,
            key_id=key_id,
        )

        if key_id is None or not key_id.strip():
            raise _create_auth_exception(
                failure_reason="jwt_missing_key_id",
                exception_context=exception_context,
            )

        try:
            key = self._get_verification_key(key_id)
            if key is None:
                raise _create_auth_exception(
                    failure_reason="jwt_unknown_key_id",
                    exception_context=exception_context,
                )

            payload = self._decode_payload(token, key)
            user_id = payload.get("id")
            if not isinstance(user_id, str) or not user_id:
                raise _create_auth_exception(
                    user_message="Token missing 'id' claim",
                    failure_reason="jwt_invalid",
                    exception_context=exception_context,
                )

            permission = _normalize_permission(payload.get("permission"))
            return DashboardJWTIdentity(user_id=user_id, permission=permission)
        except jwt.ExpiredSignatureError:
            raise _create_auth_exception(
                user_message="Token has expired",
                failure_reason="jwt_expired",
                exception_context=exception_context,
            ) from None
        except PyJWKClientConnectionError:
            raise _create_auth_exception(
                failure_reason="jwks_unavailable",
                error_category="system",
                exception_context=exception_context,
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError, PyJWKSetError, jwt.PyJWKError):
            raise _create_auth_exception(
                failure_reason="jwks_invalid",
                error_category="system",
                exception_context=exception_context,
            ) from None
        except PyJWKClientError:
            raise _create_auth_exception(
                failure_reason="jwks_invalid",
                error_category="system",
                exception_context=exception_context,
            ) from None
        except jwt.InvalidTokenError:
            raise _create_auth_exception(
                user_message="Invalid token",
                failure_reason="jwt_invalid",
                exception_context=exception_context,
            ) from None

    def _decode_payload(
        self,
        token: str,
        key: VerificationKey,
    ) -> dict[str, object]:
        payload = cast(
            dict[str, object],
            jwt.decode(
                token,
                key,
                algorithms=list(JWT_ALGORITHMS),
                leeway=timedelta(seconds=30),
                options={"verify_aud": False},
            ),
        )
        return payload

    def _get_verification_key(self, key_id: str) -> VerificationKey | None:
        """Resolve the JWT verification key from the Dashboard JWKS endpoint."""
        jwks_client = self._get_jwks_client()
        signing_keys = jwks_client.get_signing_keys()
        signing_key = jwks_client.match_kid(signing_keys, key_id)
        if signing_key is not None:
            return cast(VerificationKey, signing_key.key)

        refreshed_signing_keys = jwks_client.get_signing_keys(refresh=True)
        refreshed_signing_key = jwks_client.match_kid(
            refreshed_signing_keys,
            key_id,
        )
        if refreshed_signing_key is None:
            return None

        return cast(VerificationKey, refreshed_signing_key.key)

    def _get_jwks_client(self) -> PyJWKClient:
        """Return a cached JWKS client for Dashboard token verification."""
        if self._jwks_client is None:
            with self._jwks_client_lock:
                if self._jwks_client is None:
                    jwks_url = (
                        f"{settings.INTERNAL_DASHBOARD_ENDPOINT}{JWKS_ENDPOINT_PATH}"
                    )
                    self._jwks_client = PyJWKClient(
                        jwks_url,
                        cache_jwk_set=True,
                        lifespan=JWKS_CACHE_TTL_SECONDS,
                        timeout=30,
                    )

        return self._jwks_client


def _build_exception_context(
    *,
    algorithm: object,
    key_id: str | None,
) -> dict[str, object]:
    is_key_id_present = key_id is not None and bool(key_id.strip())
    context: dict[str, object] = {
        "auth_component": "dashboard_jwt",
        "jwt_kid_present": is_key_id_present,
    }
    if isinstance(algorithm, str) and algorithm in JWT_ALGORITHMS:
        context["jwt_algorithm"] = algorithm
    if is_key_id_present and key_id is not None:
        context["jwt_kid"] = _sanitize_key_id(key_id)
    return context


def _sanitize_key_id(key_id: str) -> str:
    sanitized_key_id = JWT_KEY_ID_UNSAFE_PATTERN.sub("_", key_id)
    return sanitized_key_id[:JWT_KEY_ID_MAX_LENGTH]


def _create_auth_exception(
    *,
    failure_reason: JWTFailureReason,
    user_message: str = "Authentication required",
    error_category: Literal["client", "system"] | None = None,
    exception_context: dict[str, object] | None = None,
) -> AuthException:
    context: dict[str, object] = {
        **(exception_context or {}),
        "failure_reason": failure_reason,
    }
    return AuthException(
        user_message=user_message,
        internal_message=f"Dashboard JWT authentication failed: {failure_reason}",
        error_category=error_category,
        exception_context=context,
    )


def _normalize_permission(value: object) -> Permission:
    if value == READ_ONLY_PERMISSION:
        return READ_ONLY_PERMISSION

    return FULL_ACCESS_PERMISSION


_dashboard_jwt_authentication_service = DashboardJWTAuthenticationService()


def get_dashboard_jwt_authentication_service() -> DashboardJWTAuthenticationService:
    """Return the process-wide Dashboard JWT authentication service."""
    return _dashboard_jwt_authentication_service
