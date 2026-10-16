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

from app.services.document_parser.support.parser_rows import PARSER_ROW_COLUMNS
from app.services.page_memory import memory_service
from app.services.page_memory.page_assets import PageAsset, build_asset_rows
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder


def _page_asset(*, kind: str, asset_index: int, **uris: str) -> PageAsset:
    return PageAsset(
        asset_id=f"asset_{kind}_{asset_index}",
        page_index=5,
        asset_index=asset_index,
        kind=kind,
        bbox_px=[10, 20, 200, 120],
        width_px=1000,
        height_px=1400,
        width_pt=500,
        height_pt=700,
        title=f"{kind} title",
        summary=f"{kind} summary",
        keywords=["k1", "k2"],
        entities=[{"text": "Toolformer", "type": "model"}],
        **uris,
    )


def test_asset_region_and_metadata_match_between_chunks_and_zip() -> None:
    figure = _page_asset(
        kind="figure",
        asset_index=1,
        image_uri="images/image_page_5_figure_1.png",
    )
    table = _page_asset(
        kind="table",
        asset_index=2,
        image_uri="images/image_page_5_table_2.png",
        html_uri="tables/table_page_5_2.html",
    )
    rows = build_asset_rows({5: [figure, table]})
    df = pd.DataFrame(rows, columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))

    chunks = dataframe_to_chunks(df)
    formatted = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={
            "asset_figure_1": {"file_path": "images/image_page_5_figure_1.png"}
        },
        table_files_map={"asset_table_2": {"file_path": "tables/table_page_5_2.html"}},
    )

    chunk_by_id = {chunk["chunk_id"]: chunk["metadata"] for chunk in chunks}
    zip_by_id = {chunk["chunk_id"]: chunk["metadata"] for chunk in formatted}
    expected_region = {
        "page_num": 5,
        "bbox": [10, 20, 200, 120],
        "coord_origin": "top_left",
        "coord_system": "page_image_px",
        "page_width": 1000,
        "page_height": 1400,
        "crop_margin_px": 4,
    }
    for asset_id in ("asset_figure_1", "asset_table_2"):
        chunk_metadata = chunk_by_id[asset_id]
        zip_metadata = zip_by_id[asset_id]
        assert chunk_metadata["asset_region"] == expected_region
        assert "image_uri" not in chunk_metadata
        assert set(zip_metadata) <= set(chunk_metadata)
        for key in (
            "asset_region",
            "entities",
            "asset_title",
            "keywords",
            "summary",
            "page_nums",
            "length",
        ):
            assert zip_metadata[key] == chunk_metadata[key], key
    assert zip_by_id["asset_figure_1"]["original_name"] == (
        chunk_by_id["asset_figure_1"]["original_name"]
    )
    assert zip_by_id["asset_figure_1"]["entities"] == [
        {"text": "Toolformer", "type": "model"}
    ]


def test_zip_chunk_schema_omits_malformed_asset_region() -> None:
    chunks = [
        {
            "chunk_id": "asset_figure_1",
            "type": "image",
            "content": "figure",
            "path": "images/a.png",
            "metadata": {
                "file_path": "images/a.png",
                "asset_region": {"page_num": 5, "bbox": [1, 2, 3]},
            },
        }
    ]

    formatted = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={"asset_figure_1": {"file_path": "images/a.png"}},
        table_files_map={},
    )

    assert "asset_region" not in formatted[0]["metadata"]


def test_zip_text_omits_legacy_tokens_field() -> None:
    chunks = dataframe_to_chunks(
        pd.DataFrame(
            [
                {
                    "content": "body",
                    "path": "demo.pdf/Root",
                    "type": "PTXT",
                    "length": 4,
                    "keywords": "",
                    "summary": "",
                    "know_id": "text-1",
                    "tokens": "",
                    "connectto": "",
                    "addtime": "2026-06-11 00:00:00",
                    "page_nums": "1",
                }
            ]
        )
    )

    formatted = ZipChunkSchemaBuilder().format_chunks(
        chunks, image_files_map={}, table_files_map={}
    )

    assert "tokens" not in chunks[0]["metadata"]
    assert "tokens" not in formatted[0]["metadata"]


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
