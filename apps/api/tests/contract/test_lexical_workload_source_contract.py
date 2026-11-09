"""Campaign writes are admitted at arrival through the real repository fence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import PostgreSQLProcess, clear_application_modules, configure_contract_environment, get_contract_database_url, prepare_contract_storage

if TYPE_CHECKING:
    from shared.services.retrieval.lexical.workload_probe import WorkloadSourceAdapter


@pytest.fixture
def workloadRuntime(monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess, tmp_path: Path) -> Iterator[tuple[Engine, dict[str, object]]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_WORKLOAD_SOURCE_CAMPAIGN", "actual-campaign")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.core.config import settings
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.user import User
    from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority
    from shared.services.retrieval.lexical.transition import LexicalTransition
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "APP_ENV", "staging")
    monkeypatch.setattr(LexicalTransition, "getSourceHash", staticmethod(lambda: "a" * 64))
    engine: Engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    factory: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(User(id="workload-owner", name="Workload contract", email="workload@example.invalid"))
        db.add(LexicalBackendControl(id=1, backend="opensearch", mode="serving", epoch=7, clusterUuid="native-one"))
    token: str = str(uuid4())
    receipt = LexicalOperatorAuthority(sessionFactory=factory).claim(logicalClusterId="us-east-1:workload",
        invocationToken=token, hostIdentity="b" * 64, operationId="workflow", expectedSourceHash="a" * 64,
        expectedEpoch=7, expectedClusterUuid="native-one")
    receiptPath: Path = tmp_path / "issuer.json"
    receiptPath.write_text(json.dumps(receipt))
    receiptPath.chmod(0o600)
    raw: bytes = json.dumps({"chunks": [{"chunk_id": "actual", "type": "text", "path": "source/section", "content": "actual retained parser boundary", "metadata": {}}],
        "result_s3_key": "retained/result.zip", "checksum": "retained-checksum", "zip_size": 10}).encode()
    prepared: Path = tmp_path / "prepared.private.json.gz"
    data: bytes = gzip.compress(raw)
    prepared.write_bytes(data)
    configuration: dict[str, object] = {"schema_version": "opensearch-workload-source-v1", "campaign_id": "actual-campaign", "source_hash": "a" * 64,
        "issuer": {"invocation_token": token, "host_identity": "b" * 64, "operation_id": "workflow", "epoch": 7, "receipt_path": str(receiptPath)},
        "cases": {"retained-one": {"owner_id": "workload-owner", "document_id": "doc_workload", "namespace": "workload",
            "source_file_name": "source",
            "job_ids": ["actual-new-job", "actual-next-job"], "prepared_path": str(prepared), "sha256": sha256(data).hexdigest(),
            "compressed_bytes": len(data), "uncompressed_bytes": len(raw), "unit_count": 2, "preseed_charge": 123}},
        "cohorts": {}, "resources": {"journal_paths": [], "maximum_journal_bytes": 65536, "freshness_seconds": 30}}
    try:
        yield engine, configuration
    finally:
        engine.dispose()


def _getAdapter(engine: Engine, configuration: dict[str, object]) -> WorkloadSourceAdapter:
    path: Path = Path(__file__).resolve().parents[4] / "packages/shared-python/shared/services/retrieval/lexical/workload_probe.py"
    assert path.is_file(), "Source-owned arrival admission must be implemented before workload execution"
    from shared.services.retrieval.lexical.workload_probe import WorkloadSourceAdapter
    return WorkloadSourceAdapter(configuration=configuration, sessionFactory=sessionmaker(engine, expire_on_commit=False))


def _admit(adapter: WorkloadSourceAdapter, *, jobId: str = "actual-new-job", event: str = "arrival-one", case: str = "retained-one", kind: str = "create", execute: bool = True) -> dict[str, object]:
    async def admit() -> dict[str, object]:
        engine = create_async_engine(get_contract_database_url())
        try:
            return await adapter.runAsync({"event": "admission", "campaign_id": "actual-campaign", "source_hash": "a" * 64,
                "arrival": {"id": event, "job_id": jobId, "case_id": case, "write_kind": kind, "offset_ms": 1000}},
                execute=execute, asyncSessionFactory=async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()
    return asyncio.run(admit())


def test_arrival_creates_job_and_binds_accepted_pointer_without_precreated_job(workloadRuntime: tuple[Engine, dict[str, object]]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    engine, configuration = workloadRuntime
    with Session(engine) as db:
        assert db.get(Job, "actual-new-job") is None and db.get(Document, "doc_workload") is None
    receipt = _admit(_getAdapter(engine, configuration))
    assert receipt["mode"] == "actual" and receipt["admitted"] is True and receipt["admitted_at_utc"]
    with Session(engine) as db:
        job = db.scalars(select(Job)).one()
        document = db.scalars(select(Document)).one()
        assert job.job_id == "actual-new-job" and document.accepted_job_id == job.job_id
        assert job.billing_status == "charged" and job.credits_charged == 123
        assert job.job_metadata and job.job_metadata["mixed_workload_event_id"] == "arrival-one"
        assert job.current_indexing_attempt_id is None and document.current_job_result_id is None


def test_dry_run_and_unapproved_job_never_admit(workloadRuntime: tuple[Engine, dict[str, object]]) -> None:
    from shared.models.database.job import Job
    engine, configuration = workloadRuntime
    adapter = _getAdapter(engine, configuration)
    assert _admit(adapter, execute=False)["mode"] == "dry-run"
    with pytest.raises(ValueError, match="approved"):
        _admit(adapter, jobId="not-approved")
    with Session(engine) as db:
        assert db.scalars(select(Job)).all() == []


def test_same_document_concurrent_update_preserves_original_active_admission(workloadRuntime: tuple[Engine, dict[str, object]]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.core.exceptions.domain_exceptions import ConflictException
    engine, configuration = workloadRuntime
    adapter = _getAdapter(engine, configuration)
    _admit(adapter)
    with pytest.raises(ConflictException):
        _admit(adapter, jobId="actual-next-job", event="arrival-two", kind="update")
    with Session(engine) as db:
        assert db.get(Job, "actual-next-job") is None
        document = db.get(Document, "doc_workload")
        assert document and document.accepted_job_id == "actual-new-job"


def test_replica_jobs_can_bind_distinct_reviewed_documents(workloadRuntime: tuple[Engine, dict[str, object]]) -> None:
    from shared.models.database.document import Document
    engine, configuration = workloadRuntime
    case = cast(dict[str, dict[str, object]], configuration["cases"])["retained-one"]
    case["document_ids_by_job"] = {"actual-next-job": "doc_replica"}
    adapter = _getAdapter(engine, configuration)
    _admit(adapter)
    _admit(adapter, jobId="actual-next-job", event="arrival-two")
    with Session(engine) as db:
        rows = db.execute(select(Document.document_id, Document.accepted_job_id).order_by(Document.document_id)).all()
        assert rows == [("doc_replica", "actual-next-job"), ("doc_workload", "actual-new-job")]


def test_changed_prepared_input_refuses_before_job_or_document_admission(workloadRuntime: tuple[Engine, dict[str, object]]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    engine, configuration = workloadRuntime
    case = cast(dict[str, dict[str, object]], configuration["cases"])["retained-one"]
    Path(str(case["prepared_path"])).write_bytes(b"changed prepared input")
    with pytest.raises(ValueError, match="checksum"):
        _admit(_getAdapter(engine, configuration))
    with Session(engine) as db:
        assert db.get(Job, "actual-new-job") is None and db.get(Document, "doc_workload") is None


def test_archive_exclusion_is_actual_and_does_not_create_or_retry(workloadRuntime: tuple[Engine, dict[str, object]]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    engine, configuration = workloadRuntime
    adapter = _getAdapter(engine, configuration)
    _admit(adapter)
    with Session(engine) as db:
        document = db.scalars(select(Document)).one()
        document.status = "archived"
        db.commit()
    receipt = _admit(adapter, kind="retry", event="retry-archived")
    assert receipt["job_id"] == "actual-new-job" and receipt["exclusion_verified"] is True and receipt["exclusion_reason"] == "archived"
    with Session(engine) as db:
        job = db.scalars(select(Job)).one()
        assert job.status == "pending" and job.current_indexing_attempt_id is None


def _seedRegistry(engine: Engine) -> tuple[str, str]:
    from shared.core.config import settings
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    scope = LexicalScope(ownerId="workload-owner", namespace="workload")
    index: str = settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX + "-" + scope.fingerprint[:48]
    with Session(engine) as db:
        db.scalars(select(LexicalBackendControl)).one().nodeIncarnation = "native-incarnation"
        db.add(LexicalNamespaceIndex(id=scope.fingerprint, ownerId=scope.ownerId, namespace=scope.namespace,
            corpusKind="private", indexName=index, indexUuid="retained-index", state="open"))
        db.commit()
    return index, scope.fingerprint


def _createNative(index: str):
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot

    class Native:
        indexName: str = index

        def __init__(self, **_options: object) -> None:
            pass

        def getLifecycleSnapshot(self, **_options: object) -> NativeLifecycleSnapshot:
            return NativeLifecycleSnapshot.model_validate({"cluster_uuid": "native-one", "nodes": {"n": {"ephemeral_id": "native-incarnation"}},
                "metadata": {"indices": {index: {"state": "open", "settings": {"index": {"uuid": "retained-index"}}}}},
                "routing_table": {"indices": {index: {"shards": {"0": [{"primary": True, "state": "STARTED"}]}}}}})

        def _request(self, method: str, path: str) -> dict[str, object]:
            assert method == "GET" and path.startswith("/_nodes/stats/")
            return {"nodes": {"n": {"os": {"cpu": {"percent": 17}}, "jvm": {"mem": {"heap_used_in_bytes": 12345},
                "gc": {"collectors": {"young": {"collection_time_in_millis": 99}}}}, "fs": {"total": {"available_in_bytes": 55555}},
                "http": {"current_open": 2}, "thread_pool": {"search": {"queue": 3, "rejected": 4}}}}}

        def close(self) -> None:
            pass

    return Native


def _run(adapter: WorkloadSourceAdapter, event: str) -> dict[str, object]:
    return asyncio.run(adapter.runAsync({"event": event, "campaign_id": "actual-campaign", "source_hash": "a" * 64}, execute=True))


def test_precondition_refuses_unreviewed_binding_before_any_namespace_mutation(
    workloadRuntime: tuple[Engine, dict[str, object]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.lexical import workload_probe
    engine, configuration = workloadRuntime
    index, _scope = _seedRegistry(engine)
    configuration["cohorts"] = {"warm": {"scopes": [{"scope": {"ownerId": "workload-owner", "namespace": "workload"},
        "index_name": index, "index_uuid": "retained-index", "action": "open"}], "expected_slot_state": "open", "artifact_sha256": "f" * 64}}
    monkeypatch.setattr(workload_probe, "OpenSearchLexicalIndex", _createNative(index))
    mutations: list[str] = []

    def ensure(_self: object, **_options: object) -> tuple[str, int]:
        mutations.append("open")
        return "retained-index", 7

    monkeypatch.setattr(workload_probe.LexicalCoordination, "ensureIndex", ensure)
    with pytest.raises(ValueError, match="reviewed frozen artifact"):
        _run(_getAdapter(engine, configuration), "precondition")
    assert mutations == []


def test_resource_samples_actual_sql_and_native_gauges_without_filling_missing_broker(
    workloadRuntime: tuple[Engine, dict[str, object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from shared.services.retrieval.lexical import workload_resources
    engine, configuration = workloadRuntime
    index, _scope = _seedRegistry(engine)
    monkeypatch.setattr(workload_resources, "OpenSearchLexicalIndex", _createNative(index))
    journal: Path = tmp_path / "stages.jsonl"
    observed: str = datetime.now(timezone.utc).isoformat()
    base: dict[str, object] = {"mode": "actual", "campaign_id": "actual-campaign", "source_hash": "a" * 64, "observed_at_utc": observed}
    journal.write_text(json.dumps({**base, "kind": "observer_install", "capabilities": sorted(workload_resources._STAGES)}) + "\n"
        + json.dumps({**base, "kind": "write", "id": "write-one", "bulk_ms": 12.25}) + "\n"
        + json.dumps({**base, "campaign_id": "foreign", "kind": "write", "id": "write-one", "bulk_ms": 100}) + "\n")
    journal.chmod(0o600)
    resources = cast(dict[str, object], configuration["resources"])
    resources.update({"journal_paths": [str(journal)], "state_path": str(tmp_path / "resource-state.json"), "export_path": str(tmp_path / "export.jsonl")})
    adapter = _getAdapter(engine, configuration)
    first = _run(adapter, "resource")
    second = _run(adapter, "resource")
    metrics = cast(dict[str, object], second["metrics"])
    assert first["complete"] is False and second["missing_measurements"] == ["parse_task_backlog"]
    assert metrics["bulk_ms"] == 12.25 and metrics["open_indexes"] == metrics["catalog_indexes"] == 1
    assert metrics["native_task_queue"] == 3 and metrics["gc_collection_time_ms"] == 99 and "gc_pause_ms" not in metrics
    assert second["missing_exact_measurements"] == ["pg_lock_wait", "gc_stop_the_world_pause"]
    assert cast(dict[str, object], second["measurement_kinds"])["bulk_ms"] == "cumulative-counter"
    export = _run(adapter, "stage-export")
    assert export["records"] == 1 and export["complete"] is True
    assert sha256(Path(str(export["telemetry_path"])).read_bytes()).hexdigest() == export["telemetry_sha256"]


def test_observer_measures_actual_async_postgresql_pool_wait(
    workloadRuntime: tuple[Engine, dict[str, object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from shared.services.retrieval.lexical.stage_observer import StageObserver
    config: Path = tmp_path / "pool-observer.json"
    journal: Path = tmp_path / "pool-stages.jsonl"
    config.write_text(json.dumps({"schema_version": "opensearch-stage-observer-v1", "campaign_id": "actual-campaign",
        "source_hash": "a" * 64, "journal_path": str(journal), "maximum_journal_bytes": 65536}))
    config.chmod(0o600)
    monkeypatch.setattr(StageObserver, "_configuration", None)
    monkeypatch.setenv("KNOWHERE_WORKLOAD_OBSERVER_CONFIG", str(config))
    StageObserver.install()

    async def campaign() -> None:
        engine = create_async_engine(get_contract_database_url(), pool_size=1, max_overflow=0)
        checkout_started = asyncio.Event()
        measure = StageObserver.measure

        @contextmanager
        def measure_and_signal(name: str, *, intervalName: str | None = None) -> Iterator[None]:
            with measure(name, intervalName=intervalName):
                if name == "pg_pool_wait_ms" and StageObserver.hasEvent():
                    checkout_started.set()
                yield

        monkeypatch.setattr(StageObserver, "measure", staticmethod(measure_and_signal))

        async def query() -> None:
            with StageObserver.bindRequest("actual-campaign:pg-pool"):
                async with engine.begin() as connection:
                    assert await connection.scalar(text("SELECT 1")) == 1

        try:
            async with engine.connect():
                task = asyncio.create_task(query())
                await asyncio.wait_for(checkout_started.wait(), timeout=5)
                await asyncio.sleep(0.06)
                assert not task.done()
            await task
        finally:
            await engine.dispose()
    asyncio.run(campaign())
    row = json.loads(journal.read_text().splitlines()[-1])
    assert row["id"] == "pg-pool" and row["pg_pool_wait_ms"] >= 40 and row["pg_transaction_ms"] > 0


@pytest.mark.parametrize("isMatchingArrival", [False, True])
def test_prepared_task_requires_exact_durable_arrival_binding_and_keeps_result(
    workloadRuntime: tuple[Engine, dict[str, object]], monkeypatch: pytest.MonkeyPatch,
    isMatchingArrival: bool,
) -> None:
    engine, configuration = workloadRuntime
    _admit(_getAdapter(engine, configuration))
    monkeypatch.setenv("KNOWHERE_PREPARED_COMPLETION_CAMPAIGN", "actual-campaign")
    path: Path = Path(__file__).resolve().parents[4] / "scripts/probes/prepared-completion-task.py"
    specification = importlib.util.spec_from_file_location("prepared_completion_contract", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    task = getattr(module, "completePreparedTask")
    assert task.ignore_result is False
    case = cast(dict[str, dict[str, object]], configuration["cases"])["retained-one"]
    request: dict[str, object] = {"campaign_id": "actual-campaign",
        "event_id": "arrival-one" if isMatchingArrival else "wrong-event", "source_hash": "a" * 64,
        "job_id": "actual-new-job", "owner_id": "workload-owner", "prepared_path": case["prepared_path"],
        "sha256": case["sha256"], "compressed_bytes": case["compressed_bytes"], "uncompressed_bytes": case["uncompressed_bytes"]}
    if not isMatchingArrival:
        with pytest.raises(ValueError, match="exact owned arrival"):
            task.run(request)
        return

    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an owned isolated native node")
    from shared.core.config import settings
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    prefix: str = "knowhere-prepared-contract"
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_URL", endpoint)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_INDEX_PREFIX", prefix)
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _build_id: False))
    with Session(engine) as db, db.begin():
        control: LexicalBackendControl | None = db.get(LexicalBackendControl, 1)
        assert control is not None
        control.clusterUuid = httpx.get(endpoint, timeout=10, trust_env=False).raise_for_status().json()["cluster_uuid"]
    native = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="workload-owner", namespace="workload"), indexPrefix=prefix)
    try:
        receipt: dict[str, object] = task.run(request)
        assert receipt["status"] == "success" and receipt["charge_unchanged"] is True
        with Session(engine) as db:
            job: Job | None = db.get(Job, "actual-new-job")
            assert job is not None and job.status == "done"
            attempt: IndexingAttempt | None = db.get(IndexingAttempt, job.current_indexing_attempt_id)
            assert attempt is not None and attempt.state == "succeeded"
            assert receipt["parsed_handoff_at_utc"] == attempt.createdAt.astimezone(timezone.utc).isoformat()
            assert receipt["search_ready_at_utc"] == job.updated_at.replace(tzinfo=timezone.utc).isoformat()
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
