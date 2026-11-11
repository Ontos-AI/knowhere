"""Reconcile a restarted native node at an already gated backend epoch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.models.database.lexicaloperatorinvocation import LexicalOperatorInvocation
from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot

__all__: list[str] = ["LexicalRestartIdentity"]


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1]
    cluster_uuid: str = Field(min_length=1, max_length=64)
    old_incarnation: str = Field(min_length=1, max_length=64)
    new_incarnation: str = Field(min_length=1, max_length=64)
    host_recovery_receipt_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class LexicalRestartIdentity:
    def __init__(self, *, sessionFactory: sessionmaker[Session], indexPrefix: str,
            readNative: Callable[[], NativeLifecycleSnapshot]) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory
        self.indexPrefix: str = indexPrefix
        self.readNative: Callable[[], NativeLifecycleSnapshot] = readNative

    def reconcile(self, plan: object, *, expectedEpoch: int, expectedBackend: str, sourceHash: str,
            issuer: dict[str, object] | None, hostReceipt: dict[str, object], hostReceiptSha256: str,
            execute: bool) -> dict[str, object]:
        expected: _Plan = _Plan.model_validate(plan)
        host: object = hostReceipt.get("host")
        fence: object = hostReceipt.get("fence")
        oldIssuer: object = hostReceipt.get("issuer")
        if (expected.old_incarnation == expected.new_incarnation
                or expected.host_recovery_receipt_sha256 != hostReceiptSha256
                or hostReceipt.get("mode") != "actual" or hostReceipt.get("status") != "recovered-host"
                or hostReceipt.get("route") not in ("process", "healthy-volume")
                or not isinstance(fence, dict) or fence.get("native_issuer_fenced") is not True
                or not isinstance(host, dict) or host.get("active_state") != "active"
                or not isinstance(host.get("main_pid"), int) or host["main_pid"] <= 0
                or not isinstance(oldIssuer, dict) or oldIssuer.get("source_hash") != sourceHash
                or oldIssuer.get("cluster_uuid") != expected.cluster_uuid or oldIssuer.get("epoch") != expectedEpoch):
            raise LexicalIndexError("Restart reconciliation requires the exact actual fenced host recovery receipt")
        if not execute:
            return {"status": "planned", "control": None, "errors": [],
                "expected_cluster_uuid": expected.cluster_uuid, "expected_incarnation": expected.new_incarnation}
        if issuer is None or issuer.get("status") != "active" or issuer.get("source_hash") != sourceHash:
            raise LexicalIndexError("Restart reconciliation requires an actual active physical issuer")
        if any(issuer.get(field) != oldIssuer.get(field) for field in ("operation_id", "logical_cluster_id")):
            raise LexicalIndexError("Restart reconciliation must retain the exact admitted recovery operation")

        def checkCurrent(database: Session, *, lock: bool) -> LexicalBackendControl:
            database.execute(text("SET LOCAL statement_timeout = '10s'"))
            database.execute(text("SET LOCAL lock_timeout = '5s'"))
            if not database.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")):
                raise LexicalIndexError("Restart reconciliation requires full private/demo maintenance visibility")
            controlStatement = select(LexicalBackendControl).where(LexicalBackendControl.id == 1)
            ownerStatement = select(LexicalOperatorIssuer).where(LexicalOperatorIssuer.controlId == 1)
            control: LexicalBackendControl | None = database.scalar(controlStatement.with_for_update() if lock else controlStatement)
            owner: LexicalOperatorIssuer | None = database.scalar(ownerStatement.with_for_update() if lock else ownerStatement)
            history: LexicalOperatorInvocation | None = database.get(LexicalOperatorInvocation, oldIssuer.get("invocation_token"))
            if (control is None or (control.mode, control.backend, control.epoch, control.clusterUuid, control.nodeIncarnation)
                    != ("gated", expectedBackend, expectedEpoch, expected.cluster_uuid, expected.old_incarnation)
                    or history is None or history.logicalClusterId != oldIssuer.get("logical_cluster_id")
                    or owner is None or (owner.state, owner.invocationToken, owner.hostIdentity, owner.operationId,
                        owner.sourceHash, owner.epoch, owner.clusterUuid, owner.logicalClusterId)
                    != ("active", issuer.get("invocation_token"), issuer.get("host_identity"), issuer.get("operation_id"),
                        sourceHash, expectedEpoch, expected.cluster_uuid, issuer.get("logical_cluster_id"))):
                raise LexicalIndexError("Restart reconciliation lost its gated epoch, old incarnation or physical issuer")
            return control

        with self.sessions.begin() as database:
            checkCurrent(database, lock=False)
        native: NativeLifecycleSnapshot = self.readNative()
        if (native.cluster_uuid, native.getIncarnation()) != (expected.cluster_uuid, expected.new_incarnation):
            raise LexicalIndexError("Native restart does not match the reviewed cluster UUID and new incarnation")
        with self.sessions.begin() as database:
            control: LexicalBackendControl = checkCurrent(database, lock=True)
            rows: list[LexicalNamespaceIndex] = list(database.scalars(select(LexicalNamespaceIndex).order_by(LexicalNamespaceIndex.id).with_for_update()))
            known: set[str] = {row.indexName for row in rows}
            observedNames: set[str] = {name for name in native.metadata.indices if name.startswith(self.indexPrefix + "-")}
            namespaceNames: set[str] = observedNames - {f"{self.indexPrefix}-published-manifests"}
            if namespaceNames - known:
                raise LexicalIndexError("Native restart observed managed index names outside the authoritative registry")
            uncertain: list[str] = []
            for row in rows:
                identity: str | None = native.getIndexUuid(row.indexName)
                isUnresolved: bool = (row.state in ("opening", "closing", "uncertain")
                    or row.state == "open" and (row.indexUuid != identity or not native.isReady(row.indexName))
                    or row.state == "closed" and (row.indexUuid != identity or not native.isClosed(row.indexName))
                    or row.state == "uncreated" and identity is not None)
                if isUnresolved:
                    # A restart is not an acknowledged close. Preserve UUID and
                    # operation ownership and charge the uncertain slot.
                    row.state = "uncertain"
                    uncertain.append(row.indexName)
            charged: set[str] = {row.indexName for row in rows if row.state in ("open", "opening", "closing", "uncertain")}
            charged.update(name for name in observedNames if native.isOpen(name))
            if len(charged) > 400:
                raise LexicalIndexError("Native restart would exceed the 400-slot authoritative union")
            control.nodeIncarnation = expected.new_incarnation
            return {"status": "reconciled", "errors": [], "control": {"backend": control.backend,
                "mode": control.mode, "epoch": control.epoch, "cluster_uuid": control.clusterUuid,
                "node_incarnation": control.nodeIncarnation}, "charged_slots": len(charged), "uncertain_indices": uncertain,
                "catalog_indices": len(namespaceNames), "native_requests_mutated": False, "service_restored": False}
