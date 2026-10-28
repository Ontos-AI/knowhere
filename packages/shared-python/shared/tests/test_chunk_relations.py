"""Contract tests for the unified chunk relation edges (embeds / embedded_by)."""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pandas as pd

from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.graph.keywords import (
    extract_keywords_from_chunk_metadata,
)
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.retrieval.hydration.row_utils import iter_connected_target_ids
from shared.services.retrieval.search.lexical_text import (
    build_content_lexical_text,
    build_content_search_text,
    build_term_search_text,
)
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder

_COLUMNS = [
    "content",
    "path",
    "type",
    "length",
    "keywords",
    "summary",
    "know_id",
    "tokens",
    "connectto",
    "addtime",
    "page_nums",
]


def _row(*, know_id: str, path: str, chunk_type: str, content: str) -> list[object]:
    return [content, path, chunk_type, len(content), "", "", know_id, "", "", "", ""]


def _chunks_with_two_embeds_of_one_image() -> list[dict]:
    df = pd.DataFrame(
        [
            _row(
                know_id="img-1",
                path="images/a.jpg",
                chunk_type="image",
                content="a chart",
            ),
            _row(
                know_id="body-1",
                path="doc.md/Intro",
                chunk_type="PTXT",
                content="before [images/a.jpg] middle [images/a.jpg] after",
            ),
        ],
        columns=pd.Index(_COLUMNS),
    )
    return dataframe_to_chunks(df)  # pyright: ignore[reportArgumentType]


def test_embedded_asset_points_back_to_owner_once() -> None:
    chunks = {chunk["chunk_id"]: chunk for chunk in _chunks_with_two_embeds_of_one_image()}

    assert [
        item["relation"] for item in chunks["body-1"]["metadata"]["connect_to"]
    ] == ["embeds", "embeds"]
    assert chunks["img-1"]["metadata"]["connect_to"] == [
        {"target": "body-1", "relation": "embedded_by"}
    ]


def test_zip_image_chunk_carries_the_same_connect_to_as_chunks() -> None:
    chunks = _chunks_with_two_embeds_of_one_image()

    formatted = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={"img-1": {"file_path": "images/a.jpg"}},
        table_files_map={},
    )

    zip_image = next(chunk for chunk in formatted if chunk["type"] == "image")
    chunk_image = next(chunk for chunk in chunks if chunk["type"] == "image")
    assert zip_image["metadata"]["connect_to"] == chunk_image["metadata"]["connect_to"]


def test_iter_connected_target_ids_follows_only_forward_relations() -> None:
    row = {
        "chunk_metadata": {
            "connect_to": [
                {"target": "embedded", "relation": "embeds"},
                {"target": "same-page", "relation": "related"},
                {"target": "unlabeled"},
                {"target": "owner", "relation": "embedded_by"},
                {"target": "other-doc", "relation": "cross_doc_entity"},
                {"target": "cited", "relation": "intra_doc_ref"},
            ]
        }
    }

    assert iter_connected_target_ids(row) == ["embedded", "same-page", "unlabeled"]


def test_mutually_linked_body_and_asset_are_not_both_dropped() -> None:
    rows = [
        {
            "document_id": "doc_a",
            "chunk_id": "body-1",
            "chunk_type": "text",
            "content": "before [images/a.jpg] after",
            "section_path": "Intro",
            "sort_order": 1,
            "chunk_metadata": {
                "connect_to": [
                    {"target": "img-1", "relation": "embeds", "ref": "[images/a.jpg]"}
                ]
            },
        },
        {
            "document_id": "doc_a",
            "chunk_id": "img-1",
            "chunk_type": "image",
            "content": "chart",
            "file_path": "images/a.jpg",
            "section_path": "Intro",
            "sort_order": 2,
            "chunk_metadata": {
                "connect_to": [{"target": "body-1", "relation": "embedded_by"}]
            },
        },
    ]

    assembled = asyncio.run(
        assemble_retrieval_results(
            rows=rows, exclude_document_ids=[], exclude_sections=[]
        )
    )

    assert [row["chunk_id"] for row in assembled] == ["body-1"]


def test_public_retrieval_projection_exposes_importance() -> None:
    response = asyncio.run(
        project_public_retrieval_response(
            {
                "status": "success",
                "results": [
                    {
                        "document_id": "doc_a",
                        "chunk_id": "body-1",
                        "chunk_type": "text",
                        "content": "hello",
                        "score": 0.85,
                        "importance": 0.42,
                        "source_file_name": "a.pdf",
                        "section_path": "a.pdf / Intro",
                    }
                ],
            }
        )
    )

    assert response["results"][0]["importance"] == 0.42


def test_metadata_consumers_prefer_entities_and_computed_tokens() -> None:
    meta = {
        "entities": [{"text": "Knowhere", "type": "product"}],
        "keywords": ["legacy-kw"],
        "tokens": ["legacy-token"],
    }
    assert extract_keywords_from_chunk_metadata(meta) == ["Knowhere"]

    table_chunk = {
        "type": "table",
        "content": "<table><tr><td>Revenue</td></tr></table>",
        "metadata": {
            "summary": "Revenue breakdown",
            "asset_title": "Table 1",
            "entities": [{"text": "ARR", "type": "metric"}],
        },
    }
    table_bm25 = build_content_search_text(table_chunk) or ""
    table_term = build_term_search_text(table_chunk, path_text="Finance") or ""
    assert "arr" in table_bm25.lower()
    assert "table" in table_bm25.lower()
    assert "arr" in table_term.lower()

    text_chunk_no_tokens = {
        "type": "text",
        "content": "Revenue growth accelerated in Q4.",
        "metadata": {},
    }
    lexical = build_content_lexical_text(text_chunk_no_tokens) or ""
    assert "revenue" in lexical.lower()

