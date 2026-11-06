"""A physical operator issuer is serialized and its descendants are bounded."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import cast

import pytest


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
