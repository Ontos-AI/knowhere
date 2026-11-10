"""Operator-only restore reservations; native restore and close stay external.

A missing-index snapshot restore assigns a new physical UUID. Adoption requires
an explicit old-to-new mapping, the reviewed snapshot and live current-build
coverage. Verification keeps a charged close fence; only acknowledged, observed
closure releases it. This module never creates, restores, closes or deletes an
index, and never changes successful jobs or current publication selections.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session, noload, sessionmaker

from shared.core.config import settings
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope

_CHARGED: frozenset[str] = frozenset(("open", "opening", "closing", "uncertain"))
_CAP: int = 400


class _BuildPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    build_id: str = Field(min_length=1, max_length=36)
    document_id: str = Field(min_length=1, max_length=36)
    revision_id: str = Field(min_length=1, max_length=36)
    unit_count: int = Field(ge=0)


class _IndexPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    owner_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1, max_length=255)
    corpus_kind: Literal["private", "demo"]
    index_name: str = Field(min_length=1, max_length=128)
    old_index_uuid: str = Field(min_length=1, max_length=64)
    expected_state: Literal["open", "closed", "opening", "closing", "uncertain"]
    expected_operation_id: str | None
    builds: tuple[_BuildPlan, ...]

    @property
    def scope(self) -> LexicalScope:
        return LexicalScope(ownerId=self.owner_id, namespace=self.namespace, corpusKind=self.corpus_kind)


class _SnapshotPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
    uuid: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _RemoteIndex(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    index_name: str
    index_uuid: str = Field(min_length=1, max_length=64)


class _RemoteState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    indices: tuple[_RemoteIndex, ...] = Field(min_length=1, max_length=400)


class _RestorePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_backend: Literal["postgresql", "opensearch"]
    expected_old_cluster_uuid: str = Field(min_length=1)
    expected_old_incarnation: str = Field(min_length=1)
    target_cluster_uuid: str = Field(min_length=1)
    target_incarnation: str = Field(min_length=1)
    writers_fenced: Literal[True]
    recovery_source: Literal["snapshot", "live_remote"] = "snapshot"
    snapshot: _SnapshotPlan | None = None
    remote_state: _RemoteState | None = None
    indices: tuple[_IndexPlan, ...] = Field(min_length=1, max_length=400)


class _IdentityMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    index_name: str
    old_index_uuid: str
    restored_index_uuid: str = Field(min_length=1, max_length=64)
    close_acknowledged: bool = False
    close_operation_id: str | None = None
    remote_restore_acknowledged: bool = False
    remote_restore_operation_id: str | None = None


class _Attestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot_uuid: str | None = None
    manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    remote_evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    identity_mapping: tuple[_IdentityMapping, ...]


class _Inventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1]
    plan: _RestorePlan
    attestation: _Attestation | None = None


@dataclass(frozen=True)
class _Binding:
    index: _IndexPlan
    units: dict[str, tuple[str, ...]]
    coverageHash: str


class _KnownRefusal(LexicalIndexError):
    pass


class LexicalRestoreAdmission:
    def __init__(self, *, sessionFactory: sessionmaker[Session]) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory

    def run(self, verb: str, *, inventory: dict[str, object], operationId: str,
            expectedEpoch: int, execute: bool = False,
            reservation: dict[str, object] | None = None) -> dict[str, object]:
        if verb not in ("reserve-restore", "verify-restore", "release-restore"):
            raise ValueError("Unknown restore admission verb")
        UUID(operationId)
        parsed: _Inventory = _Inventory.model_validate(inventory)
        plan: _RestorePlan = parsed.plan
        if (plan.recovery_source == "snapshot" and (plan.snapshot is None or plan.remote_state is not None)
            or plan.recovery_source == "live_remote" and (plan.remote_state is None or plan.snapshot is not None)):
            raise ValueError("Restore inventory requires exactly its selected recovery source evidence")
        planHash: str = sha256(json.dumps(inventory["plan"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        names: tuple[str, ...] = tuple(index.index_name for index in plan.indices)
        if len(set(names)) != len(names) or len({index.scope.fingerprint for index in plan.indices}) != len(names):
            raise ValueError("Restore inventory contains duplicate namespace identities")
        receipt: dict[str, object] = {"version": 1, "mode": verb, "verb": verb, "status": "retained", "execute": execute,
            "epoch": expectedEpoch, "operation_id": operationId, "cluster_uuid": plan.target_cluster_uuid,
            "recovery_source": plan.recovery_source,
            "inventory_sha256": planHash, "indices": [], "charged_before": {"open": 0, "reserved": 0, "uncertain": 0},
            "new_reservations": 0, "planned_new_reservations": 0, "cap": _CAP, "errors": []}
        native: OpenSearchLexicalIndex | None = None
        try:
            with self.sessions.begin() as db:
                control: LexicalBackendControl = self._checkControl(db, plan, expectedEpoch)
                self._projectControl(receipt, control)
                bindings: tuple[_Binding, ...] = self._captureBindings(db, plan)
                projected: list[dict[str, object]] = []
                for binding in bindings:
                    item: dict[str, object] = self._projectIndex(binding, operationId, expectedEpoch, planHash)
                    row: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, binding.index.scope.fingerprint)
                    if row is not None:
                        item["reservation_id"] = row.operationId or item["reservation_id"]
                        item["registered_index_uuid"] = row.indexUuid
                    projected.append(item)
                receipt["indices"] = projected
            native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                scope=LexicalScope(ownerId="operator", namespace="restore-inventory"),
                indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
            live: NativeLifecycleSnapshot = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=30)
            self._checkTarget(live, plan)
            if plan.recovery_source == "snapshot":
                self._checkSnapshot(native, plan)
            else:
                self._checkRemoteState(live, plan)
            if verb == "reserve-restore":
                self._reserve(plan, bindings, live, receipt, operationId, expectedEpoch, planHash, execute)
                return receipt
            self._checkReceipt(reservation, planHash, operationId, expectedEpoch, requireVerified=verb == "release-restore")
            mappings: dict[str, _IdentityMapping] = self._getMappings(parsed)
            if plan.recovery_source == "live_remote":
                for index in plan.indices:
                    mapping: _IdentityMapping = mappings[index.index_name]
                    if (not mapping.remote_restore_acknowledged
                        or mapping.remote_restore_operation_id != self._getReservationId(index, operationId, expectedEpoch, planHash)):
                        raise _KnownRefusal("Remote recovery requires acknowledged restore admission; unknown issuers keep their charge")
            receipt["indices"] = [self._projectIndex(binding, operationId, expectedEpoch, planHash,
                mapping=mappings[binding.index.index_name], isVerified=True) for binding in bindings]
            if verb == "verify-restore":
                for binding in bindings:
                    mapping: _IdentityMapping = mappings[binding.index.index_name]
                    if live.getIndexUuid(binding.index.index_name) != mapping.restored_index_uuid or not live.isReady(binding.index.index_name):
                        raise LexicalIndexError("Restored namespace is not ready at its attested identity")
                    self._checkBuildCoverage(native, binding)
                confirmed: NativeLifecycleSnapshot = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=30)
                self._checkTarget(confirmed, plan)
                for binding in bindings:
                    if confirmed.getIndexUuid(binding.index.index_name) != mappings[binding.index.index_name].restored_index_uuid:
                        raise LexicalIndexError("Restored namespace identity changed during verification")
                self._verify(plan, bindings, mappings, receipt, operationId, expectedEpoch, planHash, execute)
            else:
                self._release(plan, bindings, mappings, live, receipt, operationId, expectedEpoch, planHash, execute)
            return receipt
        except Exception as error:
            hasOwnedCharge: bool = False
            if execute and not isinstance(error, _KnownRefusal):
                hasOwnedCharge, observed = self._markUnknown(plan, operationId, expectedEpoch, planHash, parsed.attestation)
                entries: object = receipt.get("indices")
                if isinstance(entries, list):
                    for item in entries:
                        name: object = item.get("index_name") if isinstance(item, dict) else None
                        if isinstance(item, dict) and isinstance(name, str) and name in observed:
                            item.update(observed[name])
            receipt["status"] = "unknown" if hasOwnedCharge else "retained"
            receipt["errors"] = [str(error) if isinstance(error, LexicalIndexError) else type(error).__name__]
            return receipt
        finally:
            if native is not None:
                native.close()

    @staticmethod
    def _authorize(db: Session) -> None:
        if not db.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")):
            raise _KnownRefusal("Restore admission requires full private/demo RLS maintenance visibility")

    @classmethod
    def _checkControl(cls, db: Session, plan: _RestorePlan, epoch: int, *, lock: bool = False) -> LexicalBackendControl:
        cls._authorize(db)
        statement = select(LexicalBackendControl).where(LexicalBackendControl.id == 1)
        control: LexicalBackendControl | None = db.scalar(statement.with_for_update() if lock else statement)
        if control is None or control.epoch != epoch or control.mode != "gated" or control.backend != plan.expected_backend:
            raise _KnownRefusal("Restore admission requires the exact gated backend epoch")
        if (control.clusterUuid, control.nodeIncarnation) not in (
            (plan.expected_old_cluster_uuid, plan.expected_old_incarnation), (plan.target_cluster_uuid, plan.target_incarnation)):
            raise _KnownRefusal("Restore control differs from the reviewed old or target identity")
        return control

    @staticmethod
    def _projectControl(receipt: dict[str, object], control: LexicalBackendControl) -> None:
        receipt["backend"] = control.backend
        receipt["control"] = {"backend": control.backend, "mode": control.mode, "epoch": control.epoch,
            "cluster_uuid": control.clusterUuid, "node_incarnation": control.nodeIncarnation}

    @staticmethod
    def _captureBindings(db: Session, plan: _RestorePlan) -> tuple[_Binding, ...]:
        bindings: list[_Binding] = []
        for index in plan.indices:
            registry: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, index.scope.fingerprint)
            expectedName: str = f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{index.scope.fingerprint[:48]}"
            if (registry is None or registry.indexName != index.index_name or index.index_name != expectedName
                or (registry.ownerId, registry.namespace, registry.corpusKind) != (index.owner_id, index.namespace, index.corpus_kind)):
                raise _KnownRefusal("Restore inventory differs from a registered namespace identity")
            storage: CorpusStorage = CorpusStorage.resolve_namespace(index.namespace)
            if storage.is_demo != (index.corpus_kind == "demo") or storage.resolve_owner(index.owner_id) != index.owner_id:
                raise _KnownRefusal("Restore inventory escaped its private/demo ownership")
            documents = list(db.scalars(select(storage.Document).options(noload("*")).where(
                storage.Document.user_id == index.owner_id, storage.Document.namespace == index.namespace,
                storage.Document.status == "active", storage.Document.current_job_result_id.is_not(None))))
            plans: dict[str, _BuildPlan] = {build.document_id: build for build in index.builds}
            if len(plans) != len(index.builds) or set(plans) != {document.document_id for document in documents}:
                raise _KnownRefusal("Snapshot batch does not cover the exact current published documents")
            units: dict[str, tuple[str, ...]] = {}
            for document in documents:
                expected: _BuildPlan = plans[document.document_id]
                build: LexicalBuild | None = db.get(LexicalBuild, expected.build_id)
                marker = db.scalar(select(storage.DocumentMapUnitIndex).where(
                    storage.DocumentMapUnitIndex.document_id == expected.document_id,
                    storage.DocumentMapUnitIndex.job_result_id == expected.revision_id))
                if (document.current_job_result_id != expected.revision_id
                    or document.current_indexing_build_id not in (None, expected.build_id)
                    or build is None or build.state != "published" or build.namespaceIndexId != registry.id
                    or build.documentId != expected.document_id or build.revisionId != expected.revision_id
                    or build.unitCount != expected.unit_count or marker is None or marker.unit_count != expected.unit_count):
                    raise _KnownRefusal("Restore build differs from the current published revision/count")
                identities: tuple[str, ...] = tuple(db.scalars(select(storage.DocumentMapUnit.unit_id).where(
                    storage.DocumentMapUnit.document_id == expected.document_id,
                    storage.DocumentMapUnit.job_result_id == expected.revision_id).order_by(storage.DocumentMapUnit.unit_id)))
                if len(identities) != expected.unit_count or len(set(identities)) != len(identities):
                    raise _KnownRefusal("Current published map-unit coverage is incomplete")
                units[expected.build_id] = identities
            digest: str = sha256(json.dumps(units, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            bindings.append(_Binding(index, units, digest))
        return tuple(bindings)

    @staticmethod
    def _checkTarget(live: NativeLifecycleSnapshot, plan: _RestorePlan) -> None:
        if live.cluster_uuid != plan.target_cluster_uuid or live.getIncarnation() != plan.target_incarnation:
            raise LexicalIndexError("Live native target differs from the reviewed restore identity")

    @staticmethod
    def _checkSnapshot(native: OpenSearchLexicalIndex, plan: _RestorePlan) -> None:
        snapshot: _SnapshotPlan | None = plan.snapshot
        if snapshot is None:
            raise _KnownRefusal("Snapshot recovery has no reviewed snapshot evidence")
        response: object = native._request("GET", f"/_snapshot/{quote(snapshot.repository, safe='')}/{quote(snapshot.name, safe='')}")
        snapshots: object = response.get("snapshots") if isinstance(response, dict) else None
        if not isinstance(snapshots, list) or len(snapshots) != 1 or not isinstance(snapshots[0], dict):
            raise LexicalIndexError("Reviewed snapshot metadata is unavailable")
        selected = snapshots[0]
        names: object = selected.get("indices")
        if (selected.get("snapshot") != snapshot.name or selected.get("uuid") != snapshot.uuid or selected.get("state") != "SUCCESS"
            or not isinstance(names, list) or not {index.index_name for index in plan.indices}.issubset(set(names))):
            raise LexicalIndexError("Reviewed snapshot identity/completion/catalog does not match")

    @staticmethod
    def _checkRemoteState(live: NativeLifecycleSnapshot, plan: _RestorePlan) -> None:
        remote: _RemoteState | None = plan.remote_state
        if remote is None:
            raise _KnownRefusal("Live remote recovery has no reviewed metadata evidence")
        identities: dict[str, str] = {item.index_name: item.index_uuid for item in remote.indices}
        if len(identities) != len(remote.indices) or set(identities) != {index.index_name for index in plan.indices}:
            raise _KnownRefusal("Remote metadata evidence does not cover this exact namespace batch")
        for name, identity in identities.items():
            if live.getIndexUuid(name) != identity:
                raise LexicalIndexError("Live restored remote metadata differs from its reviewed identity")

    @staticmethod
    def _getMappings(inventory: _Inventory) -> dict[str, _IdentityMapping]:
        attestation: _Attestation | None = inventory.attestation
        if attestation is None:
            raise _KnownRefusal("Restore verification requires its reviewed recovery attestation")
        if inventory.plan.recovery_source == "snapshot":
            snapshot: _SnapshotPlan | None = inventory.plan.snapshot
            if snapshot is None or attestation.snapshot_uuid != snapshot.uuid or attestation.manifest_sha256 != snapshot.manifest_sha256:
                raise _KnownRefusal("Restore verification requires the reviewed snapshot/manifest attestation")
        else:
            remote: _RemoteState | None = inventory.plan.remote_state
            if remote is None or attestation.remote_evidence_sha256 != remote.evidence_sha256:
                raise _KnownRefusal("Remote verification requires the reviewed existing-metadata evidence")
        mappings: dict[str, _IdentityMapping] = {item.index_name: item for item in attestation.identity_mapping}
        if len(mappings) != len(attestation.identity_mapping) or set(mappings) != {index.index_name for index in inventory.plan.indices}:
            raise _KnownRefusal("Restore attestation has incomplete or duplicate physical identity mappings")
        if any(mappings[index.index_name].old_index_uuid != index.old_index_uuid for index in inventory.plan.indices):
            raise _KnownRefusal("Restore physical mapping differs from the reviewed old UUID")
        if inventory.plan.remote_state is not None and any(
            mappings[item.index_name].restored_index_uuid != item.index_uuid for item in inventory.plan.remote_state.indices):
            raise _KnownRefusal("Remote physical mapping differs from the reviewed restored metadata UUID")
        return mappings

    @staticmethod
    def _getReservationId(index: _IndexPlan, operationId: str, epoch: int, planHash: str,
                          *, mapping: _IdentityMapping | None = None, coverageHash: str = "") -> str:
        return str(uuid5(NAMESPACE_URL, json.dumps(["knowhere-restore-v1", operationId, epoch, planHash,
            index.scope.fingerprint, "verified" if mapping is not None else "reserved",
            mapping.restored_index_uuid if mapping is not None else "", coverageHash], separators=(",", ":"))))

    @classmethod
    def _projectIndex(cls, binding: _Binding, operationId: str, epoch: int, planHash: str,
                      *, mapping: _IdentityMapping | None = None, isVerified: bool = False) -> dict[str, object]:
        index: _IndexPlan = binding.index
        return {"index_name": index.index_name, "namespace": index.namespace, "owner_id": index.owner_id,
            "corpus_kind": index.corpus_kind, "already_charged": index.expected_state in _CHARGED,
            "reservation_id": cls._getReservationId(index, operationId, epoch, planHash,
                mapping=mapping if isVerified else None, coverageHash=binding.coverageHash if isVerified else ""),
            "restored_index_uuid": mapping.restored_index_uuid if mapping is not None else None,
            "coverage_sha256": binding.coverageHash if isVerified else None}

    @staticmethod
    def _checkReceipt(reservation: dict[str, object] | None, planHash: str, operationId: str, epoch: int, *, requireVerified: bool) -> None:
        if (reservation is None or reservation.get("inventory_sha256") != planHash or reservation.get("operation_id") != operationId
            or reservation.get("epoch") != epoch or reservation.get("errors") or reservation.get("execute") is not True
            or reservation.get("status") not in (("verified",) if requireVerified else ("reserved", "verified"))):
            raise _KnownRefusal("Restore reservation receipt is not bound to this executed plan/epoch/operation")

    def _reserve(self, plan: _RestorePlan, bindings: tuple[_Binding, ...], live: NativeLifecycleSnapshot,
                 receipt: dict[str, object], operationId: str, epoch: int, planHash: str, execute: bool) -> None:
        with self.sessions.begin() as db:
            control: LexicalBackendControl = self._checkControl(db, plan, epoch, lock=True)
            rows: list[LexicalNamespaceIndex] = list(db.scalars(select(LexicalNamespaceIndex).with_for_update()))
            byIdentity: dict[str, LexicalNamespaceIndex] = {row.id: row for row in rows}
            charged: list[LexicalNamespaceIndex] = [row for row in rows if row.state in _CHARGED]
            extraOpen: set[str] = {name for name in live.metadata.indices if live.isOpen(name)} - {row.indexName for row in charged}
            receipt["charged_before"] = {"open": sum(row.state == "open" for row in rows) + len(extraOpen),
                "reserved": sum(row.state in ("opening", "closing") for row in rows), "uncertain": sum(row.state == "uncertain" for row in rows)}
            newReservations: int = 0
            additionalSlots: int = 0
            for binding in bindings:
                index: _IndexPlan = binding.index
                row: LexicalNamespaceIndex = byIdentity[index.scope.fingerprint]
                reservationId: str = self._getReservationId(index, operationId, epoch, planHash)
                isOwned: bool = row.operationId == reservationId and row.state in _CHARGED
                if not isOwned and (row.state != index.expected_state or row.operationId != index.expected_operation_id or row.indexUuid != index.old_index_uuid):
                    raise _KnownRefusal("Restore reservation lost its reviewed registry state/operation/UUID")
                if not isOwned and row.state == "closed" and live.isOpen(index.index_name) and plan.recovery_source == "snapshot":
                    raise _KnownRefusal("A closed namespace was restored open before reserving capacity")
                newReservations += int(row.state not in _CHARGED)
                additionalSlots += int(row.state not in _CHARGED and index.index_name not in extraOpen)
            if len(charged) + len(extraOpen) + additionalSlots > _CAP:
                raise _KnownRefusal("Restore batch exceeds the charged namespace capacity of 400")
            receipt["planned_new_reservations"] = newReservations
            receipt["additional_slots"] = additionalSlots
            receipt["charged_after"] = len(charged) + len(extraOpen) + additionalSlots
            receipt["indices"] = [self._projectIndex(binding, operationId, epoch, planHash) for binding in bindings]
            if not execute:
                return
            if (control.clusterUuid, control.nodeIncarnation) != (plan.target_cluster_uuid, plan.target_incarnation):
                for row in charged:
                    row.state = "uncertain"
                control.clusterUuid = plan.target_cluster_uuid
                control.nodeIncarnation = plan.target_incarnation
            for binding in bindings:
                row = byIdentity[binding.index.scope.fingerprint]
                row.state = "opening"
                row.operationId = self._getReservationId(binding.index, operationId, epoch, planHash)
                row.lastUsedAt = LexicalCoordination.getDatabaseTime(db)
            receipt["new_reservations"] = newReservations
            receipt["status"] = "reserved"
            self._projectControl(receipt, control)

    @staticmethod
    def _checkBuildCoverage(native: OpenSearchLexicalIndex, binding: _Binding) -> None:
        target: OpenSearchLexicalIndex = native.forScope(binding.index.scope)
        try:
            for build in binding.index.builds:
                found: list[str] = []
                cursor: list[object] | None = None
                while True:
                    payload: dict[str, object] = {"size": 1000, "query": {"term": {"buildId": build.build_id}},
                        "sort": [{"unitId": "asc"}], "_source": ["buildId", "documentId", "revisionId", "unitId"]}
                    if cursor is not None:
                        payload["search_after"] = cursor
                    response: object = target._request("POST", f"/{binding.index.index_name}/_search?allow_partial_search_results=false&ignore_unavailable=false", payload=payload)
                    if (not isinstance(response, dict) or response.get("timed_out") or not isinstance(response.get("_shards"), dict)
                        or response["_shards"].get("failed") or not isinstance(response.get("hits"), dict)):
                        raise LexicalIndexError("Restored build coverage query did not complete")
                    hits: object = response["hits"].get("hits")
                    if not isinstance(hits, list):
                        raise LexicalIndexError("Restored build coverage response is malformed")
                    if not hits:
                        break
                    for hit in hits:
                        source: object = hit.get("_source") if isinstance(hit, dict) else None
                        if (not isinstance(source, dict) or source.get("buildId") != build.build_id
                            or source.get("documentId") != build.document_id or source.get("revisionId") != build.revision_id
                            or not isinstance(source.get("unitId"), str)):
                            raise LexicalIndexError("Restored native unit escaped its required published revision")
                        found.append(source["unitId"])
                    if len(found) > build.unit_count:
                        raise LexicalIndexError("Restored build contains unexpected map units")
                    nextCursor: object = hits[-1].get("sort")
                    if not isinstance(nextCursor, list) or nextCursor == cursor:
                        raise LexicalIndexError("Restored build coverage cursor did not advance")
                    cursor = nextCursor
                if tuple(sorted(found)) != binding.units[build.build_id]:
                    raise LexicalIndexError("Restored snapshot misses required current published build coverage")
        finally:
            target.close()

    def _verify(self, plan: _RestorePlan, bindings: tuple[_Binding, ...], mappings: dict[str, _IdentityMapping],
                receipt: dict[str, object], operationId: str, epoch: int, planHash: str, execute: bool) -> None:
        with self.sessions.begin() as db:
            control: LexicalBackendControl = self._checkControl(db, plan, epoch, lock=True)
            if self._captureBindings(db, plan) != bindings:
                raise LexicalIndexError("Current published coverage changed during restore verification")
            for binding in bindings:
                index: _IndexPlan = binding.index
                mapping: _IdentityMapping = mappings[index.index_name]
                row: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, index.scope.fingerprint, with_for_update=True)
                reservedId: str = self._getReservationId(index, operationId, epoch, planHash)
                verifiedId: str = self._getReservationId(index, operationId, epoch, planHash, mapping=mapping, coverageHash=binding.coverageHash)
                if row is None or row.operationId not in (reservedId, verifiedId) or row.state not in _CHARGED or row.indexUuid not in (index.old_index_uuid, mapping.restored_index_uuid):
                    raise _KnownRefusal("Restore verification lost its charged reservation ownership")
                if execute:
                    row.indexUuid = mapping.restored_index_uuid
                    row.operationId = verifiedId
                    row.state = "closing"
                    row.lastUsedAt = LexicalCoordination.getDatabaseTime(db)
            receipt["status"] = "verified"
            self._projectControl(receipt, control)

    def _release(self, plan: _RestorePlan, bindings: tuple[_Binding, ...], mappings: dict[str, _IdentityMapping],
                 live: NativeLifecycleSnapshot, receipt: dict[str, object], operationId: str, epoch: int, planHash: str, execute: bool) -> None:
        with self.sessions.begin() as db:
            control: LexicalBackendControl = self._checkControl(db, plan, epoch, lock=True)
            if self._captureBindings(db, plan) != bindings:
                raise _KnownRefusal("Current published coverage changed before reservation release")
            for binding in bindings:
                index: _IndexPlan = binding.index
                mapping: _IdentityMapping = mappings[index.index_name]
                verifiedId: str = self._getReservationId(index, operationId, epoch, planHash, mapping=mapping, coverageHash=binding.coverageHash)
                row: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, index.scope.fingerprint, with_for_update=True)
                if (row is None or row.indexUuid != mapping.restored_index_uuid or row.operationId != verifiedId
                    or row.state not in (*_CHARGED, "closed")):
                    raise _KnownRefusal("Restore release has no durable verified close fence")
                if (not mapping.close_acknowledged or mapping.close_operation_id != verifiedId
                    or live.getIndexUuid(index.index_name) != mapping.restored_index_uuid or not live.isClosed(index.index_name)):
                    raise _KnownRefusal("Restore release requires acknowledged and observed exact-identity closure")
                if execute:
                    row.state = "closed"
                    # CLOSED releases the charge. Keep terminal proof for an
                    # idempotent same-operation receipt; ordinary admission
                    # overwrites this ID when it reserves a future open.
                    row.operationId = verifiedId
                    row.lastUsedAt = LexicalCoordination.getDatabaseTime(db)
            receipt["status"] = "released" if execute else "retained"
            self._projectControl(receipt, control)

    def _markUnknown(self, plan: _RestorePlan, operationId: str, epoch: int, planHash: str,
                     attestation: _Attestation | None) -> tuple[bool, dict[str, dict[str, object]]]:
        try:
            with self.sessions.begin() as db:
                self._checkControl(db, plan, epoch, lock=True)
                bindings: tuple[_Binding, ...] = self._captureBindings(db, plan)
                mappings: dict[str, _IdentityMapping] = {item.index_name: item for item in attestation.identity_mapping} if attestation else {}
                changed: bool = False
                observed: dict[str, dict[str, object]] = {}
                for binding in bindings:
                    index: _IndexPlan = binding.index
                    identities: set[str] = {self._getReservationId(index, operationId, epoch, planHash)}
                    if index.index_name in mappings:
                        identities.add(self._getReservationId(index, operationId, epoch, planHash,
                            mapping=mappings[index.index_name], coverageHash=binding.coverageHash))
                    row: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, index.scope.fingerprint, with_for_update=True)
                    if row is not None and row.operationId in identities and row.state in _CHARGED:
                        row.state = "uncertain"
                        changed = True
                        observed[index.index_name] = {"reservation_id": row.operationId,
                            "registered_index_uuid": row.indexUuid, "state": row.state}
                return changed, observed
        except Exception:
            # Existing opening/closing/uncertain rows remain charged even when
            # the warning cannot be persisted under a changed gate or DB fault.
            return False, {}
