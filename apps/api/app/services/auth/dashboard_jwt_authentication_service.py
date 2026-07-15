"""Dashboard JWT authentication workflow."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, NoReturn, cast

import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError
from jwt.algorithms import AllowedPublicKeys
from jwt.types import Options
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import AuthException
from shared.core.logging import LogEvent

JWKS_ENDPOINT_PATH = "/api/auth/jwks"
JWKS_CACHE_TTL_SECONDS = 60 * 60
JWT_KEY_ID_MAX_LENGTH = 64
JWT_KEY_ID_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9._:-]")
JWT_ALGORITHMS: tuple[str, ...] = ("HS256", "RS256", "EdDSA")
JWT_STRUCTURE_ONLY_DECODE_OPTIONS: Options = {
    "verify_signature": False,
}
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


@dataclass(frozen=True)
class _DashboardJWTHeader:
    algorithm: object
    key_id: str


@dataclass(frozen=True)
class _DashboardJWTTelemetry:
    jwt_kid_present: bool
    jwt_algorithm: str | None = None
    jwt_kid: str | None = None

    def to_log_data(self) -> dict[str, object]:
        log_data: dict[str, object] = {
            "auth_component": "dashboard_jwt",
            "jwt_kid_present": self.jwt_kid_present,
        }
        if self.jwt_algorithm is not None:
            log_data["jwt_algorithm"] = self.jwt_algorithm
        if self.jwt_kid is not None:
            log_data["jwt_kid"] = self.jwt_kid
        return log_data


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
        header = _parse_header_or_reject(token)
        telemetry_context = _build_telemetry_context(header)
        _assert_token_structure_or_reject(token, telemetry_context)
        key = self._resolve_verification_key_or_reject(
            key_id=header.key_id,
            telemetry_context=telemetry_context,
        )
        payload = _verify_payload_or_reject(
            token=token,
            key=key,
            telemetry_context=telemetry_context,
        )
        return _build_identity_or_reject(payload, telemetry_context)

    def _resolve_verification_key_or_reject(
        self,
        *,
        key_id: str,
        telemetry_context: _DashboardJWTTelemetry,
    ) -> VerificationKey:
        """Resolve a JWT verification key and classify JWKS failures locally."""
        try:
            key = self._get_verification_key(key_id)
        except PyJWKClientConnectionError as error:
            _reject_jwks_dependency(
                failure_reason="jwks_unavailable",
                telemetry_context=telemetry_context,
                original_exception=error,
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            PyJWKSetError,
            jwt.PyJWKError,
        ) as error:
            _reject_jwks_dependency(
                failure_reason="jwks_invalid",
                telemetry_context=telemetry_context,
                original_exception=error,
            )
        except PyJWKClientError as error:
            _reject_jwks_dependency(
                failure_reason="jwks_invalid",
                telemetry_context=telemetry_context,
                original_exception=error,
            )

        if key is None:
            _reject_client_jwt(
                failure_reason="jwt_unknown_key_id",
                telemetry_context=telemetry_context,
            )

        return key

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


def _parse_header_or_reject(token: str) -> _DashboardJWTHeader:
    try:
        unverified_header = cast(dict[str, object], jwt.get_unverified_header(token))
    except jwt.InvalidTokenError:
        _reject_client_jwt(
            failure_reason="jwt_invalid",
            telemetry_context=_build_telemetry_context_from_values(
                algorithm=None,
                key_id=None,
            ),
        )

    algorithm = unverified_header.get("alg")
    key_id_value = unverified_header.get("kid")
    key_id = key_id_value if isinstance(key_id_value, str) else None
    if key_id is None or not key_id.strip():
        _reject_client_jwt(
            failure_reason="jwt_missing_key_id",
            telemetry_context=_build_telemetry_context_from_values(
                algorithm=algorithm,
                key_id=key_id,
            ),
        )

    return _DashboardJWTHeader(algorithm=algorithm, key_id=key_id)


def _build_telemetry_context(
    header: _DashboardJWTHeader,
) -> _DashboardJWTTelemetry:
    return _build_telemetry_context_from_values(
        algorithm=header.algorithm,
        key_id=header.key_id,
    )


def _build_telemetry_context_from_values(
    *,
    algorithm: object,
    key_id: str | None,
) -> _DashboardJWTTelemetry:
    is_key_id_present = key_id is not None and bool(key_id.strip())
    jwt_algorithm: str | None = None
    if isinstance(algorithm, str) and algorithm in JWT_ALGORITHMS:
        jwt_algorithm = algorithm
    jwt_kid: str | None = None
    if is_key_id_present and key_id is not None:
        jwt_kid = _sanitize_key_id(key_id)
    return _DashboardJWTTelemetry(
        jwt_kid_present=is_key_id_present,
        jwt_algorithm=jwt_algorithm,
        jwt_kid=jwt_kid,
    )


def _assert_token_structure_or_reject(
    token: str,
    telemetry_context: _DashboardJWTTelemetry,
) -> None:
    """Reject structurally invalid JWTs before touching Dashboard JWKS."""
    try:
        # This decode only checks token structure; verified claims come from
        # _verify_payload_or_reject after the signing key is resolved.
        jwt.decode(
            token,
            options=JWT_STRUCTURE_ONLY_DECODE_OPTIONS,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, jwt.InvalidTokenError):
        _reject_client_jwt(
            failure_reason="jwt_invalid",
            telemetry_context=telemetry_context,
        )


def _verify_payload_or_reject(
    *,
    token: str,
    key: VerificationKey,
    telemetry_context: _DashboardJWTTelemetry,
) -> dict[str, object]:
    try:
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
    except jwt.ExpiredSignatureError:
        _reject_client_jwt(
            failure_reason="jwt_expired",
            telemetry_context=telemetry_context,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, jwt.InvalidTokenError):
        _reject_client_jwt(
            failure_reason="jwt_invalid",
            telemetry_context=telemetry_context,
        )

    return payload


def _build_identity_or_reject(
    payload: dict[str, object],
    telemetry_context: _DashboardJWTTelemetry,
) -> DashboardJWTIdentity:
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id:
        _reject_client_jwt(
            failure_reason="jwt_invalid",
            telemetry_context=telemetry_context,
        )

    permission = _normalize_permission(payload.get("permission"))
    return DashboardJWTIdentity(user_id=user_id, permission=permission)


def _sanitize_key_id(key_id: str) -> str:
    sanitized_key_id = JWT_KEY_ID_UNSAFE_PATTERN.sub("_", key_id)
    return sanitized_key_id[:JWT_KEY_ID_MAX_LENGTH]


def _reject_client_jwt(
    *,
    failure_reason: JWTFailureReason,
    telemetry_context: _DashboardJWTTelemetry,
) -> NoReturn:
    _log_dashboard_jwt_auth_failure(
        failure_reason=failure_reason,
        telemetry_context=telemetry_context,
        is_jwks_dependency_failure=False,
    )
    raise AuthException() from None


def _reject_jwks_dependency(
    *,
    failure_reason: JWTFailureReason,
    telemetry_context: _DashboardJWTTelemetry,
    original_exception: Exception,
) -> NoReturn:
    _log_dashboard_jwt_auth_failure(
        failure_reason=failure_reason,
        telemetry_context=telemetry_context,
        is_jwks_dependency_failure=True,
        original_exception=original_exception,
    )
    raise AuthException() from None


def _log_dashboard_jwt_auth_failure(
    *,
    failure_reason: JWTFailureReason,
    telemetry_context: _DashboardJWTTelemetry,
    is_jwks_dependency_failure: bool,
    original_exception: Exception | None = None,
) -> None:
    log_data: dict[str, object] = {
        **telemetry_context.to_log_data(),
        "failure_reason": failure_reason,
    }
    message = f"Dashboard JWT authentication failed: {failure_reason}"
    if is_jwks_dependency_failure:
        logger.bind(
            event=LogEvent.EXCEPTION_SYSTEM.value,
            **log_data,
        ).opt(exception=original_exception).error(message)
        return

    logger.bind(
        event=LogEvent.EXCEPTION_CLIENT.value,
        **log_data,
    ).warning(message)


def _normalize_permission(value: object) -> Permission:
    if value == READ_ONLY_PERMISSION:
        return READ_ONLY_PERMISSION

    return FULL_ACCESS_PERMISSION


_dashboard_jwt_authentication_service = DashboardJWTAuthenticationService()


def get_dashboard_jwt_authentication_service() -> DashboardJWTAuthenticationService:
    """Return the process-wide Dashboard JWT authentication service."""
    return _dashboard_jwt_authentication_service
