"""Durable, owner-fenced capacity for independent snapshot verification.

No network operation or elapsed-time takeover belongs here. A reservation is
committed before restore; missing clones remain charged. A closing row preserves
its verified-payload fence even when closure becomes unknown, so an uncertain
unverified restore cannot forge eligibility for release.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from hashlib import sha256
import json
import re
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid5

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker
    from shared.models.database.lexicalofflinereservation import (
        LexicalOfflineReservation,
    )

_NAME: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,254}$")
_HASH: re.Pattern[str] = re.compile(r"^[a-f0-9]{64}$")
_BINDINGS: tuple[str, ...] = (
    "operation_id",
    "issuer_token",
    "snapshot_uuid",
    "inventory_sha256",
    "boundary_sha256",
    "target_incarnation",
    "target_cluster_uuid",
    "source_cluster_uuid",
    "batch_reservation_id",
)


def _string(value: object, *, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(
            "Offline reservation identity must be a bounded nonempty string"
        )
    return value


def _records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Offline reservation indices must be a record list")
    return cast(list[dict[str, object]], value)


def _nativeNames(proof: dict[str, object]) -> tuple[str, ...]:
    names: object = proof.get("native_open_names", [])
    if not isinstance(names, (list, tuple)):
        raise ValueError("Offline native capacity inventory must be a name list")
    return tuple(_string(name) for name in names)


def _parsePlan(value: dict[str, object]) -> dict[str, object]:
    if (
        value.get("version") != 1
        or value.get("purpose") != "offline-backup-verification"
    ):
        raise ValueError(
            "Offline reservation requires the exact independent verification purpose"
        )
    plan: dict[str, object] = {"version": 1, "purpose": "offline-backup-verification"}
    for name in (
        "source_cluster_uuid",
        "target_cluster_uuid",
        "target_incarnation",
        "snapshot_uuid",
    ):
        plan[name] = _string(value.get(name), maximum=64)
    if plan["source_cluster_uuid"] == plan["target_cluster_uuid"]:
        raise ValueError("Offline verification target must differ from the source")
    for name in ("operation_id", "batch_reservation_id"):
        identifier: str = _string(value.get(name), maximum=36)
        if str(UUID(identifier)) != identifier:
            raise ValueError(
                "Offline operation and batch identities must be canonical UUIDs"
            )
        plan[name] = identifier
    for name in ("inventory_sha256", "boundary_sha256"):
        digest: str = _string(value.get(name), maximum=64)
        if not _HASH.fullmatch(digest):
            raise ValueError(
                "Offline reservation must bind exact inventory and boundary hashes"
            )
        plan[name] = digest
    rows: list[dict[str, object]] = _records(value.get("indices"))
    if not rows or len(rows) > 400:
        raise ValueError("Offline restore batches require between one and 400 indices")
    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    originals: set[str] = set()
    for row in rows:
        original: str = _string(row.get("original_index_name"))
        name: str = _string(row.get("index_name"))
        if (
            not _NAME.fullmatch(original)
            or not _NAME.fullmatch(name)
            or name != "verify-" + UUID(str(plan["operation_id"])).hex + "-" + original
            or name in names
            or original in originals
        ):
            raise ValueError(
                "Offline clone name does not bind its unique operation and original index"
            )
        names.add(name)
        originals.add(original)
        normalized.append({"original_index_name": original, "index_name": name})
    plan["indices"] = normalized
    return plan


def _project(row: LexicalOfflineReservation) -> dict[str, object]:
    return {
        "operation_id": row.operationId,
        "issuer_token": row.issuerToken,
        "snapshot_uuid": row.snapshotUuid,
        "inventory_sha256": row.inventorySha256,
        "boundary_sha256": row.boundarySha256,
        "target_incarnation": row.targetIncarnation,
        "target_cluster_uuid": row.targetClusterUuid,
        "source_cluster_uuid": row.sourceClusterUuid,
        "batch_reservation_id": row.batchReservationId,
    }


class LexicalOfflineAdmission:
    def __init__(self, *, sessionFactory: sessionmaker[Session]) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory

    @staticmethod
    def planCharges(
        nativeOpenNames: Sequence[str],
        chargedNames: Sequence[str],
        selectedNames: Sequence[str],
    ) -> dict[str, object]:
        native: set[str] = {
            name for name in nativeOpenNames if not name.startswith(".")
        }
        charged: set[str] = {name for name in chargedNames if not name.startswith(".")}
        selected: set[str] = set(selectedNames)
        after: int = len(native | charged | selected)
        if after > 400:
            raise ValueError(
                "Offline restore exceeds 400 native/open/reserved/uncertain business slots"
            )
        return {
            "cap": 400,
            "charged_before": len(native | charged),
            "charged_after": after,
            "additional_slots": len(selected - native - charged),
            "system_open_overhead": len(
                {name for name in nativeOpenNames if name.startswith(".")}
            ),
        }

    @staticmethod
    def assertReservationOwner(
        record: dict[str, object], proof: dict[str, object]
    ) -> None:
        if any(record.get(key) != proof.get(key) for key in _BINDINGS):
            raise ValueError(
                "Offline reservation issuer or immutable operation binding differs"
            )

    @staticmethod
    def assertPayloadVerification(
        proof: dict[str, object], receipt: dict[str, object]
    ) -> None:
        fields: tuple[str, ...] = (
            "snapshot_uuid",
            "inventory_sha256",
            "boundary_sha256",
            "target_cluster_uuid",
            "target_incarnation",
            "operation_id",
            "batch_reservation_id",
        )
        selected: set[str] = {
            _string(row.get("original_index_name"))
            for row in _records(proof.get("indices"))
        }
        verified: object = receipt.get("selected_indices")
        if (
            receipt.get("version") != 1
            or receipt.get("status") != "verified"
            or receipt.get("data_verified") is not True
            or receipt.get("errors") != []
            or any(
                not proof.get(key) or receipt.get(key) != proof[key] for key in fields
            )
            or not isinstance(verified, list)
            or any(not isinstance(name, str) for name in verified)
            or set(cast(list[str], verified)) != selected
            or len(verified) != len(selected)
        ):
            raise ValueError(
                "Offline close requires exact independently verified payload provenance"
            )

    @contextmanager
    def _lock(
        self, plan: dict[str, object], issuer: dict[str, object]
    ) -> Iterator[Session]:
        from sqlalchemy import select, text
        from shared.models.database.lexicalcontrol import LexicalBackendControl
        from shared.models.database.lexicaloperatorissuer import LexicalOperatorIssuer
        from shared.services.retrieval.lexical.transition import LexicalTransition

        sourceHash: str = LexicalTransition.getSourceHash()
        if issuer.get("source_hash") != sourceHash or issuer.get("status") != "active":
            raise ValueError(
                "Offline reservation issuer source is not current and active"
            )
        with self.sessions.begin() as db:
            if not db.scalar(
                text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            ):
                raise ValueError(
                    "Offline reservation requires a full-RLS maintenance role"
                )
            control = db.scalar(
                select(LexicalBackendControl)
                .where(LexicalBackendControl.id == 1)
                .with_for_update()
            )
            owner = db.scalar(
                select(LexicalOperatorIssuer)
                .where(LexicalOperatorIssuer.controlId == 1)
                .with_for_update()
            )
            if (
                control is None
                or owner is None
                or owner.state != "active"
                or control.mode not in ("serving", "gated")
                or control.backend not in ("postgresql", "opensearch")
                or control.clusterUuid != plan["source_cluster_uuid"]
                or control.clusterUuid != owner.clusterUuid
                or control.epoch != owner.epoch
                or sourceHash != owner.sourceHash
                or (
                    owner.invocationToken,
                    owner.hostIdentity,
                    owner.operationId,
                    owner.epoch,
                    owner.clusterUuid,
                    owner.logicalClusterId,
                    owner.acquiredAt.isoformat(),
                )
                != (
                    issuer.get("invocation_token"),
                    issuer.get("host_identity"),
                    issuer.get("operation_id"),
                    issuer.get("epoch"),
                    issuer.get("cluster_uuid"),
                    issuer.get("logical_cluster_id"),
                    issuer.get("acquired_at_utc"),
                )
            ):
                raise ValueError(
                    "Offline reservation lost its current physical issuer, epoch or source identity"
                )
            lockKey: int = int.from_bytes(
                sha256(str(plan["target_cluster_uuid"]).encode()).digest()[:8],
                "big",
                signed=True,
            )
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lockKey})
            yield db

    def _load(
        self, db: Session, proof: dict[str, object], issuer: dict[str, object]
    ) -> list[LexicalOfflineReservation]:
        from sqlalchemy import select
        from shared.models.database.lexicalofflinereservation import (
            LexicalOfflineReservation,
        )

        names: list[str] = [
            _string(row.get("index_name")) for row in _records(proof.get("indices"))
        ]
        rows: list[LexicalOfflineReservation] = list(
            db.scalars(
                select(LexicalOfflineReservation)
                .where(
                    LexicalOfflineReservation.targetClusterUuid
                    == proof["target_cluster_uuid"],
                    LexicalOfflineReservation.indexName.in_(names),
                )
                .order_by(LexicalOfflineReservation.indexName)
                .with_for_update()
            )
        )
        if len(rows) != len(names) or proof.get("issuer_token") != issuer.get(
            "invocation_token"
        ):
            raise ValueError(
                "Offline reservation issuer or durable target batch is missing"
            )
        for row in rows:
            self.assertReservationOwner(_project(row), proof)
            entry: dict[str, object] = next(
                item
                for item in _records(proof["indices"])
                if item["index_name"] == row.indexName
            )
            if row.indexUuid is not None and entry.get("index_uuid") != row.indexUuid:
                raise ValueError(
                    "Offline proof changed the durable physical index identity"
                )
        return rows

    def _receipt(
        self,
        db: Session,
        plan: dict[str, object],
        rows: list[LexicalOfflineReservation],
        *,
        status: str,
        nativeOpenNames: Sequence[str],
        issuer: dict[str, object],
    ) -> dict[str, object]:
        from sqlalchemy import select
        from shared.models.database.lexicalofflinereservation import (
            LexicalOfflineReservation,
        )

        db.flush()
        charged: tuple[str, ...] = tuple(
            db.scalars(
                select(LexicalOfflineReservation.indexName).where(
                    LexicalOfflineReservation.targetClusterUuid
                    == plan["target_cluster_uuid"],
                    LexicalOfflineReservation.state != "closed",
                )
            )
        )
        byName: dict[str, LexicalOfflineReservation] = {
            row.indexName: row for row in rows
        }
        indices: list[dict[str, object]] = []
        for entry in _records(plan["indices"]):
            row: LexicalOfflineReservation = byName[_string(entry["index_name"])]
            indices.append(
                {
                    **entry,
                    "index_uuid": row.indexUuid,
                    "state": row.state,
                    "reservation_id": row.batchReservationId,
                }
            )
        return {
            **plan,
            "mode": "actual",
            "execute": True,
            "status": status,
            "issuer_token": issuer["invocation_token"],
            "native_open_names": sorted(set(nativeOpenNames)),
            "indices": indices,
            "errors": [],
            **self.planCharges(nativeOpenNames, charged, ()),
        }

    def reserve(
        self,
        plan: dict[str, object],
        *,
        nativeOpenNames: tuple[str, ...],
        issuer: dict[str, object],
    ) -> dict[str, object]:
        from sqlalchemy import select
        from shared.models.database.lexicalofflinereservation import (
            LexicalOfflineReservation,
        )

        normalized: dict[str, object] = _parsePlan(plan)
        names: tuple[str, ...] = tuple(
            _string(row["index_name"]) for row in _records(normalized["indices"])
        )
        with self._lock(normalized, issuer) as db:
            existing: list[LexicalOfflineReservation] = list(
                db.scalars(
                    select(LexicalOfflineReservation).where(
                        LexicalOfflineReservation.targetClusterUuid
                        == normalized["target_cluster_uuid"]
                    )
                )
            )
            if set(names).intersection(nativeOpenNames) or any(
                row.indexName in names for row in existing
            ):
                raise ValueError(
                    "Offline clone already has an existing identity/reservation; never repeat restore"
                )
            charges: dict[str, object] = self.planCharges(
                nativeOpenNames,
                tuple(row.indexName for row in existing if row.state != "closed"),
                names,
            )
            rows: list[LexicalOfflineReservation] = []
            for name in names:
                row = LexicalOfflineReservation(
                    targetClusterUuid=str(normalized["target_cluster_uuid"]),
                    indexName=name,
                    targetIncarnation=str(normalized["target_incarnation"]),
                    sourceClusterUuid=str(normalized["source_cluster_uuid"]),
                    state="opening",
                    operationId=str(normalized["operation_id"]),
                    issuerToken=str(issuer["invocation_token"]),
                    snapshotUuid=str(normalized["snapshot_uuid"]),
                    batchReservationId=str(normalized["batch_reservation_id"]),
                    inventorySha256=str(normalized["inventory_sha256"]),
                    boundarySha256=str(normalized["boundary_sha256"]),
                )
                db.add(row)
                rows.append(row)
            return {
                **self._receipt(
                    db,
                    normalized,
                    rows,
                    status="reserved",
                    nativeOpenNames=nativeOpenNames,
                    issuer=issuer,
                ),
                **charges,
            }

    def recordReady(
        self,
        proof: dict[str, object],
        *,
        observedIndexes: dict[str, str],
        issuer: dict[str, object],
    ) -> dict[str, object]:
        plan: dict[str, object] = _parsePlan(proof)
        with self._lock(plan, issuer) as db:
            rows: list[LexicalOfflineReservation] = self._load(db, proof, issuer)
            if set(observedIndexes) != {row.indexName for row in rows}:
                raise ValueError("Ready observation must cover every reserved clone")
            for row in rows:
                observed: str = _string(observedIndexes[row.indexName], maximum=64)
                if row.state not in (
                    "opening",
                    "uncertain",
                    "open",
                ) or row.indexUuid not in (None, observed):
                    raise ValueError(
                        "Ready observation changed a reserved identity or verified close fence"
                    )
                row.state, row.indexUuid = "open", observed
            return self._receipt(
                db,
                plan,
                rows,
                status="ready",
                nativeOpenNames=_nativeNames(proof),
                issuer=issuer,
            )

    def beginClose(
        self,
        proof: dict[str, object],
        *,
        verificationReceipt: dict[str, object],
        issuer: dict[str, object],
    ) -> dict[str, object]:
        self.assertPayloadVerification(proof, verificationReceipt)
        plan: dict[str, object] = _parsePlan(proof)
        with self._lock(plan, issuer) as db:
            rows: list[LexicalOfflineReservation] = self._load(db, proof, issuer)
            if any(row.state != "open" or not row.indexUuid for row in rows):
                raise ValueError(
                    "Offline close requires READY identities with an unused verified fence"
                )
            digest: str = sha256(
                json.dumps(
                    verificationReceipt, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            fence: str = str(
                uuid5(
                    UUID(str(plan["operation_id"])),
                    "verified-close:"
                    + str(plan["batch_reservation_id"])
                    + ":"
                    + digest,
                )
            )
            plan["batch_reservation_id"] = fence
            for row in rows:
                row.state, row.batchReservationId = "closing", fence
            return self._receipt(
                db,
                plan,
                rows,
                status="closing",
                nativeOpenNames=_nativeNames(proof),
                issuer=issuer,
            )

    def release(
        self,
        proof: dict[str, object],
        *,
        observedClosed: dict[str, str],
        closeAcknowledged: bool,
        issuer: dict[str, object],
    ) -> dict[str, object]:
        plan: dict[str, object] = _parsePlan(proof)
        with self._lock(plan, issuer) as db:
            rows: list[LexicalOfflineReservation] = self._load(db, proof, issuer)
            if any(
                row.state not in ("closing", "closed") or not row.indexUuid
                for row in rows
            ):
                raise ValueError(
                    "Offline release has no durable verified-payload close fence"
                )
            isClosed: bool = closeAcknowledged is True and observedClosed == {
                row.indexName: row.indexUuid for row in rows
            }
            if isClosed:
                for row in rows:
                    row.state = "closed"
            return self._receipt(
                db,
                plan,
                rows,
                status="closed"
                if all(row.state == "closed" for row in rows)
                else "unknown",
                nativeOpenNames=_nativeNames(proof),
                issuer=issuer,
            )

    def retainUnknown(
        self, proof: dict[str, object], *, issuer: dict[str, object]
    ) -> dict[str, object]:
        plan: dict[str, object] = _parsePlan(proof)
        with self._lock(plan, issuer) as db:
            rows: list[LexicalOfflineReservation] = self._load(db, proof, issuer)
            for row in rows:
                if row.state not in ("closed", "closing"):
                    row.state = "uncertain"
            return self._receipt(
                db,
                plan,
                rows,
                status="closed"
                if all(row.state == "closed" for row in rows)
                else "unknown",
                nativeOpenNames=_nativeNames(proof),
                issuer=issuer,
            )
