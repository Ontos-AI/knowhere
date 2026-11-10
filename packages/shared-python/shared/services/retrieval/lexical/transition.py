"""Explicit operator migration with current-revision coverage and epoch fences."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from hashlib import sha256
import json
from math import isclose
from pathlib import Path
import time
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicallease import LexicalLease
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.settlement import IndexingSettlement
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.lexical.unit import LexicalUnit
from shared.services.retrieval.scoring.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
)
from shared.services.retrieval.scoring.persisted_score_load import (
    average_idf_from_unit_dfs,
)
from shared.services.retrieval.serving_generation import (
    advance_namespace_generation,
    lock_namespace_generation,
)

_Backend = Literal["postgresql", "opensearch"]
_Receipt = dict[str, object]


@dataclass(frozen=True)
class _Binding:
    corpusKind: Literal["private", "demo"]
    ownerId: str
    namespace: str
    documentId: str
    revisionId: str
    jobId: str
    inputKey: str | None
    inputChecksum: str | None
    compiler: str | None
    buildId: str | None

    @property
    def scope(self) -> LexicalScope:
        return LexicalScope(
            ownerId=self.ownerId, namespace=self.namespace, corpusKind=self.corpusKind
        )


class LexicalTransition:
    """Never choose latest-parsed candidates or restart historical jobs.

    Operator entry requires a DB role with explicit RLS bypass. Runtime roles
    that silently see no demo rows cannot produce a complete inventory receipt.
    JSON checkpoints are observational; resumption always verifies live bindings.
    """

    def __init__(self, *, sessionFactory: sessionmaker[Session]) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory

    @staticmethod
    def getSourceHash() -> str:
        repository: Path = Path(__file__).resolve().parents[6]
        digest = sha256()
        # Bind dependent token builders, publication configuration, migrations,
        # runtime and checked-in qualification fixtures to the reviewed source.
        # External frozen corpora/inventories have their own receipt hashes.
        roots: tuple[str, ...] = (
            "packages/shared-python/shared",
            "apps/api/app",
            "apps/worker/app",
            "apps/api/alembic",
            "apps/api/tests",
            "apps/worker/tests",
            "scripts/probes",
            "deploy",
        )
        suffixes: frozenset[str] = frozenset(
            {
                ".py",
                ".mjs",
                ".json",
                ".txt",
                ".toml",
                ".yaml",
                ".yml",
                ".sh",
                ".sql",
                ".service",
                ".timer",
                ".example",
                ".template",
                ".j2",
                ".jinja",
                ".jinja2",
                ".conf",
                ".ini",
                ".cfg",
                ".properties",
                ".mako",
            }
        )
        names: frozenset[str] = frozenset(
            {"Dockerfile", "Containerfile", "Makefile", "Procfile", ".dockerignore"}
        )
        excluded: frozenset[str] = frozenset(
            {
                "__pycache__",
                ".git",
                ".venv",
                "node_modules",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                "build",
                "dist",
                "logs",
            }
        )
        operatorOutputs: frozenset[str] = frozenset(
            {
                "secrets",
                "credentials",
                "runtime",
                "state",
                "receipts",
                "artifacts",
                "output",
                "outputs",
            }
        )

        def shouldInclude(source: Path) -> bool:
            parts: tuple[str, ...] = source.relative_to(repository).parts
            return (
                source.is_file()
                and (source.suffix in suffixes or source.name in names
                     or source.name.startswith(("Dockerfile.", "Containerfile.")))
                and not excluded.intersection(parts[:-1])
                and not (
                    parts[0] == "deploy" and operatorOutputs.intersection(parts[1:-1])
                )
            )

        sources: set[Path] = {
            source
            for root in roots
            for source in (repository / root).rglob("*")
            if shouldInclude(source)
        }
        for application in ("apps/api", "apps/worker"):
            sources.update(
                source
                for source in (repository / application).glob("*")
                if shouldInclude(source)
            )
        sources.update(
            repository / name
            for name in (
                "pyproject.toml",
                "uv.lock",
                ".python-version",
                "Makefile",
                "packages/shared-python/pyproject.toml",
                "apps/api/pyproject.toml",
                "apps/worker/pyproject.toml",
            )
            if (repository / name).exists()
        )
        for source in sorted(sources):
            data: bytes = source.read_bytes()
            digest.update(str(source.relative_to(repository)).encode() + b"\0")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        digest.update(LexicalPreparation.getCompilerFingerprint().encode())
        return digest.hexdigest()

    def run(
        self,
        verb: str,
        *,
        targetBackend: str,
        execute: bool = False,
        expectedEpoch: int | None = None,
        limit: int = 100,
        documentsPerSecond: float = 1.0,
    ) -> _Receipt:
        if targetBackend not in ("postgresql", "opensearch") or verb not in (
            "inspect",
            "gate",
            "settle",
            "backfill",
            "verify",
            "resume",
            "abort",
        ):
            raise ValueError("Unknown transition verb or target backend")
        if limit < 1 or documentsPerSecond <= 0:
            raise ValueError("Backfill limits must be positive")
        if execute and verb != "inspect" and expectedEpoch is None:
            raise LexicalIndexError(
                "An explicit expected epoch is required for operator execution"
            )
        target: _Backend = cast(_Backend, targetBackend)
        changed: dict[str, object] = {}
        activationCoverage: dict[str, object] | None = None
        if execute:
            with self.sessions.begin() as db:
                self._authorize(db)
                self._getControl(db, expectedEpoch=expectedEpoch)
            if verb == "gate":
                self._gate(cast(int, expectedEpoch))
            elif verb == "settle":
                changed["settledJobs"] = self._settle(cast(int, expectedEpoch), limit)
            elif verb == "backfill":
                changed["backfilledRevisions"] = self._backfill(
                    target, cast(int, expectedEpoch), limit, documentsPerSecond
                )
            elif verb in ("resume", "abort"):
                activationCoverage = self._resume(target, cast(int, expectedEpoch))
        control, bindings = self._capture()
        coverage: dict[str, object] = (
            activationCoverage
            if activationCoverage is not None
            else (
                self._verifyCoverage(
                    target,
                    bindings,
                    allowNativeOpen=execute,
                    epoch=cast(int, control["epoch"]),
                )
                if verb == "verify"
                else {
                    "complete": False,
                    "checked": 0,
                    "records": [],
                    "errors": [],
                    "status": "not_checked",
                }
            )
        )
        return {
            "version": 1,
            "verb": verb,
            "execute": execute,
            "targetBackend": target,
            "sourceHash": self.getSourceHash(),
            "control": control,
            "currentPublished": {
                "count": len(bindings),
                "fingerprint": self._fingerprint(bindings),
            },
            "coverage": coverage,
            "errors": coverage["errors"],
            **changed,
        }

    @staticmethod
    def _authorize(db: Session) -> None:
        if not db.scalar(
            text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ):
            raise LexicalIndexError(
                "Transition inventory requires an explicitly authorized maintenance role with full private/demo RLS visibility"
            )

    @staticmethod
    def _getControl(
        db: Session, *, expectedEpoch: int | None = None, lock: bool = False
    ) -> LexicalBackendControl:
        statement = select(LexicalBackendControl).where(LexicalBackendControl.id == 1)
        if lock:
            statement = statement.with_for_update()
        control: LexicalBackendControl | None = db.scalar(statement)
        if (
            control is None
            or expectedEpoch is not None
            and control.epoch != expectedEpoch
        ):
            raise LexicalIndexError("Transition control is absent or its epoch changed")
        return control

    @classmethod
    def _getBindings(cls, db: Session) -> tuple[_Binding, ...]:
        cls._authorize(db)
        bindings: list[_Binding] = []
        for kind in ("PRIVATE", "DEMO"):
            storage: CorpusStorage = CorpusStorage(
                cast(Literal["PRIVATE", "DEMO"], kind)
            )
            for document, result in db.execute(
                select(storage.Document, JobResult)
                .join(JobResult, JobResult.id == storage.Document.current_job_result_id)
                .where(
                    storage.Document.status == "active",
                    storage.Document.current_job_result_id.is_not(None),
                )
                .order_by(storage.Document.document_id)
            ):
                bindings.append(
                    _Binding(
                        "demo" if storage.is_demo else "private",
                        document.user_id,
                        document.namespace,
                        document.document_id,
                        result.id,
                        result.job_id,
                        result.indexing_input_key,
                        result.indexing_input_checksum,
                        result.indexing_input_compiler,
                        document.current_indexing_build_id,
                    )
                )
        return tuple(
            sorted(
                bindings,
                key=lambda item: (
                    item.corpusKind,
                    item.ownerId,
                    item.namespace,
                    item.documentId,
                ),
            )
        )

    def _capture(self) -> tuple[dict[str, object], tuple[_Binding, ...]]:
        with self.sessions.begin() as db:
            self._authorize(db)
            control: LexicalBackendControl = self._getControl(db)
            return {
                "backend": control.backend,
                "mode": control.mode,
                "epoch": control.epoch,
                "cluster_uuid": control.clusterUuid,
                "node_incarnation": control.nodeIncarnation,
            }, self._getBindings(db)

    @staticmethod
    def _fingerprint(bindings: tuple[_Binding, ...]) -> str:
        return sha256(
            json.dumps(
                [asdict(binding) for binding in bindings],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _gate(self, epoch: int) -> None:
        with self.sessions.begin() as db:
            self._authorize(db)
            control: LexicalBackendControl = self._getControl(
                db, expectedEpoch=epoch, lock=True
            )
            if control.mode != "serving":
                raise LexicalIndexError(
                    "Only a serving backend can start a new publication barrier"
                )
            control.mode = "gated"
            control.epoch += 1

    def _settle(self, epoch: int, limit: int) -> int:
        with self.sessions.begin() as db:
            self._authorize(db)
            control: LexicalBackendControl = self._getControl(
                db, expectedEpoch=epoch, lock=True
            )
            if control.mode != "gated":
                raise LexicalIndexError(
                    "Unfinished indexing can only be interrupted behind the publication barrier"
                )
            jobs: list[Job] = list(
                db.scalars(
                    select(Job)
                    .join(
                        IndexingAttempt,
                        IndexingAttempt.id == Job.current_indexing_attempt_id,
                    )
                    .where(
                        Job.status.notin_(("done", "failed")),
                        Job.parsing_status == "completed",
                        IndexingAttempt.state.in_(("pending", "running")),
                    )
                    .order_by(Job.job_id)
                    .limit(limit)
                    .with_for_update(of=Job)
                )
            )
            for job in jobs:
                attempt: IndexingAttempt = db.scalars(
                    select(IndexingAttempt)
                    .where(IndexingAttempt.id == job.current_indexing_attempt_id)
                    .with_for_update()
                ).one()
                IndexingSettlement.fail(
                    db,
                    job,
                    attempt,
                    message="Indexing interrupted by planned backend transition; explicitly retry this job after service resumes",
                    code="UNAVAILABLE",
                )
            return len(jobs)

    def _loadInput(self, binding: _Binding) -> LexicalInput:
        with self.sessions.begin() as db:
            result: JobResult = db.scalars(
                select(JobResult)
                .where(JobResult.id == binding.revisionId)
                .with_for_update(of=JobResult)
            ).one()
            LexicalInputRetention.assertRetained(db, result)
            if not result.indexing_input_key or not result.indexing_input_checksum:
                raise LexicalIndexError(
                    "Published revision has no assigned frozen lexical input"
                )
            key: str = result.indexing_input_key
            checksum: str = result.indexing_input_checksum
            compiler: str | None = result.indexing_input_compiler
            if (key, checksum, compiler) != (
                binding.inputKey,
                binding.inputChecksum,
                binding.compiler,
            ):
                raise LexicalIndexError(
                    "Published frozen input binding changed during verification"
                )
        payload: LexicalInput = LexicalInputStorage().load(key=key, checksum=checksum)
        if payload.compilerFingerprint != compiler or any(
            unit.documentId != binding.documentId
            or unit.revisionId != binding.revisionId
            for unit in payload.units
        ):
            raise LexicalIndexError(
                "Published frozen input does not match its revision or compiler"
            )
        return payload

    def _verifyCoverage(
        self,
        target: _Backend,
        bindings: tuple[_Binding, ...],
        *,
        db: Session | None = None,
        payloads: dict[str, LexicalInput] | None = None,
        nativeRecords: dict[str, dict[str, object]] | None = None,
        allowNativeOpen: bool = False,
        epoch: int | None = None,
    ) -> dict[str, object]:
        records: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        for binding in bindings:
            try:
                payload: LexicalInput = (
                    payloads[binding.revisionId]
                    if payloads is not None
                    else self._loadInput(binding)
                )
                if target == "postgresql":
                    if db is None:
                        with self.sessions.begin() as current:
                            record = self._verifyPostgresql(current, binding, payload)
                    else:
                        record = self._verifyPostgresql(db, binding, payload)
                else:
                    record = (
                        nativeRecords[binding.revisionId]
                        if nativeRecords is not None
                        else self._verifyNative(
                            binding, payload, allowOpen=allowNativeOpen, epoch=epoch
                        )
                    )
                    if db is not None:
                        self._checkNativeBinding(db, binding, str(record["buildId"]))
                        registry: LexicalNamespaceIndex | None = db.get(
                            LexicalNamespaceIndex, binding.scope.fingerprint
                        )
                        control: LexicalBackendControl = self._getControl(db)
                        if (
                            registry is None
                            or registry.state not in ("open", "closed")
                            or registry.operationId is not None
                            or registry.indexUuid != record["indexUuid"]
                            or control.clusterUuid != record["clusterUuid"]
                            or control.nodeIncarnation != record["nodeIncarnation"]
                        ):
                            raise LexicalIndexError(
                                "Verified native catalog identity changed before activation"
                            )
                records.append(record)
            except Exception as error:
                errors.append(
                    {
                        "documentId": binding.documentId,
                        "revisionId": binding.revisionId,
                        "error": str(error)
                        if isinstance(error, LexicalIndexError)
                        else type(error).__name__,
                    }
                )
        return {
            "complete": not errors and len(records) == len(bindings),
            "checked": len(records),
            "records": records,
            "errors": errors,
        }

    @staticmethod
    def _verifyPostgresql(
        db: Session, binding: _Binding, payload: LexicalInput
    ) -> dict[str, object]:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(binding.namespace)
        units = list(
            db.scalars(
                select(storage.DocumentMapUnit).where(
                    storage.DocumentMapUnit.document_id == binding.documentId,
                    storage.DocumentMapUnit.job_result_id == binding.revisionId,
                )
            )
        )
        expectedUnits: dict[str, LexicalUnit] = {
            unit.unitId: unit for unit in payload.units
        }
        if len(units) != len(expectedUnits) or {unit.unit_id for unit in units} != set(
            expectedUnits
        ):
            raise LexicalIndexError(
                "PG map-unit identities differ from frozen current revision"
            )
        for unit in units:
            expected: LexicalUnit = expectedUnits[unit.unit_id]
            if (
                unit.section_id,
                unit.path_token_count,
                unit.content_token_count,
                unit.has_image,
                unit.has_table,
                unit.sort_order,
            ) != (
                expected.sectionId,
                len(expected.pathTokens.split()),
                len(expected.contentTokens.split()),
                expected.hasImage,
                expected.hasTable,
                expected.sortOrder,
            ):
                raise LexicalIndexError(
                    "PG map-unit metadata differs from frozen current revision"
                )
        ids: dict[str, str] = {unit.unit_id: unit.id for unit in units}
        rows, pathFrequency, contentFrequency = LegacyLexicalCopy._prepareRows(
            payload, ids, binding.revisionId
        )
        expectedTokens: set[tuple[str, str, str, str, int]] = {
            (str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
            for row in rows
        }
        actualTokens = set(
            db.execute(
                select(
                    storage.DocumentMapUnitToken.map_unit_id,
                    storage.DocumentMapUnitToken.channel,
                    storage.DocumentMapUnitToken.token,
                    storage.DocumentMapUnitToken.token_hash,
                    storage.DocumentMapUnitToken.frequency,
                ).where(
                    storage.DocumentMapUnitToken.map_unit_id.in_(list(ids.values()))
                )
            ).tuples()
        )
        marker = db.scalar(
            select(storage.DocumentMapUnitIndex).where(
                storage.DocumentMapUnitIndex.document_id == binding.documentId,
                storage.DocumentMapUnitIndex.job_result_id == binding.revisionId,
            )
        )
        if (
            actualTokens != expectedTokens
            or marker is None
            or marker.unit_count != len(units)
            or marker.token_count != len(rows)
            or marker.format_version != MAP_UNIT_INDEX_FORMAT_VERSION
            or marker.path_document_count
            != sum(bool(unit.pathTokens) for unit in payload.units)
            or marker.content_document_count
            != sum(bool(unit.contentTokens) for unit in payload.units)
            or marker.path_total_length
            != sum(len(unit.pathTokens.split()) for unit in payload.units)
            or marker.content_total_length
            != sum(len(unit.contentTokens.split()) for unit in payload.units)
            or not isclose(
                marker.average_idf_path,
                average_idf_from_unit_dfs(
                    unit_count=len(units), token_document_frequency=pathFrequency
                ),
                abs_tol=1e-12,
            )
            or not isclose(
                marker.average_idf_content,
                average_idf_from_unit_dfs(
                    unit_count=len(units), token_document_frequency=contentFrequency
                ),
                abs_tol=1e-12,
            )
        ):
            raise LexicalIndexError(
                "PG current-published token/count coverage is incomplete"
            )
        return {
            "documentId": binding.documentId,
            "revisionId": binding.revisionId,
            "corpusKind": binding.corpusKind,
            "scope": binding.scope.fingerprint,
            "unitCount": len(units),
            "tokenCount": len(rows),
            "inputChecksum": binding.inputChecksum,
            "compilerFingerprint": payload.compilerFingerprint,
            "tokenHash": sha256(
                json.dumps(sorted(expectedTokens), separators=(",", ":")).encode()
            ).hexdigest(),
        }

    @staticmethod
    def _checkActiveProgressiveJobs(db: Session) -> None:
        if db.scalar(
            select(Job.job_id).where(
                Job.job_type == "document_ingestion",
                Job.job_metadata["ingestion_mode"].as_string() == "progressive",
                Job.status.notin_(("done", "failed")),
            ).limit(1)
        ):
            raise LexicalIndexError("Wait for active progressive publication jobs before native activation")

    def _resume(self, target: _Backend, epoch: int) -> dict[str, object]:
        capturedControl, bindings = self._capture()
        if capturedControl["epoch"] != epoch or capturedControl["mode"] != "gated":
            raise LexicalIndexError("Backend resume requires the exact gated epoch")
        requiresRollbackCoverage: bool = (
            target == "opensearch" and capturedControl["backend"] == "postgresql"
        )
        if requiresRollbackCoverage:
            with self.sessions.begin() as db:
                self._authorize(db)
                self._checkActiveProgressiveJobs(db)
        payloads: dict[str, LexicalInput] = {
            binding.revisionId: self._loadInput(binding) for binding in bindings
        }
        coverage = self._verifyCoverage(
            target, bindings, payloads=payloads, allowNativeOpen=True, epoch=epoch
        )
        if not coverage["complete"]:
            raise LexicalIndexError(
                "Backend resume refused: current-published coverage is incomplete"
            )
        if (
            requiresRollbackCoverage
            and not self._verifyCoverage("postgresql", bindings, payloads=payloads)[
                "complete"
            ]
        ):
            raise LexicalIndexError(
                "PG rollback coverage is incomplete; repair it before PG to OpenSearch activation"
            )
        nativeRecords: dict[str, dict[str, object]] = {
            str(record["revisionId"]): record
            for record in cast(list[dict[str, object]], coverage["records"])
        }
        with self.sessions.begin() as db:
            self._authorize(db)
            control: LexicalBackendControl = self._getControl(
                db, expectedEpoch=epoch, lock=True
            )
            if control.mode != "gated":
                raise LexicalIndexError(
                    "Backend activation requires its publication barrier"
                )
            if requiresRollbackCoverage:
                self._checkActiveProgressiveJobs(db)
            current: tuple[_Binding, ...] = self._getBindings(db)
            if self._fingerprint(current) != self._fingerprint(bindings):
                raise LexicalIndexError(
                    "Current published bindings changed during coverage verification"
                )
            verified = self._verifyCoverage(
                target, current, db=db, payloads=payloads, nativeRecords=nativeRecords
            )
            if not verified["complete"]:
                raise LexicalIndexError(
                    "Current-published target coverage changed before backend activation"
                )
            if (
                requiresRollbackCoverage
                and not self._verifyCoverage(
                    "postgresql", current, db=db, payloads=payloads
                )["complete"]
            ):
                raise LexicalIndexError(
                    "PG rollback coverage changed before backend activation"
                )
            if db.scalar(
                select(Job.job_id)
                .join(
                    IndexingAttempt,
                    IndexingAttempt.id == Job.current_indexing_attempt_id,
                )
                .where(
                    Job.status.notin_(("done", "failed")),
                    Job.parsing_status == "completed",
                    IndexingAttempt.state.in_(("pending", "running")),
                )
                .limit(1)
            ):
                raise LexicalIndexError(
                    "Settle unfinished indexing before backend activation"
                )
            seenScopes: set[str] = set()
            for binding in current:
                storage: CorpusStorage = CorpusStorage.resolve_namespace(
                    binding.namespace
                )
                document = db.scalars(
                    select(storage.Document)
                    .where(storage.Document.document_id == binding.documentId)
                    .with_for_update()
                ).one()
                document.current_indexing_build_id = (
                    str(nativeRecords[binding.revisionId]["buildId"])
                    if target == "opensearch"
                    else None
                )
                if binding.scope.fingerprint not in seenScopes:
                    generation = lock_namespace_generation(
                        db, user_id=binding.ownerId, namespace=binding.namespace
                    )
                    advance_namespace_generation(
                        db,
                        user_id=binding.ownerId,
                        namespace=binding.namespace,
                        locked_generation=generation,
                    )
                    seenScopes.add(binding.scope.fingerprint)
            control.backend = target
            control.mode = "serving"
            control.epoch += 1
        return {**coverage, "verifiedAtActivation": True}

    def _backfill(self, target: _Backend, epoch: int, limit: int, rate: float) -> int:
        control, bindings = self._capture()
        if control["epoch"] != epoch or not (
            control["mode"] == "gated"
            or control["mode"] == "serving"
            and control["backend"] == "postgresql"
            and target == "opensearch"
        ):
            raise LexicalIndexError(
                "Backfill requires its exact gated epoch or explicit PG-serving shadow preparation"
            )
        completed: int = 0
        for binding in bindings:
            if completed >= limit:
                break
            started: float = time.monotonic()
            binding = self._materializeInput(binding, epoch)
            if binding is None:
                continue
            payload: LexicalInput = self._loadInput(binding)
            coverage = self._verifyCoverage(
                target,
                (binding,),
                payloads={binding.revisionId: payload},
                allowNativeOpen=True,
                epoch=epoch,
            )
            if coverage["complete"]:
                continue
            if target == "opensearch":
                self._backfillNative(binding, epoch)
            else:
                self._repairPostgresql(binding, epoch, payload)
            completed += 1
            delay: float = 1.0 / rate - (time.monotonic() - started)
            if delay > 0:
                time.sleep(min(delay, 60.0))
        return completed

    def _repairPostgresql(
        self, binding: _Binding, epoch: int, payload: LexicalInput
    ) -> None:
        deadline: IndexingDeadline = IndexingDeadline(
            settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS
        )
        writer: PostgresqlPublication = PostgresqlPublication(
            sessionFactory=self.sessions, deadline=deadline
        )
        with self.sessions.begin() as db:
            self._authorize(db)
            control: LexicalBackendControl = self._getControl(db, expectedEpoch=epoch)
            if (
                control.mode != "gated"
                or self._getCurrentBinding(db, binding) != binding
            ):
                raise LexicalIndexError(
                    "PG repair requires the exact gated current-published binding"
                )
            timeout: str = f"{max(1, int(deadline.getRemainingSeconds() * 1000))}ms"
            db.execute(
                select(
                    func.set_config("statement_timeout", timeout, True),
                    func.set_config("lock_timeout", timeout, True),
                )
            )
            writer.writeFrozenRevision(
                db,
                documentId=binding.documentId,
                revisionId=binding.revisionId,
                storage=CorpusStorage.resolve_namespace(binding.namespace),
                payload=payload,
            )
        with self.sessions.begin() as db:
            self._getControl(db, expectedEpoch=epoch, lock=True)
            if self._getCurrentBinding(db, binding) != binding:
                return  # A newer publication/archival owns current coverage now.
            self._verifyPostgresql(db, binding, payload)
            if binding.buildId:
                build: LexicalBuild | None = db.get(LexicalBuild, binding.buildId)
                if build is not None and build.revisionId == binding.revisionId:
                    build.legacyCopyState = "completed"

    @staticmethod
    def _getCurrentBinding(db: Session, binding: _Binding) -> _Binding | None:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(binding.namespace)
        row = db.execute(
            select(storage.Document, JobResult)
            .join(JobResult, JobResult.id == storage.Document.current_job_result_id)
            .where(
                storage.Document.document_id == binding.documentId,
                storage.Document.user_id == binding.ownerId,
                storage.Document.namespace == binding.namespace,
                storage.Document.status == "active",
            )
        ).first()
        if row is None:
            return None
        document, result = row
        return _Binding(
            binding.corpusKind,
            document.user_id,
            document.namespace,
            document.document_id,
            result.id,
            result.job_id,
            result.indexing_input_key,
            result.indexing_input_checksum,
            result.indexing_input_compiler,
            document.current_indexing_build_id,
        )

    def _materializeInput(self, binding: _Binding, epoch: int) -> _Binding | None:
        """Freeze historical PG bags of terms; never call a tokenizer during migration."""
        if binding.inputKey:
            return binding
        storage: CorpusStorage = CorpusStorage.resolve_namespace(binding.namespace)
        with self.sessions.begin() as db:
            self._getControl(db, expectedEpoch=epoch)
            if self._getCurrentBinding(db, binding) != binding:
                return None
            result: JobResult = db.scalars(
                select(JobResult)
                .where(JobResult.id == binding.revisionId)
                .with_for_update(of=JobResult)
            ).one()
            LexicalInputRetention.assertRetained(db, result)
            marker = db.scalar(
                select(storage.DocumentMapUnitIndex).where(
                    storage.DocumentMapUnitIndex.document_id == binding.documentId,
                    storage.DocumentMapUnitIndex.job_result_id == binding.revisionId,
                )
            )
            if marker is None:
                raise LexicalIndexError(
                    "Historical published revision has no persisted PG completeness marker"
                )
            if marker.format_version != MAP_UNIT_INDEX_FORMAT_VERSION:
                raise LexicalIndexError(
                    "Historical PG prepared-unit format is incompatible; no retokenization is permitted"
                )
            rows = list(
                db.scalars(
                    select(storage.DocumentMapUnit)
                    .where(
                        storage.DocumentMapUnit.document_id == binding.documentId,
                        storage.DocumentMapUnit.job_result_id == binding.revisionId,
                    )
                    .order_by(
                        storage.DocumentMapUnit.sort_order,
                        storage.DocumentMapUnit.unit_id,
                    )
                )
            )
            if len(rows) != marker.unit_count or len(
                {row.unit_id for row in rows}
            ) != len(rows):
                raise LexicalIndexError("Historical PG unit coverage is incomplete")
            terms: dict[tuple[str, str], list[str]] = {}
            tokenRows = list(
                db.execute(
                    select(
                        storage.DocumentMapUnitToken.map_unit_id,
                        storage.DocumentMapUnitToken.channel,
                        storage.DocumentMapUnitToken.token,
                        storage.DocumentMapUnitToken.token_hash,
                        storage.DocumentMapUnitToken.frequency,
                    )
                    .where(
                        storage.DocumentMapUnitToken.map_unit_id.in_(
                            [row.id for row in rows]
                        )
                    )
                    .order_by(
                        storage.DocumentMapUnitToken.map_unit_id,
                        storage.DocumentMapUnitToken.channel,
                        storage.DocumentMapUnitToken.token,
                    )
                ).tuples()
            )
            if len(tokenRows) != marker.token_count or len(
                {(row[0], row[1], row[2]) for row in tokenRows}
            ) != len(tokenRows):
                raise LexicalIndexError("Historical PG token coverage is incomplete")
            for unitId, channel, token, tokenHash, frequency in tokenRows:
                if (
                    channel not in ("path", "content")
                    or frequency < 1
                    or tokenHash != sha256(token.encode()).hexdigest()
                ):
                    raise LexicalIndexError("Historical PG token row is invalid")
                terms.setdefault((unitId, channel), []).extend([token] * frequency)
            paths: dict[str, str] = dict(
                db.execute(
                    select(
                        storage.DocumentSection.section_id,
                        storage.DocumentSection.section_path,
                    ).where(
                        storage.DocumentSection.document_id == binding.documentId,
                        storage.DocumentSection.job_result_id == binding.revisionId,
                    )
                )
                .tuples()
                .all()
            )
            units: list[LexicalUnit] = []
            for row in rows:
                pathTokens: list[str] = terms.get((row.id, "path"), [])
                contentTokens: list[str] = terms.get((row.id, "content"), [])
                if (
                    len(pathTokens) != row.path_token_count
                    or len(contentTokens) != row.content_token_count
                    or row.section_id not in paths
                ):
                    raise LexicalIndexError(
                        "Historical PG token lengths or canonical section binding differ"
                    )
                units.append(
                    LexicalUnit(
                        documentId=binding.documentId,
                        revisionId=binding.revisionId,
                        unitId=row.unit_id,
                        sectionId=row.section_id,
                        pathTokens=" ".join(pathTokens),
                        contentTokens=" ".join(contentTokens),
                        sectionPath=paths[row.section_id],
                        sortOrder=row.sort_order,
                        hasImage=row.has_image,
                        hasTable=row.has_table,
                    )
                )
            compiler: str = (
                result.indexing_input_compiler
                or sha256(
                    f"historical-pg-prepared-format:{marker.format_version}".encode()
                ).hexdigest()
            )
            payload: LexicalInput = LexicalInput(
                compilerFingerprint=compiler, units=tuple(units)
            )
        key, checksum, size = LexicalInputStorage().saveWithSize(
            jobId=binding.jobId, revisionId=binding.revisionId, payload=payload
        )
        with self.sessions.begin() as db:
            self._getControl(db, expectedEpoch=epoch, lock=True)
            current: _Binding | None = self._getCurrentBinding(db, binding)
            if current is None or current.revisionId != binding.revisionId:
                return None
            result = db.scalars(
                select(JobResult)
                .where(JobResult.id == binding.revisionId)
                .with_for_update(of=JobResult)
            ).one()
            LexicalInputRetention.assertRetained(db, result, key=key)
            if result.indexing_input_key is None:
                result.indexing_input_key = key
                result.indexing_input_checksum = checksum
                result.indexing_input_compiler = compiler
                result.indexing_input_bytes = size
            db.flush()
            return self._getCurrentBinding(db, binding)

    def _createNative(self, scope: LexicalScope) -> OpenSearchLexicalIndex:
        return OpenSearchLexicalIndex(
            endpoint=settings.KNOWHERE_OPENSEARCH_URL,
            scope=scope,
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
            maximumBulkBytes=settings.KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES,
            deadline=IndexingDeadline(settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS),
        )

    def _coordinator(self, epoch: int) -> LexicalCoordination:
        class OperatorCoordination(LexicalCoordination):
            @staticmethod
            def lockControl(
                db: Session, *, isExclusive: bool = False, requiresServing: bool = True
            ) -> LexicalBackendControl:
                control: LexicalBackendControl = LexicalCoordination.lockControl(
                    db, isExclusive=isExclusive, requiresServing=False
                )
                if control.epoch != epoch or not (
                    control.mode == "gated"
                    or control.mode == "serving"
                    and control.backend == "postgresql"
                ):
                    raise LexicalIndexError(
                        "Operator shadow backfill lost its exact backend/epoch authorization"
                    )
                return control

        return OperatorCoordination(self.sessions)

    def _checkNativeBinding(
        self, db: Session, binding: _Binding, buildId: str
    ) -> LexicalBuild:
        build: LexicalBuild | None = db.get(LexicalBuild, buildId)
        if (
            build is None
            or build.documentId != binding.documentId
            or build.revisionId != binding.revisionId
            or build.namespaceIndexId != binding.scope.fingerprint
            or build.state != "published"
        ):
            raise LexicalIndexError(
                "Native build does not cover the current published revision"
            )
        return build

    def _findBuild(self, binding: _Binding) -> str:
        with self.sessions.begin() as db:
            statement = (
                select(LexicalBuild)
                .where(
                    LexicalBuild.documentId == binding.documentId,
                    LexicalBuild.revisionId == binding.revisionId,
                    LexicalBuild.namespaceIndexId == binding.scope.fingerprint,
                    LexicalBuild.state == "published",
                )
                .order_by(LexicalBuild.createdAt.desc(), LexicalBuild.id)
            )
            build: LexicalBuild | None = db.scalars(statement).first()
            if build is None:
                raise LexicalIndexError(
                    "Current published revision has no prepared native build"
                )
            return build.id

    def _verifyNative(
        self,
        binding: _Binding,
        payload: LexicalInput,
        *,
        allowOpen: bool = False,
        epoch: int | None = None,
    ) -> dict[str, object]:
        buildId: str = self._findBuild(binding)
        native: OpenSearchLexicalIndex = self._createNative(binding.scope)
        try:
            with self.sessions.begin() as db:
                build = self._checkNativeBinding(db, binding, buildId)
                registry: LexicalNamespaceIndex | None = db.get(
                    LexicalNamespaceIndex, binding.scope.fingerprint
                )
                if (
                    registry is None
                    or registry.indexName != native.indexName
                    or not registry.indexUuid
                    or build.unitCount != len(payload.units)
                ):
                    raise LexicalIndexError(
                        "Native registry/count does not match current coverage"
                    )
                indexUuid: str = registry.indexUuid
            snapshot: NativeLifecycleSnapshot = native.getLifecycleSnapshot(
                isExact=True,
                timeoutSeconds=settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS,
            )
            if snapshot.isClosed(native.indexName) and allowOpen:
                if epoch is None:
                    raise LexicalIndexError(
                        "Explicit native reopen requires its exact operator epoch"
                    )
                with self.sessions.begin() as db:
                    currentControl: LexicalBackendControl = self._getControl(
                        db, expectedEpoch=epoch
                    )
                    usesNormalServing: bool = (
                        currentControl.backend == "opensearch"
                        and currentControl.mode == "serving"
                    )
                coordination: LexicalCoordination = (
                    LexicalCoordination(self.sessions)
                    if usesNormalServing
                    else self._coordinator(epoch)
                )
                reopenedUuid, reopenedEpoch = coordination.ensureIndex(
                    scope=binding.scope, native=native
                )
                if reopenedUuid != indexUuid or reopenedEpoch != epoch:
                    raise LexicalIndexError(
                        "Coverage reopen crossed a native identity or backend epoch"
                    )
                snapshot = native.getLifecycleSnapshot(
                    isExact=True,
                    timeoutSeconds=settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS,
                )
            with self.sessions.begin() as db:
                control: LexicalBackendControl = self._getControl(
                    db, expectedEpoch=epoch
                )
                if (
                    snapshot.cluster_uuid != control.clusterUuid
                    or snapshot.getIncarnation() != control.nodeIncarnation
                    or snapshot.getIndexUuid(native.indexName) != indexUuid
                    or not snapshot.isReady(native.indexName)
                ):
                    raise LexicalIndexError(
                        "Native coverage is closed, unavailable or has changed its identity"
                    )
            actual: tuple[LexicalUnit, ...] = self._readNativeUnits(native, buildId)
            if {unit.unitId: unit for unit in actual} != {
                unit.unitId: unit for unit in payload.units
            } or len(actual) != len(payload.units):
                raise LexicalIndexError(
                    "Native exact unit/token/input coverage is incomplete"
                )
            return {
                "documentId": binding.documentId,
                "revisionId": binding.revisionId,
                "corpusKind": binding.corpusKind,
                "scope": binding.scope.fingerprint,
                "buildId": buildId,
                "indexUuid": indexUuid,
                "clusterUuid": snapshot.cluster_uuid,
                "nodeIncarnation": snapshot.getIncarnation(),
                "unitCount": len(actual),
                "inputChecksum": binding.inputChecksum,
                "compilerFingerprint": payload.compilerFingerprint,
            }
        finally:
            native.close()

    @staticmethod
    def _readNativeUnits(
        native: OpenSearchLexicalIndex, buildId: str
    ) -> tuple[LexicalUnit, ...]:
        units: list[LexicalUnit] = []
        cursor: list[object] | None = None
        while True:
            request: dict[str, object] = {
                "size": 1000,
                "query": {"term": {"buildId": buildId}},
                "sort": [{"unitId": "asc"}],
                "track_total_hits": True,
            }
            if cursor is not None:
                request["search_after"] = cursor
            response: object = native._request(
                "POST", f"/{native.indexName}/_search", payload=request
            )
            if (
                not isinstance(response, dict)
                or response.get("timed_out") is not False
                or not isinstance(response.get("_shards"), dict)
                or response["_shards"].get("failed") != 0
            ):
                raise LexicalIndexError("Native coverage query did not complete")
            hitsObject: object = response.get("hits")
            hits: object = (
                hitsObject.get("hits") if isinstance(hitsObject, dict) else None
            )
            if not isinstance(hits, list):
                raise LexicalIndexError("Native coverage query has invalid hits")
            if not hits:
                return tuple(units)
            for hit in hits:
                if (
                    not isinstance(hit, dict)
                    or not isinstance(hit.get("_source"), dict)
                    or hit["_source"].get("buildId") != buildId
                ):
                    raise LexicalIndexError("Native coverage query escaped its build")
                source: dict[str, object] = dict(hit["_source"])
                source.pop("buildId")
                units.append(LexicalUnit.model_validate(source))
            nextCursor: object = hits[-1].get("sort")
            if not isinstance(nextCursor, list) or nextCursor == cursor:
                raise LexicalIndexError("Native coverage pagination did not advance")
            cursor = nextCursor

    def _backfillNative(self, binding: _Binding, epoch: int) -> None:
        payload: LexicalInput = self._loadInput(binding)
        try:
            self._verifyNative(binding, payload, allowOpen=True, epoch=epoch)
            return
        except LexicalIndexError:
            pass
        native: OpenSearchLexicalIndex = self._createNative(binding.scope)
        coordinator: LexicalCoordination = self._coordinator(epoch)
        buildId: str | None = None
        leaseId: str | None = None
        try:
            indexUuid, confirmedEpoch = coordinator.ensureIndex(
                scope=binding.scope, native=native
            )
            if confirmedEpoch != epoch:
                raise LexicalIndexError("Shadow backfill crossed a backend epoch")
            with self.sessions.begin() as db:
                coordinator.lockControl(db)
                if self._getCurrentBinding(db, binding) != binding:
                    return
                db.scalars(
                    select(Job).where(Job.job_id == binding.jobId).with_for_update()
                ).one()
                number: int = (
                    int(
                        db.scalar(
                            select(
                                func.coalesce(
                                    func.max(IndexingAttempt.attemptNumber), 0
                                )
                            ).where(IndexingAttempt.jobId == binding.jobId)
                        )
                        or 0
                    )
                    + 1
                )
                token: str = str(uuid4())
                now = LexicalCoordination.getDatabaseTime(db)
                attempt: IndexingAttempt = IndexingAttempt(
                    jobId=binding.jobId,
                    revisionId=binding.revisionId,
                    attemptNumber=number,
                    origin="migration",
                    state="running",
                    executionCount=1,
                    ownerToken=token,
                    backendEpoch=epoch,
                    startedAt=now,
                    executionDeadlineAt=now
                    + timedelta(seconds=settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS),
                )
                db.add(attempt)
                db.flush()
                build: LexicalBuild = LexicalBuild(
                    attemptId=attempt.id,
                    namespaceIndexId=binding.scope.fingerprint,
                    documentId=binding.documentId,
                    revisionId=binding.revisionId,
                    ownerToken=token,
                    unitCount=len(payload.units),
                )
                db.add(build)
                db.flush()
                buildId = build.id
                attempt.currentBuildId = buildId
                lease: LexicalLease = LexicalLease(
                    namespaceIndexId=binding.scope.fingerprint,
                    kind="index_use",
                    ownerToken=token,
                    backendEpoch=epoch,
                    buildId=buildId,
                    expiresAt=attempt.executionDeadlineAt,
                )
                db.add(lease)
                db.flush()
                leaseId = lease.id
            native.writeBuild(
                buildId=buildId,
                units=payload.units,
                beforeBatch=lambda: self._assertShadow(binding, epoch, buildId),
            )
            actual: tuple[LexicalUnit, ...] = self._readNativeUnits(native, buildId)
            if {unit.unitId: unit for unit in actual} != {
                unit.unitId: unit for unit in payload.units
            } or len(actual) != len(payload.units):
                raise LexicalIndexError(
                    "Shadow build exact coverage differs from its frozen input"
                )
            observedUuid: str = native.getIndexUuid()
            self._assertShadow(binding, epoch, buildId)
            with self.sessions.begin() as db:
                coordinator.lockControl(db)
                build = db.scalars(
                    select(LexicalBuild)
                    .where(LexicalBuild.id == buildId)
                    .with_for_update()
                ).one()
                attempt = db.scalars(
                    select(IndexingAttempt).where(IndexingAttempt.id == build.attemptId)
                ).one()
                registry = db.scalars(
                    select(LexicalNamespaceIndex).where(
                        LexicalNamespaceIndex.id == binding.scope.fingerprint
                    )
                ).one()
                if (
                    registry.indexUuid != indexUuid
                    or registry.state != "open"
                    or observedUuid != indexUuid
                    or attempt.state != "running"
                    or attempt.ownerToken != build.ownerToken
                    or attempt.executionDeadlineAt is None
                    or attempt.executionDeadlineAt
                    <= LexicalCoordination.getDatabaseTime(db)
                    or self._getCurrentBinding(db, binding) != binding
                ):
                    raise LexicalIndexError(
                        "Shadow build lost its current-published or native identity"
                    )
                build.state = "published"
                build.legacyCopyState = "completed"
                attempt.state = "succeeded"
                attempt.finishedAt = LexicalCoordination.getDatabaseTime(db)
                db.scalars(
                    select(LexicalLease).where(LexicalLease.id == leaseId)
                ).one().state = "released"
        except Exception:
            if buildId:
                with self.sessions.begin() as db:
                    build = db.get(LexicalBuild, buildId)
                    if build is not None:
                        if build.state != "writing":
                            raise  # Preserve any committed shadow publication after response/commit loss.
                        build.state = "failed"
                        build.cleanupState = "pending"
                        attempt = db.scalars(
                            select(IndexingAttempt).where(
                                IndexingAttempt.id == build.attemptId
                            )
                        ).one()
                        attempt.state = "failed"
                        attempt.finishedAt = LexicalCoordination.getDatabaseTime(db)
                    if leaseId:
                        db.scalars(
                            select(LexicalLease).where(LexicalLease.id == leaseId)
                        ).one().state = "uncertain"
            raise
        finally:
            native.close()

    def _assertShadow(self, binding: _Binding, epoch: int, buildId: str) -> None:
        with self.sessions.begin() as db:
            self._coordinator(epoch).lockControl(db)
            if self._getCurrentBinding(db, binding) != binding:
                raise LexicalIndexError(
                    "Shadow build no longer targets the current published revision"
                )
            build: LexicalBuild | None = db.get(LexicalBuild, buildId)
            if build is None or build.state != "writing":
                raise LexicalIndexError("Shadow build lost its write authority")
            now = LexicalCoordination.getDatabaseTime(db)
            attempt: IndexingAttempt | None = db.get(IndexingAttempt, build.attemptId)
            lease: LexicalLease | None = db.scalar(
                select(LexicalLease)
                .where(
                    LexicalLease.buildId == buildId,
                    LexicalLease.ownerToken == build.ownerToken,
                    LexicalLease.kind == "index_use",
                    LexicalLease.state == "active",
                )
                .limit(1)
            )
            registry: LexicalNamespaceIndex | None = db.get(
                LexicalNamespaceIndex, binding.scope.fingerprint
            )
            if (
                attempt is None
                or attempt.origin != "migration"
                or attempt.state != "running"
                or attempt.executionCount != 1
                or attempt.ownerToken != build.ownerToken
                or attempt.currentBuildId != buildId
                or attempt.backendEpoch != epoch
                or attempt.executionDeadlineAt is None
                or attempt.executionDeadlineAt <= now
                or lease is None
                or lease.backendEpoch != epoch
                or lease.expiresAt <= now
                or registry is None
                or registry.state != "open"
                or registry.indexUuid is None
            ):
                raise LexicalIndexError(
                    "Shadow execution lost its owner, deadline or native-use reservation"
                )
