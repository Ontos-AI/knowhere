import json
import os
from pathlib import Path
from stat import S_IMODE

import pytest


def test_staging_tls_bundle_is_removed_from_environment_and_written_privately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.services.retrieval.lexical.ecs_entrypoint import installTlsBundle

    bundle = {"ca": "test-ca", "cert": "test-cert", "key": "test-key"}
    monkeypatch.setenv("KNOWHERE_STAGING_NATIVE_TLS", json.dumps(bundle))
    for setting in ("CA_FILE", "CLIENT_CERT_FILE", "CLIENT_KEY_FILE"):
        monkeypatch.delenv(f"KNOWHERE_OPENSEARCH_{setting}", raising=False)
    installTlsBundle(tmp_path, "staging")

    assert "KNOWHERE_STAGING_NATIVE_TLS" not in os.environ
    for name, setting in (
        ("ca", "CA_FILE"), ("cert", "CLIENT_CERT_FILE"), ("key", "CLIENT_KEY_FILE")
    ):
        path = Path(os.environ[f"KNOWHERE_OPENSEARCH_{setting}"])
        assert path.parent == tmp_path
        assert path.read_text() == bundle[name]
        assert S_IMODE(path.stat().st_mode) == 0o600


def test_staging_tls_bundle_rejects_invalid_shapes_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.services.retrieval.lexical.ecs_entrypoint import installTlsBundle

    monkeypatch.setenv("KNOWHERE_STAGING_NATIVE_TLS", '{"key":"private-fixture"}')
    with pytest.raises(ValueError, match="Invalid staging TLS bundle") as error:
        installTlsBundle(tmp_path, "staging")
    assert "private-fixture" not in str(error.value)
    assert "KNOWHERE_STAGING_NATIVE_TLS" not in os.environ
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("backend", ["postgresql", "opensearch"])
def test_staging_entrypoint_starts_after_tls_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: str
) -> None:
    from shared.services.retrieval.lexical import ecs_entrypoint, staging_entrypoint

    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", backend)
    monkeypatch.setenv("KNOWHERE_STAGING_NATIVE_TLS", '{"ca":"ca","cert":"cert","key":"key"}')
    monkeypatch.setattr(ecs_entrypoint.sys, "argv", ["staging_entrypoint", "api"])
    monkeypatch.setattr(ecs_entrypoint, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(ecs_entrypoint, "attestNative", lambda role, scope: {"role": role, "passed": True})
    started: list[str] = []
    monkeypatch.setattr(ecs_entrypoint.os, "execv", lambda path, args: started.append(path))

    staging_entrypoint.main()

    assert started == ["/entrypoint.sh"]
    receipt = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert receipt["pgSelected"] is (backend == "postgresql")
    assert receipt["passed"] is True


def test_staging_opensearch_entrypoint_stops_on_missing_tls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared.services.retrieval.lexical import ecs_entrypoint, staging_entrypoint

    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.delenv("KNOWHERE_STAGING_NATIVE_TLS", raising=False)
    monkeypatch.setattr(ecs_entrypoint.sys, "argv", ["staging_entrypoint", "worker"])
    monkeypatch.setattr(ecs_entrypoint, "mkdtemp", lambda **kwargs: str(tmp_path))
    started: list[str] = []
    monkeypatch.setattr(ecs_entrypoint.os, "execv", lambda path, args: started.append(path))

    with pytest.raises(SystemExit):
        staging_entrypoint.main()

    assert not started
    receipt = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert receipt["passed"] is False
    assert receipt["pgSelected"] is False


@pytest.mark.parametrize(
    ("identity", "roles", "successful_shards", "passed"),
    [
        ("CN=knowhere-prod-api", ["knowhere_prod_api"], 1, True),
        ("CN=knowhere-staging-api", ["knowhere_api"], 1, False),
        ("CN=knowhere-prod-api", ["knowhere_prod_worker"], 1, False),
        ("CN=knowhere-prod-api", ["knowhere_prod_api"], 0, False),
    ],
)
def test_production_attests_identity_and_existing_probe_with_postgresql_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    identity: str,
    roles: list[str],
    successful_shards: int,
    passed: bool,
) -> None:
    import httpx

    from shared.services.retrieval.lexical import ecs_entrypoint

    endpoint: str = "https://opensearch.example.internal:9200"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "postgresql")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_INDEX_PREFIX", "knowhere-prod-lexical-v1")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", endpoint)
    monkeypatch.setenv("KNOWHERE_PROD_NATIVE_TLS", '{"ca":"ca","cert":"cert","key":"key"}')
    monkeypatch.setattr(ecs_entrypoint.sys, "argv", ["ecs_entrypoint", "api"])
    monkeypatch.setattr(ecs_entrypoint, "mkdtemp", lambda **kwargs: str(tmp_path))
    native = ecs_entrypoint.NativeClientConfig(endpoint, "test", True, None)
    monkeypatch.setattr(ecs_entrypoint.NativeClientConfig, "resolve", lambda url: native)
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/_plugins/_security/authinfo":
            return httpx.Response(200, json={"user_name": identity, "roles": roles})
        return httpx.Response(200, json={"_shards": {"successful": successful_shards}})

    client = httpx.Client(base_url=endpoint, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(ecs_entrypoint.httpx, "Client", lambda **kwargs: client)
    started: list[str] = []
    monkeypatch.setattr(ecs_entrypoint.os, "execv", lambda path, args: started.append(path))

    ecs_entrypoint.main()

    receipt_line = capsys.readouterr().out
    assert receipt_line.startswith("KNOWHERE_PROD_NATIVE=")
    receipt = json.loads(receipt_line.split("=", 1)[1])
    assert receipt["pgSelected"] is True
    assert receipt["passed"] is passed
    assert paths[0] == "/_plugins/_security/authinfo"
    if passed:
        assert paths[1:] == ["/knowhere-prod-lexical-v1-deployment-probe/_search"]
    assert started == ["/entrypoint.sh"]
    assert "KNOWHERE_PROD_NATIVE_TLS" not in os.environ
    assert ("KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE" in os.environ) is passed


def test_production_opensearch_stops_on_failed_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared.services.retrieval.lexical import ecs_entrypoint

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.delenv("KNOWHERE_PROD_NATIVE_TLS", raising=False)
    monkeypatch.setattr(ecs_entrypoint.sys, "argv", ["ecs_entrypoint", "worker"])
    monkeypatch.setattr(ecs_entrypoint, "mkdtemp", lambda **kwargs: str(tmp_path))
    started: list[str] = []
    monkeypatch.setattr(ecs_entrypoint.os, "execv", lambda path, args: started.append(path))

    with pytest.raises(SystemExit, match="Prod OpenSearch TLS attestation failed"):
        ecs_entrypoint.main()

    assert not started
    receipt = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert receipt["pgSelected"] is False
    assert receipt["passed"] is False
