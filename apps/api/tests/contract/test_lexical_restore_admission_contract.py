"""Gated restore accounting against owned PG and a fake native boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.contract.test_lexical_maintenance_contract import publishedInput as publishedInput, requireRow  # noqa: F401


@dataclass
class RestoreRuntime:
    engine: Engine
    registryId: str
    inventory: dict[str, object]
    native: object


def runAdmission(monkeypatch: pytest.MonkeyPatch, runtime: RestoreRuntime, verb: str, *, inventory: dict[str, object] | None = None,
                 reservation: dict[str, object] | None = None, execute: bool = True, operationId: str = "00000000-0000-0000-0000-000000000001", expectedEpoch: int = 5) -> dict[str, object]:
    from shared.services.retrieval.lexical import restore_admission
    monkeypatch.setattr(restore_admission, "OpenSearchLexicalIndex", lambda **arguments: runtime.native)
    return restore_admission.LexicalRestoreAdmission(sessionFactory=sessionmaker(runtime.engine, expire_on_commit=False)).run(
        verb, inventory=inventory or runtime.inventory, operationId=operationId, expectedEpoch=expectedEpoch, execute=execute, reservation=reservation)


@pytest.fixture
def restoreRuntime(publishedInput: tuple[Engine, str, str, str]) -> RestoreRuntime:
    from shared.core.config import settings
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, buildId, namespace, revisionId = publishedInput
    with Session(engine) as db:
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        build.unitCount = 1
        registry: LexicalNamespaceIndex = requireRow(db, LexicalNamespaceIndex, build.namespaceIndexId)
        registry.state = "closed"
        registry.indexName = f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{registry.id[:48]}"
        name: str = registry.indexName
        registryId: str = registry.id
        documentId: str = build.documentId
        control = db.get(LexicalBackendControl, 1)
        if control is None:
            control = LexicalBackendControl(id=1)
            db.add(control)
        control.backend = "opensearch"
        control.mode = "gated"
        control.epoch = 5
        control.clusterUuid = "old-cluster"
        control.nodeIncarnation = "old-node"
        db.commit()
    inventory: dict[str, object] = {"version": 1, "plan": {"expected_backend": "opensearch", "expected_old_cluster_uuid": "old-cluster",
        "expected_old_incarnation": "old-node", "target_cluster_uuid": "new-cluster", "target_incarnation": "new-node", "writers_fenced": True,
        "snapshot": {"repository": "owned-repository", "name": "owned-snapshot", "uuid": "snapshot-uuid", "manifest_sha256": "a" * 64},
        "indices": [{"owner_id": "maintenance-user", "namespace": namespace, "corpus_kind": "private", "index_name": name,
            "old_index_uuid": "test-index", "expected_state": "closed", "expected_operation_id": None,
            "builds": [{"build_id": buildId, "document_id": documentId, "revision_id": revisionId, "unit_count": 1}]}]}, "attestation": None}

    class Native:
        def __init__(self) -> None:
            self.indexName: str = name
            self.indexUuid: str | None = None
            self.isOpen: bool = True
            self.cluster: str = "new-cluster"
            self.incarnation: str = "new-node"
            self.snapshotUuid: str = "snapshot-uuid"
            self.hasUnits: bool = True
            self.failRequests: bool = False
            self.snapshotRequests: int = 0
        def assertNoTransaction(self) -> None:
            with Session(engine) as db:
                assert db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='idle in transaction'")) == 0
        def getLifecycleSnapshot(self, **arguments: object) -> NativeLifecycleSnapshot:
            self.assertNoTransaction()
            if self.failRequests:
                raise TimeoutError("native unavailable")
            metadata: dict[str, object] = {} if self.indexUuid is None else {name: {"state": "open" if self.isOpen else "close", "settings": {"index": {"uuid": self.indexUuid}}}}
            return NativeLifecycleSnapshot.model_validate({"cluster_uuid": self.cluster, "nodes": {"node": {"ephemeral_id": self.incarnation}},
                "metadata": {"indices": metadata}, "routing_table": {"indices": {} if self.indexUuid is None else {name: {"shards": {"0": [{"primary": True, "state": "STARTED"}]}}}},
                "blocks": {"indices": {} if self.isOpen or self.indexUuid is None else {name: {"4": {"levels": ["read", "write"]}}}}})
        def _request(self, method: str, path: str, **arguments: object) -> object:
            self.assertNoTransaction()
            if self.failRequests:
                raise TimeoutError("native unavailable")
            if path.startswith("/_snapshot/"):
                self.snapshotRequests += 1
                return {"snapshots": [{"snapshot": "owned-snapshot", "uuid": self.snapshotUuid, "state": "SUCCESS", "indices": [name]}]}
            payload = cast(dict[str, object], arguments.get("payload") or {})
            hits: list[dict[str, object]] = []
            if self.hasUnits and "search_after" not in payload:
                hits = [{"_source": {"buildId": buildId, "documentId": documentId, "revisionId": revisionId, "unitId": "unit"}, "sort": ["unit"]}]
            return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": hits, "total": {"value": len(hits), "relation": "eq"}}}
        def forScope(self, scope: LexicalScope) -> Native:
            return self
        def close(self) -> None:
            pass
    return RestoreRuntime(engine, registryId, inventory, Native())


def attest(runtime: RestoreRuntime, *, receipt: dict[str, object] | None = None) -> dict[str, object]:
    inventory: dict[str, object] = deepcopy(runtime.inventory)
    indices = cast(list[dict[str, object]], cast(dict[str, object], inventory["plan"])["indices"])
    mapping: dict[str, object] = {"index_name": indices[0]["index_name"], "old_index_uuid": "test-index", "restored_index_uuid": "restored-index"}
    if receipt is not None:
        mapping["close_acknowledged"] = True
        mapping["close_operation_id"] = cast(list[dict[str, object]], receipt["indices"])[0]["reservation_id"]
    inventory["attestation"] = {"snapshot_uuid": "snapshot-uuid", "manifest_sha256": "a" * 64, "identity_mapping": [mapping]}
    return inventory


def test_restore_reserves_before_native_work_and_idempotently_adopts_target(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    receipt = runAdmission(monkeypatch, restoreRuntime, "reserve-restore")
    assert receipt["status"] == "reserved"
    assert receipt["new_reservations"] == 1
    with Session(restoreRuntime.engine) as db:
        assert requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId).state == "opening"
        control = db.get(LexicalBackendControl, 1)
        assert control is not None and control.clusterUuid == "new-cluster" and control.mode == "gated"
    repeated = runAdmission(monkeypatch, restoreRuntime, "reserve-restore")
    assert repeated["status"] == "reserved"
    assert repeated["new_reservations"] == 0
    assert repeated["indices"] == receipt["indices"]


def test_restore_capacity_preserves_unknown_charges(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    with Session(restoreRuntime.engine) as db:
        for number in range(400):
            db.add(LexicalNamespaceIndex(id=f"{number:064x}", ownerId="unknown", namespace=f"unknown-{number}", corpusKind="private", indexName=f"unknown-{number}", indexUuid="unknown", state="uncertain"))
        db.commit()
    receipt = runAdmission(monkeypatch, restoreRuntime, "reserve-restore")
    assert receipt["status"] == "retained" and receipt["errors"]
    with Session(restoreRuntime.engine) as db:
        assert requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId).state == "closed"


def test_restore_mapping_requires_live_build_coverage_then_keeps_close_fence(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    reserved = runAdmission(monkeypatch, restoreRuntime, "reserve-restore")
    setattr(restoreRuntime.native, "indexUuid", "restored-index")
    setattr(restoreRuntime.native, "hasUnits", False)
    failed = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=attest(restoreRuntime), reservation=reserved)
    assert failed["status"] == "unknown" and failed["errors"]
    assert cast(list[dict[str, object]], failed["indices"])[0]["reservation_id"] == cast(list[dict[str, object]], reserved["indices"])[0]["reservation_id"]
    with Session(restoreRuntime.engine) as db:
        row = requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId)
        assert row.state == "uncertain" and row.indexUuid == "test-index"
    setattr(restoreRuntime.native, "hasUnits", True)
    verified = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=attest(restoreRuntime), reservation=reserved)
    assert verified["status"] == "verified" and not verified["errors"]
    with Session(restoreRuntime.engine) as db:
        row = requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId)
        assert row.state == "closing" and row.indexUuid == "restored-index"
        assert row.operationId == cast(list[dict[str, object]], verified["indices"])[0]["reservation_id"]
    assert verified["indices"] != reserved["indices"]


def test_release_requires_verified_fence_acknowledgment_and_live_closed_identity(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    reserved = runAdmission(monkeypatch, restoreRuntime, "reserve-restore")
    setattr(restoreRuntime.native, "indexUuid", "restored-index")
    verified = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=attest(restoreRuntime), reservation=reserved)
    blocked = runAdmission(monkeypatch, restoreRuntime, "release-restore", inventory=attest(restoreRuntime, receipt=verified), reservation=verified)
    assert blocked["status"] == "retained"
    setattr(restoreRuntime.native, "isOpen", False)
    released = runAdmission(monkeypatch, restoreRuntime, "release-restore", inventory=attest(restoreRuntime, receipt=verified), reservation=verified)
    assert released["status"] == "released" and not released["errors"]
    with Session(restoreRuntime.engine) as db:
        row = requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId)
        assert row.state == "closed" and row.operationId == cast(list[dict[str, object]], verified["indices"])[0]["reservation_id"] and row.indexUuid == "restored-index"


def test_restore_native_timeout_retains_reservation(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    reserved = runAdmission(monkeypatch, restoreRuntime, "reserve-restore")
    setattr(restoreRuntime.native, "failRequests", True)
    failed = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=attest(restoreRuntime), reservation=reserved)
    assert failed["status"] == "unknown"
    assert cast(list[dict[str, object]], failed["indices"])[0]["reservation_id"] == cast(list[dict[str, object]], reserved["indices"])[0]["reservation_id"]
    with Session(restoreRuntime.engine) as db:
        row = requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId)
        assert row.state == "uncertain" and row.operationId is not None


def test_restore_dry_run_does_not_adopt_or_charge(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    receipt = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", execute=False)
    assert receipt["status"] == "retained" and receipt["planned_new_reservations"] == 1
    with Session(restoreRuntime.engine) as db:
        assert requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId).state == "closed"
        control = db.get(LexicalBackendControl, 1)
        assert control is not None and control.clusterUuid == "old-cluster"


def remoteInventory(runtime: RestoreRuntime, *, reserved: dict[str, object] | None = None) -> dict[str, object]:
    inventory: dict[str, object] = deepcopy(runtime.inventory)
    plan = cast(dict[str, object], inventory["plan"])
    plan.pop("snapshot")
    plan["recovery_source"] = "live_remote"
    selected = cast(list[dict[str, object]], plan["indices"])[0]
    plan["remote_state"] = {"evidence_sha256": "b" * 64, "indices": [{"index_name": selected["index_name"], "index_uuid": "restored-index"}]}
    if reserved is not None:
        inventory["attestation"] = {"remote_evidence_sha256": "b" * 64, "identity_mapping": [{"index_name": selected["index_name"],
            "old_index_uuid": "test-index", "restored_index_uuid": "restored-index", "remote_restore_acknowledged": True,
            "remote_restore_operation_id": cast(list[dict[str, object]], reserved["indices"])[0]["reservation_id"]}]}
    return inventory


def test_live_remote_recovery_does_not_require_latest_snapshot_catalog(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    setattr(restoreRuntime.native, "indexUuid", "restored-index")
    setattr(restoreRuntime.native, "isOpen", False)
    setattr(restoreRuntime.native, "snapshotUuid", "obsolete-snapshot")
    inventory = remoteInventory(restoreRuntime)
    reserved = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", inventory=inventory)
    assert reserved["status"] == "reserved" and not reserved["errors"]
    setattr(restoreRuntime.native, "isOpen", True)
    verified = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=remoteInventory(restoreRuntime, reserved=reserved), reservation=reserved)
    assert verified["status"] == "verified" and not verified["errors"]
    assert getattr(restoreRuntime.native, "snapshotRequests") == 0
    finalInventory = remoteInventory(restoreRuntime, reserved=reserved)
    mapping = cast(list[dict[str, object]], cast(dict[str, object], finalInventory["attestation"])["identity_mapping"])[0]
    mapping["close_acknowledged"] = True
    mapping["close_operation_id"] = cast(list[dict[str, object]], verified["indices"])[0]["reservation_id"]
    setattr(restoreRuntime.native, "isOpen", False)
    released = runAdmission(monkeypatch, restoreRuntime, "release-restore", inventory=finalInventory, reservation=verified)
    assert released["status"] == "released" and not released["errors"]


def test_live_remote_unknown_acceptance_cannot_become_verified_close_fence(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    setattr(restoreRuntime.native, "indexUuid", "restored-index")
    setattr(restoreRuntime.native, "isOpen", False)
    reserved = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", inventory=remoteInventory(restoreRuntime))
    setattr(restoreRuntime.native, "isOpen", True)
    inventory = remoteInventory(restoreRuntime, reserved=reserved)
    mapping = cast(list[dict[str, object]], cast(dict[str, object], inventory["attestation"])["identity_mapping"])[0]
    mapping["remote_restore_acknowledged"] = False
    failed = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=inventory, reservation=reserved)
    assert failed["status"] == "retained" and failed["errors"]
    with Session(restoreRuntime.engine) as db:
        row = requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId)
        assert row.state == "opening" and row.indexUuid == "test-index"


def test_live_remote_existing_open_metadata_is_charged_once(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    setattr(restoreRuntime.native, "indexUuid", "restored-index")
    with Session(restoreRuntime.engine) as db:
        for number in range(399):
            db.add(LexicalNamespaceIndex(id=f"{number:064x}", ownerId="unknown", namespace=f"unknown-{number}", corpusKind="private", indexName=f"unknown-{number}", indexUuid="unknown", state="uncertain"))
        db.commit()
    receipt = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", inventory=remoteInventory(restoreRuntime))
    assert receipt["status"] == "reserved" and not receipt["errors"]
    assert receipt["charged_before"] == {"open": 1, "reserved": 0, "uncertain": 399}


def test_unowned_closed_index_cannot_release_with_a_fabricated_verified_receipt(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    dry = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", execute=False)
    dry["execute"] = True
    dry["status"] = "reserved"
    setattr(restoreRuntime.native, "indexUuid", "test-index")
    inventory = attest(restoreRuntime)
    mapping = cast(list[dict[str, object]], cast(dict[str, object], inventory["attestation"])["identity_mapping"])[0]
    mapping["restored_index_uuid"] = "test-index"
    refused = runAdmission(monkeypatch, restoreRuntime, "verify-restore", inventory=inventory, reservation=dry, execute=False)
    assert refused["status"] == "retained"
    forged = deepcopy(refused)
    forged.update({"status": "verified", "errors": [], "execute": True})
    mapping["close_acknowledged"] = True
    mapping["close_operation_id"] = cast(list[dict[str, object]], refused["indices"])[0]["reservation_id"]
    setattr(restoreRuntime.native, "isOpen", False)
    released = runAdmission(monkeypatch, restoreRuntime, "release-restore", inventory=inventory, reservation=forged)
    assert released["status"] == "retained" and released["errors"]


def test_restore_requires_exact_gate_before_native_queries(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    receipt = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", expectedEpoch=6)
    assert receipt["status"] == "retained" and receipt["errors"]
    assert getattr(restoreRuntime.native, "snapshotRequests") == 0
    with Session(restoreRuntime.engine) as db:
        assert requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId).state == "closed"


def test_restore_refuses_a_role_without_complete_private_demo_visibility(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from uuid import uuid4
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import SessionTransaction
    from shared.services.retrieval.lexical import restore_admission
    role: str = f"restore_scoped_{uuid4().hex}"
    with Session(restoreRuntime.engine) as db:
        db.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        db.commit()
    class ScopedSession(Session):
        pass
    @event.listens_for(ScopedSession, "after_begin")
    def chooseRole(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
    monkeypatch.setattr(restore_admission, "OpenSearchLexicalIndex", lambda **arguments: restoreRuntime.native)
    receipt = restore_admission.LexicalRestoreAdmission(sessionFactory=sessionmaker(restoreRuntime.engine, class_=ScopedSession)).run(
        "reserve-restore", inventory=restoreRuntime.inventory, operationId="00000000-0000-0000-0000-000000000001", expectedEpoch=5, execute=True)
    assert receipt["status"] == "retained" and receipt["errors"]
    assert getattr(restoreRuntime.native, "snapshotRequests") == 0


def test_already_charged_namespace_is_not_charged_again_on_target_adoption(monkeypatch: pytest.MonkeyPatch, restoreRuntime: RestoreRuntime) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    inventory = deepcopy(restoreRuntime.inventory)
    cast(list[dict[str, object]], cast(dict[str, object], inventory["plan"])["indices"])[0]["expected_state"] = "open"
    with Session(restoreRuntime.engine) as db:
        requireRow(db, LexicalNamespaceIndex, restoreRuntime.registryId).state = "open"
        db.add(LexicalNamespaceIndex(id="f" * 64, ownerId="old-owner", namespace="old-open", corpusKind="private",
            indexName="old-open", indexUuid="old-uuid", state="open", operationId="old-operation"))
        db.commit()
    receipt = runAdmission(monkeypatch, restoreRuntime, "reserve-restore", inventory=inventory)
    assert receipt["status"] == "reserved" and receipt["new_reservations"] == 0
    with Session(restoreRuntime.engine) as db:
        prior = requireRow(db, LexicalNamespaceIndex, "f" * 64)
        assert prior.state == "uncertain" and prior.operationId == "old-operation"
