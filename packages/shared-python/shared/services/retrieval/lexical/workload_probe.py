"""Source-owned staging admission and namespace preparation for frozen campaigns.

This operator adapter exposes the prepared parser boundary, not a public API or a
billing simulation. It requires the physical issuer, staging opt-in and full SQL
visibility. Source transfer, parsing and model calls are outside this boundary.
"""

from datetime import datetime, timezone
import gzip
import importlib
from io import BytesIO
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import time
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.job import Job
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicallease import LexicalLease
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.operator_issuer import LexicalOperatorAuthority
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


class _Issuer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_token: str
    host_identity: str
    operation_id: str
    epoch: int = Field(ge=1)
    receipt_path: str


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    owner_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    source_file_name: str = Field(min_length=1, max_length=512)
    job_ids: list[str] = Field(min_length=1)
    document_ids_by_job: dict[str, str] = Field(default_factory=dict)
    prepared_path: str
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    compressed_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    uncompressed_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    unit_count: int = Field(ge=0)
    preseed_charge: int = Field(ge=0)

    @model_validator(mode="after")
    def validateTargets(self) -> "_Case":
        if (not self.document_ids_by_job.keys() <= set(self.job_ids)
            or len(set(self.job_ids)) != len(self.job_ids)
            or any(not document or len(document) > 36 for document in [self.document_id, *self.document_ids_by_job.values()])):
            raise ValueError("Approved prepared case has invalid or duplicate job/document identities")
        return self


class _CohortScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: LexicalScope
    index_name: str
    index_uuid: str
    action: Literal["verify", "open", "close"] = "verify"


class _Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scopes: list[_CohortScope] = Field(min_length=1)
    capacity_actions: list[_CohortScope] = Field(default_factory=list)
    expected_slot_state: Literal["open", "closed_capacity_available", "closed_capacity_wait"]
    artifact_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class _Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    journal_paths: list[str]
    maximum_journal_bytes: int = Field(ge=4096, le=1024 * 1024 * 1024)
    freshness_seconds: int = Field(ge=1, le=300)
    state_path: str | None = None
    export_path: str | None = None
    broker_url_file: str | None = None
    parse_queue_name: str | None = None


class _Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["opensearch-workload-source-v1"]
    campaign_id: str = Field(pattern="^[A-Za-z0-9_.:/-]{1,128}$")
    source_hash: str = Field(pattern="^[a-f0-9]{64}$")
    issuer: _Issuer
    cases: dict[str, _Case]
    cohorts: dict[str, _Cohort]
    resources: _Resources


class _JobRepository(Protocol):
    async def create_job(self, db: AsyncSession, *, user_id: str, job_type: str, source_type: str,
                         job_id: str, metadata: dict[str, object]) -> Job | None: ...


def _readPrivate(path: str, maximum: int, *, expectedSha256: str | None = None) -> dict[str, object]:
    source: Path = Path(path)
    descriptor: int = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata: os.stat_result = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > maximum:
            raise ValueError("Operator input must be a private bounded regular file")
        data: bytes = stream.read(maximum + 1)
    if len(data) > maximum or expectedSha256 is not None and sha256(data).hexdigest() != expectedSha256:
        raise ValueError("Operator input differs from its approved checksum or size")
    value: object = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Operator input must contain a JSON object")
    return cast(dict[str, object], value)


class WorkloadSourceAdapter:
    def __init__(self, *, configuration: dict[str, object], sessionFactory: sessionmaker[Session]) -> None:
        self.configuration: _Configuration = _Configuration.model_validate(configuration)
        self.sessions: sessionmaker[Session] = sessionFactory

    def _assertAuthorized(self) -> dict[str, object]:
        configuration = self.configuration
        if (settings.ENVIRONMENT != "staging" or settings.APP_ENV != "staging"
            or os.environ.get("KNOWHERE_WORKLOAD_SOURCE_CAMPAIGN") != configuration.campaign_id
            or settings.KNOWHERE_LEXICAL_BACKEND != "opensearch"):
            raise ValueError("Source workload adapter requires the explicitly named staging campaign")
        issuer: _Issuer = configuration.issuer
        recorded = _readPrivate(issuer.receipt_path, 16384)
        current = LexicalOperatorAuthority(sessionFactory=self.sessions).assertCurrent(
            invocationToken=issuer.invocation_token, hostIdentity=issuer.host_identity, operationId=issuer.operation_id,
            expectedSourceHash=configuration.source_hash, expectedEpoch=issuer.epoch)
        if any(recorded.get(key) != current[key] for key in ("invocation_token", "host_identity", "operation_id", "source_hash", "epoch", "cluster_uuid", "acquired_at_utc")):
            raise ValueError("Campaign issuer receipt differs from actual SQL authority")
        with self.sessions.begin() as db:
            control = LexicalCoordination.lockControl(db)
            if control.backend != "opensearch" or control.mode != "serving":
                raise ValueError("Campaign source admission requires the serving native backend")
        return current

    async def runAsync(self, request: dict[str, object], *, execute: bool = False,
                       asyncSessionFactory: async_sessionmaker[AsyncSession] | None = None) -> dict[str, object]:
        configuration = self.configuration
        if request.get("campaign_id") != configuration.campaign_id or request.get("source_hash") != configuration.source_hash:
            raise ValueError("Workload request differs from approved campaign/source")
        event: object = request.get("event")
        if event not in ("precondition", "admission", "resource", "maintenance", "stage-export"):
            raise ValueError("Unknown source workload event")
        receipt: dict[str, object] = {"mode": "actual" if execute else "dry-run", "status": "passed", "errors": [],
            "campaign_id": configuration.campaign_id, "source_hash": configuration.source_hash}
        if not execute:
            return receipt
        receipt["issuer"] = self._assertAuthorized()
        if event == "admission":
            if asyncSessionFactory is None:
                from shared.core.database import AsyncSessionFactory
                asyncSessionFactory = AsyncSessionFactory
            receipt.update(await self._admit(request, asyncSessionFactory))
        elif event == "precondition":
            receipt["cohorts"] = self._precondition()
        elif event == "maintenance":
            from shared.services.retrieval.lexical.maintenance import maintainLexical
            started: float = time.perf_counter()
            receipt.update({"completed": True, "processed_or_dispatched_work": maintainLexical(),
                "maintenance_ms": (time.perf_counter() - started) * 1000})
        else:
            from shared.services.retrieval.lexical.workload_resources import WorkloadResourceSampler
            sampler = WorkloadResourceSampler(configuration=configuration.model_dump(), sessionFactory=self.sessions)
            receipt.update(sampler.export() if event == "stage-export" else sampler.sample())
        return receipt

    async def _admit(self, request: dict[str, object], factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
        repositoryType = getattr(importlib.import_module("app.repositories.job_repository"), "JobRepository")
        repository: _JobRepository = cast(_JobRepository, repositoryType())
        arrival = request.get("arrival")
        if not isinstance(arrival, dict):
            raise ValueError("Workload admission requires an exact approved arrival")
        identity: str = str(arrival.get("id", ""))
        jobId: str = str(arrival.get("job_id", ""))
        caseId: str = str(arrival.get("case_id", ""))
        case: _Case | None = self.configuration.cases.get(caseId)
        if case is None or jobId not in case.job_ids or not identity or len(identity) > 128:
            raise ValueError("Workload admission is outside the approved cases/job identities")
        documentId: str = case.document_ids_by_job.get(jobId, case.document_id)
        kind: object = arrival.get("write_kind")
        if kind not in ("create", "update", "retry"):
            raise ValueError("Workload write kind is not approved")
        storage = CorpusStorage.resolve_namespace(case.namespace)
        if storage.is_demo:
            raise ValueError("Prepared workload writes require a private owned staging corpus")
        async with factory() as db:
            job: Job | None = await db.get(Job, jobId)
            document = await db.get(storage.Document, documentId)
            if document is not None and (document.user_id != case.owner_id or document.namespace != case.namespace):
                raise ValueError("Prepared workload document belongs to another scope")
            if kind != "create" and document is not None and (document.status == "archived"
                or job is not None and document.accepted_job_id != jobId):
                if job is None or job.user_id != case.owner_id or (job.job_metadata or {}).get("document_id") != documentId:
                    raise ValueError("Domain exclusion requires the actual owned historical job")
                return {"job_id": jobId, "exclusion_verified": True,
                    "exclusion_reason": "archived" if document.status == "archived" else "superseded"}
            if kind == "retry":
                if (job is None or job.user_id != case.owner_id or job.status != "failed" or job.failure_stage != "indexing"
                    or (job.job_metadata or {}).get("document_id") != documentId):
                    raise ValueError("Manual retry requires its actual eligible retained indexing failure")
                return {"job_id": jobId, "admitted": False, "retry_eligible": True,
                    "observed_at_utc": datetime.now(timezone.utc).isoformat()}
            if job is not None:
                raise ValueError("Arrival job already exists; a precreated job cannot qualify as a new admission")
            if kind == "create" and document is not None or kind == "update" and document is None:
                raise ValueError("Arrival create/update kind does not match actual document existence")
            path: Path = Path(case.prepared_path)
            with path.open("rb") as stream:
                compressed: bytes = stream.read(case.compressed_bytes + 1)
            if len(compressed) != case.compressed_bytes or sha256(compressed).hexdigest() != case.sha256:
                raise ValueError("Approved prepared case compressed bytes/checksum changed")
            with gzip.GzipFile(fileobj=BytesIO(compressed), mode="rb") as stream:
                decoded: bytes = stream.read(case.uncompressed_bytes + 1)
            if len(decoded) != case.uncompressed_bytes:
                raise ValueError("Approved prepared case uncompressed byte count changed")
            payload: object = json.loads(decoded)
            if (not isinstance(payload, dict) or not isinstance(payload.get("chunks"), list)
                or not isinstance(payload.get("result_s3_key"), str) or not payload.get("result_s3_key")
                or not isinstance(payload.get("checksum"), str) or not payload.get("checksum")
                or type(payload.get("zip_size")) is not int or payload["zip_size"] <= 0):
                raise ValueError("Approved prepared case parser boundary changed")
            # The repository owns IndexingAdmission.bind and commits its accepted
            # pointer with the new job. Never pre-admit the hour's future updates.
            job = await repository.create_job(db, user_id=case.owner_id, job_type="document_ingestion",
                source_type="file", job_id=jobId, metadata={"document_id": documentId, "namespace": case.namespace,
                    "source_file_name": case.source_file_name, "mixed_workload_campaign": self.configuration.campaign_id,
                    "mixed_workload_event_id": identity, "mixed_workload_case_id": caseId,
                    "mixed_workload_source_hash": self.configuration.source_hash, "prepared_sha256": case.sha256})
            if job is None:
                raise ValueError("Source repository refused campaign admission")
            admittedAt: str = datetime.now(timezone.utc).isoformat()
            job.billing_status = "charged"
            job.credits_charged = case.preseed_charge
            await db.commit()
            return {"job_id": jobId, "admitted": True,
                "admitted_at_utc": admittedAt,
                "prepared_parser_boundary_only": True, "billing_boundary": "preseeded-staging-charge"}

    def _precondition(self) -> dict[str, object]:
        coordinator: LexicalCoordination = LexicalCoordination(self.sessions)
        if not self.configuration.cohorts:
            raise ValueError("Actual preconditioning requires reviewed cohort scopes")
        approved: dict[str, list[dict[str, object]]] = {}
        for name, cohort in self.configuration.cohorts.items():
            for selected in cohort.capacity_actions:
                self._captureScope(selected, isPlanned=True)
            baseline: list[dict[str, object]] = [self._captureScope(selected, isPlanned=True) for selected in cohort.scopes]
            fingerprint: str = self._hashBaseline(baseline)
            if fingerprint != cohort.artifact_sha256:
                raise ValueError("Precondition semantic baseline differs from the reviewed frozen artifact")
            expected: str = "open" if cohort.expected_slot_state == "open" else "closed"
            if any(row["state"] != expected for row in baseline):
                raise ValueError("Reviewed preconditioning actions do not produce the requested cohort state")
            approved[name] = baseline
        # Validate every planned binding before any lifecycle mutation. The
        # post-action observation must still match that exact reviewed binding.
        cohorts: dict[str, object] = {}
        # The available-capacity and capacity-wait conditions describe different
        # real instants. Per-arrival stage measurements must still prove which
        # state was encountered during the later measured traffic.
        for name, cohort in self.configuration.cohorts.items():
            for selected in (*cohort.capacity_actions, *cohort.scopes):
                if selected.action == "verify":
                    continue
                self._assertAuthorized()
                native = self._getNative(selected.scope)
                try:
                    live = native.getLifecycleSnapshot(isExact=True, timeoutSeconds=5)
                    if live.getIndexUuid(selected.index_name) != selected.index_uuid:
                        raise ValueError("Precondition target changed before its reviewed lifecycle operation")
                    if selected.action == "open" and not live.isReady(selected.index_name):
                        coordinator.ensureIndex(scope=selected.scope, native=native)
                    elif selected.action == "close" and live.isOpen(selected.index_name):
                        self._closeScope(coordinator, native, selected)
                finally:
                    native.close()
            occupied: int = self._observeOccupied(coordinator, cohort.scopes[0].scope)
            actual: list[dict[str, object]] = [self._captureScope(selected, isPlanned=False) for selected in cohort.scopes]
            if (actual != approved[name] or occupied > 400
                or cohort.expected_slot_state == "closed_capacity_available" and occupied >= 400
                or cohort.expected_slot_state == "closed_capacity_wait" and occupied != 400):
                raise ValueError("Precondition actual bindings/slots differ from the frozen cohort")
            cohorts[name] = {"mode": "actual", "artifact_sha256": cohort.artifact_sha256,
                "observed_slot_state": cohort.expected_slot_state, "occupied_slots": occupied, "baseline": actual,
                "observed_at_utc": datetime.now(timezone.utc).isoformat()}
        return cohorts

    def _observeOccupied(self, coordinator: LexicalCoordination, scope: LexicalScope) -> int:
        native = self._getNative(scope)
        try:
            inventory = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=5)
            with self.sessions.begin() as db:
                control = LexicalCoordination.lockControl(db)
                if (control.clusterUuid, control.nodeIncarnation) != (inventory.cluster_uuid, inventory.getIncarnation()):
                    raise ValueError("Precondition capacity inventory differs from actual SQL identity")
                rows: list[LexicalNamespaceIndex] = list(db.scalars(select(LexicalNamespaceIndex)))
                if any((row.state == "open" and not inventory.isOpen(row.indexName))
                    or (row.state in ("closed", "uncreated") and inventory.isOpen(row.indexName)) for row in rows):
                    raise ValueError("Precondition capacity inventory contains unreconciled namespace state")
                return coordinator._countOccupied(rows, inventory)
        finally:
            native.close()

    @staticmethod
    def _hashBaseline(baseline: list[dict[str, object]]) -> str:
        return sha256(json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _getNative(scope: LexicalScope) -> OpenSearchLexicalIndex:
        return OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=scope,
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)

    def _captureScope(self, selected: _CohortScope, *, isPlanned: bool) -> dict[str, object]:
        storage = CorpusStorage.resolve_namespace(selected.scope.namespace)
        if storage.is_demo != (selected.scope.corpusKind == "demo") or storage.resolve_owner(selected.scope.ownerId) != selected.scope.ownerId:
            raise ValueError("Precondition scope ownership is inconsistent with its corpus")
        native = self._getNative(selected.scope)
        try:
            if native.indexName != selected.index_name:
                raise ValueError("Precondition target differs from approved scope index")
            live = native.getLifecycleSnapshot(isExact=True, timeoutSeconds=5)
        finally:
            native.close()
        with self.sessions.begin() as db:
            control = LexicalCoordination.lockControl(db)
            row = db.get(LexicalNamespaceIndex, selected.scope.fingerprint)
            if (row is None or row.indexName != selected.index_name or row.indexUuid != selected.index_uuid
                or live.getIndexUuid(selected.index_name) != selected.index_uuid
                or (control.clusterUuid, control.nodeIncarnation) != (live.cluster_uuid, live.getIncarnation())
                or not (row.state == "open" and live.isReady(row.indexName) or row.state == "closed" and live.isClosed(row.indexName))):
                raise ValueError("Precondition native/SQL index identity or settled state differs from the approved existing catalog")
            document, marker = storage.Document, storage.DocumentMapUnitIndex
            rows = db.execute(select(document.document_id, document.current_job_result_id, document.current_indexing_build_id,
                LexicalBuild.documentId, LexicalBuild.revisionId, LexicalBuild.namespaceIndexId, LexicalBuild.state,
                LexicalBuild.unitCount, marker.unit_count).outerjoin(LexicalBuild, LexicalBuild.id == document.current_indexing_build_id)
                .outerjoin(marker, and_(marker.document_id == document.document_id, marker.job_result_id == document.current_job_result_id))
                .where(document.user_id == selected.scope.ownerId, document.namespace == selected.scope.namespace,
                    document.status == "active").order_by(document.document_id)).tuples().all()
            bindings: list[list[str | int | None]] = []
            for documentId, revisionId, buildId, buildDocument, buildRevision, scopeId, buildState, nativeCount, pgCount in rows:
                if revisionId is not None and (buildDocument, buildRevision, scopeId, buildState, nativeCount) != (
                    documentId, revisionId, selected.scope.fingerprint, "published", pgCount):
                    raise ValueError("Precondition PG marker and registered native build unit counts differ")
                bindings.append([documentId, revisionId, buildId, nativeCount])
            state: str = ("open" if selected.action == "open" else "closed") if isPlanned and selected.action != "verify" else row.state
            return {"scope": selected.scope.model_dump(), "index_name": row.indexName, "index_uuid": row.indexUuid,
                "state": state, "published_bindings": bindings}

    def _closeScope(self, coordinator: LexicalCoordination, native: OpenSearchLexicalIndex, selected: _CohortScope) -> None:
        deadline: float = time.monotonic() + 5
        observation = coordinator._beginObservation(deadline)
        live = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=5)
        with coordinator._transaction(deadline) as db:
            control = LexicalCoordination.lockControl(db, isExclusive=True)
            rows = coordinator._reconcile(db, control, observation, live)
            row = next((item for item in rows if item.id == selected.scope.fingerprint), None)
            if (row is None or row.state != "open" or row.indexUuid != selected.index_uuid
                or db.scalar(select(LexicalLease.id).where(LexicalLease.namespaceIndexId == row.id, LexicalLease.state != "released").limit(1))
                or db.scalar(select(LexicalBuild.id).where(LexicalBuild.namespaceIndexId == row.id, LexicalBuild.state == "writing").limit(1))):
                raise ValueError("Preconditioning cannot close an unresolved or actively writing scope")
            operation = coordinator._reserve(db, control, selected.scope, row, "close")
        coordinator._execute(native, operation, deadline=deadline)
