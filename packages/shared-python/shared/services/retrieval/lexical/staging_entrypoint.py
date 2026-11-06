"""Install the ECS-injected staging client identity before starting the app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import mkdtemp

import httpx

from shared.services.retrieval.lexical.clientconfig import NativeClientConfig


def installTlsBundle(directory: Path) -> None:
    rawBundle: str = os.environ.pop("KNOWHERE_STAGING_NATIVE_TLS")
    try:
        bundle = json.loads(rawBundle)
    except ValueError:
        raise ValueError("Invalid staging TLS bundle") from None
    settings: dict[str, str] = {
        "ca": "CA_FILE", "cert": "CLIENT_CERT_FILE", "key": "CLIENT_KEY_FILE",
    }
    if not isinstance(bundle, dict) or set(bundle) != settings.keys() or any(
        not isinstance(value, str) or not value.strip() for value in bundle.values()
    ):
        raise ValueError("Invalid staging TLS bundle")
    for name, setting in settings.items():
        path: Path = directory / f"{name}.pem"
        descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(bundle[name])
        os.environ[f"KNOWHERE_OPENSEARCH_{setting}"] = str(path)


def attestNative(role: str) -> dict[str, str | bool]:
    prefix: str = os.environ["KNOWHERE_OPENSEARCH_INDEX_PREFIX"]
    index: str = f"{prefix}-deployment-probe"
    native: NativeClientConfig = NativeClientConfig.resolve(os.environ["KNOWHERE_OPENSEARCH_URL"])
    with httpx.Client(
        base_url=native.endpoint, verify=native.verify, auth=native.auth,
        trust_env=False, timeout=3.0,
    ) as client:
        auth = client.get("/_plugins/_security/authinfo").raise_for_status().json()
        identity: str = f"CN=knowhere-staging-{role}"
        if auth.get("user_name") != identity or auth.get("roles") != [f"knowhere_{role}"]:
            raise ValueError("Unexpected staging OpenSearch identity")
        result = client.get(f"/{index}/_search", params={
            "size": 0, "ignore_unavailable": "true",
        }).raise_for_status().json()
        return {
            "role": role,
            "identity": identity,
            "allowedIndex": index,
            "verifiedExistingShard": result["_shards"]["successful"] > 0,
            "passed": True,
        }


def main() -> None:
    role: str = sys.argv[1]
    backend: str = os.environ.get("KNOWHERE_LEXICAL_BACKEND", "")
    if (
        role not in {"api", "worker"}
        or os.environ.get("ENVIRONMENT") != "staging"
        or backend not in {"postgresql", "opensearch"}
    ):
        raise SystemExit("OpenSearch TLS bootstrap requires a staging task with a supported lexical backend")
    try:
        directory = Path(mkdtemp(prefix="knowhere-staging-native-"))
        installTlsBundle(directory)
        result: dict[str, str | bool] = attestNative(role)
    except Exception as error:
        result = {"role": role, "passed": False, "errorType": type(error).__name__}
    result["pgSelected"] = backend == "postgresql"
    print("KNOWHERE_STAGING_NATIVE=" + json.dumps(result), flush=True)
    if backend == "opensearch" and result["passed"] is not True:
        raise SystemExit("Staging OpenSearch TLS attestation failed")
    os.execv("/entrypoint.sh", ["/entrypoint.sh"])


if __name__ == "__main__":
    main()
