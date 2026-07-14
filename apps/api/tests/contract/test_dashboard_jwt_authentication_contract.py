from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast

import jwt
import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient, Response
from loguru import logger
from pytest import MonkeyPatch

from app.core.exception_handlers import setup_exception_handlers
from app.services.auth.dashboard_jwt_authentication_service import (
    DashboardJWTAuthenticationService,
)
from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import AuthException
from shared.core.logging import _downgrade_expected_logfire_exception
from tests.support.dashboard_jwt import (
    create_dashboard_rsa_jwk as _create_rsa_jwk,
    create_dashboard_rsa_private_key as _create_rsa_private_key,
    create_dashboard_rsa_token as _create_rsa_token,
    serve_dashboard_jwks as _serve_jwks,
)

class _LoguruMessage(Protocol):
    @property
    def record(self) -> Mapping[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class _CapturedAuthLog:
    level: str
    event: str
    message: str
    extra: Mapping[str, object]


@dataclass
class _FakeLogfireExceptionHelper:
    exception: BaseException
    level: str = "error"
    is_recording_exception: bool = True

    def no_record_exception(self) -> None:
        self.is_recording_exception = False


class _AuthLogCapture:
    def __init__(self) -> None:
        self.records: list[_CapturedAuthLog] = []

    def capture(self, message: _LoguruMessage) -> None:
        record = message.record
        extra = cast(Mapping[str, object], record["extra"])
        if extra.get("auth_component") != "dashboard_jwt":
            return

        level = record["level"]
        self.records.append(
            _CapturedAuthLog(
                level=str(getattr(level, "name", level)),
                event=str(extra.get("event", "")),
                message=str(record["message"]),
                extra=dict(extra),
            )
        )


@contextmanager
def _capture_auth_logs() -> Iterator[_AuthLogCapture]:
    log_capture = _AuthLogCapture()
    log_sink_id = logger.add(log_capture.capture)
    try:
        yield log_capture
    finally:
        logger.remove(log_sink_id)


def _create_authentication_app() -> FastAPI:
    authentication_service = DashboardJWTAuthenticationService()
    app = FastAPI()

    @app.get("/protected")
    async def read_protected_resource(
        authorization: str = Header(),
    ) -> dict[str, str]:
        _, _, token = authorization.partition(" ")
        identity = authentication_service.decode_identity(token)
        return {
            "user_id": identity.user_id,
            "permission": identity.permission,
        }

    setup_exception_handlers(app)
    return app


async def _request_with_token(token: str) -> Response:
    app = _create_authentication_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            "/protected",
            headers={"Authorization": f"Bearer {token}"},
        )


def _assert_unauthenticated_response(
    response: Response,
    *,
    expected_message: str,
    token: str,
) -> None:
    assert response.status_code == 401
    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"] == expected_message

    serialized_response = json.dumps(response_json, default=str)
    assert "failure_reason" not in serialized_response
    assert "auth_component" not in serialized_response
    assert "dashboard_jwt" not in serialized_response
    assert "jwt_algorithm" not in serialized_response
    assert "jwt_kid" not in serialized_response
    assert token not in serialized_response
    assert f"Bearer {token}" not in serialized_response
    token_segments = token.split(".")
    if len(token_segments) > 1:
        assert token_segments[1] not in serialized_response


def _assert_log_excludes_token(
    auth_log: _CapturedAuthLog,
    *,
    token: str,
) -> None:
    serialized_log = json.dumps(auth_log.extra, default=str)
    assert token not in serialized_log
    assert f"Bearer {token}" not in serialized_log
    token_segments = token.split(".")
    if len(token_segments) > 1:
        assert token_segments[1] not in serialized_log
    assert "contract-dashboard-user" not in serialized_log


def _create_token_without_key_id() -> str:
    return jwt.encode(
        {
            "id": "contract-dashboard-user",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        "contract-secret-with-at-least-32-bytes",
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_missing_key_id_is_a_client_warning_without_fetching_jwks(
    monkeypatch: MonkeyPatch,
) -> None:
    token = _create_token_without_key_id()

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Authentication required",
        token=token,
    )
    assert jwks_server.state.request_count == 0

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["failure_reason"] == "jwt_missing_key_id"
    assert auth_log.extra["jwt_algorithm"] == "HS256"
    assert auth_log.extra["jwt_kid_present"] is False
    assert "jwt_kid" not in auth_log.extra

    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.asyncio
async def test_malformed_jwt_is_a_client_warning_without_fetching_jwks(
    monkeypatch: MonkeyPatch,
) -> None:
    token = "not-a-jwt"

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Invalid token",
        token=token,
    )
    assert jwks_server.state.request_count == 0

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["failure_reason"] == "jwt_invalid"
    assert auth_log.extra["jwt_kid_present"] is False
    assert "jwt_algorithm" not in auth_log.extra
    assert "jwt_kid" not in auth_log.extra
    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.asyncio
async def test_unknown_key_id_refreshes_once_and_remains_a_client_warning(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    attacker_key_id = f"unknown\nkey:{'x' * 80}"
    token = _create_rsa_token(
        signing_key,
        key_id=attacker_key_id,
    )

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_json_response(
                {"keys": [_create_rsa_jwk(signing_key, key_id="known-key")]}
            )
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Authentication required",
        token=token,
    )
    assert jwks_server.state.request_count == 2

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["failure_reason"] == "jwt_unknown_key_id"
    assert auth_log.extra["jwt_algorithm"] == "RS256"
    assert auth_log.extra["jwt_kid_present"] is True
    assert auth_log.extra["jwt_kid"] == "unknown_key:" + ("x" * 52)

    _assert_log_excludes_token(auth_log, token=token)
    serialized_log = json.dumps(auth_log.extra, default=str)
    assert attacker_key_id not in serialized_log


@pytest.mark.asyncio
async def test_unavailable_jwks_is_a_system_error_with_an_unchanged_response(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    token = _create_rsa_token(signing_key, key_id="unavailable-key")

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_raw_response(b"unavailable", status_code=503)
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Authentication required",
        token=token,
    )
    assert jwks_server.state.request_count == 1

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "ERROR"
    assert auth_log.event == "exception.system"
    assert auth_log.extra["error_category"] == "system"
    assert auth_log.extra["failure_reason"] == "jwks_unavailable"
    assert auth_log.extra["jwt_kid"] == "unavailable-key"

    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.parametrize(
    "jwks_body",
    [
        b'{"keys": []}',
        b"not-json",
    ],
    ids=["empty-key-set", "malformed-json"],
)
@pytest.mark.asyncio
async def test_invalid_jwks_is_a_system_error_with_an_unchanged_response(
    monkeypatch: MonkeyPatch,
    jwks_body: bytes,
) -> None:
    signing_key = _create_rsa_private_key()
    token = _create_rsa_token(signing_key, key_id="invalid-jwks-key")

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_raw_response(jwks_body)
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Authentication required",
        token=token,
    )
    assert jwks_server.state.request_count == 1

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "ERROR"
    assert auth_log.event == "exception.system"
    assert auth_log.extra["error_category"] == "system"
    assert auth_log.extra["failure_reason"] == "jwks_invalid"
    assert auth_log.extra["jwt_kid"] == "invalid-jwks-key"

    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.asyncio
async def test_valid_keyed_jwt_returns_identity_without_auth_rejection_log(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    key_id = "valid-key"
    token = _create_rsa_token(
        signing_key,
        key_id=key_id,
        payload_overrides={"permission": "read_only"},
    )

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_json_response(
                {"keys": [_create_rsa_jwk(signing_key, key_id=key_id)]}
            )
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "contract-dashboard-user",
        "permission": "read_only",
    }
    assert jwks_server.state.request_count == 1
    assert log_capture.records == []


@pytest.mark.asyncio
async def test_expired_jwt_retains_response_and_logs_client_warning(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    key_id = "expired-key"
    token = _create_rsa_token(
        signing_key,
        key_id=key_id,
        payload_overrides={"exp": datetime.now(timezone.utc) - timedelta(minutes=5)},
    )

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_json_response(
                {"keys": [_create_rsa_jwk(signing_key, key_id=key_id)]}
            )
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Token has expired",
        token=token,
    )
    assert jwks_server.state.request_count == 1

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["error_category"] == "client"
    assert auth_log.extra["failure_reason"] == "jwt_expired"
    assert auth_log.extra["jwt_algorithm"] == "RS256"
    assert auth_log.extra["jwt_kid"] == key_id
    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.asyncio
async def test_invalid_signature_retains_response_and_logs_client_warning(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    jwks_key = _create_rsa_private_key()
    key_id = "invalid-signature-key"
    token = _create_rsa_token(signing_key, key_id=key_id)

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_json_response(
                {"keys": [_create_rsa_jwk(jwks_key, key_id=key_id)]}
            )
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Invalid token",
        token=token,
    )
    assert jwks_server.state.request_count == 1

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["error_category"] == "client"
    assert auth_log.extra["failure_reason"] == "jwt_invalid"
    assert auth_log.extra["jwt_algorithm"] == "RS256"
    assert auth_log.extra["jwt_kid"] == key_id
    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.asyncio
async def test_missing_user_claim_retains_response_and_logs_client_warning(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    key_id = "missing-user-key"
    token = _create_rsa_token(
        signing_key,
        key_id=key_id,
        payload_overrides={"id": None},
    )

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_json_response(
                {"keys": [_create_rsa_jwk(signing_key, key_id=key_id)]}
            )
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Token missing 'id' claim",
        token=token,
    )
    assert jwks_server.state.request_count == 1

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["error_category"] == "client"
    assert auth_log.extra["failure_reason"] == "jwt_invalid"
    assert auth_log.extra["jwt_algorithm"] == "RS256"
    assert auth_log.extra["jwt_kid"] == key_id
    _assert_log_excludes_token(auth_log, token=token)


@pytest.mark.asyncio
async def test_non_allowlisted_algorithm_is_not_recorded_as_safe_metadata(
    monkeypatch: MonkeyPatch,
) -> None:
    signing_key = _create_rsa_private_key()
    key_id = "unsupported-algorithm-key"
    token = _create_rsa_token(
        signing_key,
        key_id=key_id,
        algorithm="PS256",
    )

    with _capture_auth_logs() as log_capture:
        with _serve_jwks() as jwks_server:
            jwks_server.state.set_json_response(
                {"keys": [_create_rsa_jwk(signing_key, key_id=key_id)]}
            )
            monkeypatch.setattr(
                settings,
                "INTERNAL_DASHBOARD_ENDPOINT",
                jwks_server.endpoint,
            )
            response = await _request_with_token(token)

    _assert_unauthenticated_response(
        response,
        expected_message="Invalid token",
        token=token,
    )

    assert len(log_capture.records) == 1
    auth_log = log_capture.records[0]
    assert auth_log.level == "WARNING"
    assert auth_log.event == "exception.client"
    assert auth_log.extra["failure_reason"] == "jwt_invalid"
    assert auth_log.extra["jwt_kid_present"] is True
    assert auth_log.extra["jwt_kid"] == key_id
    assert "jwt_algorithm" not in auth_log.extra
    _assert_log_excludes_token(auth_log, token=token)


def test_logfire_exception_callback_keeps_system_category_auth_errors_recorded() -> None:
    helper = _FakeLogfireExceptionHelper(
        exception=AuthException(
            error_category="system",
            exception_context={
                "auth_component": "dashboard_jwt",
                "failure_reason": "jwks_unavailable",
            },
        )
    )

    _downgrade_expected_logfire_exception(helper)  # pyright: ignore[reportArgumentType]

    assert helper.level == "error"
    assert helper.is_recording_exception is True


def test_logfire_exception_callback_downgrades_client_category_auth_errors() -> None:
    helper = _FakeLogfireExceptionHelper(exception=AuthException())

    _downgrade_expected_logfire_exception(helper)  # pyright: ignore[reportArgumentType]

    assert helper.level == "warning"
    assert helper.is_recording_exception is False
