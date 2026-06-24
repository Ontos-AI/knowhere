import base64
import json
from typing import Optional

import pytest

from shared.services.storage.adapters import oss_adapter
from shared.services.storage.adapters.oss_adapter import OSSStorageAdapter


class FakeOssError(Exception):
    pass


class FakeBucket:
    def __init__(self) -> None:
        self.captured_method: Optional[str] = None
        self.captured_key: Optional[str] = None
        self.captured_expires: Optional[int] = None
        self.captured_headers: Optional[dict[str, str]] = None
        self.captured_params: Optional[dict[str, str]] = None

    def sign_url(
        self,
        method: str,
        key: str,
        expires: int,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> str:
        self.captured_method = method
        self.captured_key = key
        self.captured_expires = expires
        self.captured_headers = headers
        self.captured_params = params
        return "https://examplebucket.oss-cn-guangzhou.aliyuncs.com/uploads/job.pdf"


def _decode_callback_config(encoded_callback: str) -> dict[str, str]:
    decoded_bytes: bytes = base64.b64decode(encoded_callback)
    decoded_text: str = decoded_bytes.decode("utf-8")
    return json.loads(decoded_text)


def test_generate_presigned_put_url_includes_oss_callback_headers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        oss_adapter,
        "_import_oss2",
        lambda: (object(), FakeOssError, Exception, Exception),
    )
    monkeypatch.setenv(
        "API_WEBHOOK_ENDPOINT",
        "https://api-sjg.knowhereto.com/api/v1/internal/s3-events",
    )
    monkeypatch.setenv("S3_REGION", "cn-guangzhou")
    bucket = FakeBucket()
    adapter = OSSStorageAdapter(bucket, "sjg-knowhere-storage-cn")

    adapter.generate_presigned_url(
        "uploads/job_123.pdf",
        expiration=7200,
        method="PUT",
        headers={"Content-Type": "application/pdf"},
    )

    assert bucket.captured_method == "PUT"
    assert bucket.captured_key == "uploads/job_123.pdf"
    assert bucket.captured_params is None
    assert bucket.captured_headers is not None
    assert "x-oss-callback" in bucket.captured_headers

    callback_config = _decode_callback_config(bucket.captured_headers["x-oss-callback"])
    assert (
        callback_config["callbackUrl"]
        == "https://api-sjg.knowhereto.com/api/v1/internal/s3-events"
    )
    assert callback_config["callbackBodyType"] == "application/json"

    callback_body = json.loads(callback_config["callbackBody"])
    assert callback_body["events"][0]["eventName"] == "ObjectCreated:PutObject"
    assert callback_body["events"][0]["region"] == "cn-guangzhou"
    assert callback_body["events"][0]["oss"]["bucket"]["name"] == "${bucket}"
    assert callback_body["events"][0]["oss"]["object"]["key"] == "${object}"


@pytest.mark.asyncio
async def test_generate_upload_url_returns_oss_callback_headers(monkeypatch) -> None:
    monkeypatch.setattr(
        oss_adapter,
        "_import_oss2",
        lambda: (object(), FakeOssError, Exception, Exception),
    )
    monkeypatch.setenv(
        "API_WEBHOOK_ENDPOINT",
        "https://api-sjg.knowhereto.com/api/v1/internal/s3-events",
    )
    monkeypatch.setenv("S3_REGION", "cn-guangzhou")
    required_settings = {
        "DS_KEY": "test",
        "DS_URL": "https://example.com",
        "S3_BUCKET_NAME": "sjg-knowhere-storage-cn",
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test",
        "S3_TEMP_PATH": "/tmp",
        "USERS_DATA_PATH": "/tmp/users",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/test",
        "SECRET_KEY": "test",
        "TMP_PATH": "/tmp",
        "FONT_PATH": "/tmp",
        "CHROMEDRIVER_PATH": "/tmp/chromedriver",
    }
    for key, value in required_settings.items():
        monkeypatch.setenv(key, value)

    bucket = FakeBucket()
    adapter = OSSStorageAdapter(bucket, "sjg-knowhere-storage-cn")

    from shared.services.storage import file_upload_service
    from shared.services.storage.file_upload_service import FileUploadService

    monkeypatch.setattr(
        file_upload_service.settings.__class__,
        "get_storage_adapter",
        lambda self: adapter,
    )
    monkeypatch.setattr(
        file_upload_service.settings,
        "S3_BUCKET_NAME",
        "sjg-knowhere-storage-cn",
    )
    monkeypatch.setattr(file_upload_service.settings, "JOB_WAITING_EXPIRE_SECONDS", 7200)

    upload_service = FileUploadService()
    upload_info = await upload_service.generate_upload_url("job_123", ".pdf")

    assert upload_info["upload_headers"]["Content-Type"] == "application/pdf"
    assert "x-oss-callback" in upload_info["upload_headers"]
    assert bucket.captured_headers == upload_info["upload_headers"]
