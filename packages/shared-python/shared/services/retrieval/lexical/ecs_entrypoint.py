"""Install the ECS-injected OpenSearch client identity before starting the app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import mkdtemp

import httpx

from shared.services.retrieval.lexical.clientconfig import NativeClientConfig


TLS_SETTINGS: dict[str, str] = {
    "ca": "CA_FILE", "cert": "CLIENT_CERT_FILE", "key": "CLIENT_KEY_FILE",
}
PRODUCTION_INDEX_PREFIX: str = "knowhere-prod-lexical-v1"


def installTlsBundle(directory: Path, scope: str) -> None:
    rawBundle: str = os.environ.pop(f"KNOWHERE_{scope.upper()}_NATIVE_TLS")
    try:
        bundle = json.loads(rawBundle)
    except ValueError:
        raise ValueError(f"Invalid {scope} TLS bundle") from None
    if not isinstance(bundle, dict) or set(bundle) != TLS_SETTINGS.keys() or any(
        not isinstance(value, str) or not value.strip() for value in bundle.values()
    ):
        raise ValueError(f"Invalid {scope} TLS bundle")
    for name, setting in TLS_SETTINGS.items():
        path: Path = directory / f"{name}.pem"
        descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(bundle[name])
        os.environ[f"KNOWHERE_OPENSEARCH_{setting}"] = str(path)


def attestNative(role: str, scope: str) -> dict[str, str | bool]:
    prefix: str = os.environ["KNOWHERE_OPENSEARCH_INDEX_PREFIX"]
    isProduction: bool = scope == "prod"
    if isProduction and prefix != PRODUCTION_INDEX_PREFIX:
        raise ValueError("Unexpected production OpenSearch index prefix")
    index: str = f"{prefix}-deployment-probe"
    native: NativeClientConfig = NativeClientConfig.resolve(os.environ["KNOWHERE_OPENSEARCH_URL"])
    with httpx.Client(
        base_url=native.endpoint, verify=native.verify, auth=native.auth,
        trust_env=False, timeout=3.0,
    ) as client:
        auth = client.get("/_plugins/_security/authinfo").raise_for_status().json()
        identity: str = f"CN=knowhere-{scope}-{role}"
        expectedRole: str = f"knowhere_prod_{role}" if isProduction else f"knowhere_{role}"
        if auth.get("user_name") != identity or auth.get("roles") != [expectedRole]:
            raise ValueError(f"Unexpected {scope} OpenSearch identity")
        result = client.get(f"/{index}/_search", params={
            "size": 0, "ignore_unavailable": "false" if isProduction else "true",
        }).raise_for_status().json()
        hasShard: bool = result["_shards"]["successful"] > 0
        return {
            "role": role,
            "identity": identity,
            "allowedIndex": index,
            "verifiedExistingShard": hasShard,
            "passed": hasShard or not isProduction,
        }


def main() -> None:
    role: str = sys.argv[1]
    backend: str = os.environ.get("KNOWHERE_LEXICAL_BACKEND", "")
    environment: str = os.environ.get("ENVIRONMENT", "")
    if role not in {"api", "worker"} or environment not in {"staging", "production"}:
        raise SystemExit("OpenSearch TLS bootstrap requires a staging or production API or worker task")
    scope: str = "prod" if environment == "production" else "staging"
    if backend not in {"postgresql", "opensearch"}:
        raise SystemExit("Unsupported lexical backend for OpenSearch TLS bootstrap")

    directory: Path | None = None
    try:
        directory = Path(mkdtemp(prefix=f"knowhere-{scope}-native-"))
        installTlsBundle(directory, scope)
        result: dict[str, str | bool] = attestNative(role, scope)
    except Exception as error:
        result = {"role": role, "passed": False, "errorType": type(error).__name__}
    if scope == "prod" and result["passed"] is not True:
        for setting in TLS_SETTINGS.values():
            os.environ.pop(f"KNOWHERE_OPENSEARCH_{setting}", None)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
    result["pgSelected"] = backend == "postgresql"
    print(f"KNOWHERE_{scope.upper()}_NATIVE=" + json.dumps(result), flush=True)
    if backend == "opensearch" and result["passed"] is not True:
        raise SystemExit(f"{scope.title()} OpenSearch TLS attestation failed")
    os.execv("/entrypoint.sh", ["/entrypoint.sh"])


if __name__ == "__main__":
    main()
