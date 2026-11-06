"""Bounded inventory pages without remote storage access."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from shared.services.storage.adapters.filesystem_adapter import FileSystemStorageAdapter
from shared.services.storage.adapters.s3_adapter import S3StorageAdapter
from shared.services.storage.adapters.oss_adapter import OSSStorageAdapter


def test_filesystem_inventory_resumes_after_deleted_cursor(tmp_path: Path) -> None:
    adapter: FileSystemStorageAdapter = FileSystemStorageAdapter(str(tmp_path), "inputs")
    for key in ("indexing-input/v1/a", "indexing-input/v1/b", "indexing-input/v1/c", "results/canonical"):
        adapter.upload_fileobj(BytesIO(b"data"), key)
    first = adapter.list_object_page(prefix="indexing-input/v1/", limit=2)
    assert first.objects == (("indexing-input/v1/a", 4), ("indexing-input/v1/b", 4))
    assert first.nextCursor == "indexing-input/v1/b"
    adapter.delete_object("indexing-input/v1/b")
    second = adapter.list_object_page(prefix="indexing-input/v1/", after=first.nextCursor, limit=2)
    assert second.objects == (("indexing-input/v1/c", 4),)
    assert second.nextCursor is None


def test_s3_inventory_uses_one_bounded_request_and_propagates_errors() -> None:
    client: Mock = Mock()
    client.list_objects_v2.return_value = {"Contents": [{"Key": "indexing-input/v1/b", "Size": 9}], "IsTruncated": True}
    adapter: S3StorageAdapter = S3StorageAdapter(client, "inputs")
    page = adapter.list_object_page(prefix="indexing-input/v1/", after="indexing-input/v1/a", limit=1)
    assert page.objects == (("indexing-input/v1/b", 9),)
    assert page.nextCursor == "indexing-input/v1/b"
    client.list_objects_v2.assert_called_once_with(Bucket="inputs", Prefix="indexing-input/v1/", StartAfter="indexing-input/v1/a", MaxKeys=1)


def test_oss_inventory_uses_native_marker_page() -> None:
    bucket: Mock = Mock()
    bucket.list_objects.return_value = SimpleNamespace(object_list=[SimpleNamespace(key="indexing-input/v1/b", size=7)], is_truncated=True, next_marker="indexing-input/v1/b")
    adapter: OSSStorageAdapter = OSSStorageAdapter(bucket, "inputs")
    page = adapter.list_object_page(prefix="indexing-input/v1/", after="indexing-input/v1/a", limit=1)
    assert page.objects == (("indexing-input/v1/b", 7),)
    assert page.nextCursor == "indexing-input/v1/b"
    bucket.list_objects.assert_called_once_with(prefix="indexing-input/v1/", marker="indexing-input/v1/a", max_keys=1)


def test_filesystem_inventory_orders_directory_prefixes_like_object_keys(tmp_path: Path) -> None:
    adapter: FileSystemStorageAdapter = FileSystemStorageAdapter(str(tmp_path), "inputs")
    keys: tuple[str, str] = ("indexing-input/v1/a/r/x", "indexing-input/v1/a-b/r/x")
    for key in keys:
        adapter.upload_fileobj(BytesIO(b"data"), key)
    first = adapter.list_object_page(prefix="indexing-input/v1/", limit=1)
    assert first.objects == ((keys[1], 4),)
    second = adapter.list_object_page(prefix="indexing-input/v1/", after=first.nextCursor, limit=1)
    assert second.objects == ((keys[0], 4),)
