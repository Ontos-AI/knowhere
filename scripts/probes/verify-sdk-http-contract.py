"""Run isolated real-HTTP SDK contracts using explicit unreleased local artifacts.

No parser/model/native provider is contacted. The owned loopback API runs real
job routes, phase SQL, retry service and canonical exception handlers. Native,
authentication and object-delivery boundaries use synthetic contract fixtures.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import zipfile

_ROOT: Path = Path(__file__).resolve().parents[2]


def _hashFiles(paths: list[Path], root: Path) -> str:
    digest = sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _writeJson(path: Path, payload: dict[str, object]) -> None:
    with path.open("x") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--node-sdk-root",
        type=Path,
        required=True,
        help="Standalone Node checkout with pnpm-built dist and dependencies",
    )
    parser.add_argument(
        "--python-sdk-root",
        type=Path,
        required=True,
        help="Standalone Python checkout with a locally built wheel",
    )
    parser.add_argument(
        "--python-sdk-wheel",
        type=Path,
        help="Exact wheel; required when dist contains multiple wheel versions",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        required=True,
        help="New directory for immutable invocation/result/SDK receipts",
    )
    parser.add_argument(
        "--postgresql-executable",
        type=Path,
        help="Explicit isolated pytest-postgresql pg_ctl executable",
    )
    args = parser.parse_args()
    nodeRoot: Path = args.node_sdk_root.resolve()
    pythonRoot: Path = args.python_sdk_root.resolve()
    artifactDirectory: Path = args.artifact_dir.resolve()
    if (
        not (nodeRoot / "dist/index.mjs").is_file()
        or not (nodeRoot / "node_modules").is_dir()
    ):
        parser.error(
            "Node SDK requires its actual built dist/index.mjs and installed dependencies"
        )
    wheels: list[Path] = (
        [args.python_sdk_wheel.resolve()]
        if args.python_sdk_wheel
        else list((pythonRoot / "dist").glob("*.whl"))
    )
    if len(wheels) != 1 or not wheels[0].is_file():
        parser.error("Choose exactly one existing locally built Python SDK wheel")
    if artifactDirectory.exists():
        parser.error(
            "Artifact directory already exists; choose a new immutable campaign directory"
        )
    artifactDirectory.mkdir(parents=True)
    package: Path = artifactDirectory / "python-package"
    package.mkdir()
    with zipfile.ZipFile(wheels[0]) as wheel:
        for member in wheel.infolist():
            destination: Path = (package / member.filename).resolve()
            if not destination.is_relative_to(package):
                raise ValueError("Wheel member escaped the isolated package directory")
        wheel.extractall(package)
    invocation: dict[str, object] = {
        "version": 1,
        "startedAt": datetime.now(UTC).isoformat(),
        "artifactKind": "unreleased-local-build",
        "nodeSdkRoot": str(nodeRoot),
        "nodeArtifactHash": _hashFiles(
            list((nodeRoot / "dist").glob("*.mjs")), nodeRoot
        ),
        "pythonSdkRoot": str(pythonRoot),
        "pythonWheel": wheels[0].name,
        "pythonWheelSha256": sha256(wheels[0].read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        "apiSourceHash": _hashFiles(
            list(
                (
                    _ROOT / "packages/shared-python/shared/services/retrieval/lexical"
                ).glob("*.py")
            )
            + [
                _ROOT / "apps/api/app/services/jobs/indexing_retry_service.py",
                _ROOT / "apps/api/app/services/jobs/result_projection.py",
                _ROOT / "apps/api/app/api/v2/routes/jobs.py",
                _ROOT / "apps/api/tests/contract/test_sdk_http_contract.py",
            ],
            _ROOT,
        ),
        "boundaries": {
            "real": [
                "loopback HTTP",
                "job route",
                "retry admission",
                "phase SQL",
                "result projection",
                "canonical HTTP errors",
                "webhook signing",
            ],
            "synthetic": [
                "authentication",
                "native writes",
                "object delivery",
                "callback destination",
            ],
            "parserCalls": 0,
            "modelCalls": 0,
        },
    }
    _writeJson(artifactDirectory / "invocation.json", invocation)
    plugin: Path = artifactDirectory / "sdk_http_isolated_runtime.py"
    databaseName: str = "Knowhere_sdk_http_" + uuid4().hex[:12]
    plugin.write_text(
        "from pathlib import Path\nimport shared.testing.contract_runtime as runtime\n"
        + f"runtime._TEST_TMP_ROOT = Path({str(artifactDirectory / 'runtime')!r})\n"
        + "runtime._TEST_OBJECT_STORAGE_ROOT = runtime._TEST_TMP_ROOT / 'object-storage'\n"
        + f"runtime.CONTRACT_DATABASE_NAME = {databaseName!r}\n"
    )
    environment: dict[str, str] = dict(os.environ)
    environment.update(
        {
            "KNOWHERE_NODE_SDK_ROOT": str(nodeRoot),
            "KNOWHERE_PYTHON_SDK_ROOT": str(pythonRoot),
            "KNOWHERE_PYTHON_SDK_IMPORT_PATH": str(package),
            "KNOWHERE_SDK_HTTP_REQUIRED": "1",
            "KNOWHERE_SDK_HTTP_ARTIFACT_DIR": str(artifactDirectory),
            "PYTHONPATH": os.pathsep.join(
                [
                    str(artifactDirectory),
                    str(_ROOT / "packages/shared-python"),
                    str(_ROOT / "apps/api"),
                ]
            ),
        }
    )
    if args.postgresql_executable:
        environment["PYTEST_POSTGRESQL_EXECUTABLE"] = str(
            args.postgresql_executable.resolve()
        )
    command: list[str] = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "sdk_http_isolated_runtime",
        "apps/api/tests/contract/test_sdk_http_contract.py",
        "-q",
        "--tb=short",
        f"--junitxml={artifactDirectory / 'junit.xml'}",
    ]
    outcome = subprocess.run(
        command,
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    (artifactDirectory / "pytest.stdout").write_text(outcome.stdout)
    (artifactDirectory / "pytest.stderr").write_text(outcome.stderr)
    receipts: list[dict[str, object]] = [
        json.loads((artifactDirectory / f"{name}.json").read_text())
        for name in ("node", "python-sync", "python-async")
        if (artifactDirectory / f"{name}.json").exists()
    ]
    passed: bool = (
        outcome.returncode == 0
        and len(receipts) == 3
        and all(receipt.get("status") == "passed" for receipt in receipts)
    )
    result: dict[str, object] = {
        "version": 1,
        "status": "passed" if passed else "failed",
        "finishedAt": datetime.now(UTC).isoformat(),
        "pytestExitCode": outcome.returncode,
        "campaigns": receipts,
        "artifactDirectory": str(artifactDirectory),
        "productionAcceptance": False,
        "packagePublished": False,
    }
    _writeJson(artifactDirectory / "result.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
