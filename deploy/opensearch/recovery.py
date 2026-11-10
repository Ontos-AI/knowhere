"""Guarded native restore calls around source-owned application admission.

Snapshot capture UUIDs and restore UUIDs have different contracts. A fresh
OpenSearch restore creates new index UUIDs; only an explicitly attested mapping,
approved snapshot and actual per-build checks permit adoption. An ambiguous
native reply retains ownership and business-slot charges for reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal, Self, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from execution import OperationExecution
from authority import OperatorAuthority


def _assertTarget(client: httpx.Client, plan: _Plan) -> None:
    response: httpx.Response = client.get("/_cluster/state/nodes?filter_path=cluster_uuid,nodes.*.ephemeral_id")
    response.raise_for_status()
    state: object = response.json()
    if not isinstance(state, dict) or state.get("cluster_uuid") != plan.target_cluster_uuid:
        raise ValueError("Native adapter target cluster differs from the admitted recovery target")
    nodes: object = state.get("nodes")
    if not isinstance(nodes, dict) or len(nodes) != 1:
        raise ValueError("Native adapter target must identify the admitted single node")
    node: object = next(iter(nodes.values()))
    if not isinstance(node, dict) or node.get("ephemeral_id") != plan.target_incarnation:
        raise ValueError("Native adapter node incarnation differs from the admitted recovery target")


class _Build(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    unit_count: int = Field(ge=0)


class _Index(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    corpus_kind: Literal["private", "demo"]
    index_name: str = Field(pattern="^[a-z0-9][a-z0-9_.-]+$")
    old_index_uuid: str = Field(min_length=1)
    expected_state: Literal["open", "closed", "opening", "closing", "uncertain"]
    expected_operation_id: str | None
    builds: list[_Build]


class _Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern="^[a-z0-9][a-z0-9_.-]+$")
    name: str = Field(pattern="^[a-z0-9][a-z0-9_.-]+$")
    uuid: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class _RemoteIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_name: str
    index_uuid: str = Field(min_length=1)


class _RemoteState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    indices: list[_RemoteIndex] = Field(min_length=1)


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_backend: Literal["opensearch", "postgresql"]
    expected_old_cluster_uuid: str = Field(min_length=1)
    expected_old_incarnation: str = Field(min_length=1)
    target_cluster_uuid: str = Field(min_length=1)
    target_incarnation: str = Field(min_length=1)
    writers_fenced: Literal[True]
    recovery_source: Literal["snapshot", "live_remote"] = "snapshot"
    snapshot: _Snapshot | None = None
    remote_state: _RemoteState | None = None
    indices: list[_Index] = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def validateSource(self) -> Self:
        if self.recovery_source == "snapshot":
            if self.snapshot is None or self.remote_state is not None:
                raise ValueError("Snapshot recovery requires its exact approved snapshot")
        elif self.remote_state is None or self.snapshot is not None:
            raise ValueError("Live remote recovery requires independent existing-metadata evidence")
        if self.remote_state is not None:
            if {index.index_name for index in self.remote_state.indices} != {index.index_name for index in self.indices}:
                raise ValueError("Remote state must identify the exact selected namespace catalog")
        return self


class _Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_name: str
    old_index_uuid: str
    restored_index_uuid: str = Field(min_length=1)
    close_acknowledged: bool = False
    close_operation_id: str | None = None
    remote_restore_acknowledged: bool = False
    remote_restore_operation_id: str | None = None


class _Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_uuid: str | None = None
    manifest_sha256: str | None = None
    remote_evidence_sha256: str | None = None
    identity_mapping: list[_Mapping] = Field(min_length=1)


class _Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    plan: _Plan
    attestation: _Attestation | None


class RecoveryOperations:
    @staticmethod
    def assertFinishableIntent(intent: dict[str, object]) -> None:
        if intent.get("native_outcome") != "submitted" or intent.get("reservation_held") is not True:
            raise ValueError("Unknown native issuer outcome requires explicit issuer/lifecycle reconciliation before verification or opposite close")

    @staticmethod
    def validateReceipt(receipt: dict[str, object], inventory: object, *, verb: str, status: str,
            epoch: int, operationId: str) -> None:
        parsed: _Inventory = _Inventory.model_validate(inventory)
        if (receipt.get("mode") != verb or receipt.get("status") != status
                or receipt.get("epoch") != epoch or receipt.get("operation_id") != operationId
                or receipt.get("inventory_sha256") != RecoveryOperations.inventoryDigest(inventory)
                or receipt.get("execute") is not True or receipt.get("errors") != []
                or receipt.get("cap") != 400 or receipt.get("cluster_uuid") != parsed.plan.target_cluster_uuid):
            raise ValueError("Restore callback receipt is not an actual exact-epoch bound success")
        if verb == "reserve-restore":
            charged: object = receipt.get("charged_after")
            if not isinstance(charged, int) or isinstance(charged, bool) or not 0 <= charged <= 400:
                raise ValueError("Native restore requires the authoritative charged union to fit capacity")

    @staticmethod
    def inventoryDigest(inventory: object) -> str:
        _Inventory.model_validate(inventory)
        if not isinstance(inventory, dict):
            raise ValueError("Restore inventory must retain its exact reviewed JSON plan")
        return sha256(json.dumps(inventory["plan"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def validateAttestation(inventory: object) -> dict[str, str]:
        parsed: _Inventory = _Inventory.model_validate(inventory)
        attestation: _Attestation | None = parsed.attestation
        if attestation is None:
            raise ValueError("Restore requires an explicit identity attestation")
        if parsed.plan.recovery_source == "snapshot":
            assert parsed.plan.snapshot is not None
            if attestation.snapshot_uuid != parsed.plan.snapshot.uuid or attestation.manifest_sha256 != parsed.plan.snapshot.manifest_sha256:
                raise ValueError("Restore attestation must bind the approved snapshot and manifest")
        else:
            assert parsed.plan.remote_state is not None
            if attestation.remote_evidence_sha256 != parsed.plan.remote_state.evidence_sha256:
                raise ValueError("Remote restore attestation must bind the reviewed metadata evidence")
        expected: dict[str, str] = {index.index_name: index.old_index_uuid for index in parsed.plan.indices}
        mappings: dict[str, _Mapping] = {mapping.index_name: mapping for mapping in attestation.identity_mapping}
        if len(mappings) != len(attestation.identity_mapping) or set(mappings) != set(expected) or any(mapping.old_index_uuid != expected[name] for name, mapping in mappings.items()):
            raise ValueError("Restore attestation must map every exact old index identity")
        return {name: mapping.restored_index_uuid for name, mapping in mappings.items()}

    @staticmethod
    def submit(client: httpx.Client, inventory: object, *, epoch: int, operationId: str,
            reserve: Callable[[], dict[str, object]], persist: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
        parsed: _Inventory = _Inventory.model_validate(inventory)
        if epoch < 1:
            raise ValueError("Restore requires a gated exact epoch")
        reservation: dict[str, object] = reserve()
        RecoveryOperations.validateReceipt(reservation, inventory, verb="reserve-restore", status="reserved", epoch=epoch, operationId=operationId)
        result: dict[str, object] = {"operation_id": operationId, "epoch": epoch,
            "inventory_sha256": RecoveryOperations.inventoryDigest(inventory), "reservation": reservation,
            "reservation_held": True, "native_outcome": "intent", "reopen_traffic": False}
        if persist is not None:
            persist(result)
        if parsed.plan.recovery_source == "live_remote":
            return RecoveryOperations._submitRemote(client, inventory, parsed, reservation, result, persist)
        assert parsed.plan.snapshot is not None
        path: str = f"/_snapshot/{parsed.plan.snapshot.repository}/{parsed.plan.snapshot.name}/_restore?wait_for_completion=false"
        try:
            _assertTarget(client, parsed.plan)
            response: httpx.Response = client.post(path, headers={"X-Opaque-Id": operationId}, json={
                "indices": [index.index_name for index in parsed.plan.indices],
                "ignore_unavailable": False, "include_global_state": False,
                "include_aliases": False, "partial": False, "storage_type": "local",
            })
            response.raise_for_status()
            body: object = response.json()
            result["native_outcome"] = "submitted" if isinstance(body, dict) and body.get("accepted") is True else "unknown"
        except (httpx.HTTPError, ValueError):
            result["native_outcome"] = "unknown"
        if persist is not None:
            persist(result)
        return result

    @staticmethod
    def _submitRemote(client: httpx.Client, inventory: object, parsed: _Inventory,
            reservation: dict[str, object], result: dict[str, object],
            persist: Callable[[dict[str, object]], None] | None) -> dict[str, object]:
        assert parsed.plan.remote_state is not None
        expected: dict[str, str] = {index.index_name: index.index_uuid for index in parsed.plan.remote_state.indices}
        owners: dict[str, str] = {str(index["index_name"]): str(index["reservation_id"])
            for index in cast(list[dict[str, object]], reservation.get("indices", []))}
        if set(owners) != set(expected):
            raise ValueError("Remote recovery lacks exact per-index reserved operation identities")
        mappings: list[dict[str, object]] = []
        assert isinstance(inventory, dict)
        attested: dict[str, object] = {**inventory, "attestation": {
            "remote_evidence_sha256": parsed.plan.remote_state.evidence_sha256, "identity_mapping": mappings,
        }}
        result["attested_inventory"] = attested
        for index in parsed.plan.indices:
            mapping: dict[str, object] = {"index_name": index.index_name, "old_index_uuid": index.old_index_uuid,
                "restored_index_uuid": expected[index.index_name], "remote_restore_acknowledged": False,
                "remote_restore_operation_id": owners[index.index_name]}
            mappings.append(mapping)
            try:
                observed: httpx.Response = client.get(f"/_cat/indices/{index.index_name}?format=json&h=index,uuid,status,health&expand_wildcards=all")
                observed.raise_for_status()
                rows: object = observed.json()
                if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("index") != index.index_name or rows[0].get("uuid") != expected[index.index_name] or rows[0].get("status") not in ("open", "close"):
                    raise ValueError("Remote recovery metadata differs from the reviewed identity")
                restoreAll: str = "true" if rows[0]["status"] == "close" else "false"
                _assertTarget(client, parsed.plan)
                response: httpx.Response = client.post(
                    f"/_remotestore/_restore?restore_all_shards={restoreAll}&wait_for_completion=false&cluster_manager_timeout=10000ms",
                    headers={"X-Opaque-Id": owners[index.index_name]}, json={"indices": [index.index_name]},
                )
                response.raise_for_status()
                body: object = response.json()
                if not isinstance(body, dict) or (body.get("accepted") is not True and not isinstance(body.get("remote_store"), dict)):
                    raise ValueError("Remote restore was not acknowledged")
                mapping["remote_restore_acknowledged"] = True
                if persist is not None:
                    persist(result)
            except (httpx.HTTPError, ValueError):
                result["native_outcome"] = "unknown"
                if persist is not None:
                    persist(result)
                return result
        result["native_outcome"] = "submitted"
        if persist is not None:
            persist(result)
        return result


    @staticmethod
    def finish(client: httpx.Client, inventory: object, *, epoch: int, operationId: str,
            verify: Callable[[], dict[str, object]],
            release: Callable[[dict[str, object], dict[str, object]], dict[str, object]],
            persist: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
        adopted: dict[str, str] = RecoveryOperations.validateAttestation(inventory)
        parsed: _Inventory = _Inventory.model_validate(inventory)
        verified: dict[str, object] = verify()
        RecoveryOperations.validateReceipt(verified, inventory, verb="verify-restore", status="verified", epoch=epoch, operationId=operationId)
        owners: dict[str, dict[str, object]] = {str(index["index_name"]): index for index in cast(list[dict[str, object]], verified.get("indices", []))}
        if set(owners) != set(adopted) or any(owners[name].get("restored_index_uuid") != identity or not owners[name].get("reservation_id") for name, identity in adopted.items()):
            raise ValueError("Verified close reservations do not cover the adopted index identities")
        result: dict[str, object] = {"operation_id": operationId, "epoch": epoch,
            "inventory_sha256": RecoveryOperations.inventoryDigest(inventory), "verified_reservation": verified,
            "reservation_held": True, "native_outcome": "verified-closing", "reopen_traffic": False,
            "close_acknowledgments": []}
        if persist is not None:
            persist(result)
        acknowledgments: list[dict[str, str]] = []
        assert parsed.attestation is not None
        for mapping in parsed.attestation.identity_mapping:
            closeId: str = str(owners[mapping.index_name]["reservation_id"])
            try:
                _assertTarget(client, parsed.plan)
                response: httpx.Response = client.post(
                    f"/{mapping.index_name}/_close?timeout=10000ms&cluster_manager_timeout=10000ms&wait_for_active_shards=1",
                    headers={"X-Opaque-Id": closeId},
                )
                response.raise_for_status()
                body: object = response.json()
                if not isinstance(body, dict) or body.get("acknowledged") is not True or body.get("shards_acknowledged") is not True or body.get("indices") != {mapping.index_name: {"closed": True}}:
                    raise ValueError("Native close did not confirm the exact index")
            except (httpx.HTTPError, ValueError):
                result["native_outcome"] = "unknown-close"
                if persist is not None:
                    persist(result)
                return result
            mapping.close_acknowledged = True
            mapping.close_operation_id = closeId
            acknowledgments.append({"index_name": mapping.index_name, "close_operation_id": closeId})
            result["close_acknowledgments"] = list(acknowledgments)
            if persist is not None:
                persist(result)
        assert isinstance(inventory, dict)
        updated: dict[str, object] = {**inventory, "attestation": {
            **cast(dict[str, object], inventory["attestation"]),
            "identity_mapping": [mapping.model_dump(exclude_none=True) for mapping in parsed.attestation.identity_mapping],
        }}
        released: dict[str, object] = release(updated, verified)
        RecoveryOperations.validateReceipt(released, updated, verb="release-restore", status="released", epoch=epoch, operationId=operationId)
        result.update({"reservation_held": False, "native_outcome": "closed-and-reconciled", "release_receipt": released})
        if persist is not None:
            persist(result)
        return result



def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("submit", "finish"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--receipt-directory", required=True, type=Path)
    parser.add_argument("--transition-source-hash", required=True)
    parser.add_argument("--transition-script-sha256", required=True)
    parser.add_argument("--reservation", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--operation-state-directory", type=Path, default=Path("/var/lib/knowhere-native-ops"))
    OperatorAuthority.addArguments(parser)
    parser.add_argument("--execute", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    repository: Path = Path(__file__).resolve().parents[2]
    transition: Path = repository / "scripts/probes/manage-opensearch-transition.py"
    from manage import _nativeClient, _readJson, _requireObject, _validateApproval, _writeJson
    from operations import OpenSearchOperations

    inventory: dict[str, object] = _requireObject(_readJson(arguments.inventory))
    digest: str = RecoveryOperations.inventoryDigest(inventory)
    manifest: dict[str, object] = _requireObject(_readJson(arguments.manifest))
    bundle: dict[str, object] = OpenSearchOperations.renderBundle(manifest)
    installation: dict[str, object] = _requireObject(bundle["installation-receipt.json"])
    mode: str = "dry-run" if not arguments.execute else "execute"
    plan: dict[str, object] = {"mode": mode, "phase": arguments.phase, "epoch": arguments.epoch,
        "operation_id": arguments.operation_id, "inventory_sha256": digest,
        "transition_tool": str(transition), "source_hash": arguments.transition_source_hash,
        "operation_state_directory": str(arguments.operation_state_directory.resolve()),
        "verbs": ["reserve-restore"] if arguments.phase == "submit" else ["verify-restore", "bounded-close-under-verified-reservation", "release-restore"],
        "unknown_outcome_retains_reservations": True, "reopen_traffic": False,
        "creates_or_deletes_indexes": False}
    if not arguments.execute:
        print(json.dumps(plan, indent=2))
        return 0
    _validateApproval(arguments.approval, manifestSha256=str(installation["manifest_sha256"]), purpose="recovery")
    assert arguments.approval is not None
    OperationExecution.validateOperatorApproval(_requireObject(_readJson(arguments.approval)), arguments.operation_state_directory)
    if _requireObject(_readJson(arguments.approval)).get("restore_inventory_sha256") != digest:
        raise ValueError("Recovery approval must bind the exact reviewed snapshot/target/index inventory")
    if not transition.is_file() or sha256(transition.read_bytes()).hexdigest() != arguments.transition_script_sha256:
        raise ValueError("Transition command differs from the reviewed source binding")
    if not arguments.receipt_directory.is_dir():
        raise ValueError("Create a new private receipt directory before recovery")
    with OperatorAuthority.fromArguments(arguments, manifest, operationId=arguments.operation_id,
        sourceHash=arguments.transition_source_hash, epoch=arguments.epoch) as authority:
        with OperationExecution.claim(arguments.operation_state_directory,
            resourceId=str(manifest["region"]) + ":" + str(manifest["cluster_name"]), operationId=arguments.operation_id,
            authorityReceipt=authority.receipt) as issuer:
            if arguments.phase == "submit" and arguments.journal.exists():
                raise ValueError("Recovery intent already exists; reconcile it without repeating native restore")
            if arguments.phase == "finish":
                previous: dict[str, object] = _requireObject(_readJson(arguments.journal))
                if previous.get("inventory_sha256") != digest or previous.get("epoch") != arguments.epoch or previous.get("operation_id") != arguments.operation_id:
                    raise ValueError("Finish does not match the admitted restore intent")
                RecoveryOperations.assertFinishableIntent(previous)
                if arguments.reservation is None:
                    raise ValueError("Finish requires the original source-owned reservation receipt")
            else:
                _writeJson(arguments.journal, {**plan, "native_outcome": "admission-intent", "reservation_held": True})

            from uuid import uuid4

            receipts: dict[str, Path] = {}

            def invoke(verb: str, inputPath: Path, reservationPath: Path | None = None) -> dict[str, object]:
                if sha256(transition.read_bytes()).hexdigest() != arguments.transition_script_sha256:
                    raise ValueError("Transition source changed during recovery")
                receiptPath: Path = arguments.receipt_directory / f"{verb}-{uuid4().hex}.json"
                command: list[str] = [sys.executable, str(transition), verb,
                    "--execute", "--epoch", str(authority.epoch), "--inventory", str(inputPath.resolve()),
                    "--operation-id", arguments.operation_id, "--expected-source-hash", arguments.transition_source_hash,
                    "--receipt", str(receiptPath.resolve()), *authority.getCommandArguments()]
                if reservationPath is not None:
                    command.extend(["--reservation", str(reservationPath.resolve())])
                completed: subprocess.CompletedProcess[bytes] = OperationExecution.run(command, cwd=repository,
                    timeoutSeconds=120, issuer=issuer)
                if not receiptPath.is_file():
                    raise ValueError("Transition did not persist its recovery receipt")
                receiptPath.chmod(0o600)
                receipt: dict[str, object] = _requireObject(_readJson(receiptPath))
                if completed.returncode != 0 or receipt.get("sourceHash") != arguments.transition_source_hash or receipt.get("expectedSourceHash") != arguments.transition_source_hash or receipt.get("execute") is not True or receipt.get("errors") != []:
                    raise ValueError("Transition refused recovery or changed source identity")
                receipts[verb] = receiptPath
                authority.refresh(receipt)
                return receipt

            def persist(value: dict[str, object]) -> None:
                _writeJson(arguments.journal, {**value, "mode": "execute", "source_hash": arguments.transition_source_hash}, replace=True)

            with _nativeClient(manifest) as client:
                def guardMutation(request: httpx.Request) -> None:
                    if request.method not in ("GET", "HEAD"):
                        authority.beforeMutation()

                client.event_hooks["request"].append(guardMutation)
                if arguments.phase == "submit":
                    result: dict[str, object] = RecoveryOperations.submit(client, inventory, epoch=arguments.epoch,
                        operationId=arguments.operation_id, reserve=lambda: invoke("reserve-restore", arguments.inventory), persist=persist)
                else:
                    def release(updated: dict[str, object], _verified: dict[str, object]) -> dict[str, object]:
                        updatedPath: Path = arguments.receipt_directory / f"close-attestation-{uuid4().hex}.json"
                        _writeJson(updatedPath, updated)
                        return invoke("release-restore", updatedPath, receipts["verify-restore"])

                    result = RecoveryOperations.finish(client, inventory, epoch=arguments.epoch, operationId=arguments.operation_id,
                        verify=lambda: invoke("verify-restore", arguments.inventory, arguments.reservation), release=release, persist=persist)
            print(json.dumps({key: value for key, value in result.items() if key not in ("reservation", "verified_reservation", "release_receipt")}, indent=2))
            if result["native_outcome"] in ("submitted", "closed-and-reconciled"):
                issuer.complete()
                authority.complete()
                return 0
            return 1


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError, subprocess.SubprocessError, httpx.HTTPError):
        print("Native recovery refused or incomplete; preserve reservations and inspect the private journal/receipts.", file=sys.stderr)
        raise SystemExit(2) from None
