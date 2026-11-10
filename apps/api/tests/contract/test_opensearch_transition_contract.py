"""Operator transitions fence unfinished jobs and verify exact current coverage."""

from __future__ import annotations
from typing import TypeVar, cast
from collections.abc import Iterator
import asyncio
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)


@pytest.fixture
def transitionRuntime(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess
) -> Iterator[Engine]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "postgresql")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.models.database.lexicalcontrol import LexicalBackendControl

    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    with Session(engine) as db:
        db.add(
            LexicalBackendControl(id=1, backend="postgresql", mode="serving", epoch=1)
        )
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def createParsed(engine: Engine) -> tuple[str, str, str]:
    from shared.models.database.job import Job
    from shared.models.database.user import User
    from shared.services.retrieval.lexical.parsing import ParsedRevisionFinalizer
    from shared.models.database.job_result import JobResult

    identity = uuid4().hex[:10]
    jobId = f"transition-{identity}"
    with Session(engine) as db:
        db.add(
            User(id=identity, name="Transition", email=f"{identity}@example.invalid")
        )
        db.flush()
        db.add(
            Job(
                job_id=jobId,
                user_id=identity,
                job_type="document_ingestion",
                source_type="file",
                status="running",
                credits_charged=991,
                billing_status="charged",
                webhook_enabled=False,
                job_metadata={
                    "document_id": f"doc_{identity}",
                    "namespace": f"ns-{identity}",
                    "source_file_name": "report.pdf",
                },
            )
        )
        db.commit()
        ParsedRevisionFinalizer().finalize(
            db,
            jobId=jobId,
            resultKey="result.zip",
            checksum="checksum",
            resultSize=100,
            chunks=[
                {
                    "chunk_id": "chunk-1",
                    "type": "text",
                    "content": "exact preserved words",
                    "path": "report.pdf/Section",
                    "metadata": {},
                }
            ],
            storedCount=1,
            deliveryMode="url",
            sectionSummaries=None,
            documentTopSummary=None,
        )
        db.commit()
        result = db.scalars(select(JobResult).where(JobResult.job_id == jobId)).one()
        return (jobId, f"doc_{identity}", result.id)


def test_transition_dry_run_does_not_gate_or_settle(transitionRuntime: Engine) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.transition import LexicalTransition

    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    receipt = service.run("gate", targetBackend="opensearch")
    assert receipt["execute"] is False
    assert receipt["sourceHash"] and _object(receipt["currentPublished"])["count"] == 0
    with Session(transitionRuntime) as db:
        control = _require(db.get(LexicalBackendControl, 1))
        assert (
            control.mode == "serving"
            and control.epoch == 1
            and (control.backend == "postgresql")
        )


def test_gate_settle_resume_does_not_revive_failed_or_regress_done(
    transitionRuntime: Engine,
) -> None:
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, documentId, revisionId = createParsed(transitionRuntime)
    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    gated = service.run(
        "gate", targetBackend="postgresql", execute=True, expectedEpoch=1
    )
    assert (
        _object(gated["control"])["mode"] == "gated"
        and _object(gated["control"])["epoch"] == 2
    )
    settled = service.run(
        "settle", targetBackend="postgresql", execute=True, expectedEpoch=2
    )
    assert settled["settledJobs"] == 1
    resumed = service.run(
        "resume", targetBackend="postgresql", execute=True, expectedEpoch=2
    )
    assert resumed["control"] == {
        "backend": "postgresql",
        "mode": "serving",
        "epoch": 3,
        "cluster_uuid": None,
        "node_incarnation": None,
    }
    with Session(transitionRuntime) as db:
        job = _require(db.get(Job, jobId))
        attempt = _require(db.get(IndexingAttempt, job.current_indexing_attempt_id))
        assert (
            job.status == "failed"
            and job.parsing_status == "completed"
            and (job.failure_stage == "indexing")
        )
        assert (
            job.credits_charged == 991
            and attempt.state == "failed"
            and (attempt.executionCount == 0)
        )
        assert _require(db.get(LexicalBackendControl, 1)).backend == "postgresql"


def test_resume_requires_live_current_coverage_not_old_receipt(
    transitionRuntime: Engine,
) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, documentId, revisionId = createParsed(transitionRuntime)
    with Session(transitionRuntime) as db:
        document = _require(db.get(Document, documentId))
        document.current_job_result_id = revisionId
        job = _require(db.get(Job, jobId))
        job.status = "done"
        job.parsing_status = "completed"
        job.indexing_status = "completed"
        db.commit()
    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    service.run("gate", targetBackend="postgresql", execute=True, expectedEpoch=1)
    receipt = service.run(
        "verify", targetBackend="postgresql", execute=True, expectedEpoch=2
    )
    assert _object(receipt["coverage"])["complete"] is False
    with pytest.raises(LexicalIndexError):
        service.run("resume", targetBackend="postgresql", execute=True, expectedEpoch=2)
    with Session(transitionRuntime) as db:
        assert _require(db.get(Job, jobId)).status == "done"


def test_transition_rejects_stale_epoch_before_any_mutation(
    transitionRuntime: Engine,
) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.transition import LexicalTransition

    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    with pytest.raises(LexicalIndexError):
        service.run("gate", targetBackend="opensearch", execute=True, expectedEpoch=99)


def test_native_resume_waits_for_active_progressive_publication(
    transitionRuntime: Engine,
) -> None:
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, _, _ = createParsed(transitionRuntime)
    with Session(transitionRuntime) as db:
        job = _require(db.get(Job, jobId))
        job.job_metadata = {**(job.job_metadata or {}), "ingestion_mode": "progressive"}
        db.commit()

    service = LexicalTransition(sessionFactory=sessionmaker(transitionRuntime))
    service.run("gate", targetBackend="opensearch", execute=True, expectedEpoch=1)
    with pytest.raises(LexicalIndexError, match="active progressive publication"):
        service.run("resume", targetBackend="opensearch", execute=True, expectedEpoch=2)


def publishPostgresql(engine: Engine, jobId: str) -> None:
    from datetime import timedelta
    from sqlalchemy import func
    from shared.models.database.indexing import IndexingAttempt
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.worker import IndexingWorker

    sessions = sessionmaker(engine, expire_on_commit=False)
    context = IndexingWorker(sessionFactory=sessions)._readContext(jobId)
    assert not isinstance(context, str)
    ownerToken = str(uuid4())
    with Session(engine) as db:
        attempt = _require(db.get(IndexingAttempt, context.attemptId))
        now = db.scalars(select(func.clock_timestamp())).one()
        attempt.state = "running"
        attempt.executionCount = 1
        attempt.ownerToken = ownerToken
        attempt.startedAt = now
        attempt.executionDeadlineAt = now + timedelta(seconds=300)
        attempt.claimExpiresAt = attempt.executionDeadlineAt
        db.commit()
    PostgresqlPublication(sessionFactory=sessions, deadline=IndexingDeadline(300)).run(
        context, ownerToken=ownerToken
    )


@pytest.mark.parametrize(
    "hasPendingEnrichment", [False, True], ids=["full", "pending-enrichment"]
)
def test_pg_serving_shadow_backfill_only_activates_after_gated_live_verification(
    transitionRuntime: Engine, monkeypatch: pytest.MonkeyPatch, hasPendingEnrichment: bool
) -> None:
    from shared.core.config import settings
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.transition import LexicalTransition
    from shared.services.retrieval.lexical.error import LexicalIndexError

    jobId, documentId, revisionId = createParsed(transitionRuntime)
    publishPostgresql(transitionRuntime, jobId)
    if hasPendingEnrichment:
        with Session(transitionRuntime) as db:
            result = _require(db.get(JobResult, revisionId))
            result.document_metadata = {
                **(result.document_metadata or {}),
                "progressive": {"stages": {"finalization": {"status": "pending"}}},
            }
            db.commit()
    sessions = sessionmaker(transitionRuntime, expire_on_commit=False)
    service = LexicalTransition(sessionFactory=sessions)
    builds = {}
    nativeState = {"value": "open", "opens": 0}

    class MemoryNative:
        def __init__(self, scope):
            self.scope = scope
            self.indexName = (
                f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{scope.fingerprint[:48]}"
            )

        def getIndexUuid(self):
            return "native-original"

        def getLifecycleSnapshot(self, *, isExact, timeoutSeconds):
            from shared.services.retrieval.lexical.nativestate import (
                NativeLifecycleSnapshot,
            )

            return NativeLifecycleSnapshot.model_validate(
                {
                    "cluster_uuid": "native-cluster",
                    "nodes": {"node": {"ephemeral_id": "native-node"}},
                    "metadata": {
                        "indices": {
                            self.indexName: {
                                "state": nativeState["value"],
                                "settings": {"index": {"uuid": "native-original"}},
                            }
                        }
                    },
                    "blocks": {
                        "indices": {
                            self.indexName: {"4": {"levels": ["read", "write"]}}
                        }
                    }
                    if nativeState["value"] == "close"
                    else {},
                    "routing_table": {
                        "indices": {
                            self.indexName: {
                                "shards": {"0": [{"primary": True, "state": "STARTED"}]}
                            }
                        }
                    },
                }
            )

        def writeBuild(self, *, buildId, units, beforeBatch=None):
            if beforeBatch:
                beforeBatch()
            builds[buildId] = units

        def _request(self, method, path, *, payload):
            buildId = _object(_object(payload["query"])["term"])["buildId"]
            units = builds.get(buildId, ()) if "search_after" not in payload else ()
            return {
                "timed_out": False,
                "_shards": {"failed": 0},
                "hits": {
                    "hits": [
                        {
                            "_source": {**unit.model_dump(), "buildId": buildId},
                            "sort": [unit.unitId],
                        }
                        for unit in sorted(units, key=lambda unit: unit.unitId)
                    ]
                },
            }

        def close(self):
            return None

    def ensureIndex(self, *, scope, native):
        with sessions.begin() as db:
            control = self.lockControl(db)
            control.clusterUuid = "native-cluster"
            control.nodeIncarnation = "native-node"
            registry = db.get(LexicalNamespaceIndex, scope.fingerprint)
            if registry is None:
                db.add(
                    LexicalNamespaceIndex(
                        id=scope.fingerprint,
                        ownerId=scope.ownerId,
                        namespace=scope.namespace,
                        corpusKind=scope.corpusKind,
                        indexName=native.indexName,
                        indexUuid="native-original",
                        state="open",
                    )
                )
            else:
                registry.state = "open"
                nativeState["value"] = "open"
                nativeState["opens"] += 1
            return ("native-original", control.epoch)

    monkeypatch.setattr(service, "_createNative", MemoryNative)
    monkeypatch.setattr(LexicalCoordination, "ensureIndex", ensureIndex)
    receipt = service.run(
        "backfill",
        targetBackend="opensearch",
        execute=True,
        expectedEpoch=1,
        documentsPerSecond=100000,
    )
    assert receipt["backfilledRevisions"] == 1
    nativeState["value"] = "close"
    with Session(transitionRuntime) as db:
        db.scalars(select(LexicalNamespaceIndex)).one().state = "closed"
        db.commit()
    repeated = service.run(
        "backfill",
        targetBackend="opensearch",
        execute=True,
        expectedEpoch=1,
        documentsPerSecond=100000,
    )
    assert repeated["backfilledRevisions"] == 0 and len(builds) == 1
    assert nativeState["opens"] == 1
    with Session(transitionRuntime) as db:
        document = _require(db.get(Document, documentId))
        job = _require(db.get(Job, jobId))
        originalAttemptId = job.current_indexing_attempt_id
        assert document.current_job_result_id == revisionId
        assert document.current_indexing_build_id is None
        assert job.status == "done"
    service.run("gate", targetBackend="opensearch", execute=True, expectedEpoch=1)
    service.run("settle", targetBackend="opensearch", execute=True, expectedEpoch=2)
    assert _object(
        service.run(
            "verify", targetBackend="opensearch", execute=True, expectedEpoch=2
        )["coverage"]
    )["complete"]
    from shared.models.database.document import DocumentMapUnitToken

    with Session(transitionRuntime) as db:
        _require(db.scalars(select(DocumentMapUnitToken)).first()).frequency += 10
        db.commit()
    with pytest.raises(LexicalIndexError, match="PG rollback"):
        service.run("resume", targetBackend="opensearch", execute=True, expectedEpoch=2)
    service.run(
        "backfill",
        targetBackend="postgresql",
        execute=True,
        expectedEpoch=2,
        documentsPerSecond=100000,
    )
    resumed = service.run(
        "resume", targetBackend="opensearch", execute=True, expectedEpoch=2
    )
    assert resumed["control"] == {
        "backend": "opensearch",
        "mode": "serving",
        "epoch": 3,
        "cluster_uuid": "native-cluster",
        "node_incarnation": "native-node",
    }
    with Session(transitionRuntime) as db:
        assert (
            _require(db.get(Document, documentId)).current_indexing_build_id in builds
        )
        assert _require(db.get(Job, jobId)).status == "done"
        assert (
            _require(db.get(Job, jobId)).current_indexing_attempt_id
            == originalAttemptId
        )
    service.run("gate", targetBackend="postgresql", execute=True, expectedEpoch=3)
    next(iter(builds.values()))
    builds.clear()
    with pytest.raises(LexicalIndexError):
        service.run("resume", targetBackend="opensearch", execute=True, expectedEpoch=4)


def test_historical_pg_prepared_terms_freeze_without_retokenizing(
    transitionRuntime: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.preparation import LexicalPreparation
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, documentId, revisionId = createParsed(transitionRuntime)
    publishPostgresql(transitionRuntime, jobId)
    with Session(transitionRuntime) as db:
        result = _require(db.get(JobResult, revisionId))
        result.indexing_input_key = None
        result.indexing_input_checksum = None
        result.indexing_input_compiler = None
        db.commit()

    def refusePreparation(**kwargs):
        raise AssertionError(
            "Migration cannot retokenize historical persisted PG terms"
        )

    monkeypatch.setattr(LexicalPreparation, "prepare", refusePreparation)
    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    _, bindings = service._capture()
    binding = service._materializeInput(bindings[0], 1)
    assert binding is not None and binding.inputKey and binding.inputChecksum
    payload = service._loadInput(binding)
    with Session(transitionRuntime) as db:
        coverage = service._verifyPostgresql(db, binding, payload)
        assert (
            isinstance(coverage["unitCount"], int)
            and coverage["unitCount"] > 0
            and isinstance(coverage["tokenCount"], int)
            and coverage["tokenCount"] > 0
        )


def test_transition_inventory_refuses_a_runtime_role_without_full_demo_visibility(
    transitionRuntime: Engine,
) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.transition import LexicalTransition

    with transitionRuntime.begin() as connection:
        connection.exec_driver_sql(
            "CREATE ROLE kw10_limited_inventory NOLOGIN NOBYPASSRLS"
        )
        connection.exec_driver_sql("SET LOCAL ROLE kw10_limited_inventory")
        service = LexicalTransition(
            sessionFactory=sessionmaker(bind=connection, expire_on_commit=False)
        )
        with pytest.raises(LexicalIndexError, match="maintenance role"):
            service.run("inspect", targetBackend="postgresql")


def test_gated_pg_repair_reuses_current_frozen_revision_without_new_job(
    transitionRuntime: Engine,
) -> None:
    from shared.models.database.document import DocumentMapUnitToken
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, documentId, revisionId = createParsed(transitionRuntime)
    publishPostgresql(transitionRuntime, jobId)
    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    with Session(transitionRuntime) as db:
        token = _require(db.scalars(select(DocumentMapUnitToken)).first())
        token.frequency += 10
        db.commit()
    service.run("gate", targetBackend="postgresql", execute=True, expectedEpoch=1)
    assert not _object(
        service.run(
            "verify", targetBackend="postgresql", execute=True, expectedEpoch=2
        )["coverage"]
    )["complete"]
    repaired = service.run(
        "backfill",
        targetBackend="postgresql",
        execute=True,
        expectedEpoch=2,
        documentsPerSecond=100000,
    )
    assert repaired["backfilledRevisions"] == 1
    assert _object(
        service.run(
            "verify", targetBackend="postgresql", execute=True, expectedEpoch=2
        )["coverage"]
    )["complete"]
    with Session(transitionRuntime) as db:
        assert _require(db.get(Job, jobId)).status == "done"
        assert _require(db.get(Job, jobId)).credits_charged == 991


@pytest.mark.parametrize("corruption", ["format", "token_hash", "statistics"])
def test_pg_coverage_rejects_incompatible_or_corrupt_lookup_metadata(
    transitionRuntime: Engine, corruption: str
) -> None:
    from shared.models.database.document import (
        DocumentMapUnitIndex,
        DocumentMapUnitToken,
    )
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, documentId, revisionId = createParsed(transitionRuntime)
    publishPostgresql(transitionRuntime, jobId)
    with Session(transitionRuntime) as db:
        marker = db.scalars(select(DocumentMapUnitIndex)).one()
        if corruption == "format":
            marker.format_version += 100
        elif corruption == "token_hash":
            _require(db.scalars(select(DocumentMapUnitToken)).first()).token_hash = (
                "0" * 64
            )
        else:
            marker.path_total_length = _require(marker.path_total_length) + 100
        db.commit()
    service = LexicalTransition(
        sessionFactory=sessionmaker(transitionRuntime, expire_on_commit=False)
    )
    assert not _object(service.run("verify", targetBackend="postgresql")["coverage"])[
        "complete"
    ]


_Value = TypeVar("_Value")


def _require(value: _Value | None) -> _Value:
    assert value is not None
    return value


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_shadow_input_preflight_checks_chunk_permissions_before_writing(
    transitionRuntime: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.transition import LexicalTransition

    jobId, _, revisionId = createParsed(transitionRuntime)
    publishPostgresql(transitionRuntime, jobId)
    with Session(transitionRuntime) as db:
        result = _require(db.get(JobResult, revisionId))
        result.indexing_input_key = None
        result.indexing_input_checksum = None
        result.indexing_input_compiler = None
        db.commit()
    _, bindings = LexicalTransition(sessionFactory=sessionmaker(transitionRuntime))._capture()
    binding = next(binding for binding in bindings if binding.revisionId == revisionId)

    def refuseSave(*args: object, **kwargs: object) -> None:
        pytest.fail("Shadow input preflight must never upload")

    monkeypatch.setattr(LexicalInputStorage, "saveWithSize", refuseSave)
    roleName = f"kw_shadow_{uuid4().hex[:12]}"
    with transitionRuntime.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {roleName} NOLOGIN BYPASSRLS")
        connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {roleName}")
        connection.exec_driver_sql(
            "GRANT SELECT ON lexical_backend_control, documents, job_results, jobs, document_map_unit_indexes, "
            f"document_map_units, document_map_unit_tokens, document_sections TO {roleName}"
        )

    def preflight() -> None:
        with transitionRuntime.begin() as connection:
            connection.exec_driver_sql(f"SET LOCAL ROLE {roleName}")
            service = LexicalTransition(sessionFactory=sessionmaker(connection))
            service.preflightShadowInput(binding, epoch=1)

    try:
        with pytest.raises(DBAPIError, match="permission denied for table job_chunks"):
            preflight()
        with transitionRuntime.begin() as connection:
            connection.exec_driver_sql(f"GRANT SELECT ON job_chunks TO {roleName}")
        preflight()
        with Session(transitionRuntime) as db:
            assert _require(db.get(JobResult, revisionId)).indexing_input_key is None
    finally:
        with transitionRuntime.begin() as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {roleName}")
            connection.exec_driver_sql(f"DROP ROLE {roleName}")
