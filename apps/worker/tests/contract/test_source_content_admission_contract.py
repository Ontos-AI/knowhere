from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from support.worker_parse_contract import WorkerParseContract


@dataclass(frozen=True)
class SourceJob:
    job_id: str
    user_id: str
    document_id: str
    local_path: Path
    storage_key: str


def create_source_job(
    contract: WorkerParseContract,
    directory: Path,
    *,
    content: bytes,
    user_id: str | None = None,
    namespace: str = "worker-contract",
    file_name: str = "original.md",
    document_id: str | None = None,
    source_type: str = "file",
) -> SourceJob:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job

    record: dict[str, object] = contract.create_file_job(
        source_file_name=file_name,
        user_id=user_id,
    )
    job_id: str = str(record["job_id"])
    effective_document_id: str = document_id or f"doc_{uuid4().hex[:12]}"
    path: Path = directory / job_id
    path.write_bytes(content)
    storage_key: str = str(record["s3_key"])
    contract.upload_source_file(local_file_path=path, s3_key=storage_key)
    with get_sync_db_context() as database:
        job: Job | None = database.get(Job, job_id)
        assert job is not None
        job.source_type = source_type
        job.job_metadata = {
            **dict(job.job_metadata or {}),
            "namespace": namespace,
            "document_id": effective_document_id,
            "original_request": {"document_id": document_id},
        }
    return SourceJob(
        job_id=job_id,
        user_id=str(record["user_id"]),
        document_id=effective_document_id,
        local_path=path,
        storage_key=storage_key,
    )


def admit_source(source: SourceJob) -> dict[str, object]:
    from app.services.document_ingestion.source_content_admission import (
        SourceContentAdmission,
    )

    return SourceContentAdmission().admit_source(
        job_id=source.job_id,
        local_file_path=str(source.local_path),
    )


def publish_source(source: SourceJob, *, namespace: str = "worker-contract") -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult

    with get_sync_db_context() as database:
        job: Job | None = database.get(Job, source.job_id)
        assert job is not None
        job.status = "done"
        document: Document | None = database.get(Document, source.document_id)
        if document is None:
            document = Document(
                document_id=source.document_id,
                user_id=source.user_id,
                namespace=namespace,
                source_file_name="original.md",
            )
            database.add(document)
            database.flush()
        result: JobResult = JobResult(
            job_id=source.job_id,
            document_id=source.document_id,
            delivery_mode="inline",
        )
        database.add(result)
        database.flush()
        document.current_job_result_id = result.id


@pytest.mark.parametrize("file_name", ["original.md", "renamed.md"])
@pytest.mark.parametrize("source_type", ["file", "url"])
def test_rejects_identical_content_independently_of_filename_and_source_type(
    worker_contract_environment: None,
    tmp_path: Path,
    file_name: str,
    source_type: str,
) -> None:
    from shared.core.exceptions.domain_exceptions import ConflictException

    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(
        contract, tmp_path, content=b"original content"
    )
    admit_source(existing)
    publish_source(existing)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"original content",
        user_id=existing.user_id,
        file_name=file_name,
        source_type=source_type,
    )

    with pytest.raises(ConflictException) as failure:
        admit_source(incoming)
    assert failure.value.to_client(incoming.job_id)["error"]["details"] == {
        "reason": "ALREADY_EXISTS",
        "resource": "Document",
        "id": existing.document_id,
    }


def test_accepts_same_filename_and_same_size_when_bytes_differ(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(
        contract, tmp_path, content=b"first content"
    )
    admit_source(existing)
    publish_source(existing)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"other content",
        user_id=existing.user_id,
    )
    fingerprint: dict[str, object] = admit_source(incoming)
    assert fingerprint["source_size_bytes"] == len(b"other content")
    assert (
        fingerprint["source_content_sha256"]
        != contract.get_job_metadata(existing.job_id)["source_content_sha256"]
    )


def test_rejects_unchanged_explicit_document_update(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from shared.core.exceptions.domain_exceptions import ConflictException

    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(contract, tmp_path, content=b"unchanged")
    admit_source(existing)
    publish_source(existing)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"unchanged",
        user_id=existing.user_id,
        document_id=existing.document_id,
    )
    with pytest.raises(ConflictException) as failure:
        admit_source(incoming)
    assert failure.value.to_client(incoming.job_id)["error"]["details"]["id"] == existing.document_id


@pytest.mark.parametrize("scope_change", ["user", "namespace", "archived"])
def test_scopes_duplicate_content_to_current_active_user_documents(
    worker_contract_environment: None,
    tmp_path: Path,
    scope_change: str,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.document import Document

    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(contract, tmp_path, content=b"shared bytes")
    admit_source(existing)
    publish_source(existing)
    if scope_change == "archived":
        with get_sync_db_context() as database:
            document: Document | None = database.get(Document, existing.document_id)
            assert document is not None
            document.status = "archived"
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"shared bytes",
        user_id=None if scope_change == "user" else existing.user_id,
        namespace="another-namespace"
        if scope_change == "namespace"
        else "worker-contract",
    )
    assert admit_source(incoming)["source_content_sha256"]


def test_compares_only_the_current_revision_and_allows_changed_updates(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(contract, tmp_path, content=b"old revision")
    admit_source(existing)
    publish_source(existing)
    revision: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"new revision",
        user_id=existing.user_id,
        document_id=existing.document_id,
    )
    admit_source(revision)
    publish_source(revision)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"old revision",
        user_id=existing.user_id,
    )
    assert admit_source(incoming)["source_content_sha256"]


def test_ignores_existing_sources_without_hashes_and_records_future_uploads(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from shared.core.exceptions.domain_exceptions import ConflictException

    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(contract, tmp_path, content=b"legacy bytes")
    publish_source(existing)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"legacy bytes",
        user_id=existing.user_id,
    )
    # An unavailable historical original does not participate in admission.
    contract.storage.delete_upload_file(existing.storage_key)
    admit_source(incoming)
    publish_source(incoming)
    assert "source_content_sha256" not in contract.get_job_metadata(existing.job_id)
    repeat: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"legacy bytes",
        user_id=existing.user_id,
    )
    with pytest.raises(ConflictException):
        admit_source(repeat)


def test_keeps_rejecting_a_duplicate_when_publication_commits_during_the_lookup(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from shared.core.exceptions.domain_exceptions import ConflictException

    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(
        contract, tmp_path, content=b"publishing bytes"
    )
    admit_source(existing)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"publishing bytes",
        user_id=existing.user_id,
    )
    has_published: bool = False

    def publish_after_document_lookup(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        is_many: bool,
    ) -> None:
        nonlocal has_published
        if has_published or "JOIN job_results" not in statement:
            return
        has_published = True
        publish_source(existing)

    event.listen(contract.engine, "after_cursor_execute", publish_after_document_lookup)
    try:
        with pytest.raises(ConflictException):
            admit_source(incoming)
    finally:
        event.remove(
            contract.engine, "after_cursor_execute", publish_after_document_lookup
        )
    assert has_published


def test_preserves_demo_corpus_admission_when_source_bytes_match_a_personal_document(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job

    contract: WorkerParseContract = WorkerParseContract.create()
    existing: SourceJob = create_source_job(contract, tmp_path, content=b"demo bytes")
    admit_source(existing)
    publish_source(existing)
    incoming: SourceJob = create_source_job(
        contract,
        tmp_path,
        content=b"demo bytes",
        user_id=existing.user_id,
    )
    with get_sync_db_context() as database:
        job: Job | None = database.get(Job, incoming.job_id)
        assert job is not None
        job.job_metadata = {**dict(job.job_metadata or {}), "corpus_target": "DEMO"}
    assert admit_source(incoming)["source_content_sha256"]


def test_serializes_concurrent_content_claims_and_allows_retries_after_failure(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.core.exceptions.domain_exceptions import ConflictException
    from shared.models.database.job import Job

    contract: WorkerParseContract = WorkerParseContract.create()
    first: SourceJob = create_source_job(
        contract, tmp_path, content=b"concurrent bytes"
    )
    second: SourceJob = create_source_job(
        contract, tmp_path, content=b"concurrent bytes", user_id=first.user_id
    )
    has_read_candidates: Event = Event()
    can_save_claim: Event = Event()

    def pause_first_claim_after_lookup(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        is_many: bool,
    ) -> None:
        if "JOIN job_results" not in statement or has_read_candidates.is_set():
            return
        has_read_candidates.set()
        assert can_save_claim.wait(timeout=10)

    def claim_content(source: SourceJob) -> bool:
        try:
            admit_source(source)
            return True
        except ConflictException:
            return False

    # Hold the first transaction after its empty snapshot. The second must wait
    # for its advisory lock, rather than also admitting from an empty snapshot.
    event.listen(contract.engine, "after_cursor_execute", pause_first_claim_after_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_claim: Future[bool] = executor.submit(claim_content, first)
            try:
                assert has_read_candidates.wait(timeout=10)
                second_claim: Future[bool] = executor.submit(claim_content, second)
                deadline: float = monotonic() + 10
                with contract.engine.connect() as connection:
                    while not second_claim.done():
                        is_waiting_for_claim: bool = connection.execute(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE locktype = 'advisory' AND NOT granted)"
                            )
                        ).scalar_one()
                        if is_waiting_for_claim:
                            break
                        assert monotonic() < deadline
                        sleep(0.01)
            finally:
                can_save_claim.set()
            results: list[bool] = [first_claim.result(), second_claim.result()]
    finally:
        event.remove(
            contract.engine, "after_cursor_execute", pause_first_claim_after_lookup
        )
    assert sorted(results) == [False, True]
    winner: SourceJob = first if results[0] else second
    loser: SourceJob = second if results[0] else first
    # A redelivered task may claim its own content again.
    assert admit_source(winner)["source_content_sha256"]
    with get_sync_db_context() as database:
        job: Job = database.execute(
            select(Job).where(Job.job_id == winner.job_id)
        ).scalar_one()
        job.status = "failed"
    assert admit_source(loser)["source_content_sha256"]
