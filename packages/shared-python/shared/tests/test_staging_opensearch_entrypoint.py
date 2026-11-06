import json
import os
from pathlib import Path
from stat import S_IMODE

import pytest


def test_staging_tls_bundle_is_removed_from_environment_and_written_privately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.services.retrieval.lexical.staging_entrypoint import installTlsBundle

    bundle = {"ca": "test-ca", "cert": "test-cert", "key": "test-key"}
    monkeypatch.setenv("KNOWHERE_STAGING_NATIVE_TLS", json.dumps(bundle))
    for setting in ("CA_FILE", "CLIENT_CERT_FILE", "CLIENT_KEY_FILE"):
        monkeypatch.delenv(f"KNOWHERE_OPENSEARCH_{setting}", raising=False)
    installTlsBundle(tmp_path)

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
    from shared.services.retrieval.lexical.staging_entrypoint import installTlsBundle

    monkeypatch.setenv("KNOWHERE_STAGING_NATIVE_TLS", '{"key":"private-fixture"}')
    with pytest.raises(ValueError, match="Invalid staging TLS bundle") as error:
        installTlsBundle(tmp_path)
    assert "private-fixture" not in str(error.value)
    assert "KNOWHERE_STAGING_NATIVE_TLS" not in os.environ
    assert not list(tmp_path.iterdir())
