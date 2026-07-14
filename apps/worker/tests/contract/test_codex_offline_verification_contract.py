from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.services.codex_export.offline_verifier import (
    OfflineVerificationError,
    OfflineVerificationRequest,
    WindowsFirewallController,
    verify_offline_validation,
)


class RecordingCommands:
    def __init__(self, report_path: Path, validator_result: int = 0) -> None:
        self.report_path = report_path
        self.validator_result = validator_result
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.raise_validator = False

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if argv[0] == "netsh" and "show" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Enabled: Yes\nDirection: Out\nAction: Block\n",
                stderr="",
            )
        if argv[0] != "netsh":
            if self.raise_validator:
                raise OSError("validator launch failed")
            if self.validator_result == 0:
                self.report_path.parent.mkdir(parents=True, exist_ok=True)
                self.report_path.write_text('{"summary":{"runs":1}}', encoding="utf-8")
            return subprocess.CompletedProcess(
                argv, self.validator_result, stdout="", stderr="validation failed"
            )
        return subprocess.CompletedProcess(argv, 0, stdout="Ok.", stderr="")


def _request(tmp_path: Path) -> OfflineVerificationRequest:
    uv = tmp_path / "uv.exe"
    python = tmp_path / "python.exe"
    uv.write_bytes(b"uv executable")
    python.write_bytes(b"python executable")
    report = tmp_path / "output" / "validation-report.json"
    return OfflineVerificationRequest(
        validator_argv=(str(uv), "run", "validator.py", "--offline"),
        uv_executable=uv,
        mineru_python=python,
        report_path=report,
        attestation_path=tmp_path / "offline-attestation.json",
        rule_id="contract-test",
    )


def test_offline_verifier_rejects_non_admin_before_creating_rules(tmp_path: Path) -> None:
    request = _request(tmp_path)
    commands = RecordingCommands(request.report_path)
    controller = WindowsFirewallController(run_command=commands)

    with pytest.raises(OfflineVerificationError, match="Administrator privileges"):
        verify_offline_validation(request, controller=controller, is_admin=lambda: False)

    assert commands.calls == []
    assert not request.attestation_path.exists()


def test_offline_verifier_enforces_both_rules_and_writes_verified_attestation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    commands = RecordingCommands(request.report_path)
    controller = WindowsFirewallController(run_command=commands)

    result = verify_offline_validation(
        request, controller=controller, is_admin=lambda: True
    )

    assert result.verified is True
    argv = [call[0] for call in commands.calls]
    uv_rule = "Knowhere-MinerU-Offline-contract-test-uv"
    python_rule = "Knowhere-MinerU-Offline-contract-test-python"
    assert argv[0] == [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={uv_rule}", "dir=out", "action=block",
        f"program={request.uv_executable.resolve()}", "enable=yes", "profile=any",
    ]
    assert argv[1] == [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={python_rule}", "dir=out", "action=block",
        f"program={request.mineru_python.resolve()}", "enable=yes", "profile=any",
    ]
    assert argv[2] == [
        "netsh", "advfirewall", "firewall", "show", "rule",
        f"name={uv_rule}", "verbose",
    ]
    assert argv[3] == [
        "netsh", "advfirewall", "firewall", "show", "rule",
        f"name={python_rule}", "verbose",
    ]
    assert argv[4] == list(request.validator_argv)
    assert argv[-2:] == [
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={python_rule}"],
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={uv_rule}"],
    ]
    assert all(call[1]["shell"] is False for call in commands.calls)
    validator_environment = commands.calls[4][1]["env"]
    assert validator_environment["HF_HUB_OFFLINE"] == "1"
    assert validator_environment["TRANSFORMERS_OFFLINE"] == "1"
    attestation = json.loads(request.attestation_path.read_text(encoding="utf-8"))
    assert attestation["schema_version"] == "codex-offline-attestation/1.0"
    assert attestation["verified"] is True
    assert len(attestation["executables"]) == 2
    assert len(attestation["report_sha256"]) == 64


@pytest.mark.parametrize("failure_mode", ["return-code", "exception"])
def test_offline_verifier_cleans_rules_and_records_unverified_failure(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    request = _request(tmp_path)
    commands = RecordingCommands(request.report_path, validator_result=7)
    commands.raise_validator = failure_mode == "exception"
    controller = WindowsFirewallController(run_command=commands)

    result = verify_offline_validation(
        request, controller=controller, is_admin=lambda: True
    )

    assert result.verified is False
    delete_calls = [argv for argv, _ in commands.calls if "delete" in argv]
    assert len(delete_calls) == 2
    attestation = json.loads(request.attestation_path.read_text(encoding="utf-8"))
    assert attestation["verified"] is False
    assert attestation["failure_type"] in {"ValidatorExitError", "OSError"}
