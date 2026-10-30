"""Apply or restore a bounded failure in one acceptance document's Fine input.

Run in a Knowhere API/Worker container with the evidence directory mounted.
Only the working Phase B manifest is changed. Published snapshots and source
pages stay intact. Real workers must perform the failure and recovery attempts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from sqlalchemy import select

from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import Document, DocumentScopeEnrichment
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.storage.result_storage import ResultStorage, get_result_storage

INPUT_RELATIVE_PATH: str = "phase_b/manifest.json"
FAULT_CONTENT: bytes = b'{"acceptanceFault":"owned-fine-input-unavailable","scopes":[]}\n'


def writePrivateJson(path: Path, value: dict[str, object]) -> None:
    descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def readManifest(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected an acceptance ownership manifest object.")
    manifest: dict[str, object] = value
    if isinstance(manifest.get("browser_manifest"), dict):
        manifest = cast(dict[str, object], manifest["browser_manifest"])
    if "isolated-account-and-project" not in manifest.get("stages", []):
        raise ValueError("The manifest does not record an isolated acceptance project.")
    if not str(manifest.get("runId", "")).startswith("progressive-e2e-"):
        raise ValueError("The manifest does not identify a progressive acceptance run.")
    return manifest


def readTarget(manifest: dict[str, object], *, canApply: bool) -> dict[str, object]:
    with get_sync_db_context() as database:
        document: Document | None = database.get(Document, str(manifest["documentId"]))
        if document is None or document.status != "active" or document.namespace != manifest["namespace"]:
            raise ValueError("The exact owned active document could not be resolved.")
        revision: JobResult | None = database.get(JobResult, document.current_job_result_id)
        job: Job | None = None if revision is None else database.get(Job, revision.job_id)
        if revision is None or job is None:
            raise ValueError("The owned document has no current Job revision.")
        metadata: dict[str, object] = dict(job.job_metadata or {})
        configuredRefs: object = metadata.get("page_memory_phase_b")
        mapping: object = metadata.get("page_memory_phase_b_artifacts")
        if not isinstance(configuredRefs, list) or INPUT_RELATIVE_PATH not in configuredRefs:
            raise ValueError("The working Fine input manifest is not configured.")
        if not isinstance(mapping, dict):
            raise ValueError("Expected the generation-frozen Fine input mapping.")
        relativePath: object = mapping.get(INPUT_RELATIVE_PATH)
        if not isinstance(relativePath, str) or not relativePath.startswith("phase_b/") or not relativePath.endswith("/manifest.json"):
            raise ValueError("Refusing to change an unversioned or published manifest.")
        rawPrefix: object = metadata.get("page_memory_working_raw_prefix") or metadata.get("result_raw_prefix")
        if not isinstance(rawPrefix, str) or not rawPrefix.startswith("results/"):
            raise ValueError("Expected a task-owned working result prefix.")
        scopes: list[DocumentScopeEnrichment] = list(database.scalars(
            select(DocumentScopeEnrichment).where(DocumentScopeEnrichment.job_result_id == revision.id)
        ))
        if not scopes:
            raise ValueError("Wait for the real coarse structure and Fine scopes first.")
        if canApply and any(scope.state != "coarse" or scope.attempts != 0 for scope in scopes):
            raise ValueError("Apply the fault before any Fine scope is queued or claimed.")
        return {
            "documentId": document.document_id, "namespace": document.namespace,
            "jobResultId": revision.id, "jobId": job.job_id,
            "relativePath": relativePath, "rawPrefix": rawPrefix,
            "scopes": [{"scopeId": scope.scope_id, "state": scope.state, "attempts": scope.attempts} for scope in scopes],
        }


def downloadInput(storage: ResultStorage, target: dict[str, object], directory: Path) -> Path:
    return Path(storage.download_raw_to_temp(
        job_id=str(target["jobId"]), relative_path=str(target["relativePath"]),
        raw_prefix=str(target["rawPrefix"]), suffix=".json", temp_dir=str(directory),
    ))


def uploadInput(storage: ResultStorage, target: dict[str, object], path: Path) -> None:
    storage.upload_raw_file(
        job_id=str(target["jobId"]), relative_path=str(target["relativePath"]),
        raw_prefix=str(target["rawPrefix"]), local_file_path=str(path),
    )


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backup-directory", required=True, type=Path)
    parser.add_argument("--action", required=True, choices=("apply", "restore", "status"))
    arguments: argparse.Namespace = parser.parse_args()
    directory: Path = arguments.backup_directory
    directory.mkdir(parents=True, exist_ok=True)
    receiptPath: Path = directory / "receipt.json"
    originalPath: Path = directory / "original-manifest.json"
    injectedPath: Path = directory / "injected-manifest.json"
    manifest: dict[str, object] = readManifest(arguments.manifest)
    target: dict[str, object] = readTarget(manifest, canApply=arguments.action == "apply")
    storage: ResultStorage = get_result_storage()
    if arguments.action == "status":
        print(json.dumps({key: target[key] for key in ("documentId", "jobResultId", "scopes")}))
        return
    if arguments.action == "apply":
        if receiptPath.exists() or originalPath.exists():
            raise ValueError("Choose a new empty backup directory; never overwrite a recovery backup.")
        downloaded: Path = downloadInput(storage, target, directory)
        downloaded.replace(originalPath)
        originalBytes: bytes = originalPath.read_bytes()
        originalJson: object = json.loads(originalBytes)
        if not isinstance(originalJson, dict) or not originalJson.get("scopes"):
            raise ValueError("The original Fine manifest must be valid and contain real scopes.")
        injectedPath.write_bytes(FAULT_CONTENT)
        receipt: dict[str, object] = {
            **target, "runId": manifest["runId"], "status": "backed-up",
            "originalSha256": hashlib.sha256(originalBytes).hexdigest(),
            "injectedSha256": hashlib.sha256(FAULT_CONTENT).hexdigest(),
            "backedUpAt": datetime.now(timezone.utc).isoformat(),
        }
        writePrivateJson(receiptPath, receipt)
        uploadInput(storage, target, injectedPath)
        receipt["status"] = "applied"
        receipt["appliedAt"] = datetime.now(timezone.utc).isoformat()
        writePrivateJson(receiptPath, receipt)
        print(json.dumps({"status": "applied", "documentId": target["documentId"], "jobResultId": target["jobResultId"], "changedArtifact": "working-phase-b-manifest-only"}))
        return
    receiptValue: object = json.loads(receiptPath.read_text(encoding="utf-8"))
    if not isinstance(receiptValue, dict):
        raise ValueError("The recovery receipt is invalid.")
    for key in ("documentId", "namespace", "jobResultId", "jobId", "relativePath", "rawPrefix"):
        if receiptValue.get(key) != target[key]:
            raise ValueError("The current document does not match this exact backup.")
    if hashlib.sha256(originalPath.read_bytes()).hexdigest() != receiptValue["originalSha256"]:
        raise ValueError("The original recovery backup checksum does not match.")
    downloaded = downloadInput(storage, target, directory)
    try:
        currentChecksum: str = hashlib.sha256(downloaded.read_bytes()).hexdigest()
        if currentChecksum not in (receiptValue["injectedSha256"], receiptValue["originalSha256"]):
            raise ValueError("The working input changed externally; refusing to overwrite it.")
    finally:
        downloaded.unlink(missing_ok=True)
    uploadInput(storage, target, originalPath)
    receiptValue["status"] = "restored"
    receiptValue["restoredAt"] = datetime.now(timezone.utc).isoformat()
    writePrivateJson(receiptPath, receiptValue)
    print(json.dumps({"status": "restored", "documentId": target["documentId"], "originalChecksumVerified": True}))


if __name__ == "__main__":
    main()
