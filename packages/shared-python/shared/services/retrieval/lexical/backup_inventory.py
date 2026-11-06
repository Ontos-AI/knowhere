"""Read-only authoritative backup selections and independent restored payloads.

Coverage v1 commits every unit metadata field and exact channel term frequencies.
It deliberately makes no claim about raw token order or byte identity. Archived
selections are recorded; absence of retired physical records is not asserted.
Closed source indexes are never opened and metadata alone never verifies data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import ssl
from typing import cast
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.runtime import OpenSearchRuntime
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.lexical.transition import LexicalTransition
from shared.services.retrieval.lexical.unit import LexicalUnit
from shared.services.retrieval.scoring.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
)

_ALGORITHM: str = "unit-metadata-and-channel-term-frequencies-v1"
_NAME: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,254}$")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LexicalIndexError("Backup metadata must be a JSON object")
    return cast(dict[str, object], value)


def _records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LexicalIndexError("Backup metadata must contain a record list")
    return [_object(item) for item in value]


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise LexicalIndexError("Backup identity must be a nonempty string")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LexicalIndexError("Backup counts must be nonnegative integers")
    return value


def _time(value: datetime | None) -> str | None:
    return (
        value.replace(tzinfo=value.tzinfo or timezone.utc)
        .astimezone(timezone.utc)
        .isoformat()
        if value
        else None
    )


class _Coverage:
    def __init__(self, documentId: str, revisionId: str) -> None:
        self.documentId: str = documentId
        self.revisionId: str = revisionId
        self.unitCount: int = 0
        self.tokenRows: int = 0
        self.lastUnit: str | None = None
        self.metadata = sha256((_ALGORITHM + ":metadata\n").encode())
        self.tokens = sha256((_ALGORITHM + ":tokens\n").encode())

    def add(
        self, metadata: dict[str, object], path: Counter[str], content: Counter[str]
    ) -> None:
        identity: str = _string(metadata.get("unitId"))
        if (
            metadata.get("documentId") != self.documentId
            or metadata.get("revisionId") != self.revisionId
            or self.lastUnit is not None
            and identity <= self.lastUnit
        ):
            raise LexicalIndexError(
                "Backup unit identity/order escapes its frozen revision"
            )
        if any(
            not token or frequency < 1
            for channel in (path, content)
            for token, frequency in channel.items()
        ):
            raise LexicalIndexError("Backup term frequencies are invalid")
        self.lastUnit = identity
        self.unitCount += 1
        self.tokenRows += len(path) + len(content)
        self.metadata.update(
            _canonical(
                {
                    **metadata,
                    "path_token_count": sum(path.values()),
                    "content_token_count": sum(content.values()),
                }
            )
            + b"\n"
        )
        self.tokens.update(
            _canonical(
                {
                    "unit_id": identity,
                    "path": sorted(path.items()),
                    "content": sorted(content.items()),
                }
            )
            + b"\n"
        )

    def addUnit(self, unit: LexicalUnit) -> None:
        metadata: dict[str, object] = cast(
            dict[str, object], unit.model_dump(exclude={"pathTokens", "contentTokens"})
        )
        self.add(
            metadata,
            Counter(unit.pathTokens.split()),
            Counter(unit.contentTokens.split()),
        )

    def result(self) -> dict[str, object]:
        return {
            "unit_count": self.unitCount,
            "unit_metadata_sha256": self.metadata.hexdigest(),
            "tokens_sha256": self.tokens.hexdigest(),
        }


@dataclass(frozen=True)
class _Input:
    record: dict[str, object]
    key: str | None
    checksum: str | None
    compiler: str
    hasRetainedInput: bool


class LexicalBackupInventory:
    def __init__(
        self,
        *,
        sessionFactory: sessionmaker[Session],
        clientFactory: Callable[[str], httpx.Client] | None = None,
    ) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory
        self.clients: Callable[[str], httpx.Client] = clientFactory or (
            lambda endpoint: OpenSearchRuntime.resolve(endpoint).client
        )

    @contextmanager
    def _snapshot(self) -> Iterator[Session]:
        with self.sessions.begin() as db:
            db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            if not db.scalar(
                text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            ):
                raise LexicalIndexError(
                    "Backup inventory requires an authorized full-RLS maintenance role"
                )
            yield db

    @staticmethod
    def _control(db: Session) -> dict[str, object]:
        control: LexicalBackendControl | None = db.get(LexicalBackendControl, 1)
        if control is None:
            raise LexicalIndexError("Backup source has no backend identity")
        return {
            "backend": control.backend,
            "mode": control.mode,
            "epoch": control.epoch,
            "cluster_uuid": control.clusterUuid,
            "node_incarnation": control.nodeIncarnation,
        }

    @staticmethod
    def _catalog(db: Session) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for row in db.scalars(
            select(LexicalNamespaceIndex).order_by(LexicalNamespaceIndex.indexName)
        ):
            if row.corpusKind not in ("private", "demo"):
                raise LexicalIndexError(
                    "Backup catalog contains an unknown corpus kind"
                )
            scope: LexicalScope = LexicalScope(
                ownerId=row.ownerId,
                namespace=row.namespace,
                corpusKind="demo" if row.corpusKind == "demo" else "private",
            )
            if scope.fingerprint != row.id or not _NAME.fullmatch(row.indexName):
                raise LexicalIndexError(
                    "Backup catalog scope or native name is inconsistent"
                )
            records.append(
                {
                    "namespace_index_id": row.id,
                    "owner_id": row.ownerId,
                    "namespace": row.namespace,
                    "corpus_kind": row.corpusKind,
                    "index_name": row.indexName,
                    "index_uuid": row.indexUuid,
                    "state": row.state,
                    "operation_id": row.operationId,
                    "last_used_at": _time(row.lastUsedAt),
                }
            )
        return records

    @staticmethod
    def _documents(db: Session) -> list[dict[str, object]]:
        documents: list[dict[str, object]] = []
        for kind in ("PRIVATE", "DEMO"):
            storage: CorpusStorage = CorpusStorage(
                "DEMO" if kind == "DEMO" else "PRIVATE"
            )
            model = storage.Document
            for row in db.execute(
                select(
                    model.document_id,
                    model.user_id,
                    model.namespace,
                    model.status,
                    model.current_job_result_id,
                    model.current_indexing_build_id,
                    model.updated_at,
                    model.archived_at,
                ).order_by(model.document_id)
            ):
                documents.append(
                    {
                        "corpus_kind": "demo" if storage.is_demo else "private",
                        "owner_id": row.user_id,
                        "namespace": row.namespace,
                        "document_id": row.document_id,
                        "status": row.status,
                        "revision_id": row.current_job_result_id,
                        "build_id": row.current_indexing_build_id,
                        "updated_at": _time(row.updated_at),
                        "archived_at": _time(row.archived_at),
                    }
                )
        return documents

    def export(
        self, *, expectedEpoch: int, expectedClusterUuid: str, expectedIncarnation: str
    ) -> dict[str, object]:
        inputs: list[_Input] = []
        builds: list[dict[str, object]] = []
        with self._snapshot() as db:
            control: dict[str, object] = self._control(db)
            if (
                control["backend"] not in ("postgresql", "opensearch")
                or control["mode"] not in ("serving", "gated")
                or control["epoch"] != expectedEpoch
                or control["cluster_uuid"] != expectedClusterUuid
                or control["node_incarnation"] != expectedIncarnation
            ):
                raise LexicalIndexError(
                    "Backup source backend, epoch or native identity changed"
                )
            captured: datetime = db.scalars(select(func.transaction_timestamp())).one()
            catalog: list[dict[str, object]] = self._catalog(db)
            byScope: dict[str, dict[str, object]] = {
                _string(row["namespace_index_id"]): row for row in catalog
            }
            inventory: list[dict[str, object]] = []
            for row in catalog:
                if (
                    row["state"] == "uncreated"
                    and row["index_uuid"] is None
                    and row["operation_id"] is None
                ):
                    continue
                if (
                    row["state"] not in ("open", "closed")
                    or not row["index_uuid"]
                    or row["operation_id"] is not None
                ):
                    raise LexicalIndexError(
                        "Backup catalog has an unresolved native lifecycle; retry after reconciliation",
                        isRetryable=True,
                    )
                inventory.append(
                    {
                        "name": row["index_name"],
                        "uuid": row["index_uuid"],
                        "state": row["state"],
                        "primary_shards": 1,
                    }
                )
            documents: list[dict[str, object]] = self._documents(db)
            for document in documents:
                if document["status"] != "active" or document["revision_id"] is None:
                    continue
                state: _Input = self._readBuild(
                    db,
                    document,
                    byScope,
                    isPostgresqlPrimary=control["backend"] == "postgresql",
                )
                if control["backend"] == "postgresql":
                    state.record["postgresql_source_coverage"] = (
                        self._postgresqlCoverage(db, state)
                    )
                if state.hasRetainedInput:
                    inputs.append(state)
                else:
                    builds.append(
                        {
                            **state.record,
                            **self._postgresqlCoverage(db, state),
                            "fingerprint_source": "postgresql-frequencies",
                        }
                    )
        # No SQL transaction or publication/catalog lock spans object I/O.
        for state in inputs:
            if state.key is None or state.checksum is None:
                raise LexicalIndexError(
                    "Retained backup input has no immutable key/checksum"
                )
            payload: LexicalInput = LexicalInputStorage().load(
                key=state.key, checksum=state.checksum
            )
            if payload.compilerFingerprint != state.compiler:
                raise LexicalIndexError(
                    "Backup retained compiler differs from its assigned revision"
                )
            coverage: _Coverage = _Coverage(
                _string(state.record["document_id"]),
                _string(state.record["revision_id"]),
            )
            for unit in sorted(payload.units, key=lambda item: item.unitId):
                coverage.addUnit(unit)
            if coverage.unitCount != state.record["unit_count"]:
                raise LexicalIndexError(
                    "Backup retained input count differs from its published build"
                )
            if (
                "postgresql_source_coverage" in state.record
                and coverage.result() != state.record["postgresql_source_coverage"]
            ):
                raise LexicalIndexError(
                    "Native shadow source differs from the current published PG frequencies"
                )
            builds.append(
                {
                    **state.record,
                    **coverage.result(),
                    "fingerprint_source": "assigned-input",
                }
            )
        builds.sort(
            key=lambda row: (_string(row["index_name"]), _string(row["build_id"]))
        )
        capturedMillis: int = int(captured.timestamp() * 1000)
        boundary: dict[str, object] = {
            "version": 1,
            "captured_at_millis": capturedMillis,
            "control": control,
            "coverage_algorithm": _ALGORITHM,
            "token_order_identity_claimed": False,
            "retired_record_absence_claimed": False,
            "catalog": catalog,
            "documents": documents,
            "builds": builds,
        }
        return {
            "version": 1,
            "mode": "actual",
            "status": "exported",
            "sourceHash": LexicalTransition.getSourceHash(),
            "control": control,
            "captured_at_millis": capturedMillis,
            "inventory": inventory,
            "boundary": boundary,
            "inventory_sha256": _digest(inventory),
            "boundary_sha256": _digest(boundary),
            "data_verified": False,
            "errors": [],
        }

    def verifyRestored(
        self,
        exportedReceipt: dict[str, object],
        *,
        endpoint: str,
        indexMapping: dict[str, str],
        admissionProof: dict[str, object],
        expectedSnapshotUuid: str,
        selectedIndices: Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Verify a reserved offline restore, without opening or rebuilding anything.

        The proof binds the whole frozen export even when this call checks one
        bounded batch. Current PG drift is reported separately from historical
        payload integrity. Callers must aggregate every batch before declaring
        the complete snapshot data verified.
        """
        receipt: dict[str, object] = {
            "version": 1,
            "mode": "actual",
            "status": "unverified",
            "data_verified": False,
            "inventory_sha256": exportedReceipt.get("inventory_sha256"),
            "boundary_sha256": exportedReceipt.get("boundary_sha256"),
            "verified_builds": [],
            "selected_indices": [],
            "complete_inventory_verified": False,
            "errors": [],
        }
        try:
            boundary: dict[str, object] = _object(exportedReceipt.get("boundary"))
            inventory: list[dict[str, object]] = _records(
                exportedReceipt.get("inventory")
            )
            if (
                exportedReceipt.get("version") != 1
                or exportedReceipt.get("status") != "exported"
                or exportedReceipt.get("mode") != "actual"
                or exportedReceipt.get("errors") != []
                or _digest(boundary) != exportedReceipt.get("boundary_sha256")
                or _digest(inventory) != exportedReceipt.get("inventory_sha256")
                or boundary.get("coverage_algorithm") != _ALGORITHM
            ):
                raise LexicalIndexError(
                    "Frozen backup export or coverage algorithm is invalid"
                )
            originalNames: set[str] = {_string(row.get("name")) for row in inventory}
            selected: set[str] = (
                set(selectedIndices) if selectedIndices is not None else originalNames
            )
            if (
                not selected
                or not selected <= originalNames
                or len(originalNames) != len(inventory)
                or set(indexMapping) != selected
                or len(set(indexMapping.values())) != len(selected)
            ):
                raise LexicalIndexError(
                    "Restored verification batch does not match the frozen inventory"
                )
            proof: dict[str, dict[str, object]] = self._validateAdmission(
                exportedReceipt, admissionProof, indexMapping, expectedSnapshotUuid
            )
            receipt.update(self._currentDrift(boundary))
            receipt["selected_indices"] = sorted(selected)
            receipt["snapshot_uuid"] = expectedSnapshotUuid
            receipt["operation_id"] = admissionProof["operation_id"]
            receipt["batch_reservation_id"] = admissionProof["batch_reservation_id"]
            receipt["target_cluster_uuid"] = admissionProof["target_cluster_uuid"]
            receipt["target_incarnation"] = admissionProof["target_incarnation"]
            client: httpx.Client = self.clients(endpoint)
            receipt["system_open_overhead"] = self._assertTarget(
                client, admissionProof, proof
            )
            verified: list[dict[str, object]] = []
            for build in _records(boundary.get("builds")):
                name: str = _string(build.get("index_name"))
                if name not in selected:
                    continue
                coverage: dict[str, object] = self._readRestoredBuild(
                    client, indexMapping[name], build
                )
                if any(coverage[key] != build.get(key) for key in coverage):
                    raise LexicalIndexError(
                        "Restored unit metadata or term frequencies differ from the frozen boundary"
                    )
                verified.append(
                    {"build_id": build["build_id"], "index_name": name, **coverage}
                )
            receipt["system_open_overhead"] = self._assertTarget(
                client, admissionProof, proof
            )
            receipt.update(
                {
                    "status": "verified",
                    "data_verified": True,
                    "verified_builds": verified,
                    "complete_inventory_verified": selected == originalNames,
                }
            )
        except (LexicalIndexError, ValidationError, ValueError, KeyError) as error:
            receipt["errors"] = [
                str(error)
                if isinstance(error, LexicalIndexError)
                else "Invalid restored backup metadata"
            ]
        return receipt

    @staticmethod
    def _validateAdmission(
        exported: dict[str, object],
        proof: dict[str, object],
        mapping: dict[str, str],
        snapshot: str,
    ) -> dict[str, dict[str, object]]:
        source: dict[str, object] = _object(exported.get("control"))
        if (
            proof.get("version") != 1
            or proof.get("purpose") != "offline-backup-verification"
            or proof.get("execute") is not True
            or proof.get("status") != "ready"
            or proof.get("source_cluster_uuid") != source.get("cluster_uuid")
            or proof.get("target_cluster_uuid") == source.get("cluster_uuid")
            or proof.get("target_incarnation") == source.get("node_incarnation")
            or proof.get("snapshot_uuid") != snapshot
            or not snapshot
            or proof.get("inventory_sha256") != exported.get("inventory_sha256")
            or proof.get("boundary_sha256") != exported.get("boundary_sha256")
            or proof.get("cap") != 400
            or _integer(proof.get("charged_after")) > 400
        ):
            raise LexicalIndexError(
                "Offline restore admission does not bind this source, snapshot, target and capacity"
            )
        _string(proof.get("target_cluster_uuid"))
        _string(proof.get("target_incarnation"))
        operation: UUID = UUID(_string(proof.get("operation_id")))
        UUID(_string(proof.get("batch_reservation_id")))
        rows: list[dict[str, object]] = _records(proof.get("indices"))
        byName: dict[str, dict[str, object]] = {
            _string(row.get("original_index_name")): row for row in rows
        }
        if (
            len(byName) != len(rows)
            or set(byName) != set(mapping)
            or len(rows) > _integer(proof.get("charged_after"))
        ):
            raise LexicalIndexError(
                "Offline restore batch reservations do not cover the selected indexes"
            )
        for original, target in mapping.items():
            row: dict[str, object] = byName[original]
            if (
                not _NAME.fullmatch(target)
                or target != f"verify-{operation.hex}-{original}"
                or row.get("index_name") != target
                or row.get("state") != "open"
            ):
                raise LexicalIndexError(
                    "Offline restored index escapes its operation reservation"
                )
            _string(row.get("index_uuid"))
        return byName

    def _currentDrift(self, boundary: dict[str, object]) -> dict[str, object]:
        with self._snapshot() as db:
            documents: list[dict[str, object]] = self._documents(db)
        frozen: set[tuple[object, ...]] = {
            (
                row.get("corpus_kind"),
                row.get("document_id"),
                row.get("revision_id"),
                row.get("published_build_id", row.get("build_id")),
            )
            for row in _records(boundary.get("builds"))
        }
        missing: list[dict[str, object]] = [
            row
            for row in documents
            if row["status"] == "active"
            and row["revision_id"] is not None
            and (
                row["corpus_kind"],
                row["document_id"],
                row["revision_id"],
                row["build_id"],
            )
            not in frozen
        ]
        return {
            "current_boundary_matches": documents == boundary.get("documents"),
            "current_coverage_complete": not missing,
            "missing_current_builds": missing,
        }

    @staticmethod
    def _request(
        client: httpx.Client,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            with client.stream(method, path, json=body, timeout=10.0) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size: int = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > settings.KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES:
                        raise LexicalIndexError(
                            "Restored verification response exceeds the indexing input budget"
                        )
                    chunks.append(chunk)
                return _object(json.loads(b"".join(chunks)))
        except (httpx.HTTPError, ssl.SSLError) as error:
            raise LexicalIndexError(
                "Offline restored native target could not be read", isRetryable=True
            ) from error

    def _assertTarget(
        self,
        client: httpx.Client,
        admission: dict[str, object],
        proof: dict[str, dict[str, object]],
    ) -> int:
        state: NativeLifecycleSnapshot = NativeLifecycleSnapshot.model_validate(
            self._request(
                client,
                "GET",
                OpenSearchLexicalIndex.buildLifecyclePath("", isExact=False),
            )
        )
        if (
            state.cluster_uuid != admission["target_cluster_uuid"]
            or state.getIncarnation() != admission["target_incarnation"]
            or sum(
                state.isOpen(name)
                for name in state.metadata.indices
                if not name.startswith(".")
            )
            > _integer(admission["charged_after"])
        ):
            raise LexicalIndexError(
                "Offline verification target identity or live capacity changed"
            )
        for row in proof.values():
            name: str = _string(row["index_name"])
            if state.getIndexUuid(name) != row["index_uuid"] or not state.isReady(name):
                raise LexicalIndexError(
                    "Offline restored index identity or readiness changed"
                )
        return sum(
            state.isOpen(name)
            for name in state.metadata.indices
            if name.startswith(".")
        )

    def _readRestoredBuild(
        self, client: httpx.Client, indexName: str, build: dict[str, object]
    ) -> dict[str, object]:
        buildId: str = _string(build.get("build_id"))
        coverage: _Coverage = _Coverage(
            _string(build.get("document_id")), _string(build.get("revision_id"))
        )
        after: list[str] | None = None
        while True:
            body: dict[str, object] = {
                "query": {"term": {"buildId": buildId}},
                "size": 1000,
                "sort": [{"unitId": "asc"}],
                "track_total_hits": True,
                "_source": True,
            }
            if after is not None:
                body["search_after"] = after
            response: dict[str, object] = self._request(
                client, "POST", f"/{indexName}/_search", body
            )
            hits: dict[str, object] = _object(response.get("hits"))
            total: dict[str, object] = _object(hits.get("total"))
            if (
                response.get("timed_out") is not False
                or _object(response.get("_shards")).get("failed") != 0
                or total.get("relation") != "eq"
                or total.get("value") != build.get("unit_count")
            ):
                raise LexicalIndexError("Restored native build coverage is incomplete")
            rows: list[dict[str, object]] = _records(hits.get("hits"))
            if not rows:
                break
            for row in rows:
                source: dict[str, object] = dict(_object(row.get("_source")))
                if source.pop("buildId", None) != buildId:
                    raise LexicalIndexError(
                        "Restored unit belongs to another native build"
                    )
                if set(source) != set(LexicalUnit.model_fields):
                    raise LexicalIndexError(
                        "Restored unit metadata fields are incomplete"
                    )
                unit: LexicalUnit = LexicalUnit.model_validate(source, strict=True)
                if row.get("_id") != _digest([buildId, unit.unitId]) or row.get(
                    "sort"
                ) != [unit.unitId]:
                    raise LexicalIndexError(
                        "Restored unit identity or pagination is invalid"
                    )
                coverage.addUnit(unit)
                if coverage.unitCount > _integer(build.get("unit_count")):
                    raise LexicalIndexError(
                        "Restored native build contains excess units"
                    )
            after = [_string(_object(rows[-1].get("_source")).get("unitId"))]
        return coverage.result()

    @staticmethod
    def _readBuild(
        db: Session,
        document: dict[str, object],
        catalog: dict[str, dict[str, object]],
        *,
        isPostgresqlPrimary: bool,
    ) -> _Input:
        kind: str = _string(document["corpus_kind"])
        scope: LexicalScope = LexicalScope(
            ownerId=_string(document["owner_id"]),
            namespace=_string(document["namespace"]),
            corpusKind="demo" if kind == "demo" else "private",
        )
        registry: dict[str, object] | None = catalog.get(scope.fingerprint)
        build: LexicalBuild | None
        if isPostgresqlPrimary:
            shadows: list[LexicalBuild] = list(
                db.scalars(
                    select(LexicalBuild)
                    .where(
                        LexicalBuild.documentId == document["document_id"],
                        LexicalBuild.revisionId == document["revision_id"],
                        LexicalBuild.namespaceIndexId == scope.fingerprint,
                        LexicalBuild.state == "published",
                    )
                    .limit(2)
                )
            )
            if len(shadows) != 1:
                raise LexicalIndexError(
                    "Current PG published revision requires one unambiguous prepared native shadow"
                )
            build = shadows[0]
        else:
            build = db.get(LexicalBuild, document["build_id"])
        if (
            registry is None
            or build is None
            or build.state != "published"
            or build.namespaceIndexId != scope.fingerprint
            or build.documentId != document["document_id"]
            or build.revisionId != document["revision_id"]
        ):
            raise LexicalIndexError(
                "Current published selection has no matching native build/catalog"
            )
        result = db.execute(
            select(
                JobResult.indexing_input_key,
                JobResult.indexing_input_checksum,
                JobResult.indexing_input_compiler,
                JobResult.indexing_input_gc_state,
                JobResult.document_id,
                JobResult.demo_document_id,
            ).where(JobResult.id == build.revisionId)
        ).one_or_none()
        if (
            result is None
            or not result.indexing_input_compiler
            or (result.demo_document_id if kind == "demo" else result.document_id)
            != build.documentId
        ):
            raise LexicalIndexError(
                "Published backup revision/assigned compiler is inconsistent"
            )
        record: dict[str, object] = {
            "corpus_kind": kind,
            "owner_id": scope.ownerId,
            "namespace": scope.namespace,
            "namespace_index_id": scope.fingerprint,
            "index_name": registry["index_name"],
            "index_uuid": registry["index_uuid"],
            "document_id": build.documentId,
            "revision_id": build.revisionId,
            "build_id": build.id,
            "published_build_id": document["build_id"],
            "selection": "native-shadow" if isPostgresqlPrimary else "native-binding",
            "unit_count": build.unitCount,
            "compiler_fingerprint": result.indexing_input_compiler,
            "assigned_input_sha256": result.indexing_input_checksum,
            "activation_generation": build.activationGeneration,
            "legacy_copy_state": build.legacyCopyState,
        }
        return _Input(
            record,
            result.indexing_input_key,
            result.indexing_input_checksum,
            result.indexing_input_compiler,
            result.indexing_input_gc_state == "retained"
            and result.indexing_input_key is not None,
        )

    @staticmethod
    def _postgresqlCoverage(db: Session, state: _Input) -> dict[str, object]:
        storage: CorpusStorage = CorpusStorage(
            "DEMO" if state.record["corpus_kind"] == "demo" else "PRIVATE"
        )
        documentId: str = _string(state.record["document_id"])
        revisionId: str = _string(state.record["revision_id"])
        if state.record["legacy_copy_state"] != "completed":
            raise LexicalIndexError(
                "Collected backup input has no completed frozen PG copy"
            )
        marker = db.scalar(
            select(storage.DocumentMapUnitIndex).where(
                storage.DocumentMapUnitIndex.document_id == documentId,
                storage.DocumentMapUnitIndex.job_result_id == revisionId,
            )
        )
        if (
            marker is None
            or marker.format_version != MAP_UNIT_INDEX_FORMAT_VERSION
            or marker.unit_count != state.record["unit_count"]
        ):
            raise LexicalIndexError("Backup PG format/cardinality marker is incomplete")
        unit = storage.DocumentMapUnit
        section = storage.DocumentSection
        token = storage.DocumentMapUnitToken
        rows = db.execute(
            select(
                unit.id,
                unit.unit_id,
                unit.section_id,
                unit.path_token_count,
                unit.content_token_count,
                unit.sort_order,
                unit.has_image,
                unit.has_table,
                section.section_path,
                token.channel,
                token.token,
                token.token_hash,
                token.frequency,
            )
            .outerjoin(
                section,
                (section.section_id == unit.section_id)
                & (section.document_id == unit.document_id)
                & (section.job_result_id == unit.job_result_id),
            )
            .outerjoin(token, token.map_unit_id == unit.id)
            .where(unit.document_id == documentId, unit.job_result_id == revisionId)
            .order_by(unit.unit_id, token.channel, token.token)
            .execution_options(yield_per=1000)
        )
        coverage: _Coverage = _Coverage(documentId, revisionId)
        current: str | None = None
        metadata: dict[str, object] = {}
        path: Counter[str] = Counter()
        content: Counter[str] = Counter()
        pathCount: int = 0
        contentCount: int = 0
        statistics: list[int] = [0, 0, 0, 0]

        def flush() -> None:
            if current is not None:
                if (
                    sum(path.values()) != pathCount
                    or sum(content.values()) != contentCount
                ):
                    raise LexicalIndexError(
                        "Backup PG frequency totals differ from prepared units"
                    )
                statistics[0] += pathCount
                statistics[1] += contentCount
                statistics[2] += bool(pathCount)
                statistics[3] += bool(contentCount)
                coverage.add(metadata, path, content)

        for row in rows:
            if row.unit_id != current:
                flush()
                current = row.unit_id
                if row.section_path is None:
                    raise LexicalIndexError(
                        "Backup PG unit has no canonical revision section"
                    )
                metadata = {
                    "documentId": documentId,
                    "revisionId": revisionId,
                    "unitId": row.unit_id,
                    "sectionId": row.section_id,
                    "sectionPath": row.section_path,
                    "sortOrder": row.sort_order,
                    "hasImage": row.has_image,
                    "hasTable": row.has_table,
                }
                path, content = Counter(), Counter()
                pathCount, contentCount = row.path_token_count, row.content_token_count
            if row.channel is not None:
                if (
                    row.channel not in ("path", "content")
                    or row.frequency < 1
                    or row.token_hash != sha256(row.token.encode()).hexdigest()
                ):
                    raise LexicalIndexError(
                        "Backup PG channel/frequency/hash row is invalid"
                    )
                channel: Counter[str] = path if row.channel == "path" else content
                if row.token in channel:
                    raise LexicalIndexError(
                        "Backup PG contains duplicate term-frequency rows"
                    )
                channel[row.token] = row.frequency
        flush()
        if (
            coverage.unitCount != marker.unit_count
            or coverage.tokenRows != marker.token_count
            or statistics
            != [
                marker.path_total_length,
                marker.content_total_length,
                marker.path_document_count,
                marker.content_document_count,
            ]
        ):
            raise LexicalIndexError("Backup PG unit/token cardinalities are incomplete")
        return coverage.result()
