from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO


class FakeStorageAdapter:
    def __init__(self) -> None:
        self.existing_keys: set[tuple[str, str]] = set()
        self.object_sizes: dict[tuple[str, str], int] = {}
        self.upload_calls: list[tuple[str, str, str]] = []
        self.download_calls: list[tuple[str, str, str]] = []
        self.presigned_calls: list[tuple[str, str, int, str]] = []

    def generate_presigned_url(
        self,
        s3_key: str,
        expiration: int = 3600,
        bucket: str | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> str:
        del headers
        assert bucket is not None
        self.presigned_calls.append((s3_key, bucket, expiration, method))
        return f"https://storage.example.test/{bucket}/{s3_key}"

    def exists(self, s3_key: str, bucket: str) -> bool:
        return (s3_key, bucket) in self.existing_keys

    def get_object_size(self, s3_key: str, bucket: str) -> int:
        return self.object_sizes[(s3_key, bucket)]

    def upload_file(self, file_path: str, s3_key: str, bucket: str) -> dict[str, Any]:
        self.upload_calls.append((file_path, s3_key, bucket))
        self.existing_keys.add((s3_key, bucket))
        self.object_sizes[(s3_key, bucket)] = Path(file_path).stat().st_size
        return {"bucket": bucket, "key": s3_key}

    def upload_fileobj(
        self,
        file_obj: BinaryIO,
        s3_key: str,
        bucket: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        del file_obj, content_type
        return {"bucket": bucket, "key": s3_key}

    def download_file(self, s3_key: str, local_path: str, bucket: str) -> str:
        self.download_calls.append((s3_key, local_path, bucket))
        Path(local_path).write_bytes(b"downloaded")
        return local_path


def test_job_file_storage_should_hide_upload_bucket_rules_for_worker_source_files(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from shared.services.storage.job_file_storage import JobFileStorage

    del worker_contract_environment

    storage_adapter = FakeStorageAdapter()
    storage = JobFileStorage(
        storage_adapter=storage_adapter,
        uploads_bucket="uploads-bucket",
        results_bucket="results-bucket",
    )
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"pdf")

    storage.upload_source_file(str(source_path), "uploads/job_123.pdf")
    file_info = storage.verify_upload_exists("uploads/job_123.pdf")
    download_info = storage.generate_upload_download_url(
        "uploads/job_123.pdf",
        expires_in=60,
    )
    downloaded_path = storage.download_upload_to_temp(
        "uploads/job_123.pdf",
        suffix=".pdf",
        temp_dir=str(tmp_path),
    )

    assert file_info == {
        "exists": True,
        "size": 3,
        "content_type": None,
        "last_modified": None,
        "etag": None,
    }
    assert download_info == {
        "download_url": "https://storage.example.test/uploads-bucket/uploads/job_123.pdf",
        "expires_in": 60,
    }
    assert Path(downloaded_path).read_bytes() == b"downloaded"
    assert storage_adapter.upload_calls == [
        (str(source_path), "uploads/job_123.pdf", "uploads-bucket")
    ]
    assert storage_adapter.download_calls == [
        ("uploads/job_123.pdf", downloaded_path, "uploads-bucket")
    ]
    assert storage_adapter.presigned_calls == [
        ("uploads/job_123.pdf", "uploads-bucket", 60, "GET")
    ]
