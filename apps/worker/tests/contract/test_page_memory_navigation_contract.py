from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder
from shared.services.storage.zip_result_service import ZipResultService


def test_page_doc_nav_uses_path_based_leaf_summaries_and_page_counts() -> None:
    doc_nav = ZipDocNavigationBuilder().build_doc_nav(
        _page_chunks(),
        "demo.pdf",
    )

    sections = doc_nav["sections"]
    assert len(sections) == 1
    parent = sections[0]
    assert parent["title"] == "3 基本规定"
    assert parent["chunk_count"] == 3

    child_counts = {
        child["title"]: child["chunk_count"] for child in parent["children"]
    }
    assert child_counts == {
        "3.1 职责": 1,
        "3.2 管理规定": 2,
    }

    child_summaries = {
        child["title"]: child["summary"] for child in parent["children"]
    }
    assert child_summaries == {
        "3.1 职责": "page 231 summary",
        "3.2 管理规定": "pages 232-233 summary",
    }


def test_zip_result_service_builds_navigation_from_chunk_paths() -> None:
    doc_nav, hierarchy = ZipResultService()._build_navigation_outputs(  # noqa: SLF001
        formatted_chunks=_page_chunks(),
        source_file_name="demo.pdf",
    )

    assert doc_nav is not None
    assert hierarchy == {
        "3 基本规定": {
            "3.1 职责": {},
            "3.2 管理规定": {},
        }
    }


def _page_chunks() -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "page-231",
            "type": "page",
            "content": "page 231",
            "path": "demo.pdf/3 基本规定/3.1 职责",
            "metadata": {"page_nums": [231], "summary": "page 231 summary"},
        },
        {
            "chunk_id": "page-232-233",
            "type": "page",
            "content": "pages 232 and 233",
            "path": "demo.pdf/3 基本规定/3.2 管理规定",
            "metadata": {"page_nums": [232, 233], "summary": "pages 232-233 summary"},
        },
    ]
