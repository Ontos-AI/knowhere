"""Bounded retained-token qualification; generated rows test the tool only."""

import gzip
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


def loadProbe() -> ModuleType:
    path: Path = Path(__file__).parents[1] / "qualify-retained-lexical-input.py"
    assert path.is_file(), "Retained-token memory qualification must be an explicit offline probe"
    specification = importlib.util.spec_from_file_location("retained_input_qualification", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_probe_measures_existing_tokens_without_claiming_raw_compilation(tmp_path: Path) -> None:
    path: Path = tmp_path / "automated-token-fixture.gz"
    rows = [{"buildId": "old", "documentId": "document", "revisionId": "revision", "unitId": f"unit-{number}",
        "sectionId": f"section-{number}", "pathTokens": "path", "contentTokens": "content"} for number in range(2)]
    path.write_bytes(gzip.compress(("\n".join(json.dumps(row) for row in rows) + "\n").encode()))
    # RSS is a process high-water mark. Measure in a fresh process just as the
    # qualification launcher does, independent of earlier tests in this run.
    program: str = """
import json
from pathlib import Path
import sys
from test_retained_input_qualification import loadProbe
probe = loadProbe()
result = probe.RetainedInputQualification.run(Path(sys.argv[1]), expectedSha256=sys.argv[2],
    expectedNamespaceUnits=2, expectedLargestUnits=2, expectedTokenOccurrences=4)
print(json.dumps(result))
"""
    environment: dict[str, str] = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).parent), environment.get("PYTHONPATH", ""))))
    run: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-c", program, str(path), sha256(path.read_bytes()).hexdigest()],
        env=environment, capture_output=True, text=True, check=True, timeout=15)
    result: dict[str, object] = json.loads(run.stdout)
    assert result["passed"] is True and result["largest_unit_count"] == 2
    assert result["bulk_request_count"] == 1 and result["token_occurrences"] == 4
    assert result["bulk_build_id_length"] == 36
    assert result["raw_compilation_qualified"] is False
    assert result["raw_sql_hydration_budget_qualified"] is False
    assert result["traced_peak_bytes"] > 0


def test_probe_rejects_changed_fixture_before_deserialization(tmp_path: Path) -> None:
    probe = loadProbe()
    path: Path = tmp_path / "changed.gz"
    path.write_bytes(b"not a gzip fixture")
    with pytest.raises(ValueError, match="checksum"):
        probe.RetainedInputQualification.run(path, expectedSha256="0" * 64)


def test_probe_receipt_cannot_modify_input_fixture_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = loadProbe()
    path: Path = tmp_path / "retained.gz"
    path.write_bytes(b"guard must run before decoding")
    monkeypatch.setattr(sys, "argv", [str(probe.__file__), "--input", str(path), "--expected-sha256",
        sha256(path.read_bytes()).hexdigest(), "--receipt", str(tmp_path / "result.json")])
    with pytest.raises(ValueError, match="fixture"):
        probe._main()
    assert not (tmp_path / "result.json").exists()
