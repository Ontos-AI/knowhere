"""Run the authoritative backup workflow within a staging workload's existing issuer."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from typing import cast

from authority import OperatorAuthority
from manage import _readJson, _requireObject, _validateApproval, _writeJson
from operations import OpenSearchOperations


def _run(configuration: dict[str, object], request: dict[str, object], *, shouldExecute: bool) -> dict[str, object]:
    identity: dict[str, object] = {key: configuration[key] for key in ("campaign_id", "source_hash")}
    if request.get("event") != "snapshot" or any(request.get(key) != value for key, value in identity.items()):
        raise ValueError("Snapshot overlap request does not match the frozen staging campaign")
    result: dict[str, object] = {**identity, "mode": "actual" if shouldExecute else "dry-run",
        "status": "planned", "completed": False, "coverage_verified": False, "errors": []}
    if not shouldExecute:
        return result
    installation: dict[str, object] = _requireObject(_readJson(Path(str(configuration["operator_manifest_path"]))))
    manifestReceipt: dict[str, object] = _requireObject(OpenSearchOperations.renderBundle(installation)["installation-receipt.json"])
    planPath: Path = Path(str(configuration["backup_plan_path"]))
    approvalPath: Path = Path(str(configuration["backup_approval_path"]))
    plan: dict[str, object] = _requireObject(_readJson(planPath))
    planDigest: str = sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _validateApproval(approvalPath, manifestSha256=str(manifestReceipt["manifest_sha256"]), purpose="backup")
    approval: dict[str, object] = _requireObject(_readJson(approvalPath))
    if approval.get("operation_plan_sha256") != planDigest or approval.get("source_hash") != configuration["source_hash"]:
        raise ValueError("Backup approval does not bind the actual overlap operation")
    authority: OperatorAuthority = OperatorAuthority.fromExisting(_requireObject(_readJson(Path(str(configuration["issuer_receipt_path"])))))
    if authority.sourceHash != configuration["source_hash"] or authority.receipt["logical_cluster_id"] != str(installation["region"]) + ":" + str(installation["cluster_name"]):
        raise ValueError("Backup overlap must remain inside the exact reviewed active issuer")

    journal: Path = Path(str(configuration["backup_journal_path"]))
    binding: dict[str, object] = {"operation_id": authority.operationId, "source_hash": authority.sourceHash,
        "plan_sha256": planDigest, "manifest_sha256": manifestReceipt["manifest_sha256"]}
    progress: dict[str, object] = _requireObject(_readJson(journal)) if journal.exists() else {}
    if progress.get("binding", binding) != binding:
        raise ValueError("Snapshot overlap journal belongs to another admitted operation")

    def persist(value: dict[str, object]) -> None:
        _writeJson(journal, {**value, "binding": binding}, replace=journal.exists())

    specification = importlib.util.spec_from_file_location("knowhere_native_operator", Path(__file__).with_name("operator.py"))
    if specification is None or specification.loader is None:
        raise ValueError("Authoritative backup implementation is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    runBackup = cast(Callable[..., dict[str, object]], module._backup)
    backup: dict[str, object] = runBackup(argparse.Namespace(operation_id=authority.operationId, plan=planPath),
        installation, plan, authority, progress, persist)
    result["backup_status"] = backup["status"]
    if backup.get("status") != "verified" or backup.get("data_verified") is not True or backup.get("coverage_verified") is not True:
        result.update({"status": "incomplete", "errors": ["full-independent-backup-not-verified"]})
        return result
    point: dict[str, object] = _requireObject(backup["point"])
    result.update({"status": "passed", "completed": True, "coverage_verified": True,
        "snapshot_uuid": point["snapshot_uuid"], "independent_verification_sha256": point["independent_verification_sha256"],
        "snapshot_interval_utc": {key: datetime.fromtimestamp(int(str(point[field])) / 1000, tz=timezone.utc).isoformat()
            for key, field in (("started_at_utc", "started_at_millis"), ("finished_at_utc", "completed_at_millis"))}})
    return result


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", required=True, type=Path)
    parser.add_argument("--expected-configuration-sha256", required=True)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args: argparse.Namespace = parser.parse_args()
    if args.receipt.exists():
        raise ValueError("Use a new private receipt")
    raw: bytes = args.configuration.read_bytes()
    if sha256(raw).hexdigest() != args.expected_configuration_sha256:
        raise ValueError("Backup overlap configuration differs from the reviewed checksum")
    result: dict[str, object] = _run(_requireObject(json.loads(raw)), _requireObject(_readJson(args.request)),
        shouldExecute=args.execute)
    _writeJson(args.receipt, result)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError) as error:
        print("Workload backup was refused or incomplete: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2) from None
