import json
import zipfile

import pytest
import pandas as pd
from PIL import Image

from shared.core.exceptions.domain_exceptions import StorageServiceException
from shared.services.redis.chunks_redis_service import ChunksRedisService
from shared.services.storage.zip_result_service import ZipResultService
from shared.utils.chunk_refs import render_legacy_image_chunk_content


def test_dataframe_to_chunks_preserves_embedded_atlas_image_filename():
    df = pd.DataFrame(
        [
            {
                "content": "[images/05SG522 （总说明）page 4.jpg]\n[page-4] 05SG522 （总说明）page 4\n",
                "path": "05SG522 （总说明）page 4",
                "type": "image",
                "length": 42,
                "keywords": "",
                "summary": "",
                "know_id": "atlas-page-4",
                "tokens": "",
                "connectto": "",
                "addtime": "2026-04-13T00:00:00Z",
                "page_nums": "4",
            }
        ]
    )

    chunks = ChunksRedisService(None)._dataframe_to_chunks(df)

    assert chunks[0]["metadata"]["file_path"] == "images/05SG522 （总说明）page 4.jpg"


def test_legacy_image_content_removes_only_line_leading_parser_page_label():
    rendered_content = render_legacy_image_chunk_content(
        (
            "[images/24S913 page 22.jpg]\n"
            "[page-27] 24S913 page 22\n"
            "Keep inline reference [page-27] in OCR text\n"
        ),
        "image-chunk-1",
    )

    assert rendered_content == (
        "IMAGE_image-chunk-1_IMAGE\n"
        "24S913 page 22\n"
        "Keep inline reference [page-27] in OCR text"
    )


def test_dataframe_to_chunks_preserves_actual_extension_for_vector_images():
    df = pd.DataFrame(
        [
            {
                "content": "image-16",
                "path": "table-27-image-16 施工方法及操作要求.emf",
                "type": "image",
                "length": 40,
                "keywords": "",
                "summary": "image-16",
                "know_id": "table-27-image-16",
                "tokens": "",
                "connectto": "",
                "addtime": "2026-04-15T00:00:00Z",
                "page_nums": "",
            }
        ]
    )

    chunks = ChunksRedisService(None)._dataframe_to_chunks(df)

    assert chunks[0]["type"] == "image"
    assert (
        chunks[0]["metadata"]["file_path"]
        == "images/table-27-image-16 施工方法及操作要求.emf"
    )


def test_collect_image_files_uses_metadata_file_path_for_atlas_images(tmp_path, monkeypatch):
    images_dir = tmp_path / "images"
    images_dir.mkdir()

    wrong_path = images_dir / "wrong-first.jpg"
    correct_path = images_dir / "05SG522 （总说明）page 4.jpg"
    Image.new("RGB", (8, 8), color="red").save(wrong_path)
    Image.new("RGB", (8, 8), color="blue").save(correct_path)

    monkeypatch.setattr(
        "os.listdir",
        lambda _: ["wrong-first.jpg", "05SG522 （总说明）page 4.jpg"],
    )

    image_files = ZipResultService()._collect_image_files(
        [
            {
                "chunk_id": "atlas-page-4",
                "type": "image",
                "path": "05SG522 （总说明）page 4",
                "metadata": {
                    "file_path": "images/05SG522 （总说明）page 4.jpg",
                    "original_name": "05SG522 （总说明）page 4.jpg",
                },
            }
        ],
        str(images_dir),
    )

    assert image_files[0]["source_path"] == str(correct_path)


def test_generate_zip_package_preserves_actual_extension_for_vector_images(tmp_path):
    add_dir = tmp_path / "parsed"
    images_dir = add_dir / "images"
    images_dir.mkdir(parents=True)

    vector_filename = "table-27-image-16 施工方法及操作要求.emf"
    (images_dir / vector_filename).write_bytes(b"fake emf bytes")

    df = pd.DataFrame(
        [
            {
                "content": "image-16",
                "path": "table-27-image-16 施工方法及操作要求.emf",
                "type": "image",
                "length": 40,
                "keywords": "",
                "summary": "image-16",
                "know_id": "table-27-image-16",
                "tokens": "",
                "connectto": "",
                "addtime": "2026-04-15T00:00:00Z",
                "page_nums": "",
            }
        ]
    )

    chunks = ChunksRedisService(None)._dataframe_to_chunks(df)

    zip_file_path, _, _, _ = ZipResultService().generate_zip_package(
        job_id="job-vector-image",
        chunks=chunks,
        add_dir=str(add_dir),
        source_file_name="source.docx",
        data_id=None,
        job_metadata={},
        parsed_df=df,
        temp_dir=str(tmp_path),
    )

    with zipfile.ZipFile(zip_file_path) as zip_file:
        zip_image_entries = sorted(
            name for name in zip_file.namelist() if name.startswith("images/")
        )
        chunk_payload = json.loads(zip_file.read("chunks.json"))["chunks"]

    assert zip_image_entries == [f"images/{vector_filename}"]
    assert chunk_payload[0]["content"] == "IMAGE_table-27-image-16_IMAGE"
    assert chunk_payload[0]["metadata"]["file_path"] == f"images/{vector_filename}"


def test_collect_image_files_raises_when_declared_image_is_missing(tmp_path, monkeypatch):
    images_dir = tmp_path / "images"
    images_dir.mkdir()

    wrong_path = images_dir / "wrong-first.jpg"
    Image.new("RGB", (8, 8), color="red").save(wrong_path)

    monkeypatch.setattr("os.listdir", lambda _: ["wrong-first.jpg"])

    with pytest.raises(StorageServiceException, match="Cannot resolve image file"):
        ZipResultService()._collect_image_files(
            [
                {
                    "chunk_id": "atlas-page-4",
                    "type": "image",
                    "path": "05SG522 （总说明）page 4",
                    "metadata": {
                        "file_path": "images/05SG522 （总说明）page 4.jpg",
                        "original_name": "05SG522 （总说明）page 4.jpg",
                    },
                }
            ],
            str(images_dir),
        )


def test_collect_table_files_raises_when_declared_table_is_missing(tmp_path, monkeypatch):
    tables_dir = tmp_path / "tables"
    tables_dir.mkdir()

    wrong_path = tables_dir / "wrong-first.html"
    wrong_path.write_text("<table><tr><td>wrong</td></tr></table>", encoding="utf-8")

    monkeypatch.setattr("os.listdir", lambda _: ["wrong-first.html"])

    with pytest.raises(StorageServiceException, match="Cannot resolve table file"):
        ZipResultService()._collect_table_files(
            [
                {
                    "chunk_id": "table-1",
                    "type": "table",
                    "path": "05SG522 表1",
                    "metadata": {
                        "file_path": "tables/05SG522 表1.html",
                        "original_name": "05SG522 表1.html",
                    },
                }
            ],
            str(tables_dir),
        )
