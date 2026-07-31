from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.chunks.canonical_chunk_builder import rows_to_chunks
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder


def test_zip_formatter_preserves_page_memory_metadata_fields() -> None:
    chunks = rows_to_chunks(
        [
            {
                "content": "owned page body",
                "path": "demo.pdf/Section",
                "type": "page",
                "length": 15,
                "keywords": "Acme",
                "summary": "Page summary",
                "know_id": "node_owner",
                "tokens": "",
                "connectto": '[{"target":"node_other","relation":"same_as","ref":"[SAME-AS demo.pdf/Other p2]","page":2}]',
                "page_nums": "1,2",
                "owned_page_nums": "1",
                "entities": '[{"text":"Acme","type":"organization"}]',
                "asset_title": "",
                "extra_metadata": {
                    "content_kind": "body",
                    "page_assets": [
                        {
                            "page_num": 1,
                            "artifact_ref": "page_citation_assets/page-1.png",
                            "content_type": "image/png",
                            "source": "knowhere-rendered-page-citation-source",
                        }
                    ],
                },
            }
        ]
    )

    formatted = ZipChunkSchemaBuilder().format_chunks(chunks, {}, {})
    metadata = formatted[0]["metadata"]

    assert metadata["summary"] == "Page summary"
    assert metadata["entities"] == [{"text": "Acme", "type": "organization"}]
    assert metadata["keywords"] == ["Acme"]
    assert metadata["page_nums"] == [1, 2]
    assert metadata["owned_page_nums"] == [1]
    assert metadata["content_kind"] == "body"
    assert metadata["page_assets"][0]["artifact_ref"] == (
        "page_citation_assets/page-1.png"
    )
    assert metadata["connect_to"][0]["relation"] == "same_as"
    assert metadata["connect_to"][0]["target"] == "node_other"
    assert metadata["connect_to"][0]["page"] == 2
