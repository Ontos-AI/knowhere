"""Replay a frozen ranking manifest through the classic retrieval entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from types import ModuleType
from typing import Any, Literal, ParamSpec, Protocol, TypeVar, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from tests.contract.test_lexical_maintenance_contract import publishedInput as publishedInput  # noqa: F401

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


class _Document(Protocol):
    key: str
    namespace: str
    file_name: str
    chunks_path: Path
    replacement_chunks_path: Path | None
    replacement_file_name: str | None
    replace: bool


class _Evidence(Protocol):
    document: str
    chunk_id: str


class _Query(Protocol):
    id: str
    namespace: str
    text: str
    top_k: int
    expected: list[list[_Evidence]]
    replacement_expected: list[list[_Evidence]] | None
    include_documents: list[str] | None
    exclude_documents: list[str]
    exclude_sections: list[_ExcludedSection]
    chunk_types: list[str] | None
    internal_recall_k: int | None
    require_empty: bool


class _ExcludedSection(Protocol):
    document: str
    section_path: str


class _Suite(Protocol):
    kind: str

    def model_dump(self, *, mode: str) -> dict[str, object]: ...


class _Manifest(Protocol):
    provenance: str
    reviewer: str | None
    suite: _Suite
    derivation: dict[str, str | bool] | None
    documents: list[_Document]
    queries: list[_Query]


def _load_ranking_support() -> ModuleType:
    support_path: Path = (
        Path(__file__).resolve().parents[4]
        / "scripts/probes/opensearch-ranking-support.py"
    )
    specification = importlib.util.spec_from_file_location(
        "knowhere_ranking_support", support_path
    )
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _resolveRankingOutput(manifest: Path, *, configuredPath: Path) -> Path:
    output: Path = configuredPath.resolve()
    if output.is_relative_to(manifest.parent.resolve()):
        raise ValueError("Ranking report output must be outside the immutable fixture directory")
    if output.exists():
        raise ValueError("Ranking report output already exists; choose a new owned result file")
    if not output.parent.is_dir():
        raise ValueError("Create the owned ranking report directory before execution")
    return output


@contextmanager
def _selectPostgresqlReference(monkeypatch: pytest.MonkeyPatch,
                                factory: sessionmaker[Session]) -> Iterator[None]:
    """Select PG only in this process-owned isolated replay database.

    This compares explicit canonical PG token rows; it is not a product backend
    transition and does not exercise migration, epoch advancement or cutover.
    """
    from shared.core.config import settings
    from shared.models.database.lexicalcontrol import LexicalBackendControl

    with factory.begin() as db:
        control = db.get(LexicalBackendControl, 1, with_for_update=True)
        assert control is not None and control.mode == "serving"
        originalBackend: str = control.backend
        assert originalBackend == "opensearch", "The replay must restore its native selected backend"
        control.backend = "postgresql"
    try:
        with monkeypatch.context() as context:
            context.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
            yield
    finally:
        with factory.begin() as db:
            control = db.get(LexicalBackendControl, 1, with_for_update=True)
            assert control is not None
            control.backend = originalBackend


@pytest.mark.skipif(
    not os.environ.get("KNOWHERE_RANKING_MANIFEST"),
    reason="Set KNOWHERE_RANKING_MANIFEST to run the isolated ranking replay",
)
def test_frozen_ranking_across_publication(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    manifest_name: str = os.environ["KNOWHERE_RANKING_MANIFEST"]
    endpoint: str = os.environ["KNOWHERE_TEST_OPENSEARCH_URL"]
    assert urlsplit(endpoint).hostname in ("127.0.0.1", "::1"), (
        "Use an owned loopback test node"
    )
    manifest_path: Path = Path(manifest_name)
    support: ModuleType = _load_ranking_support()
    manifest: _Manifest = cast(_Manifest, support.ReplaySupport.loadManifest(manifest_path))
    output_path: Path = _resolveRankingOutput(manifest_path,
        configuredPath=Path(os.environ["KNOWHERE_RANKING_OUTPUT"]))
    assert manifest.suite.kind == os.environ.get(
        "KNOWHERE_RANKING_SUITE", manifest.suite.kind
    ), "The requested baseline/extended suite does not match the frozen manifest"

    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", endpoint)
    prefix: str = f"knowhere-ranking-{uuid4().hex[:10]}"
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_INDEX_PREFIX", prefix)
    asyncio.run(prepare_contract_storage())

    from shared.core.config import settings
    from shared.models.database.document import Document, DocumentChunk, DocumentSection
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.user import User
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.corpus_storage import CorpusStorage
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.input import LexicalInput
    from shared.services.retrieval.lexical.input_budget import LexicalInputBudget
    from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.preparation import LexicalPreparation
    from shared.services.retrieval.lexical.retry import IndexingRetry
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.map_unit_index import _to_section_row, _to_unit_row, replace_document_map_units
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.scoring.knowhere_provider import SectionRow, UnitRow
    from shared.services.retrieval.search.section_filters import is_excluded_section
    from shared.services.storage.result_storage import get_result_storage

    async def skip_cache(**_options: object) -> tuple[None, None]:
        return None, None

    def freeze_statistics(**_options: object) -> None:
        return None

    monkeypatch.setattr(plan, "_read_cached_response", skip_cache)
    monkeypatch.setattr(plan, "_schedule_hit_stats_update", freeze_statistics)
    # Canonical reference rows are built explicitly below. Optional shadow-copy
    # threads must not race the test-only temporary backend selection.
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _buildId: False))
    database_url: str = get_contract_database_url()
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg2"))
    factory: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    worker: IndexingWorker = IndexingWorker(sessionFactory=factory)
    cleanup: LexicalCleanup = LexicalCleanup(sessionFactory=factory)
    owner: str = "ranking-replay-owner"
    doc_ids: dict[str, str] = {
        doc.key: "doc_" + sha256(doc.key.encode()).hexdigest()[:24]
        for doc in manifest.documents
    }
    doc_keys: dict[str, str] = {identity: key for key, identity in doc_ids.items()}
    inputs: dict[Path, list[dict[str, Any]]] = {}
    summaries: dict[Path, dict[str, str]] = {}
    digests: dict[str, str] = {}
    inputMode: str = os.environ.get("KNOWHERE_RANKING_INPUT_MODE", "raw")
    assert inputMode in ("raw", "precompiled")
    report: dict[str, Any] = {
        "schema_version": "ranking-replay-v2",
        "suite": manifest.suite.model_dump(mode="json"),
        "provenance": manifest.provenance,
        "reviewer": manifest.reviewer,
        "derivation": manifest.derivation,
        "query_count": len(manifest.queries),
        "completed": False,
        "indexing_policy": "single-execution-per-attempt-with-explicit-manual-admission",
        "indexing_input_mode": inputMode,
        "raw_compilation_deadline_qualified": False,
        "indexing_deadline_seconds": settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS,
        "offline_input_preparation": [],
        "postgresql_reference": {
            "mode": "isolated-test-only",
            "token_source": "explicit-canonical-map-unit-construction-from-checksummed-prepared-chunks",
            "backend_selection": "temporary-runtime-settings-and-isolated-control-backend-restored-in-finally",
            "epoch_or_publication_pointer_changes": False,
            "optional_pg_copy_dispatch": "disabled-reference-built-explicitly",
            "qualifies_product_transition": False,
        },
        "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "inputs": digests,
        "stages": [],
        "visibility_observations": [],
        "unexpected_indexing_outcomes": [],
        "indexing_stage_timing": [],
        "compiler_startup": "warmed-before-execution-as-in-the-worker",
        "observation_policy": {
            "full_replay": "outside-production-indexing-deadline",
            "failed_write_stages": "after-intentional-failure-and-durable-settlement",
            "successful_retry_stage": "after-committed-activation",
            "inside_write": "authoritative-write-visibility-acknowledgment-only",
        },
    }

    activeLabel: str = ""

    def measureStage(name: str, operation: Callable[_Parameters, _Result]) -> Callable[_Parameters, _Result]:
        def measure(*arguments: _Parameters.args, **keywords: _Parameters.kwargs) -> _Result:
            started: float = perf_counter()
            try:
                return operation(*arguments, **keywords)
            finally:
                report["indexing_stage_timing"].append({"label": activeLabel, "stage": name,
                    "elapsed_milliseconds": (perf_counter() - started) * 1000})
        return measure

    monkeypatch.setattr(worker, "_prepareInput", measureStage("prepare_input", worker._prepareInput))
    monkeypatch.setattr(worker, "_readInputSource", measureStage("hydrate_input", worker._readInputSource))
    monkeypatch.setattr(worker, "_compileInput", measureStage("compile_input", worker._compileInput))
    monkeypatch.setattr(worker, "_assignInput", measureStage("assign_input", worker._assignInput))
    monkeypatch.setattr(worker, "_completeInput", measureStage("record_input", worker._completeInput))
    monkeypatch.setattr(worker, "_writeBuild", measureStage("native_write_and_visibility", worker._writeBuild))
    monkeypatch.setattr(worker, "_activate", measureStage("activate", worker._activate))
    monkeypatch.setattr(worker, "_recordFailure", measureStage("settle_failure", worker._recordFailure))
    monkeypatch.setattr(worker, "_finishUse", measureStage("release_use", worker._finishUse))
    measureAttempt: Callable[[str], str] = measureStage("whole_attempt", lambda identity: worker.run(identity))

    def runIndexing(jobId: str, *, label: str, expected: Literal["completed", "failed"]) -> None:
        nonlocal activeLabel
        activeLabel = label
        outcome: str = measureAttempt(jobId)
        if outcome == expected == "completed":
            return
        with factory.begin() as db:
            job: Job | None = db.get(Job, jobId)
            attempt: IndexingAttempt | None = db.scalars(
                select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)
                .order_by(IndexingAttempt.attemptNumber.desc()).limit(1)
            ).first()
            reason: str | None = attempt.errorMessage if attempt is not None else None
            if outcome == expected == "failed" and reason == "Injected indexing failure after visible write":
                return
            failure: dict[str, object] = {"label": label, "expected": expected, "outcome": outcome,
                "job_error_code": job.error_code if job is not None else None,
                "attempt_state": attempt.state if attempt is not None else None,
                "attempt_execution_count": attempt.executionCount if attempt is not None else None,
                "attempt_reason": reason[:512] if reason else None}
            report["unexpected_indexing_outcomes"].append(failure)
        pytest.fail(f"Ranking setup did not reach the expected indexing boundary: {failure}")

    def load_chunks(path: Path) -> list[dict[str, Any]]:
        if path not in inputs:
            data: bytes = (manifest_path.parent / path).read_bytes()
            digests[str(path)] = sha256(data).hexdigest()
            decoded = json.loads(data)
            inputs[path] = decoded["chunks"] if isinstance(decoded, dict) else decoded
            if isinstance(decoded, dict):
                summaries[path] = decoded.get("section_summaries") or {}
        return inputs[path]

    def prepareReplayInput(*, documentId: str, revisionId: str, jobId: str,
                           namespace: str, label: str) -> None:
        """Ranking-only preparation from this run's newly persisted canonical rows."""
        started: float = perf_counter()
        storage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        assert storage.DocumentSection is DocumentSection and storage.DocumentChunk is DocumentChunk
        with factory.begin() as db:
            LexicalInputBudget.assertCanHydrate(db, storage=storage,
                documentId=documentId, revisionId=revisionId)
            sections: tuple[SectionRow, ...] = tuple(_to_section_row(section) for section in db.scalars(
                select(DocumentSection).where(
                    DocumentSection.document_id == documentId, DocumentSection.job_result_id == revisionId,
                ).order_by(DocumentSection.sort_order, DocumentSection.section_id)))
            chunks: tuple[UnitRow, ...] = tuple(_to_unit_row(chunk) for chunk in db.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.document_id == documentId, DocumentChunk.job_result_id == revisionId,
                ).order_by(DocumentChunk.sort_order, DocumentChunk.chunk_id, DocumentChunk.id)))
        payload: LexicalInput = LexicalPreparation.prepare(documentId=documentId,
            revisionId=revisionId, sections=sections, chunks=chunks)
        key, checksum, byteCount = LexicalInputStorage().saveWithSize(
            jobId=jobId, revisionId=revisionId, payload=payload)
        with factory.begin() as db:
            attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(
                IndexingAttempt.jobId == jobId, IndexingAttempt.revisionId == revisionId,
                IndexingAttempt.attemptNumber == 1,
            ).with_for_update()).one()
            assert attempt.state == "pending" and attempt.executionCount == 0 and attempt.ownerToken is None
            result: JobResult | None = db.scalars(select(JobResult).where(
                JobResult.id == revisionId).with_for_update(of=JobResult)).one_or_none()
            assert result is not None and result.job_id == jobId
            assert result.indexing_input_key is None and result.indexing_input_checksum is None
            assert result.indexing_input_compiler == payload.compilerFingerprint
            LexicalInputRetention.assertRetained(db, result, key=key)
            result.indexing_input_key = key
            result.indexing_input_checksum = checksum
            result.indexing_input_bytes = byteCount
        report["offline_input_preparation"].append({"label": label, "checksum": checksum,
            "unit_count": len(payload.units), "compressed_bytes": byteCount,
            "elapsed_milliseconds": (perf_counter() - started) * 1000})

    def stage(doc: _Document, *, replacement: bool = False) -> tuple[str, str]:
        path: Path = (
            (doc.replacement_chunks_path or doc.chunks_path)
            if replacement
            else doc.chunks_path
        )
        file_name: str = (
            doc.replacement_file_name
            if replacement and doc.replacement_file_name
            else doc.file_name
        )
        job_id: str = "job_" + uuid4().hex[:24]
        with factory.begin() as db:
            db.add(
                Job(
                    job_id=job_id,
                    user_id=owner,
                    job_type="document_ingestion",
                    source_type="file",
                    status="running",
                    billing_status="charged",
                    credits_charged=100,
                    job_metadata={
                        "document_id": doc_ids[doc.key],
                        "namespace": doc.namespace,
                        "source_file_name": file_name,
                    },
                )
            )
            if replacement:
                current = db.get(Document, doc_ids[doc.key])
                assert current is not None
                current.accepted_job_id = job_id
        chunks: list[dict[str, Any]] = load_chunks(path)
        with factory.begin() as db:
            outcome = SyncJobSuccessFinalizer().finalize(
                db,
                job_id=job_id,
                result_s3_key=f"results/{job_id}.zip",
                checksum=digests[str(path)],
                zip_size=0,
                chunks=chunks,
                stored_count=len(chunks),
                delivery_mode="url",
                section_summaries=summaries.get(path),
            )
            assert outcome.response.should_commit()
            revision: str = db.scalars(
                select(JobResult.id).where(JobResult.job_id == job_id)
            ).one()
        if inputMode == "precompiled":
            prepareReplayInput(documentId=doc_ids[doc.key], revisionId=revision, jobId=job_id,
                namespace=doc.namespace, label=f"{doc.key}-replacement" if replacement else doc.key)
        return job_id, revision

    def build_pg(doc: _Document, revision: str) -> None:
        with factory.begin() as db:
            active = db.get(Document, doc_ids[doc.key])
            assert active is not None and active.current_job_result_id == revision
            replace_document_map_units(
                db,
                scope=DocumentPublicationScope(
                    user_id=owner,
                    namespace=doc.namespace,
                    document_id=doc_ids[doc.key],
                    job_result_id=revision,
                    source_file_name=active.source_file_name or "",
                ),
            )

    async def retrieve(queries: list[_Query]) -> dict[str, list[dict[str, Any]]]:
        database = create_async_engine(database_url)
        try:
            hits: dict[str, list[dict[str, Any]]] = {}
            async with AsyncSession(database) as db:
                for query in queries:
                    result = await plan.run_retrieval_query(
                        db=db,
                        user_id=owner,
                        namespace=query.namespace,
                        query=query.text,
                        top_k=query.top_k,
                        include_document_ids=[
                            doc_ids[key] for key in query.include_documents
                        ]
                        if query.include_documents is not None
                        else None,
                        exclude_document_ids=[doc_ids[key] for key in query.exclude_documents],
                        exclude_sections=[
                            {"document_id": doc_ids[section.document], "section_path": section.section_path}
                            for section in query.exclude_sections
                        ],
                        chunk_types=set(query.chunk_types) if query.chunk_types is not None else None,
                        internal_recall_k=query.internal_recall_k,
                        use_agentic=False,
                    )
                    assert result["router_used"] in ("classic_topk", "small_corpus_all", "empty_query_filtered"), query.id
                    hits[query.id] = result["results"]
                    await db.rollback()
            return hits
        finally:
            await database.dispose()

    def capture(
        label: str,
        native: OpenSearchLexicalIndex,
        queries: list[_Query],
        revisions: dict[str, str],
        baseline: dict[str, list[dict[str, Any]]],
        *,
        activated: bool = False,
    ) -> None:
        native_hits = asyncio.run(retrieve(queries))
        with httpx.Client(base_url=endpoint, timeout=5, trust_env=False) as client:
            response = client.post(
                f"/{native.indexName}/_search",
                json={
                    "size": 0,
                    "aggs": {
                        "builds": {
                            "terms": {"field": "buildId", "size": len(revisions) + 5}
                        }
                    },
                },
            )
            response.raise_for_status()
            physical: list[dict[str, Any]] = response.json()["aggregations"]["builds"][
                "buckets"
            ]
        stage_record: dict[str, Any] = {
            "stage": label,
            "observation_point": "outside-production-indexing-deadline",
            "namespace": queries[0].namespace,
            "physical_build_counts": {row["key"]: row["doc_count"] for row in physical},
            "queries": [],
        }
        report["stages"].append(stage_record)
        with factory.begin() as db:
            current: dict[str, str | None] = dict(
                db.execute(
                    select(
                        Document.document_id,
                        Document.current_job_result_id,
                    ).where(
                        Document.user_id == owner,
                        Document.namespace == queries[0].namespace,
                    )
                )
                .tuples()
                .all()
            )
            assert current == revisions
            for query in queries:
                results: dict[str, list[dict[str, Any]]] = {}
                ranks: dict[str, list[int | None]] = {}
                identity_failures: list[str] = []
                groups: list[list[_Evidence]] = (
                    (query.replacement_expected if query.replacement_expected is not None else query.expected)
                    if activated
                    else query.expected
                )
                for backend, hits in (
                    ("postgresql", baseline[query.id]),
                    ("opensearch", native_hits[query.id]),
                ):
                    rows: list[dict[str, Any]] = []
                    if len(hits) > query.top_k:
                        identity_failures.append(f"{backend}: exceeds frozen final result count")
                    if query.require_empty and hits:
                        identity_failures.append(f"{backend}: forbidden results in frozen empty-result case")
                    for hit in hits:
                        source = hit["source"]
                        identity: str = source["document_id"]
                        revision: str | None = revisions.get(identity)
                        key: str = doc_keys.get(identity, "unknown-document")
                        if revision is None:
                            identity_failures.append(f"{backend}: unpublished or out-of-namespace document")
                            continue
                        chunk = db.scalars(
                            select(DocumentChunk)
                            .join(DocumentSection)
                            .where(
                                DocumentChunk.document_id == identity,
                                DocumentChunk.job_result_id == revision,
                                DocumentChunk.chunk_id == hit["chunk_id"],
                                DocumentSection.section_path == source["section_path"],
                            )
                        ).first()
                        if chunk is None:
                            identity_failures.append(f"{backend}: citation does not resolve to a published canonical chunk")
                            continue
                        active = db.get(Document, identity)
                        assert active is not None
                        metadata = chunk.chunk_metadata or {}
                        canonical: dict[str, object] = {
                            "permission": True,
                            "revision": revision,
                            "source_revision": revision,
                            "source_file_name": active.source_file_name,
                            "chunk_type": chunk.chunk_type,
                            "content": str(metadata.get("summary") or "").strip() if chunk.chunk_type == "page" else (chunk.content or ""),
                            "file_path": chunk.file_path,
                            "source_chunk_path": chunk.source_chunk_path,
                        }
                        actual: dict[str, object] = {
                            "permission": (
                                active.user_id == owner and active.namespace == query.namespace
                                and active.status == "active"
                                and (query.include_documents is None or key in query.include_documents)
                                and key not in query.exclude_documents
                                and not is_excluded_section(
                                    document_id=identity, section_path=source["section_path"],
                                    exclude_sections=[{"document_id": doc_ids[section.document], "section_path": section.section_path} for section in query.exclude_sections],
                                )
                            ),
                            "revision": hit.get("job_result_id"),
                            "source_revision": source.get("job_result_id"),
                            "source_file_name": source.get("source_file_name"),
                            "chunk_type": hit.get("chunk_type"),
                            "content": hit.get("content"),
                            "file_path": hit.get("file_path"),
                            "source_chunk_path": hit.get("source_chunk_path"),
                        }
                        if chunk.chunk_type == "page":
                            canonical["pages"] = metadata.get("page_nums")
                            actual["pages"] = source.get("page_nums")
                        if chunk.chunk_type in ("image", "table") and chunk.file_path:
                            job_result = db.get(JobResult, revision)
                            assert job_result is not None
                            expected_asset: str | None = get_result_storage().generate_artifact_url(
                                job_id=job_result.job_id, artifact_ref=chunk.file_path
                            )
                            assert expected_asset, "Canonical test storage must resolve media assets"
                            canonical["asset_path"] = unquote(urlsplit(expected_asset).path)
                            actual["asset_path"] = unquote(urlsplit(str(hit.get("asset_url") or "")).path)
                        identity_failures.extend(
                            f"{backend}: {reason}"
                            for reason in support.ReplaySupport.findIdentityFailures(actual, canonical)
                        )
                        rows.append(
                            {
                                "document": key,
                                "revision": revision,
                                "chunk_id": hit["chunk_id"],
                                "section": source["section_path"],
                                "pages": source.get("page_nums", []),
                                "score": hit.get("score"),
                            }
                        )
                    results[backend] = rows
                    ranks[backend] = [
                        next(
                            (
                                position
                                for position, row in enumerate(rows, 1)
                                if any(
                                    row["document"] == evidence.document
                                    and row["chunk_id"] == evidence.chunk_id
                                    for evidence in group
                                )
                            ),
                            None,
                        )
                        for group in groups
                    ]
                stage_record["queries"].append(
                    {
                        "id": query.id,
                        "top_k": query.top_k,
                        "evidence_groups": [
                            [{"document": evidence.document, "chunk_id": evidence.chunk_id} for evidence in group]
                            for group in groups
                        ],
                        "identity_failures": identity_failures,
                        "ranks": ranks,
                        "results": results,
                    }
                )

    def pg_baseline(queries: list[_Query]) -> dict[str, list[dict[str, Any]]]:
        with _selectPostgresqlReference(monkeypatch, factory):
            return asyncio.run(retrieve(queries))

    indexes: list[OpenSearchLexicalIndex] = []
    try:
        LexicalPreparation.warmCompiler()
        with factory.begin() as db:
            db.add(User(id=owner, name=owner, email=f"{owner}@example.invalid"))
        for namespace in dict.fromkeys(query.namespace for query in manifest.queries):
            queries: list[_Query] = [
                query for query in manifest.queries if query.namespace == namespace
            ]
            documents: list[_Document] = [
                doc for doc in manifest.documents if doc.namespace == namespace
            ]
            targets: list[_Document] = [doc for doc in documents if doc.replace]
            assert len(targets) == 1, namespace
            target: _Document = targets[0]
            native = OpenSearchLexicalIndex(
                endpoint=endpoint,
                scope=LexicalScope(ownerId=owner, namespace=namespace),
                indexPrefix=prefix,
            )
            indexes.append(native)
            revisions: dict[str, str] = {}
            for doc in documents:
                job_id, revision = stage(doc)
                runIndexing(job_id, label=doc.key, expected="completed")
                build_pg(doc, revision)
                revisions[doc_ids[doc.key]] = revision
            old_pg = pg_baseline(queries)
            capture("before-staging", native, queries, revisions, old_pg)
            with factory.begin() as db:
                active = db.get(Document, doc_ids[target.key])
                assert active is not None and active.current_indexing_build_id
                old_build: str = active.current_indexing_build_id
            candidate, new_revision = stage(target, replacement=True)
            capture("parsed-unactivated", native, queries, revisions, old_pg)
            original_write = OpenSearchLexicalIndex.writeBuild

            def observe_write(
                index: OpenSearchLexicalIndex,
                *,
                buildId: str,
                units: tuple[LexicalUnit, ...],
                beforeBatch: Callable[[], None] | None = None,
            ) -> None:
                assert len(units) > 1
                batch = (
                    units[: len(units) // 2] if label == "partial-visible" else units
                )
                original_write(
                    index, buildId=buildId, units=batch, beforeBatch=beforeBatch
                )
                report["visibility_observations"].append({
                    "stage": label, "build_id": buildId, "written_units": len(batch),
                    "observation_point": "inside-write-after-authoritative-visibility-count-check",
                    "full_retrieval_matrix": False,
                })
                if label != "explicit-retry-visible":
                    raise LexicalIndexError(
                        "Injected indexing failure after visible write",
                        isRetryable=True,
                    )

            with monkeypatch.context() as fault:
                fault.setattr(OpenSearchLexicalIndex, "writeBuild", observe_write)
                for execution, label in enumerate(
                    (
                        "partial-visible",
                        "manual-retry-visible",
                        "complete-unactivated",
                    )
                ):
                    if execution:
                        with factory.begin() as db:
                            assert IndexingRetry().request(db, jobId=candidate, userId=owner)
                    runIndexing(candidate, label=label, expected="failed")
                    capture(
                        f"{label}-after-failure-and-settlement",
                        native,
                        queries,
                        revisions,
                        old_pg,
                    )
                    with factory.begin() as db:
                        attempt = db.scalars(
                            select(IndexingAttempt).where(
                                IndexingAttempt.jobId == candidate,
                                IndexingAttempt.attemptNumber == execution + 1,
                            )
                        ).one()
                        assert attempt.executionCount == 1
                        assert attempt.origin == ("initial" if execution == 0 else "manual")
                with factory.begin() as db:
                    assert IndexingRetry().request(db, jobId=candidate, userId=owner)
                label = "explicit-retry-visible"
                runIndexing(candidate, label=label, expected="completed")
            revisions[doc_ids[target.key]] = new_revision
            build_pg(target, new_revision)
            new_pg = pg_baseline(queries)
            capture(
                "activated-before-cleanup",
                native,
                queries,
                revisions,
                new_pg,
                activated=True,
            )
            with factory.begin() as db:
                failures: list[str] = list(
                    db.scalars(
                        select(LexicalBuild.id).where(
                            LexicalBuild.revisionId == new_revision,
                            LexicalBuild.state == "failed",
                        )
                    )
                )
                job = db.get(Job, candidate)
                assert (
                    job is not None
                    and job.credits_charged == 100
                    and job.billing_status == "charged"
                )
            assert len(failures) == 3
            assert cleanup.collect(failures[0])
            capture(
                "cleanup-in-progress",
                native,
                queries,
                revisions,
                new_pg,
                activated=True,
            )
            for obsolete in (*failures[1:], old_build):
                assert cleanup.collect(obsolete)
            capture("after-cleanup", native, queries, revisions, new_pg, activated=True)
        if inputMode == "precompiled":
            assert not any(stage["stage"] == "compile_input" for stage in report["indexing_stage_timing"])
        report["completed"] = True
        report["raw_compilation_deadline_qualified"] = inputMode == "raw"
    finally:
        if report["stages"]:
            report["quality_gate"] = support.ReplaySupport.evaluateReport(report)
            if not report["completed"]:
                report["quality_gate"]["passed"] = False
                report["quality_gate"]["incomplete_run"] = True
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            with httpx.Client(base_url=endpoint, timeout=10, trust_env=False) as client:
                for index in indexes:
                    response = client.delete(f"/{index.indexName}")
                    if response.status_code != 404:
                        response.raise_for_status()
        finally:
            for index in indexes:
                index.close()
            engine.dispose()

    assert report["quality_gate"]["passed"], (
        "The relative evidence/identity gate failed; inspect the persisted quality_gate groups and identity_failures"
    )


@pytest.fixture
def pgReferenceCorpus(
    publishedInput: tuple[Engine, str, str, str],
) -> tuple[sessionmaker[Session], str]:
    from shared.models.database.document import Document
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.publication_content import replace_document_revision_content
    from shared.services.retrieval.publication_models import DocumentPublicationScope

    engine, _, namespace, revision = publishedInput
    factory: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        document = db.scalars(select(Document).where(Document.current_job_result_id == revision)).one()
        replace_document_revision_content(db, scope=DocumentPublicationScope(
            user_id="maintenance-user", namespace=namespace, document_id=document.document_id,
            job_result_id=revision, source_file_name="reference.pdf"), chunks=[
                {"chunk_id": "reference-hit", "type": "text", "content": "frozen reference alpha marker", "path": "reference.pdf/Hit/body"},
                {"chunk_id": "reference-noise-a", "type": "text", "content": "unrelated reference paragraph", "path": "reference.pdf/Other/body"},
                {"chunk_id": "reference-noise-b", "type": "text", "content": "additional unrelated paragraph", "path": "reference.pdf/Extra/body"},
            ])
        control = db.get(LexicalBackendControl, 1)
        assert control is not None
        control.backend = "opensearch"
        control.mode = "serving"
        control.epoch = 17
        control.clusterUuid = "reference-cluster"
        control.nodeIncarnation = "reference-node"
    return factory, namespace


def test_pg_reference_reads_actual_nonempty_classic_tokens_and_restores_control(
    monkeypatch: pytest.MonkeyPatch, pgReferenceCorpus: tuple[sessionmaker[Session], str],
) -> None:
    from shared.core.config import settings
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.execution import plan

    factory, namespace = pgReferenceCorpus
    async def retrieveReference() -> dict[str, Any]:
        engine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(engine) as db:
                return await plan.run_retrieval_query(db=db, user_id="maintenance-user", namespace=namespace,
                    query="frozen reference alpha marker", top_k=1, include_document_ids=None,
                    exclude_document_ids=[], exclude_sections=[], use_agentic=False)
        finally:
            await engine.dispose()
    async def skipCache(**_options: object) -> tuple[None, None]:
        return None, None
    monkeypatch.setattr(plan, "_read_cached_response", skipCache)
    monkeypatch.setattr(plan, "_schedule_hit_stats_update", lambda **_options: None)
    with monkeypatch.context() as settingsOnly:
        settingsOnly.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
        with pytest.raises(UnavailableException):
            asyncio.run(retrieveReference())
    with _selectPostgresqlReference(monkeypatch, factory):
        actual = asyncio.run(retrieveReference())
    assert actual["router_used"] == "classic_topk"
    assert [row["chunk_id"] for row in actual["results"]] == ["reference-hit"]
    with factory.begin() as db:
        control = db.get(LexicalBackendControl, 1)
        assert control is not None
        assert (control.backend, control.mode, control.epoch, control.clusterUuid, control.nodeIncarnation) == (
            "opensearch", "serving", 17, "reference-cluster", "reference-node")
    assert settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"


def test_pg_reference_restores_backend_and_settings_when_query_fails(
    monkeypatch: pytest.MonkeyPatch, pgReferenceCorpus: tuple[sessionmaker[Session], str],
) -> None:
    from shared.core.config import settings
    from shared.models.database.lexicalcontrol import LexicalBackendControl

    factory, _ = pgReferenceCorpus
    injectedFailure: RuntimeError = RuntimeError("injected reference failure")
    caughtError: RuntimeError | None = None
    try:
        with _selectPostgresqlReference(monkeypatch, factory):
            assert settings.KNOWHERE_LEXICAL_BACKEND == "postgresql"
            with factory.begin() as db:
                control = db.get(LexicalBackendControl, 1)
                assert control is not None and control.backend == "postgresql"
            raise injectedFailure
    except RuntimeError as error:
        caughtError = error
    assert caughtError is injectedFailure
    with factory.begin() as db:
        control = db.get(LexicalBackendControl, 1)
        assert control is not None
        assert (control.backend, control.mode, control.epoch, control.clusterUuid, control.nodeIncarnation) == (
            "opensearch", "serving", 17, "reference-cluster", "reference-node")
    assert settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"


def test_staged_visibility_callback_excludes_full_replay_from_indexing_deadline() -> None:
    import ast
    tree: ast.Module = ast.parse(Path(__file__).read_text())
    callback = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "observe_write")
    calls: list[str] = [node.func.id for node in ast.walk(callback) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "capture" not in calls, "Full paired retrieval replay must run after the indexing execution has returned"
    original = next(node for node in ast.walk(callback) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "original_write")
    assert any(keyword.arg == "beforeBatch" for keyword in original.keywords), "Staged writes must retain the real publication-owner checkpoint"


def test_ranking_report_stays_outside_immutable_fixture(tmp_path: Path) -> None:
    fixture: Path = tmp_path / "fixture"
    fixture.mkdir()
    manifest: Path = fixture / "manifest.json"
    manifest.write_text("{}")
    alias: Path = tmp_path / "fixture-link"
    alias.symlink_to(fixture, target_is_directory=True)
    with pytest.raises(ValueError, match="fixture"):
        _resolveRankingOutput(alias / "manifest.json", configuredPath=fixture / "result.json")
    output: Path = tmp_path / "owned-ranking.json"
    assert _resolveRankingOutput(manifest, configuredPath=output) == output
    output.write_text("preserve previous result")
    with pytest.raises(ValueError, match="exists"):
        _resolveRankingOutput(manifest, configuredPath=output)
