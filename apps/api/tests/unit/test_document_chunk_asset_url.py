"""Unit tests for document chunk asset URL generation."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

# Defer importing apps/api `app` until test bodies run. Module-level imports
# would cache API's `app` package and break apps/worker contract collection in
# the same pytest process (both packages are named `app`).
configure_import_environment()
ensure_import_paths()

_API_ROOT = str(Path(__file__).resolve().parents[2])


def _is_api_app_module(module: ModuleType | None) -> bool:
    if module is None:
        return False
    module_file = getattr(module, "__file__", None)
    if isinstance(module_file, str) and module_file.startswith(_API_ROOT):
        return True
    module_paths = getattr(module, "__path__", ())
    try:
        return any(str(path).startswith(_API_ROOT) for path in module_paths)
    except KeyError:
        return False


def _drop_api_app_modules() -> None:
    for module_name in sorted(sys.modules, key=len, reverse=True):
        if module_name != "app" and not module_name.startswith("app."):
            continue
        if _is_api_app_module(sys.modules.get(module_name)):
            sys.modules.pop(module_name, None)


def _prioritize_api_import_root() -> None:
    """Keep apps/api ahead of apps/worker for the shared `app` package name."""
    ensure_import_paths()
    if _API_ROOT in sys.path:
        sys.path.remove(_API_ROOT)
    sys.path.insert(0, _API_ROOT)


@pytest.fixture(autouse=True)
def _clear_api_app_modules_after_unit_test():
    """Avoid leaving API's `app` package cached for later worker contract tests."""
    yield
    _drop_api_app_modules()


class FakeResultStorage:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.checked: list[str] = []

    def verify_raw_exists(self, *, job_id: str, relative_path: str) -> bool:
        self.checked.append(f"{job_id}/{relative_path}")
        return self.exists

    def generate_artifact_url(
        self,
        *,
        job_id: str,
        artifact_ref: str,
        expires_in: int = 3600,
    ) -> str | None:
        return f"https://assets.example.test/{job_id}/{artifact_ref}"


def _document_chunk_asset_url(**kwargs: Any) -> str | None:
    _prioritize_api_import_root()
    _drop_api_app_modules()
    from app.services.documents.lifecycle_service import (
        _document_chunk_asset_url,
    )

    return _document_chunk_asset_url(**kwargs)


def test_asset_url_generated_when_raw_file_exists() -> None:
    storage = FakeResultStorage(exists=True)

    url = _document_chunk_asset_url(
        chunk_type="table",
        job_id="job-1",
        file_path="tables/table-1 Test.html",
        include_asset_urls=True,
        result_storage=storage,
    )

    assert url == "https://assets.example.test/job-1/tables/table-1 Test.html"
    assert storage.checked == ["job-1/tables/table-1 Test.html"]


def test_asset_url_skipped_when_raw_file_missing() -> None:
    storage = FakeResultStorage(exists=False)

    url = _document_chunk_asset_url(
        chunk_type="table",
        job_id="job-1",
        file_path="tables/table-1 Test.html",
        include_asset_urls=True,
        result_storage=storage,
    )

    assert url is None
    assert storage.checked == ["job-1/tables/table-1 Test.html"]


def test_asset_url_falls_back_on_verification_error() -> None:
    class ExplodingStorage:
        def verify_raw_exists(self, *, job_id: str, relative_path: str) -> bool:
            raise RuntimeError("storage unavailable")

        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str | None:
            return f"https://assets.example.test/{job_id}/{artifact_ref}"

    with patch(
        "app.services.documents.lifecycle_service.logger.warning"
    ) as mock_warning:
        url = _document_chunk_asset_url(
            chunk_type="image",
            job_id="job-1",
            file_path="images/page-1.png",
            include_asset_urls=True,
            result_storage=ExplodingStorage(),
        )

    assert url is None
    assert mock_warning.call_count == 1


def test_asset_url_skipped_for_non_media_chunk_types() -> None:
    storage = FakeResultStorage(exists=True)

    url = _document_chunk_asset_url(
        chunk_type="text",
        job_id="job-1",
        file_path="tables/table-1 Test.html",
        include_asset_urls=True,
        result_storage=storage,
    )

    assert url is None
    assert storage.checked == []


def test_asset_url_skipped_when_include_asset_urls_false() -> None:
    storage = FakeResultStorage(exists=True)

    url = _document_chunk_asset_url(
        chunk_type="table",
        job_id="job-1",
        file_path="tables/table-1 Test.html",
        include_asset_urls=False,
        result_storage=storage,
    )

    assert url is None
    assert storage.checked == []
