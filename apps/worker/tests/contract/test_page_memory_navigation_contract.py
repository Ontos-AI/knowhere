from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pandas as pd

from shared.services.storage.zip_doc_navigation import build_doc_nav_from_skeletons
from shared.services.storage.zip_result_service import ZipResultService


def test_doc_nav_from_skeletons_preserves_same_page_sibling_sections() -> None:
    skeletons = _same_page_sibling_skeletons()
    chunks = _page_chunks()

    doc_nav = build_doc_nav_from_skeletons(
        skeletons,
        chunks,
        "demo.pdf",
    )

    sections = doc_nav["sections"]
    assert len(sections) == 1
    parent = sections[0]
    assert parent["title"] == "3 基本规定"
    assert parent["chunk_count"] == 2

    child_counts = {
        child["title"]: child["chunk_count"] for child in parent["children"]
    }
    assert child_counts == {
        "3.1 职责": 1,
        "3.2 管理规定": 2,
    }


def test_doc_nav_from_skeletons_synthesizes_missing_ancestor_sections() -> None:
    skeletons = [
        {
            "section_path": "demo.pdf/安全类/SJSYJ-SC103/3 基本规定/3.1 职责",
            "level": 4,
            "start_page": 231,
            "end_page": 231,
            "title": "3.1 职责",
            "parent_path": "demo.pdf/安全类/SJSYJ-SC103/3 基本规定",
        },
        {
            "section_path": "demo.pdf/安全类/SJSYJ-SC103/3 基本规定/3.2 管理规定",
            "level": 4,
            "start_page": 231,
            "end_page": 232,
            "title": "3.2 管理规定",
            "parent_path": "demo.pdf/安全类/SJSYJ-SC103/3 基本规定",
        },
    ]

    doc_nav = build_doc_nav_from_skeletons(
        skeletons,
        _page_chunks(),
        "demo.pdf",
    )

    assert doc_nav["sections"][0]["title"] == "安全类"
    assert doc_nav["sections"][0]["chunk_count"] == 2
    standard = doc_nav["sections"][0]["children"][0]
    basic = standard["children"][0]
    assert basic["title"] == "3 基本规定"
    assert [child["title"] for child in basic["children"]] == [
        "3.1 职责",
        "3.2 管理规定",
    ]


def test_zip_result_service_uses_page_memory_skeletons_for_manifest_hierarchy() -> None:
    parsed_df = pd.DataFrame()
    parsed_df.attrs["page_memory_skeletons"] = _same_page_sibling_skeletons()

    doc_nav, hierarchy = ZipResultService()._build_navigation_outputs(  # noqa: SLF001
        formatted_chunks=_page_chunks(),
        source_file_name="demo.pdf",
        parsed_df=parsed_df,
    )

    assert doc_nav is not None
    assert hierarchy == {
        "3 基本规定": {
            "3.1 职责": {},
            "3.2 管理规定": {},
        }
    }


def _same_page_sibling_skeletons() -> list[dict[str, object]]:
    return [
        {
            "section_path": "demo.pdf/3 基本规定",
            "level": 1,
            "start_page": 231,
            "end_page": 232,
            "title": "3 基本规定",
            "parent_path": None,
        },
        {
            "section_path": "demo.pdf/3 基本规定/3.1 职责",
            "level": 2,
            "start_page": 231,
            "end_page": 231,
            "title": "3.1 职责",
            "parent_path": "demo.pdf/3 基本规定",
        },
        {
            "section_path": "demo.pdf/3 基本规定/3.2 管理规定",
            "level": 2,
            "start_page": 231,
            "end_page": 232,
            "title": "3.2 管理规定",
            "parent_path": "demo.pdf/3 基本规定",
        },
    ]


def _page_chunks() -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "page-231",
            "type": "page",
            "content": "page 231",
            "path": "demo.pdf/3 基本规定/3.2 管理规定",
            "metadata": {"page_nums": [231], "summary": "page 231 summary"},
        },
        {
            "chunk_id": "page-232",
            "type": "page",
            "content": "page 232",
            "path": "demo.pdf/3 基本规定/3.2 管理规定",
            "metadata": {"page_nums": [232], "summary": "page 232 summary"},
        },
    ]
