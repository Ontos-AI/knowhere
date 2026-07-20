from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, cast

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    generate_private_key,
)
from httpx import AsyncClient
from pytest import MonkeyPatch

DashboardPermission = Literal["read_only", "full_access"]


@dataclass
class JWKSResponseState:
    body: bytes = b'{"keys": []}'
    status_code: int = 200
    request_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_request(self) -> tuple[bytes, int]:
        with self.lock:
            self.request_count += 1
            return self.body, self.status_code

    def set_json_response(
        self,
        response: Mapping[str, object],
        *,
        status_code: int = 200,
    ) -> None:
        with self.lock:
            self.body = json.dumps(response).encode("utf-8")
            self.status_code = status_code

    def set_raw_response(
        self,
        body: bytes,
        *,
        status_code: int = 200,
    ) -> None:
        with self.lock:
            self.body = body
            self.status_code = status_code


@dataclass(frozen=True)
class LocalJWKSServer:
    endpoint: str
    state: JWKSResponseState


def create_dashboard_rsa_private_key() -> RSAPrivateKey:
    return generate_private_key(public_exponent=65537, key_size=2048)


def create_dashboard_rsa_jwk(
    private_key: RSAPrivateKey,
    *,
    key_id: str,
) -> dict[str, object]:
    jwk = cast(
        dict[str, object],
        jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True),
    )
    return {
        **jwk,
        "kid": key_id,
        "use": "sig",
        "alg": "RS256",
    }


def create_dashboard_rsa_token(
    private_key: RSAPrivateKey,
    *,
    key_id: str,
    user_id: str = "contract-dashboard-user",
    permission: DashboardPermission | None = None,
    expires_at: datetime | None = None,
    payload_overrides: Mapping[str, object] | None = None,
    algorithm: str = "RS256",
) -> str:
    payload: dict[str, object] = {
        "id": user_id,
        "exp": expires_at or datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    if permission is not None:
        payload["permission"] = permission
    payload.update(payload_overrides or {})

    return jwt.encode(
        payload,
        private_key,
        algorithm=algorithm,
        headers={"kid": key_id},
    )


def _create_jwks_handler(
    state: JWKSResponseState,
) -> type[BaseHTTPRequestHandler]:
    class JWKSRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body, status_code = state.record_request()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return JWKSRequestHandler


@contextmanager
def serve_dashboard_jwks() -> Iterator[LocalJWKSServer]:
    state = JWKSResponseState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _create_jwks_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    try:
        yield LocalJWKSServer(endpoint=f"http://{host}:{port}", state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def use_dashboard_jwks_token(
    api_client: AsyncClient,
    monkeypatch: MonkeyPatch,
    *,
    user_id: str,
    permission: DashboardPermission | None = None,
) -> Iterator[str]:
    key_id = f"contract-dashboard-key-{user_id}"
    signing_key = create_dashboard_rsa_private_key()
    token = create_dashboard_rsa_token(
        signing_key,
        key_id=key_id,
        user_id=user_id,
        permission=permission,
    )

    with serve_dashboard_jwks() as jwks_server:
        from shared.core.config import settings

        jwks_server.state.set_json_response(
            {"keys": [create_dashboard_rsa_jwk(signing_key, key_id=key_id)]}
        )
        monkeypatch.setattr(
            settings,
            "INTERNAL_DASHBOARD_ENDPOINT",
            jwks_server.endpoint,
        )
        api_client.headers.update({"Authorization": f"Bearer {token}"})
        yield token
