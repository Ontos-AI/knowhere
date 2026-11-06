"""Guard individual two-release steps with actual receipts and the abort budget.

The source-owned transition CLI controls SQL publication. An independently
reviewed existing customer-traffic gate must remain closed through deployment,
SQL resume and live API verification; this tool never invents an edge gate or
reopens customer traffic merely because a backend flag changed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from operations import OpenSearchOperations
from execution import OperationExecution
from authority import OperatorAuthority

_REQUIRED: tuple[str, ...] = (
    "source-and-config", "compatible-API-worker-SDKs", "native-current-published-coverage",
    "PG-rollback-coverage", "quality", "mixed-load", "native-recovery", "full-snapshot-coverage",
    "alert-delivery", "manual-response-coverage", "restoration-rehearsal", "production-and-cost-approval",
    "customer-traffic-gate-closed", "matching-target-binaries-and-configuration",
)


class ReleaseOperations:
    @staticmethod
    def assertPhaseTarget(*, phase: str, targetBackend: str) -> None:
        if phase not in ("forward", "rollback") or targetBackend not in ("postgresql", "opensearch"):
            raise ValueError("Declare the release phase and exact target backend")
        if phase == "rollback" and targetBackend != "postgresql":
            raise ValueError("The rollback policy requires the compatible PostgreSQL backend")

    @staticmethod
    def qualify(receipts: dict[str, object], *, sourceHash: str, manifestSha256: str,
            phase: str = "forward") -> dict[str, object]:
        required: tuple[str, ...] = _REQUIRED if phase == "forward" else tuple(
            name for name in _REQUIRED if name not in ("native-current-published-coverage", "quality", "mixed-load", "native-recovery", "full-snapshot-coverage")
        )
        if phase == "prepare":
            required = ("source-and-config", "compatible-API-worker-SDKs", "PG-rollback-coverage", "production-and-cost-approval")
        missing: list[str] = []
        for name in required:
            receipt: object = receipts.get(name)
            if (not isinstance(receipt, dict) or receipt.get("mode") != "actual"
                    or receipt.get("status") != "passed" or receipt.get("sourceHash") != sourceHash
                    or receipt.get("manifest_sha256") != manifestSha256
                    or not receipt.get("artifact_sha256")):
                missing.append(name)
        return {"ready": not missing, "required": list(required), "missing": missing,
            "executed": False, "independent_customer_gate_required": True}

    @staticmethod
    def plan(firstImpact: datetime, restorationReceipt: dict[str, object], *, marginSeconds: int) -> dict[str, object]:
        if (restorationReceipt.get("mode") != "actual" or restorationReceipt.get("status") != "passed"
                or restorationReceipt.get("current_published_coverage_complete") is not True
                or restorationReceipt.get("canonical_api_smoke_passed") is not True
                or restorationReceipt.get("includes_detection_human_response_and_failed_attempts") is not True):
            raise ValueError("Use the actual full disruption-to-serving restoration receipt")
        started: datetime = datetime.fromisoformat(str(restorationReceipt.get("first_customer_impact_at", "")))
        restored: datetime = datetime.fromisoformat(str(restorationReceipt.get("service_restored_at", "")))
        if started.tzinfo is None or restored.tzinfo is None:
            raise ValueError("Restoration measurements require explicit timezones")
        measured: int = int((restored - started).total_seconds())
        return OpenSearchOperations.planRelease(firstImpact, restorationSeconds=measured, marginSeconds=marginSeconds)

    @staticmethod
    def assertWithinAbortBudget(plan: dict[str, object], *, phase: str, now: datetime) -> None:
        latest: datetime = datetime.fromisoformat(str(plan["latest_abort_at"]))
        if now.tzinfo is None or phase not in ("forward", "rollback"):
            raise ValueError("Declare the release phase and current timezone")
        if phase == "forward" and now >= latest:
            raise ValueError("The latest abort time has arrived; stop forward work and restore service")


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=("inspect", "gate", "settle", "backfill", "verify", "resume", "abort"))
    parser.add_argument("--phase", required=True, choices=("forward", "rollback"))
    parser.add_argument("--target-backend", required=True, choices=("postgresql", "opensearch"))
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--qualification-receipts", required=True, type=Path)
    parser.add_argument("--restoration-receipt", required=True, type=Path)
    parser.add_argument("--first-impact", required=True)
    parser.add_argument("--margin-seconds", required=True, type=int)
    parser.add_argument("--step-timeout-seconds", required=True, type=int)
    parser.add_argument("--transition-source-hash", required=True)
    parser.add_argument("--transition-script-sha256", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--operation-state-directory", type=Path, default=Path("/var/lib/knowhere-native-ops"))
    OperatorAuthority.addArguments(parser)
    parser.add_argument("--execute", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    ReleaseOperations.assertPhaseTarget(phase=arguments.phase, targetBackend=arguments.target_backend)
    from manage import _readJson, _requireObject, _validateApproval, _writeJson

    manifest: dict[str, object] = _requireObject(_readJson(arguments.manifest))
    bundle: dict[str, object] = OpenSearchOperations.renderBundle(manifest)
    installation: dict[str, object] = _requireObject(bundle["installation-receipt.json"])
    qualification: dict[str, object] = ReleaseOperations.qualify(_requireObject(_readJson(arguments.qualification_receipts)),
        sourceHash=arguments.transition_source_hash, manifestSha256=str(installation["manifest_sha256"]), phase=arguments.phase)
    plan: dict[str, object] = ReleaseOperations.plan(datetime.fromisoformat(arguments.first_impact),
        _requireObject(_readJson(arguments.restoration_receipt)), marginSeconds=arguments.margin_seconds)
    repository: Path = Path(__file__).resolve().parents[2]
    script: Path = repository / "scripts/probes/manage-opensearch-transition.py"
    command: list[str] = [sys.executable, str(script), arguments.verb,
        "--target-backend", arguments.target_backend, "--epoch", str(arguments.epoch),
        "--expected-source-hash", arguments.transition_source_hash, "--receipt", str(arguments.receipt.resolve())]
    result: dict[str, object] = {"mode": "execute" if arguments.execute else "dry-run", "phase": arguments.phase,
        "qualification": qualification, "abort_plan": plan, "argv": command,
        "operation_state_directory": str(arguments.operation_state_directory.resolve()),
        "schema_downgrade": False, "reopen_customer_traffic": False,
        "interrupted_indexing_automatically_resumed": False, "service_restoration_verified": False}
    if not arguments.execute:
        print(json.dumps(result, indent=2))
        return 0
    _validateApproval(arguments.approval, manifestSha256=str(installation["manifest_sha256"]), purpose="release")
    assert arguments.approval is not None
    OperationExecution.validateOperatorApproval(_requireObject(_readJson(arguments.approval)), arguments.operation_state_directory)
    if qualification["ready"] is not True:
        raise ValueError("Actual release qualification receipts are incomplete")
    if not script.is_file() or sha256(script.read_bytes()).hexdigest() != arguments.transition_script_sha256:
        raise ValueError("Transition command differs from its reviewed source binding")
    if arguments.receipt.exists() or arguments.step_timeout_seconds < 1:
        raise ValueError("Use a new immutable step receipt and a bounded step timeout")
    with OperatorAuthority.fromArguments(arguments, manifest, operationId=str(arguments.issuer_token),
        sourceHash=arguments.transition_source_hash, epoch=arguments.epoch) as authority:
        with OperationExecution.claim(arguments.operation_state_directory,
            resourceId=str(manifest["region"]) + ":" + str(manifest["cluster_name"]), operationId=str(arguments.receipt.resolve()),
            authorityReceipt=authority.receipt) as issuer:
            now: datetime = datetime.now(timezone.utc)
            ReleaseOperations.assertWithinAbortBudget(plan, phase=arguments.phase, now=now)
            timeout: float = float(arguments.step_timeout_seconds)
            if arguments.phase == "forward":
                remaining: float = (datetime.fromisoformat(str(plan["latest_abort_at"])) - now).total_seconds()
                timeout = min(timeout, remaining)
            _writeJson(arguments.receipt.with_suffix(arguments.receipt.suffix + ".intent"), result)
            completed: subprocess.CompletedProcess[bytes] = OperationExecution.run([*command, "--execute", *authority.getCommandArguments()], cwd=repository,
                timeoutSeconds=timeout, issuer=issuer)
            if not arguments.receipt.is_file():
                raise ValueError("Transition outcome is uncertain; retain the customer gate and reconcile live control")
            arguments.receipt.chmod(0o600)
            receipt: dict[str, object] = _requireObject(_readJson(arguments.receipt))
            if completed.returncode != 0 or receipt.get("sourceHash") != arguments.transition_source_hash or receipt.get("expectedSourceHash") != arguments.transition_source_hash or receipt.get("execute") is not True or receipt.get("errors") != []:
                raise ValueError("Transition step failed or source identity changed; preserve the immutable receipt")
            control: dict[str, object] = _requireObject(receipt["control"])
            authority.refresh(receipt)
            print(json.dumps({"verb": arguments.verb, "control": control, "sourceHash": receipt["sourceHash"],
                "customer_traffic_gate_must_remain_closed": True, "service_restoration_verified": False}, indent=2))
            issuer.complete()
            authority.complete()
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError, subprocess.SubprocessError):
        print("Release step refused or incomplete; keep the customer gate closed and follow the recorded abort/rollback procedure.", file=sys.stderr)
        raise SystemExit(2) from None
