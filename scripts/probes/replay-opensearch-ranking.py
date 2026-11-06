"""Prepare/evaluate frozen suites or replay one suite on an owned local node.

Examples (run baseline and extended separately, with different report/runtime paths):
  uv run --offline --frozen python scripts/probes/replay-opensearch-ranking.py derive \
    --baseline /private/manifest.json --output /private/extended-v1.json \
    --suite-id automated-filter-extension --suite-version 1
  uv run --offline --frozen python scripts/probes/replay-opensearch-ranking.py run \
    --manifest /private/manifest.json --output /private/baseline-report.json \
    --suite baseline --endpoint http://127.0.0.1:19200 \
    --runtime-directory /private/baseline-runtime

The run subcommand prints its source-matched isolated command by default. Add
--execute after resource preflight; the command creates its own PG test process
and mutates only unique test indexes on the explicitly supplied loopback node.
It does not provision a node or perform final staging load qualification.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import cast
from urllib.parse import urlsplit


def _loadSupport() -> ModuleType:
    path: Path = Path(__file__).with_name("opensearch-ranking-support.py")
    specification = importlib.util.spec_from_file_location("knowhere_ranking_support", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("Cannot load ranking support from the delivered source")
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _writeNewJson(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def _buildRunPlan(arguments: argparse.Namespace, support: ModuleType) -> dict[str, object]:
    endpoint = urlsplit(arguments.endpoint)
    if endpoint.hostname not in ("127.0.0.1", "::1") or endpoint.scheme not in ("http", "https"):
        raise ValueError("Use an explicitly owned loopback OpenSearch endpoint")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("Test endpoint must not contain credentials, query, or fragment")
    manifestPath: Path = Path(arguments.manifest).resolve()
    manifest = support.ReplaySupport.loadManifest(manifestPath)
    if manifest.suite.kind != arguments.suite:
        raise ValueError("Requested baseline/extended suite does not match the manifest")
    outputPath: Path = Path(arguments.output).resolve()
    runtimePath: Path = Path(arguments.runtime_directory).resolve()
    if outputPath.exists() or runtimePath.exists():
        raise ValueError("Use new report and private runtime paths for each suite/campaign")
    repository: Path = Path(__file__).resolve().parents[2]
    plugin: str = "\n".join([
        "from pathlib import Path",
        "from shared.testing import contract_runtime",
        f"contract_runtime._TEST_TMP_ROOT = Path({str(runtimePath / 'contract')!r})",
        "contract_runtime._TEST_OBJECT_STORAGE_ROOT = contract_runtime._TEST_TMP_ROOT / 'object-storage'",
        "",
    ])
    environment: dict[str, str] = {
        "KNOWHERE_RANKING_MANIFEST": str(manifestPath),
        "KNOWHERE_RANKING_OUTPUT": str(outputPath),
        "KNOWHERE_RANKING_SUITE": arguments.suite,
        "KNOWHERE_RANKING_INPUT_MODE": arguments.input_mode,
        "KNOWHERE_TEST_OPENSEARCH_URL": arguments.endpoint,
        "PYTHONPATH": os.pathsep.join([str(runtimePath), str(repository / "packages/shared-python")]),
        "UV_OFFLINE": "1",
        "LOGFIRE_SEND_TO_LOGFIRE": "false",
        "OTEL_SDK_DISABLED": "true",
        "SENTRY_DSN": "",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    pythonExecutable: Path | None = arguments.python_executable
    if pythonExecutable is not None and (not pythonExecutable.is_file() or not os.access(pythonExecutable, os.X_OK)):
        raise ValueError("The reviewed existing Python interpreter must be an executable file")
    # Keep a virtualenv interpreter's symlink path: resolving it to the base
    # binary would silently change the dependency environment being reviewed.
    prefix: list[str] = [str(pythonExecutable.absolute())] if pythonExecutable is not None else ["uv", "run", "--offline", "--frozen", "python"]
    argv: list[str] = [
        *prefix, "-m", "pytest",
        "-q", "-rs", "--tb=short", "-p", "knowhere_ranking_isolation",
        "--basetemp", str(runtimePath / "pytest"),
        "tests/contract/test_opensearch_ranking_replay.py",
    ]
    return {
        "schema_version": "ranking-command-manifest-v1",
        "mode": "execute" if arguments.execute else "dry-run",
        "indexing_input_mode": arguments.input_mode,
        "suite": manifest.suite.model_dump(mode="json"),
        "provenance": manifest.provenance,
        "manifest_sha256": sha256(manifestPath.read_bytes()).hexdigest(),
        "query_count": len(manifest.queries),
        "cwd": str(repository / "apps/api"),
        "argv": argv,
        "environment": environment,
        "isolation_plugin": plugin,
    }


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    derive = commands.add_parser("derive", help="Derive an explicitly automated extended filter suite")
    derive.add_argument("--baseline", required=True, type=Path)
    derive.add_argument("--output", required=True, type=Path)
    derive.add_argument("--suite-id", required=True)
    derive.add_argument("--suite-version", required=True)
    evaluate = commands.add_parser("evaluate", help="Reevaluate a v2 replay report without services")
    evaluate.add_argument("--report", required=True, type=Path)
    evaluate.add_argument("--output", required=True, type=Path)
    replay = commands.add_parser("run", help="Print or execute one isolated suite command")
    replay.add_argument("--manifest", required=True, type=Path)
    replay.add_argument("--output", required=True, type=Path)
    replay.add_argument("--suite", required=True, choices=("baseline", "extended"))
    replay.add_argument("--endpoint", required=True)
    replay.add_argument("--runtime-directory", required=True, type=Path)
    replay.add_argument("--python-executable", type=Path, help="Borrow this reviewed existing interpreter without creating or syncing an environment")
    replay.add_argument("--input-mode", choices=("raw", "precompiled"), default="raw",
        help="Compile inside the attempt (default), or qualify ranking with an immutable input prepared outside its deadline")
    replay.add_argument("--execute", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    support: ModuleType = _loadSupport()
    if arguments.command == "derive":
        manifest = support.ReplaySupport.deriveExtendedManifest(
            arguments.baseline, suiteId=arguments.suite_id, suiteVersion=arguments.suite_version
        )
        _writeNewJson(arguments.output, manifest.model_dump(mode="json"))
        print(json.dumps({"suite": manifest.suite.model_dump(), "query_count": len(manifest.queries), "provenance": manifest.provenance}))
        return 0
    if arguments.command == "evaluate":
        report: dict[str, object] = json.loads(arguments.report.read_bytes())
        result: dict[str, object] = support.ReplaySupport.evaluateReport(report)
        if report.get("completed") is not True:
            result["passed"] = False
            result["incomplete_run"] = True
        _writeNewJson(arguments.output, result)
        print(json.dumps({key: value for key, value in result.items() if key != "groups"}))
        return 0 if result["passed"] else 1
    plan: dict[str, object] = _buildRunPlan(arguments, support)
    if not arguments.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    executable: str | None = os.environ.get("PYTEST_POSTGRESQL_EXECUTABLE")
    if executable is None or not Path(executable).is_file():
        raise ValueError("Set PYTEST_POSTGRESQL_EXECUTABLE to the isolated installed pg_ctl executable")
    runtimePath: Path = arguments.runtime_directory.resolve()
    runtimePath.mkdir(mode=0o700)
    (runtimePath / "knowhere_ranking_isolation.py").write_text(str(plan["isolation_plugin"]))
    _writeNewJson(runtimePath / "command-manifest.json", plan)
    overrides: dict[str, str] = cast(dict[str, str], plan["environment"])
    command: list[str] = cast(list[str], plan["argv"])
    completed: subprocess.CompletedProcess[bytes] = subprocess.run(
        command, cwd=str(plan["cwd"]), env={**os.environ, **overrides}, check=False
    )
    output: Path = arguments.output.resolve()
    report: dict[str, object] = {}
    reportError: str | None = None
    try:
        value: object = json.loads(output.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("Replay output is not an object")
        report = cast(dict[str, object], value)
    except (OSError, ValueError):
        reportError = "Replay did not persist a valid report; a skip is not completion"
    gateValue: object = report.get("quality_gate")
    gate: dict[str, object] = cast(dict[str, object], gateValue) if isinstance(gateValue, dict) else {}
    isCompleted: bool = report.get("completed") is True
    expectedRawQualification: bool = isCompleted and arguments.input_mode == "raw"
    hasMatchingIdentity: bool = (
        report.get("manifest_sha256") == plan["manifest_sha256"]
        and report.get("suite") == plan["suite"]
        and report.get("indexing_input_mode") == plan["indexing_input_mode"]
        and report.get("raw_compilation_deadline_qualified") is expectedRawQualification
    )
    isPassed: bool = completed.returncode == 0 and isCompleted and hasMatchingIdentity and gate.get("passed") is True
    _writeNewJson(runtimePath / "exit-receipt.json", {"pytest_returncode": completed.returncode,
        "returncode": completed.returncode if completed.returncode else (0 if isPassed else 1),
        "completed": isCompleted, "passed": isPassed, "quality_gate": gate,
        "indexing_input_mode": plan["indexing_input_mode"],
        "raw_compilation_deadline_qualified": hasMatchingIdentity and expectedRawQualification,
        "suite": plan["suite"], "manifest_sha256": plan["manifest_sha256"],
        "report_sha256": sha256(output.read_bytes()).hexdigest() if output.is_file() else None,
        "has_matching_report_identity": hasMatchingIdentity, "report_error": reportError})
    return completed.returncode if completed.returncode else (0 if isPassed else 1)


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError) as err:
        print(str(err), file=sys.stderr)
        raise SystemExit(2) from err
