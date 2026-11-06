"""The replay CLI freezes separate suite identities and isolated commands."""

from __future__ import annotations

import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
from typing import cast

import pytest

from test_opensearch_ranking_support import createManifest


def executeCommand(*arguments: str) -> subprocess.CompletedProcess[str]:
    command: Path = Path(__file__).parents[1] / "replay-opensearch-ranking.py"
    assert command.is_file(), "The isolated replay command is not implemented"
    return subprocess.run([sys.executable, str(command), *arguments], capture_output=True, text=True, check=False)


def test_derives_separate_versioned_manifest_without_modifying_baseline(tmp_path: Path) -> None:
    baseline: Path = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(createManifest()))
    frozen: bytes = baseline.read_bytes()
    extended: Path = tmp_path / "extended.json"
    result = executeCommand("derive", "--baseline", str(baseline), "--output", str(extended), "--suite-id", "derived-filters", "--suite-version", "1")
    assert result.returncode == 0, result.stderr
    assert baseline.read_bytes() == frozen
    manifest: dict[str, object] = json.loads(extended.read_text())
    assert manifest["provenance"] == "automated_derived"
    assert cast(dict[str, object], manifest["suite"])["id"] == "derived-filters"
    assert len(cast(list[object], manifest["queries"])) == 60
    repeated = executeCommand("derive", "--baseline", str(baseline), "--output", str(extended), "--suite-id", "derived-filters", "--suite-version", "1")
    assert repeated.returncode != 0


def test_dry_run_emits_owned_isolation_and_explicit_baseline_command(tmp_path: Path) -> None:
    baseline: Path = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(createManifest()))
    runtime: Path = tmp_path / "runtime"
    result = executeCommand("run", "--manifest", str(baseline), "--output", str(tmp_path / "baseline-report.json"), "--suite", "baseline", "--endpoint", "http://127.0.0.1:19200", "--runtime-directory", str(runtime))
    assert result.returncode == 0, result.stderr
    plan: dict[str, object] = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["indexing_input_mode"] == "raw"
    assert environmentMode(plan) == "raw"
    environment: dict[str, str] = cast(dict[str, str], plan["environment"])
    assert environment["KNOWHERE_RANKING_SUITE"] == "baseline"
    assert environment["KNOWHERE_RANKING_MANIFEST"] == str(baseline)
    assert "--basetemp" in cast(list[str], plan["argv"])
    assert "knowhere_ranking_isolation" in cast(list[str], plan["argv"])
    assert str(runtime / "contract") in str(plan["isolation_plugin"])
    assert not runtime.exists()


def environmentMode(plan: dict[str, object]) -> str:
    return cast(dict[str, str], plan["environment"])["KNOWHERE_RANKING_INPUT_MODE"]


def test_rejects_foreign_endpoint_and_wrong_suite_before_any_execution(tmp_path: Path) -> None:
    baseline: Path = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(createManifest()))
    arguments: list[str] = ["run", "--manifest", str(baseline), "--output", str(tmp_path / "report.json"), "--suite", "extended", "--endpoint", "http://127.0.0.1:19200", "--runtime-directory", str(tmp_path / "runtime")]
    assert executeCommand(*arguments).returncode != 0
    arguments[arguments.index("extended")] = "baseline"
    arguments[arguments.index("http://127.0.0.1:19200")] = "http://private.example:9200"
    assert executeCommand(*arguments).returncode != 0


def test_derived_suite_builds_separate_extended_run_plan_with_readonly_baseline(tmp_path: Path) -> None:
    baseline: Path = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(createManifest()))
    baseline.chmod(0o400)
    before: str = sha256(baseline.read_bytes()).hexdigest()
    extended: Path = tmp_path / "business-extended-automated-v1.json"
    derived = executeCommand("derive", "--baseline", str(baseline), "--output", str(extended),
        "--suite-id", "business-automated-filters", "--suite-version", "1")
    assert derived.returncode == 0, derived.stderr
    result = executeCommand("run", "--manifest", str(extended), "--output", str(tmp_path / "extended-report.json"),
        "--suite", "extended", "--endpoint", "http://127.0.0.1:19200", "--runtime-directory", str(tmp_path / "extended-runtime"))
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["query_count"] == 60
    assert plan["suite"]["kind"] == "extended"
    assert plan["suite"]["baseline_manifest_sha256"] == before
    assert plan["provenance"] == "automated_derived"
    assert plan["environment"]["KNOWHERE_RANKING_SUITE"] == "extended"
    assert sha256(baseline.read_bytes()).hexdigest() == before
    assert baseline.stat().st_mode & 0o777 == 0o400


def test_run_plan_can_borrow_an_explicit_existing_interpreter_without_environment_rebuild(tmp_path: Path) -> None:
    baseline: Path = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(createManifest()))
    result = executeCommand("run", "--manifest", str(baseline), "--output", str(tmp_path / "report.json"),
        "--suite", "baseline", "--endpoint", "http://127.0.0.1:19200", "--runtime-directory", str(tmp_path / "runtime"),
        "--python-executable", sys.executable)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["argv"][:4] == [str(Path(sys.executable).absolute()), "-m", "pytest", "-q"]
    assert "uv" not in plan["argv"]


@pytest.mark.parametrize(("hasCompletedReport", "reportInputMode", "shouldPass"), [
    (False, "precompiled", False),
    (True, "raw", False),
    (True, "precompiled", True),
])
def test_exit_receipt_binds_mode_and_requires_completed_gate(
    tmp_path: Path, hasCompletedReport: bool, reportInputMode: str, shouldPass: bool,
) -> None:
    baseline: Path = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(createManifest()))
    executable: Path = tmp_path / "uv"
    executable.write_text("\n".join([
        "#!" + sys.executable,
        "import os, json, hashlib",
        "from pathlib import Path",
        "source = Path(os.environ['KNOWHERE_RANKING_MANIFEST'])",
        f"completed = {hasCompletedReport!r}",
        f"inputMode = {reportInputMode!r}",
        "assert os.environ['KNOWHERE_RANKING_INPUT_MODE'] == 'precompiled'",
        "if completed:",
        "    Path(os.environ['KNOWHERE_RANKING_OUTPUT']).write_text(json.dumps({'completed':True,'indexing_input_mode':inputMode,'raw_compilation_deadline_qualified':inputMode=='raw','quality_gate':{'passed':True},'suite':{'kind':'baseline','id':'original-20','version':'1','baseline_manifest_sha256':None},'manifest_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}))",
        "",
    ]))
    executable.chmod(0o700)
    runtime: Path = tmp_path / "runtime"
    command: Path = Path(__file__).parents[1] / "replay-opensearch-ranking.py"
    result = subprocess.run([sys.executable, str(command), "run", "--manifest", str(baseline),
        "--output", str(tmp_path / "report.json"), "--suite", "baseline", "--endpoint", "http://127.0.0.1:19200",
        "--runtime-directory", str(runtime), "--input-mode", "precompiled", "--execute"], capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"], "PYTEST_POSTGRESQL_EXECUTABLE": sys.executable})
    assert result.returncode == (0 if shouldPass else 1), result.stderr
    receipt = json.loads((runtime / "exit-receipt.json").read_text())
    assert receipt["completed"] is hasCompletedReport
    assert receipt["passed"] is shouldPass
    assert receipt["indexing_input_mode"] == "precompiled"
    assert not receipt["raw_compilation_deadline_qualified"]
    assert receipt["pytest_returncode"] == 0
    assert receipt["manifest_sha256"] == sha256(baseline.read_bytes()).hexdigest()
