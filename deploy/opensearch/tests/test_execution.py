"""A physical operator issuer is serialized and its descendants are bounded."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from test_operations import createManifest


def loadExecution() -> type:
    path: Path = Path(__file__).parents[1] / "execution.py"
    assert path.is_file(), "Single-writer operator execution is absent"
    specification = importlib.util.spec_from_file_location("operator_execution", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.OperationExecution)


def test_duplicate_finish_issuer_cannot_enter_until_first_completes(tmp_path: Path) -> None:
    execution: type = loadExecution()
    with execution.claim(tmp_path, resourceId="cluster-one", operationId="first-finish") as first:
        with pytest.raises(ValueError, match="issuer"):
            with execution.claim(tmp_path, resourceId="cluster-one", operationId="duplicate-finish"):
                pytest.fail("A second native close issuer entered")
        first.complete()
    with execution.claim(tmp_path, resourceId="cluster-one", operationId="next-command") as nextIssuer:
        nextIssuer.complete()


def test_unknown_issuer_remains_durable_after_process_lock_is_released(tmp_path: Path) -> None:
    execution: type = loadExecution()
    with execution.claim(tmp_path, resourceId="cluster-one", operationId="unknown"):
        pass
    with pytest.raises(ValueError, match="reconciliation"):
        with execution.claim(tmp_path, resourceId="cluster-one", operationId="opposite-operation"):
            pytest.fail("Unknown issuer was silently discarded")
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "unknown"


def test_timeout_stops_the_entire_descendant_group_before_releasing_host(tmp_path: Path) -> None:
    execution: type = loadExecution()
    marker: Path = tmp_path / "late-native-mutation"
    child: str = "import time; from pathlib import Path; time.sleep(0.5); Path(" + repr(str(marker)) + ").write_text('late')"
    parent: str = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(3)"
    with pytest.raises(TimeoutError):
        execution.run([sys.executable, "-c", parent], timeoutSeconds=0.2)
    time.sleep(0.6)
    assert not marker.exists(), "A child continued mutating after the wrapper timed out"


def test_process_group_is_stopped_when_recording_its_identity_fails(tmp_path: Path) -> None:
    execution: type = loadExecution()
    marker: Path = tmp_path / "unrecorded-late-mutation"
    child: str = "import time; from pathlib import Path; time.sleep(0.4); Path(" + repr(str(marker)) + ").write_text('late')"
    parent: str = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(2)"

    class BrokenJournal:
        def recordProcessGroup(self, _identity: int) -> None:
            raise OSError("Injected durable journal I/O failure")

    with pytest.raises(OSError):
        execution.run([sys.executable, "-c", parent], timeoutSeconds=1, issuer=BrokenJournal())
    time.sleep(0.6)
    assert not marker.exists(), "Unrecorded command survived journal failure"


def test_manifest_seed_is_an_explicit_fenced_operator_command(tmp_path: Path) -> None:
    directory: Path = Path(__file__).parents[1]
    manifest: Path = tmp_path / "installation.json"
    manifest.write_text(json.dumps(createManifest()))
    plan: Path = tmp_path / "seed.json"
    plan.write_text(json.dumps({"batch_limit": 10, "maximum_step_seconds": 60}))
    arguments: list[str] = ["seed-manifests", "--manifest", str(manifest), "--plan", str(plan),
        "--journal", str(tmp_path / "journal.json"), "--output", str(tmp_path / "receipt.json"),
        "--operation-id", "00000000-0000-4000-8000-000000000002", "--epoch", "7", "--source-hash", "a" * 64]
    launch: subprocess.CompletedProcess[str] = subprocess.run([sys.executable, str(directory / "launch-issuer.py"),
        "--kind", "operator", "--environment-file", str(tmp_path / "environment"), "--maximum-seconds", "120", "--", *arguments],
        capture_output=True, text=True, check=False)
    assert launch.returncode == 0, launch.stderr
    launchPlan = json.loads(launch.stdout)
    assert launchPlan["resource_mutations_issued"] is False
    assert launchPlan["command"][-len(arguments) - 4:] == [str(directory / "operator.py"), *arguments,
        "--issuer-token", launchPlan["invocation_token"], "--execute"]

    result: subprocess.CompletedProcess[str] = subprocess.run([sys.executable, str(directory / "operator.py"), *arguments],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operation"] == "seed-manifests"
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("receiptEpoch", [7, 8])
def test_manifest_seed_requires_receipt_from_the_admitted_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receiptEpoch: int,
) -> None:
    path: Path = Path(__file__).parents[1] / "operator.py"
    monkeypatch.syspath_prepend(str(path.parent))
    specification = importlib.util.spec_from_file_location("native_operator", path)
    assert specification is not None and specification.loader is not None
    operator: ModuleType = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(operator)
    sourceReceipt: Path = tmp_path / "source.json"
    token: str = "00000000-0000-4000-8000-000000000003"
    sourceHash: str = "a" * 64
    calls: list[str] = []
    command: list[str] = ["seed-manifests", "--epoch", "7", "--receipt", str(sourceReceipt), "--execute"]
    issuer: object = object()

    def run(arguments: list[str], *, timeoutSeconds: float, cwd: Path,
            issuer: object) -> subprocess.CompletedProcess[bytes]:
        assert calls == ["manifest-seed-intent", "before-mutation"]
        assert arguments == [*command, "--issuer-token", token]
        assert timeoutSeconds == 60 and cwd.is_dir() and issuer is not None
        sourceReceipt.write_text(json.dumps({"verb": "seed-manifests", "targetBackend": "opensearch",
            "execute": True, "errors": [], "sourceHash": sourceHash, "expectedSourceHash": sourceHash,
            "control": {"epoch": receiptEpoch, "cluster_uuid": "cluster-one", "backend": "opensearch", "mode": "serving"},
            "issuer": {"invocation_token": token, "status": "active"}, "seededNamespaces": 2}))
        return subprocess.CompletedProcess(arguments, 0)

    def persist(result: dict[str, object]) -> None:
        calls.append(str(result["status"]))

    authority: SimpleNamespace = SimpleNamespace(epoch=7, clusterUuid="cluster-one", sourceHash=sourceHash,
        token=token, beforeMutation=lambda: calls.append("before-mutation"),
        getCommandArguments=lambda: ["--issuer-token", token], assertCurrent=lambda: calls.append("assert-current"))
    monkeypatch.setattr(operator.OperationExecution, "run", staticmethod(run))
    if receiptEpoch != 7:
        with pytest.raises(ValueError, match="serving epoch"):
            operator._seedManifests(command, 60, sourceReceipt, authority, issuer, persist)
        assert calls == ["manifest-seed-intent", "before-mutation"]
        return
    result: dict[str, object] = operator._seedManifests(command, 60, sourceReceipt, authority, issuer, persist)
    assert result["seededNamespaces"] == 2
    assert calls == ["manifest-seed-intent", "before-mutation", "assert-current", "manifests-seeded"]
