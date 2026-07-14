"""Externally enforced Windows Firewall verification for offline batch runs."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence


ATTESTATION_SCHEMA_VERSION = "codex-offline-attestation/1.0"
RULE_PREFIX = "Knowhere-MinerU-Offline-"


class OfflineVerificationError(RuntimeError):
    """Raised when offline enforcement cannot safely begin."""


class FirewallCommandError(OfflineVerificationError):
    """Raised when a firewall rule cannot be installed or verified."""


class ValidatorExitError(OfflineVerificationError):
    """Raised when the wrapped validation command returns nonzero."""


@dataclass(frozen=True)
class OfflineVerificationRequest:
    validator_argv: tuple[str, ...]
    uv_executable: Path
    mineru_python: Path
    report_path: Path
    attestation_path: Path
    rule_id: str


@dataclass(frozen=True)
class OfflineVerificationResult:
    verified: bool
    validator_return_code: int | None
    failure_type: str | None
    attestation_path: Path


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class WindowsFirewallController:
    """Small argv-only adapter around netsh advanced firewall commands."""

    def __init__(self, *, run_command: RunCommand = subprocess.run) -> None:
        self._run_command = run_command

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run_command(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )

    def add_outbound_block(self, name: str, program: Path) -> None:
        result = self._run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}",
                "dir=out",
                "action=block",
                f"program={program.resolve()}",
                "enable=yes",
                "profile=any",
            ]
        )
        if result.returncode != 0:
            raise FirewallCommandError(f"Could not create firewall rule {name}")

    def outbound_block_is_active(self, name: str) -> bool:
        result = self._run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "show",
                "rule",
                f"name={name}",
                "verbose",
            ]
        )
        if result.returncode != 0:
            return False
        normalized = " ".join(result.stdout.lower().split())
        return all(
            token in normalized
            for token in ("enabled: yes", "direction: out", "action: block")
        )

    def delete(self, name: str) -> None:
        self._run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "delete",
                "rule",
                f"name={name}",
            ]
        )

    def run_validator(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return self._run_command(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env=dict(env),
        )


def is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_request(request: OfflineVerificationRequest) -> tuple[Path, Path]:
    if not request.validator_argv:
        raise OfflineVerificationError("Validator argv must not be empty")
    if not request.rule_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
        for character in request.rule_id
    ):
        raise OfflineVerificationError("Firewall rule id is invalid")
    uv = request.uv_executable.resolve(strict=False)
    python = request.mineru_python.resolve(strict=False)
    if not uv.is_file() or not python.is_file():
        raise OfflineVerificationError("Both offline executables must exist")
    return uv, python


def verify_offline_validation(
    request: OfflineVerificationRequest,
    *,
    controller: WindowsFirewallController | None = None,
    is_admin: Callable[[], bool] = is_windows_admin,
) -> OfflineVerificationResult:
    """Block both launch executables, run validation, clean up, and attest."""

    if not is_admin():
        raise OfflineVerificationError(
            "Administrator privileges are required for offline firewall verification"
        )
    uv, python = _validate_request(request)
    firewall = controller or WindowsFirewallController()
    rules = (
        (f"{RULE_PREFIX}{request.rule_id}-uv", uv),
        (f"{RULE_PREFIX}{request.rule_id}-python", python),
    )
    verified = False
    validator_return_code: int | None = None
    failure: Exception | None = None
    cleanup_failed = False
    try:
        for name, program in rules:
            firewall.add_outbound_block(name, program)
        for name, _ in rules:
            if not firewall.outbound_block_is_active(name):
                raise FirewallCommandError(f"Firewall rule is not active: {name}")
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "MODELSCOPE_OFFLINE": "1",
            }
        )
        validator = firewall.run_validator(request.validator_argv, env=environment)
        validator_return_code = validator.returncode
        if validator.returncode != 0:
            raise ValidatorExitError(
                f"Validator exited with return code {validator.returncode}"
            )
        if not request.report_path.is_file():
            raise ValidatorExitError("Validator did not produce its JSON report")
        verified = True
    except Exception as error:
        failure = error
    finally:
        for name, _ in reversed(rules):
            try:
                firewall.delete(name)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            verified = False
            if failure is None:
                failure = FirewallCommandError("Firewall rule cleanup failed")

    report_sha256 = _sha256(request.report_path) if verified else None
    payload: dict[str, object] = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "verified": verified,
        "enforcement": "windows-firewall-program-outbound-block",
        "rules": [name for name, _ in rules],
        "executables": [
            {"filename": uv.name, "sha256": _sha256(uv)},
            {"filename": python.name, "sha256": _sha256(python)},
        ],
        "validator_return_code": validator_return_code,
        "report_sha256": report_sha256,
        "cleanup_completed": not cleanup_failed,
        "failure_type": type(failure).__name__ if failure else None,
    }
    _atomic_json(request.attestation_path, payload)
    return OfflineVerificationResult(
        verified=verified,
        validator_return_code=validator_return_code,
        failure_type=type(failure).__name__ if failure else None,
        attestation_path=request.attestation_path,
    )
