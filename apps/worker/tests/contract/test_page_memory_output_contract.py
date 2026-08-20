from __future__ import annotations

import os
from types import SimpleNamespace

import pandas as pd

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory import memory_service
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder


def test_dataframe_converter_accepts_page_chunks_with_extra_metadata() -> None:
    df = pd.DataFrame(
        [
            {
                "content": "[SUMMARY]\nshort\n\n[RAW]\nbody",
                "path": "demo.pdf/Root",
                "type": "page",
                "length": 26,
                "keywords": "",
                "summary": "short",
                "know_id": "page-1",
                "tokens": "",
                "connectto": "",
                "addtime": "2026-06-11 00:00:00",
                "page_nums": "1,2",
                "extra_metadata": {},
            }
        ]
    )

    chunks = dataframe_to_chunks(df)

    assert chunks[0]["type"] == "page"
    assert chunks[0]["metadata"]["page_nums"] == [1, 2]
    assert "page_image_uris" not in chunks[0]["metadata"]


def test_zip_chunk_schema_preserves_page_memory_node_metadata() -> None:
    chunks = [
        {
            "chunk_id": "page-231",
            "type": "page",
            "content": "page body",
            "path": "demo.pdf/3 基本规定/3.2 管理规定",
            "metadata": {
                "summary": "page summary",
                "page_nums": [231],
                "page_assets": [
                    {
                        "page_num": 231,
                        "artifact_ref": "page_citation_assets/page-231.png",
                        "content_type": "image/png",
                        "width": 1200,
                        "height": 1800,
                        "source": "knowhere-rendered-page-citation-source",
                    }
                ],
            },
        }
    ]

    formatted = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={},
        table_files_map={},
    )

    metadata = formatted[0]["metadata"]
    assert metadata["page_nums"] == [231]
    assert metadata["page_assets"] == [
        {
            "page_num": 231,
            "artifact_ref": "page_citation_assets/page-231.png",
            "content_type": "image/png",
            "width": 1200,
            "height": 1800,
            "source": "knowhere-rendered-page-citation-source",
        }
    ]
    assert "page_assets" not in formatted[0]
    assert "page_image_uris" not in metadata
    assert "granularity" not in metadata
    assert "page_indices" not in metadata
    assert "owned_pages" not in metadata
    assert "section_path" not in metadata


def test_page_memory_granularity_routes_supported_page_modes() -> None:
    assert (
        memory_service._decide_granularity(  # noqa: SLF001
            SimpleNamespace(page_count=6, toc=SimpleNamespace(has_toc=False))
        )
        == "whole_doc"
    )
    assert (
        memory_service._decide_granularity(  # noqa: SLF001
            SimpleNamespace(page_count=7, toc=SimpleNamespace(has_toc=False))
        )
        == "page"
    )
    assert (
        memory_service._decide_granularity(  # noqa: SLF001
            SimpleNamespace(page_count=201, toc=SimpleNamespace(has_toc=False))
        )
        == "page"
    )
