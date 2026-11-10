"""Real HTTPX TLS handshakes against an owned loopback component responder.

This exercises client transport only. The responder is not OpenSearch and does
not qualify its Security plugin, production PKI, network or deployment policy.
No certificate/private key generated here is a real deployment credential.
"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
from pathlib import Path
import ssl
from threading import Event, Thread
from time import monotonic
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import httpx
import pytest
import pytest_asyncio

from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.clientconfig import NativeClientConfig
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.runtime import OpenSearchRuntime
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope

_Kind = Literal["sync", "async"]
_Runtime = OpenSearchRuntime | AsyncOpenSearchRuntime
_NativeIndex = OpenSearchLexicalIndex | AsyncOpenSearchLexicalIndex
_ENVIRONMENT_NAMES: tuple[str, ...] = (
    "KNOWHERE_OPENSEARCH_CA_FILE",
    "KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE",
    "KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE",
    "KNOWHERE_OPENSEARCH_USERNAME",
    "KNOWHERE_OPENSEARCH_PASSWORD_FILE",
)


@dataclass(frozen=True)
class _Certificates:
    authority: Path
    wrongAuthority: Path
    server: Path
    serverKey: Path
    wrongHostname: Path
    wrongHostnameKey: Path
    client: Path
    clientKey: Path
    untrustedClient: Path
    untrustedClientKey: Path


@dataclass
class _ResponderState:
    expectedPassword: str = "synthetic-first-secret"
    requiresBasic: bool = True
    delaySeconds: float = 0
    requestCount: int = 0
    clientCertificates: list[bool] = field(default_factory=list)
    protocolVersions: list[str] = field(default_factory=list)


class _NativeResponder(ThreadingHTTPServer):
    """Only HTTP/TLS authentication and a tiny JSON reply, with bounded shutdown."""

    daemon_threads = False
    block_on_close = True

    def __init__(
        self,
        context: ssl.SSLContext,
        state: _ResponderState,
        *,
        handshakeDelay: float = 0,
    ) -> None:
        self.context: ssl.SSLContext = context
        self.state: _ResponderState = state
        self.handshakeDelay: float = handshakeDelay
        super().__init__(("127.0.0.1", 0), _ResponderHandler)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(0.5)
        try:
            if self.handshakeDelay:
                Event().wait(self.handshakeDelay)
            secured: ssl.SSLSocket = self.context.wrap_socket(
                connection, server_side=True
            )
            return secured, address
        except BaseException:
            connection.close()
            raise

    def handle_error(self, request: object, client_address: object) -> None:
        # A deliberate client timeout can terminate a test connection mid-write.
        # Never print request headers or credential material during teardown.
        return None


class _ResponderHandler(BaseHTTPRequestHandler):
    server: _NativeResponder

    def do_GET(self) -> None:
        state: _ResponderState = self.server.state
        state.requestCount += 1
        assert isinstance(self.connection, ssl.SSLSocket)
        state.clientCertificates.append(bool(self.connection.getpeercert()))
        state.protocolVersions.append(self.connection.version() or "unknown")
        expected: str = (
            "Basic "
            + b64encode(f"knowhere-api:{state.expectedPassword}".encode()).decode()
        )
        hasAccess: bool = not state.requiresBasic or compare_digest(
            self.headers.get("Authorization", ""), expected
        )
        if state.delaySeconds:
            Event().wait(state.delaySeconds)
        body: bytes = json.dumps(
            {
                "authenticated": hasAccess,
                "hasClientCertificate": state.clientCertificates[-1],
            }
        ).encode()
        try:
            self.send_response(200 if hasAccess else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ssl.SSLError):
            return None

    def log_message(self, format: str, *arguments: object) -> None:
        return None


def _createAuthority(name: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key: ec.EllipticCurvePrivateKey = ec.generate_private_key(ec.SECP256R1())
    subject: x509.Name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now: datetime = datetime.now(timezone.utc)
    certificate: x509.Certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return certificate, key


def _writeCertificate(
    directory: Path,
    name: str,
    authority: x509.Certificate,
    authorityKey: ec.EllipticCurvePrivateKey,
    *,
    isClient: bool = False,
    hasWrongHostname: bool = False,
) -> tuple[Path, Path]:
    key: ec.EllipticCurvePrivateKey = ec.generate_private_key(ec.SECP256R1())
    now: datetime = datetime.now(timezone.utc)
    builder: x509.CertificateBuilder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.CLIENT_AUTH
                    if isClient
                    else ExtendedKeyUsageOID.SERVER_AUTH
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                authorityKey.public_key()
            ),
            critical=False,
        )
    )
    if not isClient:
        names: list[x509.GeneralName] = (
            [x509.DNSName("wrong-hostname.invalid")]
            if hasWrongHostname
            else [x509.IPAddress(ip_address("127.0.0.1")), x509.DNSName("localhost")]
        )
        builder = builder.add_extension(
            x509.SubjectAlternativeName(names), critical=False
        )
    certificate: x509.Certificate = builder.sign(authorityKey, hashes.SHA256())
    certificatePath: Path = directory / f"{name}.pem"
    keyPath: Path = directory / f"{name}-key.pem"
    certificatePath.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyPath.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keyPath.chmod(0o600)
    return certificatePath, keyPath


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> _Certificates:
    directory: Path = tmp_path_factory.mktemp("knowhere-owned-native-tls")
    authority, authorityKey = _createAuthority("Owned component authority")
    wrongAuthority, wrongAuthorityKey = _createAuthority(
        "Untrusted component authority"
    )
    authorityPath: Path = directory / "authority.pem"
    wrongAuthorityPath: Path = directory / "untrusted-authority.pem"
    authorityPath.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    wrongAuthorityPath.write_bytes(
        wrongAuthority.public_bytes(serialization.Encoding.PEM)
    )
    server, serverKey = _writeCertificate(directory, "server", authority, authorityKey)
    wrongHostname, wrongHostnameKey = _writeCertificate(
        directory, "wrong-hostname", authority, authorityKey, hasWrongHostname=True
    )
    client, clientKey = _writeCertificate(
        directory, "client", authority, authorityKey, isClient=True
    )
    untrustedClient, untrustedClientKey = _writeCertificate(
        directory, "untrusted-client", wrongAuthority, wrongAuthorityKey, isClient=True
    )
    return _Certificates(
        authorityPath,
        wrongAuthorityPath,
        server,
        serverKey,
        wrongHostname,
        wrongHostnameKey,
        client,
        clientKey,
        untrustedClient,
        untrustedClientKey,
    )


@pytest_asyncio.fixture(autouse=True)
async def closeOwnedPools(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    for name in _ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        await AsyncOpenSearchRuntime.closeCurrent()
        OpenSearchRuntime.closeCurrent()


def _configureBasic(
    monkeypatch: pytest.MonkeyPatch, directory: Path, certificates: _Certificates
) -> Path:
    password: Path = directory / "password"
    password.write_text("synthetic-first-secret\n")
    password.chmod(0o600)
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_CA_FILE", str(certificates.authority))
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_USERNAME", "knowhere-api")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_PASSWORD_FILE", str(password))
    return password


def _configureClient(
    monkeypatch: pytest.MonkeyPatch,
    certificates: _Certificates,
    *,
    isUntrusted: bool = False,
) -> None:
    monkeypatch.setenv(
        "KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE",
        str(certificates.untrustedClient if isUntrusted else certificates.client),
    )
    monkeypatch.setenv(
        "KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE",
        str(certificates.untrustedClientKey if isUntrusted else certificates.clientKey),
    )


@contextmanager
def _serve(
    certificates: _Certificates,
    *,
    requiresClient: bool = False,
    requiresBasic: bool = True,
    hasWrongHostname: bool = False,
    delaySeconds: float = 0,
    handshakeDelay: float = 0,
) -> Iterator[tuple[str, _ResponderState]]:
    context: ssl.SSLContext = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        str(certificates.wrongHostname if hasWrongHostname else certificates.server),
        str(
            certificates.wrongHostnameKey
            if hasWrongHostname
            else certificates.serverKey
        ),
    )
    context.load_verify_locations(cafile=str(certificates.authority))
    context.verify_mode = ssl.CERT_REQUIRED if requiresClient else ssl.CERT_OPTIONAL
    state: _ResponderState = _ResponderState(
        requiresBasic=requiresBasic, delaySeconds=delaySeconds
    )
    server: _NativeResponder = _NativeResponder(
        context, state, handshakeDelay=handshakeDelay
    )
    thread: Thread = Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        name="owned-native-tls-component",
    )
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "Owned TLS component server did not settle"


def _resolve(kind: _Kind, endpoint: str) -> _Runtime:
    return (
        OpenSearchRuntime.resolve(endpoint)
        if kind == "sync"
        else AsyncOpenSearchRuntime.resolve(endpoint)
    )


async def _request(runtime: _Runtime, *, timeout: float = 1) -> httpx.Response:
    if isinstance(runtime, OpenSearchRuntime):
        return runtime.client.get("/component", timeout=timeout)
    return await runtime.client.get("/component", timeout=timeout)


def _rotatePassword(path: Path, secret: str) -> None:
    replacement: Path = path.with_suffix(".replacement")
    replacement.write_text(secret + "\n")
    replacement.chmod(0o600)
    replacement.replace(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_real_tls_basic_auth_and_optional_client_certificate(
    kind: _Kind,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    _configureBasic(monkeypatch, tmp_path, certificates)
    with _serve(certificates) as (endpoint, state):
        configuration: NativeClientConfig = NativeClientConfig.resolve(endpoint)
        assert (
            isinstance(configuration.verify, ssl.SSLContext)
            and configuration.verify.check_hostname
        )
        response: httpx.Response = await _request(_resolve(kind, endpoint))
        assert response.status_code == 200 and response.json()["authenticated"] is True
        assert state.requestCount == 1 and state.clientCertificates == [False]
        assert state.protocolVersions[0] in ("TLSv1.2", "TLSv1.3")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_real_tls_requires_and_verifies_client_certificate(
    kind: _Kind,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    _configureBasic(monkeypatch, tmp_path, certificates)
    _configureClient(monkeypatch, certificates)
    monkeypatch.delenv("KNOWHERE_OPENSEARCH_USERNAME")
    monkeypatch.delenv("KNOWHERE_OPENSEARCH_PASSWORD_FILE")
    with _serve(certificates, requiresClient=True, requiresBasic=False) as (
        endpoint,
        state,
    ):
        response: httpx.Response = await _request(_resolve(kind, endpoint))
        assert (
            response.status_code == 200
            and response.json()["hasClientCertificate"] is True
        )
        assert state.requestCount == 1 and state.clientCertificates == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize(
    "failure", ["wrong-ca", "wrong-hostname", "missing-client", "untrusted-client"]
)
async def test_real_tls_rejects_untrusted_or_mismatched_peer_before_http(
    kind: _Kind,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    _configureBasic(monkeypatch, tmp_path, certificates)
    if failure == "wrong-ca":
        monkeypatch.setenv(
            "KNOWHERE_OPENSEARCH_CA_FILE", str(certificates.wrongAuthority)
        )
    if failure == "untrusted-client":
        _configureClient(monkeypatch, certificates, isUntrusted=True)
    with _serve(
        certificates,
        requiresClient=failure in ("missing-client", "untrusted-client"),
        hasWrongHostname=failure == "wrong-hostname",
    ) as (endpoint, state):
        started: float = monotonic()
        # HTTPX backends may expose the TLS alert directly. The native-domain
        # contract below must convert either backend shape before application use.
        with pytest.raises((httpx.TransportError, ssl.SSLError)):
            await _request(_resolve(kind, endpoint))
        assert monotonic() - started < 1.5
        assert state.requestCount == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_real_tls_revoked_secret_and_rotation_generation_limit(
    kind: _Kind,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    password: Path = _configureBasic(monkeypatch, tmp_path, certificates)
    with _serve(certificates) as (endpoint, state):
        first: _Runtime = _resolve(kind, endpoint)
        assert (await _request(first)).status_code == 200
        state.expectedPassword = "synthetic-second-secret"
        assert (await _request(first)).status_code == 401
        _rotatePassword(password, state.expectedPassword)
        second: _Runtime = _resolve(kind, endpoint)
        assert second is not first and (await _request(second)).status_code == 200
        state.expectedPassword = "synthetic-third-secret"
        _rotatePassword(password, state.expectedPassword)
        before: int = state.requestCount
        with pytest.raises(LexicalIndexError, match="coordinated restart"):
            _resolve(kind, endpoint)
        assert state.requestCount == before == 3
        await AsyncOpenSearchRuntime.closeCurrent()
        OpenSearchRuntime.closeCurrent()
        assert (await _request(_resolve(kind, endpoint))).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("isHandshake", [False, True])
async def test_real_tls_transport_timeouts_are_bounded_without_retry(
    kind: _Kind,
    isHandshake: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    _configureBasic(monkeypatch, tmp_path, certificates)
    with _serve(
        certificates,
        delaySeconds=0 if isHandshake else 0.2,
        handshakeDelay=0.2 if isHandshake else 0,
    ) as (endpoint, state):
        runtime: _Runtime = _resolve(kind, endpoint)
        started: float = monotonic()
        with pytest.raises(httpx.TimeoutException):
            await _request(runtime, timeout=0.05)
        assert monotonic() - started < 0.75
        assert state.requestCount == (0 if isHandshake else 1)


def _createNative(kind: _Kind, endpoint: str) -> _NativeIndex:
    factory: type[OpenSearchLexicalIndex] | type[AsyncOpenSearchLexicalIndex] = (
        OpenSearchLexicalIndex if kind == "sync" else AsyncOpenSearchLexicalIndex
    )
    return factory(
        endpoint=endpoint,
        scope=LexicalScope(ownerId="tls-component", namespace="transport"),
        timeoutSeconds=1,
    )


async def _requestNative(native: _NativeIndex) -> object:
    if isinstance(native, OpenSearchLexicalIndex):
        return native._request("GET", "/component")
    return await native._request("GET", "/component")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("errorType", [ssl.SSLError, ssl.SSLCertVerificationError])
async def test_native_request_wraps_raw_ssl_backend_failure(
    kind: _Kind,
    errorType: type[ssl.SSLError],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    _configureBasic(monkeypatch, tmp_path, certificates)
    error: ssl.SSLError = errorType(
        ssl.SSL_ERROR_SSL, "synthetic certificate-required alert"
    )
    calls: list[str] = []

    def failSync(*arguments: object, **options: object) -> None:
        calls.append("native-request")
        raise error

    async def failAsync(*arguments: object, **options: object) -> None:
        calls.append("native-request")
        raise error

    with _serve(certificates) as (endpoint, state):
        native: _NativeIndex = _createNative(kind, endpoint)
        monkeypatch.setattr(
            native._client,
            "request",
            failSync if isinstance(native, OpenSearchLexicalIndex) else failAsync,
        )
        try:
            with pytest.raises(LexicalIndexError, match="transport") as captured:
                await _requestNative(native)
            assert captured.value.isRetryable and captured.value.hasUnknownOutcome
            assert captured.value.__cause__ is error
            assert calls == ["native-request"] and state.requestCount == 0
        finally:
            native.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize(
    "failure", ["wrong-ca", "wrong-hostname", "missing-client", "untrusted-client"]
)
async def test_real_tls_denial_is_a_sticky_native_domain_failure(
    kind: _Kind,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    certificates: _Certificates,
) -> None:
    from shared.services.retrieval.execution.native_failure import NativeFailureState

    _configureBasic(monkeypatch, tmp_path, certificates)
    if failure == "wrong-ca":
        monkeypatch.setenv(
            "KNOWHERE_OPENSEARCH_CA_FILE", str(certificates.wrongAuthority)
        )
    if failure == "untrusted-client":
        _configureClient(monkeypatch, certificates, isUntrusted=True)
    with _serve(
        certificates,
        requiresClient=failure in ("missing-client", "untrusted-client"),
        hasWrongHostname=failure == "wrong-hostname",
    ) as (endpoint, state):
        native: _NativeIndex = _createNative(kind, endpoint)
        try:
            with pytest.raises(LexicalIndexError, match="transport") as captured:
                await _requestNative(native)
            assert captured.value.isRetryable and captured.value.hasUnknownOutcome
            assert isinstance(
                captured.value.__cause__, (httpx.TransportError, ssl.SSLError)
            )
            assert state.requestCount == 0
            fatal: NativeFailureState = NativeFailureState()
            with fatal.bind():
                NativeFailureState.recordCurrent(captured.value)
                with pytest.raises(LexicalIndexError) as sticky:
                    NativeFailureState.raiseCurrent()
            assert sticky.value is captured.value and fatal.failure is captured.value
        finally:
            native.close()
