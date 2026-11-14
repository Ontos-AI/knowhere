from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from pytest import MonkeyPatch

from support.worker_parse_contract import WorkerParseContract
from support.worker_url_upload_contract import WorkerUrlUploadContract


class _SilentStaticFileHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _serve_directory(directory: Path) -> Iterator[str]:
    handler = partial(_SilentStaticFileHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_should_upload_a_url_job_to_the_expected_storage_key_and_publish_progress(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "contract-source.pdf"
    source_file.write_bytes(b"pdf")

    contract = WorkerUrlUploadContract.create()
    contract.use_workspace_root(monkeypatch, tmp_path)
    contract.allow_private_url_sources(monkeypatch)

    with _serve_directory(source_dir) as server_url:
        source_url = f"{server_url}/{source_file.name}"
        job = contract.create_url_job(source_url=source_url)

        celery_result = contract.enqueue_upload_url_task(
            job_id=job["job_id"],
            source_url=source_url,
            user_id=job["user_id"],
        )

    assert celery_result.successful()
    assert celery_result.result == {
        "status": "success",
        "job_id": job["job_id"],
        "s3_key": job["s3_key"],
        "file_size": len(b"pdf"),
    }

    uploaded_source = contract.verify_uploaded_source_object(job["s3_key"])
    assert uploaded_source["exists"] is True
    assert uploaded_source["size"] == len(b"pdf")
    assert contract.read_uploaded_source_object(job["s3_key"]) == b"pdf"

    progress = contract.get_task_progress(job["job_id"])
    assert progress["progress"] == 100
    assert progress["message"] == "URL file upload complete, waiting for processing..."
    assert progress["timestamp"]

    job_row = contract.observe_job(job["job_id"])
    assert job_row["status"] == "waiting-file"
    assert job_row["source_type"] == "url"
    assert job_row["s3_key"] == job["s3_key"]


def test_should_persist_the_downloaded_source_size_after_a_progressive_parse(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.services.jobs.progressive_ingestion import read_progressive_state

    source_bytes: bytes = "# Budget\r\n\r\nLocal budget for café.\r\n".encode()
    source_file: Path = tmp_path / "budget.md"
    source_file.write_bytes(source_bytes)
    upload_contract: WorkerUrlUploadContract = WorkerUrlUploadContract.create()
    upload_contract.use_workspace_root(monkeypatch, tmp_path)
    upload_contract.allow_private_url_sources(monkeypatch)
    parse_contract: WorkerParseContract = WorkerParseContract.create()
    parse_contract.use_billing(monkeypatch, is_enabled=False)

    with _serve_directory(tmp_path) as server_url:
        source_url: str = f"{server_url}/{source_file.name}"
        job: dict[str, str] = upload_contract.create_url_job(
            source_url=source_url, source_file_name=source_file.name
        )
        with get_sync_db_context() as database:
            job_row: Job | None = database.get(Job, job["job_id"])
            assert job_row is not None
            job_row.job_metadata = {
                **(job_row.job_metadata or {}),
                "api_version": "v2",
                "ingestion_mode": "progressive",
                "parse_track": "chunk",
                "parsing_params": {"top_summary_use_llm": False},
            }

        upload_result = upload_contract.enqueue_upload_url_task(
            job_id=job["job_id"], source_url=source_url, user_id=job["user_id"]
        )
        assert upload_result.successful()

    # Simulate the API's source-upload confirmation before dispatching the worker.
    with get_sync_db_context() as database:
        job_row = database.get(Job, job["job_id"])
        assert job_row is not None
        job_row.status = "pending"

    parse_result = parse_contract.enqueue_parse_task(
        job_id=job["job_id"], user_id=job["user_id"]
    )
    assert parse_result.successful(), parse_result.result
    assert parse_result.result["status"] == "success"
    observed = parse_contract.observe_successful_job(job["job_id"])
    assert observed["job"]["status"] == "done"
    assert observed["job"]["job_metadata"]["source_size_bytes"] == len(source_bytes)
    progressive_state = read_progressive_state(observed["result"]["document_metadata"])
    assert progressive_state is not None
    assert [snapshot.stage for snapshot in progressive_state.snapshots] == ["baseline"]
