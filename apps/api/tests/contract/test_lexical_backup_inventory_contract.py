"""Authoritative PG backup boundaries and independent restored native payloads."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.contract.test_postgresql_primary_contract import (
    postgresqlPrimaryRuntime as postgresqlPrimaryRuntime,
    getContext,
)

if TYPE_CHECKING:
    from shared.services.retrieval.lexical.input import LexicalInput


@dataclass(frozen=True)
class _BackupRuntime:
    engine: Engine
    documentId: str
    revisionId: str
    buildId: str
    payload: LexicalInput


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _records(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_object(item) for item in value]


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture
def backupRuntime(postgresqlPrimaryRuntime) -> _BackupRuntime:
    from shared.models.database.document import Document
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.models.database.demo_corpus import DemoDocument
    from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE, DEMO_OWNER
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    PostgresqlPublication(
        sessionFactory=sessionmaker(engine, expire_on_commit=False),
        deadline=IndexingDeadline(300),
    ).run(context, ownerToken=ownerToken)
    buildId: str = str(uuid4())
    with Session(engine) as db:
        result = db.get(JobResult, context.revisionId)
        document = db.get(Document, context.documentId)
        control = db.get(LexicalBackendControl, 1)
        assert result is not None and document is not None and control is not None
        assert result.indexing_input_key and result.indexing_input_checksum
        payload = LexicalInputStorage().load(
            key=result.indexing_input_key, checksum=result.indexing_input_checksum
        )
        scope = LexicalScope(
            ownerId=context.scope.ownerId, namespace=context.scope.namespace
        )
        db.add(
            LexicalNamespaceIndex(
                id=scope.fingerprint,
                ownerId=scope.ownerId,
                namespace=scope.namespace,
                corpusKind="private",
                indexName=f"knowhere-backup-{scope.fingerprint[:48]}",
                indexUuid="source-index-uuid",
                state="open",
            )
        )
        emptyScope = LexicalScope(
            ownerId=context.scope.ownerId, namespace="retained-empty"
        )
        db.add(
            LexicalNamespaceIndex(
                id=emptyScope.fingerprint,
                ownerId=emptyScope.ownerId,
                namespace=emptyScope.namespace,
                corpusKind="private",
                indexName=f"knowhere-backup-{emptyScope.fingerprint[:48]}",
                indexUuid="closed-index-uuid",
                state="closed",
            )
        )
        db.flush()
        db.add(
            LexicalBuild(
                id=buildId,
                attemptId=context.attemptId,
                namespaceIndexId=scope.fingerprint,
                documentId=context.documentId,
                revisionId=context.revisionId,
                ownerToken=ownerToken,
                state="published",
                unitCount=len(payload.units),
                activationGeneration=1,
                legacyCopyState="completed",
            )
        )
        document.current_indexing_build_id = buildId
        control.backend, control.mode, control.epoch = "opensearch", "serving", 1
        control.clusterUuid, control.nodeIncarnation = "source-cluster", "source-node"
        db.add(
            DemoDocument(
                document_id="backup-demo-archived",
                demo_source_id="backup-demo-source",
                title="Archived demo",
                user_id=DEMO_OWNER,
                namespace=DEMO_NAMESPACE,
                status="archived",
            )
        )
        db.commit()
    return _BackupRuntime(
        engine, context.documentId, context.revisionId, buildId, payload
    )


def _export(runtime: _BackupRuntime) -> dict[str, object]:
    from shared.services.retrieval.lexical.backup_inventory import (
        LexicalBackupInventory,
    )

    return LexicalBackupInventory(
        sessionFactory=sessionmaker(runtime.engine, expire_on_commit=False)
    ).export(
        expectedEpoch=1,
        expectedClusterUuid="source-cluster",
        expectedIncarnation="source-node",
    )


def test_export_captures_all_catalog_and_document_selection_without_tokens(
    backupRuntime: _BackupRuntime,
) -> None:
    exported = _export(backupRuntime)
    assert exported["mode"] == "actual" and exported["status"] == "exported"
    assert exported["inventory_sha256"] == _digest(exported["inventory"])
    assert exported["boundary_sha256"] == _digest(exported["boundary"])
    assert {row["state"] for row in _records(exported["inventory"])} == {
        "open",
        "closed",
    }
    boundary = _object(exported["boundary"])
    assert len(_records(boundary["catalog"])) == 2
    assert {row["corpus_kind"] for row in _records(boundary["documents"])} == {
        "private",
        "demo",
    }
    assert _records(boundary["builds"])[0]["build_id"] == backupRuntime.buildId
    serialized: str = json.dumps(exported)
    assert (
        "pathTokens" not in serialized
        and "contentTokens" not in serialized
        and "frozen evidence" not in serialized
    )
    assert _object(exported["control"])["cluster_uuid"] == "source-cluster"


@pytest.mark.parametrize(
    "field,value",
    [
        ("expectedEpoch", 2),
        ("expectedClusterUuid", "wrong"),
        ("expectedIncarnation", "wrong"),
    ],
)
def test_export_rejects_wrong_epoch_or_native_identity(
    backupRuntime: _BackupRuntime, field: str, value: object
) -> None:
    from shared.services.retrieval.lexical.backup_inventory import (
        LexicalBackupInventory,
    )
    from shared.services.retrieval.lexical.error import LexicalIndexError

    options = {
        "expectedEpoch": 1,
        "expectedClusterUuid": "source-cluster",
        "expectedIncarnation": "source-node",
        field: value,
    }
    with pytest.raises((LexicalIndexError, ValueError)):
        LexicalBackupInventory(
            sessionFactory=sessionmaker(backupRuntime.engine)
        ).export(**options)


def test_gc_input_uses_validated_pg_frequencies_without_retokenization(
    backupRuntime: _BackupRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.preparation import LexicalPreparation

    retained = _records(_object(_export(backupRuntime)["boundary"])["builds"])[0]
    with Session(backupRuntime.engine) as db:
        result = db.get(JobResult, backupRuntime.revisionId)
        assert result is not None
        result.indexing_input_gc_state = "deleted"
        db.commit()

    def refuse(*arguments: object, **options: object) -> None:
        raise AssertionError(
            "Collected input must use validated PG frequencies, never storage or tokenizer"
        )

    monkeypatch.setattr(LexicalInputStorage, "load", refuse)
    monkeypatch.setattr(LexicalPreparation, "prepare", refuse)
    collected = _records(_object(_export(backupRuntime)["boundary"])["builds"])[0]
    assert retained["unit_metadata_sha256"] == collected["unit_metadata_sha256"]
    assert retained["tokens_sha256"] == collected["tokens_sha256"]
    assert collected["fingerprint_source"] == "postgresql-frequencies"


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-marker",
        "missing-token",
        "wrong-frequency",
        "wrong-statistics",
        "wrong-format",
    ],
)
def test_collected_input_requires_complete_pg_proof(
    backupRuntime: _BackupRuntime, corruption: str
) -> None:
    from shared.models.database.document import (
        DocumentMapUnitIndex,
        DocumentMapUnitToken,
    )
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.error import LexicalIndexError

    with Session(backupRuntime.engine) as db:
        result = db.get(JobResult, backupRuntime.revisionId)
        assert result is not None
        result.indexing_input_gc_state = "deleted"
        if corruption == "missing-marker":
            marker = db.scalars(
                select(DocumentMapUnitIndex).where(
                    DocumentMapUnitIndex.job_result_id == backupRuntime.revisionId
                )
            ).one()
            db.delete(marker)
        elif corruption in ("wrong-statistics", "wrong-format"):
            marker = db.scalars(
                select(DocumentMapUnitIndex).where(
                    DocumentMapUnitIndex.job_result_id == backupRuntime.revisionId
                )
            ).one()
            if corruption == "wrong-statistics":
                marker.path_total_length += 1
            else:
                marker.format_version += 1
        else:
            token = db.scalars(select(DocumentMapUnitToken)).first()
            assert token is not None
            if corruption == "missing-token":
                db.delete(token)
            else:
                token.frequency += 1
        db.commit()
    with pytest.raises(LexicalIndexError):
        _export(backupRuntime)


class _RestoredNative:
    def __init__(
        self,
        runtime: _BackupRuntime,
        exported: dict[str, object],
        *,
        corruption: str = "",
    ) -> None:
        self.runtime: _BackupRuntime = runtime
        self.exported: dict[str, object] = exported
        self.corruption: str = corruption
        self.calls: list[str] = []
        self.operationId: str = "00000000-0000-0000-0000-000000000001"
        self.mapping: dict[str, str] = {
            str(row["name"]): "verify-00000000000000000000000000000001-"
            + str(row["name"])
            for row in _records(exported["inventory"])
        }
        self.indices: list[dict[str, object]] = [
            {
                "original_index_name": row["name"],
                "index_name": self.mapping[str(row["name"])],
                "index_uuid": "restored-" + str(row["uuid"]),
                "state": "open",
            }
            for row in _records(exported["inventory"])
        ]
        self.proof: dict[str, object] = {
            "version": 1,
            "purpose": "offline-backup-verification",
            "execute": True,
            "status": "ready",
            "source_cluster_uuid": "source-cluster",
            "target_cluster_uuid": "offline-cluster",
            "target_incarnation": "offline-node",
            "snapshot_uuid": "snapshot-backup-test",
            "inventory_sha256": exported["inventory_sha256"],
            "boundary_sha256": exported["boundary_sha256"],
            "operation_id": self.operationId,
            "batch_reservation_id": "00000000-0000-0000-0000-000000000002",
            "cap": 400,
            "charged_after": len(self.indices),
            "indices": self.indices,
        }

    def respond(self, request: httpx.Request) -> httpx.Response:
        with Session(self.runtime.engine) as db:
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='idle in transaction'"
                    )
                )
                == 0
            )
        self.calls.append(request.method + " " + request.url.path)
        if request.method == "GET":
            metadata = {
                str(row["index_name"]): {
                    "state": "open",
                    "settings": {"index": {"uuid": row["index_uuid"]}},
                }
                for row in self.indices
            }
            if self.corruption == "system-index":
                metadata[".opendistro_security"] = {
                    "state": "open",
                    "settings": {"index": {"uuid": "security-system"}},
                }
            routing = {
                str(row["index_name"]): {
                    "shards": {"0": [{"primary": True, "state": "STARTED"}]}
                }
                for row in self.indices
            }
            return httpx.Response(
                200,
                json={
                    "cluster_uuid": "wrong-target"
                    if self.corruption == "identity"
                    else "offline-cluster",
                    "nodes": {"offline": {"ephemeral_id": "offline-node"}},
                    "metadata": {"indices": metadata},
                    "routing_table": {"indices": routing},
                    "blocks": {},
                },
            )
        assert request.method == "POST" and request.url.path.endswith("/_search")
        body = _object(json.loads(request.content))
        buildId: str = str(_object(_object(body["query"])["term"])["buildId"])
        units = (
            list(self.runtime.payload.units) if buildId == self.runtime.buildId else []
        )
        if self.corruption == "missing":
            units = []
        hits: list[dict[str, object]] = []
        for unit in sorted(units, key=lambda item: item.unitId):
            source = unit.model_dump()
            if self.corruption == "tokens":
                source["contentTokens"] += " unexpected-token"
            if self.corruption == "reordered":
                source["contentTokens"] = " ".join(
                    reversed(str(source["contentTokens"]).split())
                )
            if self.corruption == "missing-metadata":
                source.pop("hasImage")
            if self.corruption == "document":
                source["documentId"] = "another-document"
            source["buildId"] = buildId
            hits.append(
                {
                    "_id": sha256(
                        json.dumps(
                            [buildId, unit.unitId], separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    "sort": [unit.unitId],
                    "_source": source,
                }
            )
        if body.get("search_after"):
            hits = []
        return httpx.Response(
            200,
            json={
                "timed_out": False,
                "_shards": {"failed": 0},
                "hits": {
                    "total": {"value": len(units), "relation": "eq"},
                    "hits": hits,
                },
            },
        )


def _verify(
    runtime: _BackupRuntime, exported: dict[str, object], native: _RestoredNative
) -> dict[str, object]:
    from shared.services.retrieval.lexical.backup_inventory import (
        LexicalBackupInventory,
    )

    with httpx.Client(
        base_url="http://127.0.0.1:19989", transport=httpx.MockTransport(native.respond)
    ) as client:
        return LexicalBackupInventory(
            sessionFactory=sessionmaker(runtime.engine, expire_on_commit=False),
            clientFactory=lambda endpoint: client,
        ).verifyRestored(
            exported,
            endpoint="http://127.0.0.1:19989",
            indexMapping=native.mapping,
            admissionProof=native.proof,
            expectedSnapshotUuid="snapshot-backup-test",
        )


@pytest.mark.parametrize("corruption", ["", "reordered"])
def test_restored_data_verification_compares_exact_metadata_and_term_frequencies(
    backupRuntime: _BackupRuntime, corruption: str
) -> None:
    exported = _export(backupRuntime)
    native = _RestoredNative(backupRuntime, exported, corruption=corruption)
    verified = _verify(backupRuntime, exported, native)
    assert verified["status"] == "verified" and verified["data_verified"] is True
    assert verified["current_coverage_complete"] is True
    assert verified["boundary_sha256"] == exported["boundary_sha256"]
    assert verified["snapshot_uuid"] == "snapshot-backup-test"
    assert verified["operation_id"] == native.operationId
    assert verified["batch_reservation_id"] == native.proof["batch_reservation_id"]
    assert len(_records(verified["verified_builds"])) == 1


@pytest.mark.parametrize(
    "corruption", ["tokens", "missing", "document", "identity", "missing-metadata"]
)
def test_restored_data_corruption_or_wrong_target_cannot_be_verified(
    backupRuntime: _BackupRuntime, corruption: str
) -> None:
    exported = _export(backupRuntime)
    native = _RestoredNative(backupRuntime, exported, corruption=corruption)
    verified = _verify(backupRuntime, exported, native)
    assert verified["status"] == "unverified" and verified["data_verified"] is False
    assert verified["errors"]


def test_historical_boundary_remains_verifiable_after_current_archival(
    backupRuntime: _BackupRuntime,
) -> None:
    from shared.models.database.document import Document

    exported = _export(backupRuntime)
    with Session(backupRuntime.engine) as db:
        document = db.get(Document, backupRuntime.documentId)
        assert document is not None
        document.status = "archived"
        db.commit()
    verified = _verify(
        backupRuntime, exported, _RestoredNative(backupRuntime, exported)
    )
    assert (
        verified["data_verified"] is True
        and verified["current_boundary_matches"] is False
    )
    assert _object(exported["boundary"])["retired_record_absence_claimed"] is False


def test_export_never_holds_sql_transaction_over_assigned_object_read(
    backupRuntime: _BackupRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    load = LexicalInputStorage.load

    def assertNoTransaction(self: LexicalInputStorage, **options):
        with Session(backupRuntime.engine) as db:
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='idle in transaction'"
                    )
                )
                == 0
            )
        return load(self, **options)

    monkeypatch.setattr(LexicalInputStorage, "load", assertNoTransaction)
    assert _export(backupRuntime)["status"] == "exported"


def test_system_index_overhead_is_separate_from_business_cap(
    backupRuntime: _BackupRuntime,
) -> None:
    exported = _export(backupRuntime)
    verified = _verify(
        backupRuntime,
        exported,
        _RestoredNative(backupRuntime, exported, corruption="system-index"),
    )
    assert verified["data_verified"] is True
    assert verified["system_open_overhead"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_uuid", "different"),
        ("boundary_sha256", "0" * 64),
        ("target_cluster_uuid", "source-cluster"),
        ("charged_after", 401),
    ],
)
def test_invalid_offline_admission_cannot_send_native_request(
    backupRuntime: _BackupRuntime, field: str, value: object
) -> None:
    exported = _export(backupRuntime)
    native = _RestoredNative(backupRuntime, exported)
    native.proof[field] = value
    verified = _verify(backupRuntime, exported, native)
    assert verified["data_verified"] is False and verified["errors"]
    assert native.calls == []


def test_batch_verification_preserves_full_boundary_without_claiming_other_indices(
    backupRuntime: _BackupRuntime,
) -> None:
    from shared.services.retrieval.lexical.backup_inventory import (
        LexicalBackupInventory,
    )

    exported = _export(backupRuntime)
    native = _RestoredNative(backupRuntime, exported)
    name = str(_records(_object(exported["boundary"])["builds"])[0]["index_name"])
    native.proof["indices"] = [
        row for row in native.indices if row["original_index_name"] == name
    ]
    with httpx.Client(
        base_url="http://127.0.0.1:19989", transport=httpx.MockTransport(native.respond)
    ) as client:
        verified = LexicalBackupInventory(
            sessionFactory=sessionmaker(backupRuntime.engine),
            clientFactory=lambda endpoint: client,
        ).verifyRestored(
            exported,
            endpoint="http://127.0.0.1:19989",
            indexMapping={name: native.mapping[name]},
            admissionProof=native.proof,
            expectedSnapshotUuid="snapshot-backup-test",
            selectedIndices=[name],
        )
    assert (
        verified["data_verified"] is True
        and verified["complete_inventory_verified"] is False
    )
    assert verified["boundary_sha256"] == exported["boundary_sha256"]
    assert verified["selected_indices"] == [name]


def test_backup_inventory_refuses_role_without_full_private_and_demo_visibility(
    backupRuntime: _BackupRuntime,
) -> None:
    from shared.services.retrieval.lexical.backup_inventory import (
        LexicalBackupInventory,
    )
    from shared.services.retrieval.lexical.error import LexicalIndexError

    role: str = "backup_limited_" + uuid4().hex[:12]
    with backupRuntime.engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS")
    sessions = sessionmaker(backupRuntime.engine)

    def useLimitedRole(session: Session, transaction, connection) -> None:
        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")

    event.listen(sessions, "after_begin", useLimitedRole)
    try:
        with pytest.raises(LexicalIndexError, match="maintenance role"):
            LexicalBackupInventory(sessionFactory=sessions).export(
                expectedEpoch=1,
                expectedClusterUuid="source-cluster",
                expectedIncarnation="source-node",
            )
    finally:
        with backupRuntime.engine.begin() as connection:
            connection.exec_driver_sql(f"DROP ROLE {role}")


def _selectPostgresqlPrimary(runtime: _BackupRuntime) -> None:
    from shared.models.database.document import Document
    from shared.models.database.lexicalcontrol import LexicalBackendControl

    with Session(runtime.engine) as db:
        document = db.get(Document, runtime.documentId)
        control = db.get(LexicalBackendControl, 1)
        assert document is not None and control is not None
        document.current_indexing_build_id = None
        control.backend = "postgresql"
        db.commit()


def test_pg_serving_export_selects_unique_exact_current_native_shadow_without_activation(
    backupRuntime: _BackupRuntime,
) -> None:
    from shared.models.database.document import Document

    _selectPostgresqlPrimary(backupRuntime)
    exported = _export(backupRuntime)
    build = _records(_object(exported["boundary"])["builds"])[0]
    assert build["selection"] == "native-shadow"
    assert build["build_id"] == backupRuntime.buildId
    assert _records(_object(exported["boundary"])["documents"])[0]["build_id"] is None
    verified = _verify(
        backupRuntime, exported, _RestoredNative(backupRuntime, exported)
    )
    assert (
        verified["data_verified"] is True
        and verified["current_coverage_complete"] is True
    )
    with Session(backupRuntime.engine) as db:
        document = db.get(Document, backupRuntime.documentId)
        assert document is not None and document.current_indexing_build_id is None


@pytest.mark.parametrize(
    "corruption", ["missing", "ambiguous", "wrong-revision", "pg-corruption"]
)
def test_pg_serving_export_refuses_unproven_shadow(
    backupRuntime: _BackupRuntime, corruption: str
) -> None:
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.document import DocumentMapUnitToken
    from shared.services.retrieval.lexical.error import LexicalIndexError

    _selectPostgresqlPrimary(backupRuntime)
    with Session(backupRuntime.engine) as db:
        build = db.get(LexicalBuild, backupRuntime.buildId)
        assert build is not None
        if corruption == "missing":
            build.state = "retired"
        elif corruption == "wrong-revision":
            build.documentId = "other-document"
        elif corruption == "ambiguous":
            db.add(
                LexicalBuild(
                    id=str(uuid4()),
                    attemptId=build.attemptId,
                    namespaceIndexId=build.namespaceIndexId,
                    documentId=build.documentId,
                    revisionId=build.revisionId,
                    ownerToken=build.ownerToken,
                    state="published",
                    unitCount=build.unitCount,
                    legacyCopyState="completed",
                )
            )
        else:
            token = db.scalars(select(DocumentMapUnitToken)).first()
            assert token is not None
            token.frequency += 1
        db.commit()
    with pytest.raises(LexicalIndexError):
        _export(backupRuntime)


def test_pg_current_update_does_not_invalidate_frozen_snapshot_integrity(
    backupRuntime: _BackupRuntime,
) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult

    _selectPostgresqlPrimary(backupRuntime)
    exported = _export(backupRuntime)
    nextRevision: str = str(uuid4())
    with Session(backupRuntime.engine) as db:
        document = db.get(Document, backupRuntime.documentId)
        assert document is not None
        nextJob: str = str(uuid4())
        db.add(
            Job(
                job_id=nextJob,
                user_id=document.user_id,
                job_type="document_ingestion",
                source_type="file",
                status="done",
            )
        )
        db.flush()
        db.add(
            JobResult(
                id=nextRevision,
                job_id=nextJob,
                document_id=document.document_id,
                delivery_mode="inline",
                inline_payload={},
            )
        )
        db.flush()
        document.current_job_result_id = nextRevision
        db.commit()
    verified = _verify(
        backupRuntime, exported, _RestoredNative(backupRuntime, exported)
    )
    assert (
        verified["data_verified"] is True
        and verified["current_coverage_complete"] is False
    )
    assert verified["current_boundary_matches"] is False
    assert (
        _records(verified["missing_current_builds"])[0]["revision_id"] == nextRevision
    )
