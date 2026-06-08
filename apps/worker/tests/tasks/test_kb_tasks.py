from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.core.tasks import kb_tasks
from app.services.document_parser import parse_service


class _FakeLifecycleService:
    def __init__(self) -> None:
        self.progress_updates: list[dict[str, object]] = []
        self.success_calls: list[dict[str, object]] = []

    def update_progress(self, job_id: str, progress: int, message: str = "") -> bool:
        self.progress_updates.append(
            {
                "job_id": job_id,
                "progress": progress,
                "message": message,
            }
        )
        return True

    def finalize_job_success(self, **kwargs) -> dict[str, object]:
        self.success_calls.append(kwargs)
        return {
            "status": "success",
            "job_id": kwargs["job_id"],
            "stored_count": kwargs.get("stored_count", 0),
        }


class _FakeJobInfoRedisService:
    def __init__(self, redis_service):
        self.redis_service = redis_service

    def get_job_info(self, job_id: str):
        return {"s3_key": "uploads/test.pdf", "user_id": "user_123"}


class _FakeMetadataService:
    def __init__(self, redis_service):
        self.redis_service = redis_service

    def get_metadata(self, job_id: str):
        return {"source_file_name": "test.pdf"}


def test_parse_skips_when_job_is_already_terminal(monkeypatch, tmp_path):
    redis_service = MagicMock()
    lifecycle_service = _FakeLifecycleService()
    redis_lock_cls = MagicMock(side_effect=AssertionError("lock should not be acquired"))

    monkeypatch.setattr(kb_tasks, "get_sync_job_lifecycle_service", lambda: lifecycle_service)
    monkeypatch.setattr(
        kb_tasks.SyncRedisServiceFactory,
        "get_service",
        staticmethod(lambda: redis_service),
    )
    monkeypatch.setattr(kb_tasks, "SyncJobInfoRedisService", _FakeJobInfoRedisService)
    monkeypatch.setattr(kb_tasks, "SyncJobMetadataService", _FakeMetadataService)
    monkeypatch.setattr(
        kb_tasks,
        "verify_s3_file_exists",
        lambda s3_key: {"exists": True, "size": 1024},
    )
    monkeypatch.setattr(kb_tasks.settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(kb_tasks, "mark_job_running", lambda job_id, redis: False)
    monkeypatch.setattr(kb_tasks, "RedisJobLock", redis_lock_cls)

    result = kb_tasks._parse("job_123", "user_123")

    assert result == {
        "status": "skipped",
        "job_id": "job_123",
        "reason": "job_already_terminal",
    }
    assert lifecycle_service.progress_updates == []
    assert lifecycle_service.success_calls == []
    redis_lock_cls.assert_not_called()


class _FakeSuccessJobInfoRedisService:
    def __init__(self, redis_service):
        self.redis_service = redis_service

    def get_job_info(self, job_id: str):
        return {"s3_key": "uploads/test.pdf", "user_id": "user_123"}


class _FakeSuccessMetadataService:
    def __init__(self, redis_service):
        self.redis_service = redis_service
        self.metadata = {"source_file_name": "test.pdf"}
        self.updates = []

    def get_metadata(self, job_id: str):
        return dict(self.metadata)

    def update_metadata(self, job_id: str, payload):
        self.updates.append(payload)
        self.metadata.update(payload)


class _FakeChunksRedisService:
    def __init__(self, redis_service):
        self.redis_service = redis_service

    def dataframe_to_chunks(self, dataframe: pd.DataFrame):
        return [{"chunk_id": "chunk-1", "type": "text", "path": "doc/test", "content": "hello", "metadata": {}}]

    def save_chunks(self, job_id: str, chunks):
        return True


class _FailOnSaveChunksRedisService:
    expected_chunks = [{"chunk_id": "chunk-1", "type": "text", "path": "doc/test", "content": "hello", "metadata": {}}]

    def __init__(self, redis_service):
        self.redis_service = redis_service

    def dataframe_to_chunks(self, dataframe: pd.DataFrame):
        return list(self.expected_chunks)

    def save_chunks(self, job_id: str, chunks):
        raise AssertionError("chunks should stay in memory instead of round-tripping through Redis")


class _FakeRedisJobLock:
    def __init__(self, redis_service, job_id: str):
        self.redis_service = redis_service
        self.job_id = job_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeDbResult:
    def __init__(self, job):
        self.job = job

    def scalar_one_or_none(self):
        return self.job


class _FakeDbContext:
    def __init__(self, job):
        self.job = job

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement):
        return _FakeDbResult(self.job)


def _find_task_workspaces(root: Path, job_id: str) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith(f"kb_task_{job_id}_")
    )


def test_parse_cleans_task_workspace_after_success(monkeypatch, tmp_path):
    redis_service = MagicMock()
    job = type("JobRow", (), {"billing_status": "charged"})()
    metadata_service = _FakeSuccessMetadataService(redis_service)
    lifecycle_service = _FakeLifecycleService()
    source_file_name = "GB 50243-2016 通风与空调工程施工质量验收规范.pdf"
    normalized_file_name = "GB_50243-2016_通风与空调工程施工质量验收规范.pdf"
    parse_call = {}

    def fake_generate_zip_package(
        self,
        job_id: str,
        chunks,
        add_dir: str,
        source_file_name: str,
        data_id,
        job_metadata,
        parsed_df=None,
        temp_dir: str | None = None,
    ):
        assert temp_dir is not None
        zip_path = Path(temp_dir) / f"result_{job_id}.zip"
        zip_path.write_bytes(b"zip")
        return str(zip_path), {"value": "checksum"}, {"total_chunks": 1}, 3

    def fake_checkerboard_inject_parse(**kwargs):
        parse_call.update(kwargs)
        assert kwargs["filename"] == source_file_name
        assert kwargs["internal_output_filename"] == normalized_file_name
        assert kwargs["file_full_path"].endswith(f"/{normalized_file_name}")
        assert "/input/" in kwargs["file_full_path"]
        assert kwargs["kb_dir"] == "Default_Root"
        assert str(tmp_path) in kwargs["output_dir"]
        assert kwargs["output_dir"].endswith("/output")

        output_dir = Path(kwargs["output_dir"]) / kwargs["kb_dir"] / kwargs["internal_output_filename"]
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full.md").write_text("body", encoding="utf-8")
        dataframe = pd.DataFrame(
            [
                {
                    "content": "hello",
                    "path": "doc/test",
                    "type": "text",
                    "length": 5,
                    "keywords": "",
                    "summary": "",
                    "know_id": "kid",
                    "tokens": "",
                    "connectto": "",
                    "addtime": "now",
                    "page_nums": "1",
                }
            ]
        )
        return str(output_dir), dataframe

    monkeypatch.setattr(kb_tasks, "get_sync_job_lifecycle_service", lambda: lifecycle_service)
    monkeypatch.setattr(
        kb_tasks.SyncRedisServiceFactory,
        "get_service",
        staticmethod(lambda: redis_service),
    )
    monkeypatch.setattr(kb_tasks, "SyncJobInfoRedisService", _FakeSuccessJobInfoRedisService)
    monkeypatch.setattr(kb_tasks, "SyncJobMetadataService", lambda redis: metadata_service)
    monkeypatch.setattr(kb_tasks, "SyncChunksRedisService", _FakeChunksRedisService)
    monkeypatch.setattr(kb_tasks, "verify_s3_file_exists", lambda s3_key: {"exists": True, "size": 1024})
    monkeypatch.setattr(kb_tasks.settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(kb_tasks, "mark_job_running", lambda job_id, redis: True)
    monkeypatch.setattr(kb_tasks, "RedisJobLock", _FakeRedisJobLock)
    monkeypatch.setattr(kb_tasks, "generate_download_url", lambda s3_key, bucket: {"download_url": "https://example.test/file.pdf"})
    metadata_service.metadata["source_file_name"] = source_file_name

    def fake_download_s3_file_to_temp(file_url: str, file_ext: str, temp_dir: str) -> str:
        source_path = Path(temp_dir) / f"downloaded{file_ext}"
        source_path.write_bytes(b"pdf")
        return str(source_path)

    monkeypatch.setattr(kb_tasks, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(kb_tasks.PageEstimator, "estimate", staticmethod(lambda path: 1))
    monkeypatch.setattr(kb_tasks, "get_sync_db_context", lambda: _FakeDbContext(job))
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)
    monkeypatch.setattr(kb_tasks.ZipResultService, "generate_zip_package", fake_generate_zip_package)
    monkeypatch.setattr(kb_tasks, "upload_zip_result", lambda job_id, zip_file_path: f"results/{job_id}.zip")

    result = kb_tasks._parse("job_123", "user_123")

    assert _find_task_workspaces(tmp_path, "job_123") == []
    assert parse_call["filename"] == source_file_name
    assert parse_call["internal_output_filename"] == normalized_file_name
    assert parse_call["file_full_path"].endswith(f"/{normalized_file_name}")
    assert lifecycle_service.success_calls[0]["delivery_mode"] == "url"
    assert lifecycle_service.success_calls[0]["stored_count"] == 0
    assert result["add_dir"] is None
    assert "add_dir" not in metadata_service.metadata
    assert "workspace_cleaned" not in metadata_service.metadata


def test_parse_uses_s3_extension_for_internal_parse_name(monkeypatch, tmp_path):
    redis_service = MagicMock()
    job = type("JobRow", (), {"billing_status": "charged"})()
    metadata_service = _FakeSuccessMetadataService(redis_service)
    lifecycle_service = _FakeLifecycleService()
    parse_call = {}
    estimated_paths = []

    metadata_service.metadata["source_file_name"] = "legacy-upload.txt"
    metadata_service.metadata["parsing_params"] = {"is_atlas": True}

    def fake_checkerboard_inject_parse(**kwargs):
        parse_call.update(kwargs)
        output_dir = Path(kwargs["output_dir"]) / kwargs["kb_dir"] / kwargs["internal_output_filename"]
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full.md").write_text("body", encoding="utf-8")
        return str(output_dir), pd.DataFrame(
            [
                {
                    "content": "hello",
                    "path": "doc/test",
                    "type": "text",
                    "length": 5,
                    "keywords": "",
                    "summary": "",
                    "know_id": "kid",
                    "tokens": "",
                    "connectto": "",
                    "addtime": "now",
                    "page_nums": "1",
                }
            ]
        )

    def fake_download_s3_file_to_temp(file_url: str, file_ext: str, temp_dir: str) -> str:
        source_path = Path(temp_dir) / f"downloaded{file_ext}"
        source_path.write_bytes(b"pdf")
        return str(source_path)

    def fake_estimate(path: str) -> int:
        estimated_paths.append(path)
        return 1

    monkeypatch.setattr(kb_tasks, "get_sync_job_lifecycle_service", lambda: lifecycle_service)
    monkeypatch.setattr(
        kb_tasks.SyncRedisServiceFactory,
        "get_service",
        staticmethod(lambda: redis_service),
    )
    monkeypatch.setattr(kb_tasks, "SyncJobInfoRedisService", _FakeSuccessJobInfoRedisService)
    monkeypatch.setattr(kb_tasks, "SyncJobMetadataService", lambda redis: metadata_service)
    monkeypatch.setattr(kb_tasks, "SyncChunksRedisService", _FakeChunksRedisService)
    monkeypatch.setattr(kb_tasks, "verify_s3_file_exists", lambda s3_key: {"exists": True, "size": 1024})
    monkeypatch.setattr(kb_tasks.settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(kb_tasks, "mark_job_running", lambda job_id, redis: True)
    monkeypatch.setattr(kb_tasks, "RedisJobLock", _FakeRedisJobLock)
    monkeypatch.setattr(kb_tasks, "generate_download_url", lambda s3_key, bucket: {"download_url": "https://example.test/file.pdf"})
    monkeypatch.setattr(kb_tasks, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(kb_tasks.PageEstimator, "estimate", staticmethod(fake_estimate))
    monkeypatch.setattr(kb_tasks, "get_sync_db_context", lambda: _FakeDbContext(job))
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)
    monkeypatch.setattr(
        kb_tasks.ZipResultService,
        "generate_zip_package",
        lambda self, **kwargs: (str(Path(kwargs["temp_dir"]) / "result.zip"), {"value": "checksum"}, {"total_chunks": 1}, 3),
    )
    monkeypatch.setattr(kb_tasks, "upload_zip_result", lambda job_id, zip_file_path: f"results/{job_id}.zip")

    kb_tasks._parse("job_123", "user_123")

    assert estimated_paths[0].endswith("/legacy-upload.pdf")
    assert parse_call["file_full_path"].endswith("/legacy-upload.pdf")
    assert parse_call["filename"] == "legacy-upload.txt"
    assert parse_call["internal_output_filename"] == "legacy-upload.pdf"
    assert parse_call["is_atlas"] is True
    assert parse_call["page_count"] == 1
    assert _find_task_workspaces(tmp_path, "job_123") == []


def test_parse_passes_chunks_directly_to_finalize_job_success(monkeypatch, tmp_path):
    redis_service = MagicMock()
    job = type("JobRow", (), {"billing_status": "charged"})()
    metadata_service = _FakeSuccessMetadataService(redis_service)
    lifecycle_service = _FakeLifecycleService()

    def fake_checkerboard_inject_parse(**kwargs):
        output_dir = Path(kwargs["output_dir"]) / kwargs["kb_dir"] / kwargs["internal_output_filename"]
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "full.md").write_text("body", encoding="utf-8")
        return str(output_dir), pd.DataFrame(
            [
                {
                    "content": "hello",
                    "path": "doc/test",
                    "type": "text",
                    "length": 5,
                    "keywords": "",
                    "summary": "",
                    "know_id": "kid",
                    "tokens": "",
                    "connectto": "",
                    "addtime": "now",
                    "page_nums": "1",
                }
            ]
        )

    def fake_download_s3_file_to_temp(file_url: str, file_ext: str, temp_dir: str) -> str:
        source_path = Path(temp_dir) / f"downloaded{file_ext}"
        source_path.write_bytes(b"pdf")
        return str(source_path)

    monkeypatch.setattr(kb_tasks, "get_sync_job_lifecycle_service", lambda: lifecycle_service)
    monkeypatch.setattr(
        kb_tasks.SyncRedisServiceFactory,
        "get_service",
        staticmethod(lambda: redis_service),
    )
    monkeypatch.setattr(kb_tasks, "SyncJobInfoRedisService", _FakeSuccessJobInfoRedisService)
    monkeypatch.setattr(kb_tasks, "SyncJobMetadataService", lambda redis: metadata_service)
    monkeypatch.setattr(kb_tasks, "SyncChunksRedisService", _FailOnSaveChunksRedisService)
    monkeypatch.setattr(kb_tasks, "verify_s3_file_exists", lambda s3_key: {"exists": True, "size": 1024})
    monkeypatch.setattr(kb_tasks.settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(kb_tasks, "mark_job_running", lambda job_id, redis: True)
    monkeypatch.setattr(kb_tasks, "RedisJobLock", _FakeRedisJobLock)
    monkeypatch.setattr(kb_tasks, "generate_download_url", lambda s3_key, bucket: {"download_url": "https://example.test/file.pdf"})
    monkeypatch.setattr(kb_tasks, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(kb_tasks.PageEstimator, "estimate", staticmethod(lambda path: 1))
    monkeypatch.setattr(kb_tasks, "get_sync_db_context", lambda: _FakeDbContext(job))
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)
    monkeypatch.setattr(
        kb_tasks.ZipResultService,
        "generate_zip_package",
        lambda self, **kwargs: (str(Path(kwargs["temp_dir"]) / "result.zip"), {"value": "checksum"}, {"total_chunks": 1}, 3),
    )
    monkeypatch.setattr(kb_tasks, "upload_zip_result", lambda job_id, zip_file_path: f"results/{job_id}.zip")

    kb_tasks._parse("job_123", "user_123")

    assert lifecycle_service.success_calls[0]["chunks"] == _FailOnSaveChunksRedisService.expected_chunks
    assert "chunks_job_id" not in lifecycle_service.success_calls[0]


def test_parse_cleans_task_workspace_after_failure(monkeypatch, tmp_path):
    redis_service = MagicMock()
    job = type("JobRow", (), {"billing_status": "charged"})()
    metadata_service = _FakeSuccessMetadataService(redis_service)
    lifecycle_service = _FakeLifecycleService()

    def fake_checkerboard_inject_parse(**kwargs):
        raise RuntimeError("parse failed")

    def fake_download_s3_file_to_temp(file_url: str, file_ext: str, temp_dir: str) -> str:
        source_path = Path(temp_dir) / f"downloaded{file_ext}"
        source_path.write_bytes(b"pdf")
        return str(source_path)

    monkeypatch.setattr(kb_tasks, "get_sync_job_lifecycle_service", lambda: lifecycle_service)
    monkeypatch.setattr(
        kb_tasks.SyncRedisServiceFactory,
        "get_service",
        staticmethod(lambda: redis_service),
    )
    monkeypatch.setattr(kb_tasks, "SyncJobInfoRedisService", _FakeSuccessJobInfoRedisService)
    monkeypatch.setattr(kb_tasks, "SyncJobMetadataService", lambda redis: metadata_service)
    monkeypatch.setattr(kb_tasks, "SyncChunksRedisService", _FakeChunksRedisService)
    monkeypatch.setattr(kb_tasks, "verify_s3_file_exists", lambda s3_key: {"exists": True, "size": 1024})
    monkeypatch.setattr(kb_tasks.settings, "TMP_PATH", str(tmp_path))
    monkeypatch.setattr(kb_tasks, "mark_job_running", lambda job_id, redis: True)
    monkeypatch.setattr(kb_tasks, "RedisJobLock", _FakeRedisJobLock)
    monkeypatch.setattr(kb_tasks, "generate_download_url", lambda s3_key, bucket: {"download_url": "https://example.test/file.pdf"})
    monkeypatch.setattr(kb_tasks, "download_s3_file_to_temp", fake_download_s3_file_to_temp)
    monkeypatch.setattr(kb_tasks.PageEstimator, "estimate", staticmethod(lambda path: 1))
    monkeypatch.setattr(kb_tasks, "get_sync_db_context", lambda: _FakeDbContext(job))
    monkeypatch.setattr(parse_service, "checkerboard_inject_parse", fake_checkerboard_inject_parse)

    with pytest.raises(RuntimeError, match="parse failed"):
        kb_tasks._parse("job_123", "user_123")

    assert _find_task_workspaces(tmp_path, "job_123") == []
    assert lifecycle_service.success_calls == []
