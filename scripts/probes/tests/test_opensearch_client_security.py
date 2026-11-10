"""Private endpoint authentication and rotation without an application bootstrap."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path
import socket
import ssl
import subprocess
from types import ModuleType

import httpx
import pytest


def loadConfiguration() -> ModuleType:
    name: str = "shared.services.retrieval.lexical.clientconfig"
    assert importlib.util.find_spec(name) is not None, "Native client security configuration is not implemented"
    return importlib.import_module(name)


@pytest.fixture
def certificates(tmp_path: Path) -> tuple[Path, Path]:
    certificate: Path = tmp_path / "client.pem"
    key: Path = tmp_path / "client-key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", "1", "-nodes",
        "-subj", "/CN=knowhere-test", "-keyout", str(key), "-out", str(certificate),
    ], check=True, capture_output=True)
    key.chmod(0o600)
    return certificate, key


def configureBasic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, certificate: Path
) -> Path:
    password: Path = tmp_path / "password"
    password.write_text("first-test-secret\n")
    password.chmod(0o600)
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_CA_FILE", str(certificate))
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_USERNAME", "knowhere-api")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_PASSWORD_FILE", str(password))
    return password


@pytest.mark.parametrize("endpoint", ["http://10.0.0.2:9200", "https://8.8.8.8:9200", "http://user:secret@127.0.0.1:9200", "https://169.254.169.254", "https://127.0.0.1:9200/path", "https://127.0.0.1?secret=value"])
def test_rejects_insecure_private_public_metadata_and_credential_urls(endpoint: str) -> None:
    configuration: ModuleType = loadConfiguration()
    with pytest.raises(ValueError):
        configuration.NativeClientConfig.resolve(endpoint)


def test_allows_existing_explicit_loopback_component_endpoint() -> None:
    configuration: ModuleType = loadConfiguration()
    resolved = configuration.NativeClientConfig.resolve("http://127.0.0.1:19200")
    assert resolved.auth is None
    assert resolved.endpoint == "http://127.0.0.1:19200"


def test_requires_authentication_and_explicit_trust_for_private_endpoint(
    monkeypatch: pytest.MonkeyPatch, certificates: tuple[Path, Path]
) -> None:
    configuration: ModuleType = loadConfiguration()
    with pytest.raises(ValueError, match="CA"):
        configuration.NativeClientConfig.resolve("https://10.0.0.2:9200")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_CA_FILE", str(certificates[0]))
    with pytest.raises(ValueError, match="authentication"):
        configuration.NativeClientConfig.resolve("https://10.0.0.2:9200")


def test_rotation_changes_pool_identity_and_secret_never_enters_representation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, certificates: tuple[Path, Path]
) -> None:
    configuration: ModuleType = loadConfiguration()
    password: Path = configureBasic(monkeypatch, tmp_path, certificates[0])
    first = configuration.NativeClientConfig.resolve("https://10.0.0.2:9200")
    assert isinstance(first.verify, ssl.SSLContext)
    assert first.verify.check_hostname is True
    assert first.verify.verify_mode == ssl.CERT_REQUIRED
    assert isinstance(first.auth, httpx.BasicAuth)
    password.write_text("second-test-secret\n")
    second = configuration.NativeClientConfig.resolve("https://10.0.0.2:9200")
    assert first.identity != second.identity
    assert configuration.NativeClientConfig.resolve("https://10.0.0.2:9200").verify is second.verify
    assert "test-secret" not in repr(first)
    assert "test-secret" not in repr(second)


def test_rejects_world_readable_password_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, certificates: tuple[Path, Path]
) -> None:
    configuration: ModuleType = loadConfiguration()
    password: Path = configureBasic(monkeypatch, tmp_path, certificates[0])
    password.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        configuration.NativeClientConfig.resolve("https://10.0.0.2:9200")


def test_supports_mutual_tls_and_requires_both_certificate_and_key(
    monkeypatch: pytest.MonkeyPatch, certificates: tuple[Path, Path]
) -> None:
    configuration: ModuleType = loadConfiguration()
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_CA_FILE", str(certificates[0]))
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE", str(certificates[0]))
    with pytest.raises(ValueError, match="certificate and key"):
        configuration.NativeClientConfig.resolve("https://10.0.0.2:9200")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE", str(certificates[1]))
    assert isinstance(configuration.NativeClientConfig.resolve("https://10.0.0.2:9200").verify, ssl.SSLContext)


def test_rejects_dns_name_with_any_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    configuration: ModuleType = loadConfiguration()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_options: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 9200)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 9200)),
    ])
    with pytest.raises(ValueError, match="private"):
        configuration.NativeClientConfig.resolve("https://native.internal:9200")


def test_sync_and_async_runtimes_select_new_authenticated_pools_on_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, certificates: tuple[Path, Path]
) -> None:
    loadConfiguration()
    from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
    from shared.services.retrieval.lexical.runtime import OpenSearchRuntime
    from shared.services.retrieval.lexical.error import LexicalIndexError

    password: Path = configureBasic(monkeypatch, tmp_path, certificates[0])
    endpoint: str = "https://10.0.0.2:19200"
    first = OpenSearchRuntime.resolve(endpoint)
    password.write_text("rotated-sync-secret")
    second = OpenSearchRuntime.resolve(endpoint)
    assert first is not second
    assert isinstance(second.client.auth, httpx.BasicAuth)
    password.write_text("third-sync-secret")
    with pytest.raises(LexicalIndexError, match="coordinated restart"):
        OpenSearchRuntime.resolve(endpoint)

    async def verifyAsync() -> None:
        initial = AsyncOpenSearchRuntime.resolve(endpoint)
        password.write_text("rotated-async-secret")
        rotated = AsyncOpenSearchRuntime.resolve(endpoint)
        assert initial is not rotated
        assert isinstance(rotated.client.auth, httpx.BasicAuth)
        password.write_text("third-async-secret")
        with pytest.raises(LexicalIndexError, match="coordinated restart"):
            AsyncOpenSearchRuntime.resolve(endpoint)
        await AsyncOpenSearchRuntime.closeCurrent()

    try:
        asyncio.run(verifyAsync())
    finally:
        first.close()
        second.close()


def test_worker_shutdown_closes_every_current_process_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, certificates: tuple[Path, Path]
) -> None:
    loadConfiguration()
    from shared.services.retrieval.lexical.runtime import OpenSearchRuntime

    password: Path = configureBasic(monkeypatch, tmp_path, certificates[0])
    first = OpenSearchRuntime.resolve("https://10.0.0.2:19201")
    password.write_text("rotated-shutdown-secret")
    second = OpenSearchRuntime.resolve("https://10.0.0.2:19201")
    OpenSearchRuntime.closeCurrent()
    assert first.client.is_closed is True
    assert second.client.is_closed is True
    assert not OpenSearchRuntime._instances


def test_invalid_runtime_trust_is_a_canonical_native_unavailability() -> None:
    loadConfiguration()
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.runtime import OpenSearchRuntime
    from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime

    with pytest.raises(LexicalIndexError, match="configuration"):
        OpenSearchRuntime.resolve("https://8.8.8.8:9200")

    async def verifyAsync() -> None:
        with pytest.raises(LexicalIndexError, match="configuration"):
            AsyncOpenSearchRuntime.resolve("https://8.8.8.8:9200")

    asyncio.run(verifyAsync())
