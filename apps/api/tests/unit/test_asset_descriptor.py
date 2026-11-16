from __future__ import annotations

from shared.services.chunks.asset_descriptor import (
    PageMedia,
    describe_extracted_asset,
    embedded_media_path,
    normalize_page_media_assets,
    page_media_connections,
    page_media_marker,
)
from shared.services.storage.result_storage import JobResultStorage
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder

_AUDIO = {
    "asset_id": "asset_a",
    "kind": "audio",
    "origin": "embedded",
    "title": None,
    "page_nums": [3],
    "region": None,
    "duration_ms": 3_725_000,
    "ref": {"kind": "artifact", "path": "embedded_media/abc-click.wav"},
}
_LINKED = {
    "asset_id": "asset_l",
    "kind": "video",
    "origin": "linked",
    "title": "Talk",
    "page_nums": [3],
    "region": None,
    "ref": {"kind": "external", "url": "https://example.com/talk.mp4"},
}


def test_media_paths_stay_inside_the_embedded_media_directory() -> None:
    assert embedded_media_path("embedded_media/abc-a.mp4") == "embedded_media/abc-a.mp4"
    assert embedded_media_path("embedded_media/../result.zip") is None
    assert embedded_media_path("images/a.png") is None
    assert embedded_media_path("/embedded_media/a.mp4") is None
    assert embedded_media_path("embedded_media") is None


def test_malformed_descriptors_are_dropped() -> None:
    escaped = {**_AUDIO, "ref": {"kind": "artifact", "path": "embedded_media/../x"}}
    image = {**_AUDIO, "kind": "image"}
    assert normalize_page_media_assets([_AUDIO, escaped, image, "x"]) == [
        {**_AUDIO, "summary": None, "content_type": None, "checksum_sha256": None, "size_bytes": None}
    ]


def test_markers_name_duration_or_link() -> None:
    assert page_media_marker(_AUDIO) == "[audio: 1:02:05 · asset:asset_a]"
    assert page_media_marker(_LINKED) == "[video: Talk · linked · asset:asset_l]"


def test_attaching_is_idempotent_and_keeps_existing_fields() -> None:
    page_media = PageMedia.from_descriptors([_AUDIO, _LINKED], [])
    metadata: dict[str, object] = {}
    content = page_media.attach(content="Slide", metadata=metadata, pages=[3])
    assert content == (
        "Slide\n[audio: 1:02:05 · asset:asset_a]\n[video: Talk · linked · asset:asset_l]"
    )
    assert page_media.attach(content=content, metadata=metadata, pages=[3]) == content

    kept: dict[str, object] = {"assets": []}
    assert page_media.attach(content="Slide", metadata=kept, pages=[3]) == "Slide"
    assert kept == {"assets": []}


def test_extracted_assets_reuse_the_descriptor_shape() -> None:
    descriptor = describe_extracted_asset(
        chunk_id="asset_img",
        chunk_type="image",
        metadata={
            "file_path": "images/chart.png",
            "asset_title": "Revenue",
            "summary": "",
            "page_nums": [4],
            "asset_region": {
                "page_num": 4,
                "bbox": [1, 2, 30, 40],
                "coord_origin": "top_left",
                "coord_system": "page_image_px",
                "page_width": 100,
                "page_height": 200,
                "crop_margin_px": 8,
            },
        },
    )
    assert descriptor == {
        "asset_id": "asset_img",
        "kind": "image",
        "origin": "extracted",
        "title": "Revenue",
        "summary": None,
        "page_nums": [4],
        "region": {
            "page_num": 4,
            "bbox": [1, 2, 30, 40],
            "coord_system": "page_image_px",
            "page_width": 100,
            "page_height": 200,
        },
        "content_type": "image/png",
        "size_bytes": None,
        "checksum_sha256": None,
        "duration_ms": None,
        "ref": {"kind": "artifact", "path": "images/chart.png"},
    }
    assert describe_extracted_asset(chunk_id="x", chunk_type="page", metadata={}) is None


def test_result_package_keeps_page_media_and_describes_tables() -> None:
    chunks = ZipChunkSchemaBuilder().format_chunks(
        [
            {
                "chunk_id": "page-3",
                "type": "page",
                "content": "Slide",
                "path": "deck.pptx/p3",
                "metadata": {"page_nums": [3], "assets": [_AUDIO], "hidden_page_nums": [3]},
            },
            {
                "chunk_id": "table-1",
                "type": "table",
                "content": "<table></table>",
                "path": "tables/t1.html",
                "metadata": {"page_nums": [3], "file_path": "tables/t1.html"},
            },
        ],
        image_files_map={},
        table_files_map={},
    )
    page, table = chunks
    assert page["metadata"]["assets"][0]["asset_id"] == "asset_a"
    assert page["metadata"]["hidden_page_nums"] == [3]
    assert table["metadata"]["asset"]["ref"] == {"kind": "artifact", "path": "tables/t1.html"}
    assert table["metadata"]["asset"]["content_type"] == "text/html"


class _SigningStorage(JobResultStorage):
    def __init__(self) -> None:
        self.signed: list[str] = []

    def build_raw_prefix(self, *, job_id: str) -> str:
        return f"results/{job_id}/"

    def generate_url(self, *, storage_key: str, expires_in: int = 3600) -> str | None:
        self.signed.append(storage_key)
        return f"https://signed.test/{storage_key}"


def test_embedded_media_is_signed_under_the_job_prefix_only() -> None:
    storage = _SigningStorage()
    assert (
        storage.generate_embedded_media_url(job_id="job_1", media_path="embedded_media/a.mp4")
        == "https://signed.test/results/job_1/embedded_media/a.mp4"
    )
    assert storage.generate_embedded_media_url(job_id="job_1", media_path="images/a.png") is None
    assert storage.signed == ["results/job_1/embedded_media/a.mp4"]


def test_document_chunks_sign_embedded_media_and_pass_links_through() -> None:
    from app.services.documents.lifecycle_service import _document_page_media

    storage = _SigningStorage()
    media = _document_page_media(
        metadata={"assets": [_AUDIO, _LINKED]},
        job_id="job_1",
        include_asset_urls=True,
        result_storage=storage,
    )
    assert [asset["url"] for asset in media] == [
        "https://signed.test/results/job_1/embedded_media/abc-click.wav",
        "https://example.com/talk.mp4",
    ]
    unsigned = _document_page_media(
        metadata={"assets": [_AUDIO]},
        job_id="job_1",
        include_asset_urls=False,
        result_storage=storage,
    )
    assert "url" not in unsigned[0]


def test_page_rows_embed_the_media_they_carry() -> None:
    assert page_media_connections({"assets": [_AUDIO, _LINKED, {"kind": "video"}]}) == [
        {"target": "asset_a", "relation": "embeds"},
        {"target": "asset_l", "relation": "embeds"},
    ]
    assert page_media_connections({}) == []


def test_media_chunks_keep_their_descriptor_and_are_counted() -> None:
    builder = ZipChunkSchemaBuilder()
    media_chunks = [
        {
            "chunk_id": "asset_a",
            "type": "audio",
            "content": "[audio: 1:02:05 · asset:asset_a]",
            "path": "embedded_media/asset_a",
            "metadata": {"page_nums": [3], "asset": {**_AUDIO, "unknown": 1}},
        },
        {
            "chunk_id": "asset_l",
            "type": "video",
            "content": "[video: Talk · linked · asset:asset_l]",
            "path": "embedded_media/asset_l",
            "metadata": {"page_nums": [3], "asset": _LINKED},
        },
    ]
    audio, video = builder.format_chunks(media_chunks, image_files_map={}, table_files_map={})
    assert audio["type"] == "audio"
    assert audio["metadata"]["asset"] == normalize_page_media_assets([_AUDIO])[0]
    assert video["type"] == "video"
    assert video["metadata"]["asset"]["ref"] == _LINKED["ref"]
    statistics = builder.calculate_statistics(media_chunks)
    assert (statistics["audio_chunks"], statistics["video_chunks"], statistics["text_chunks"]) == (1, 1, 0)


def test_media_rows_become_root_parked_chunks_embedded_by_their_page() -> None:
    import json

    import pandas as pd

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.chunks.document_path import split_document_path

    chunks = dataframe_to_chunks(
        pd.DataFrame(
            [
                {
                    "know_id": "page-3",
                    "type": "page",
                    "content": "Slide",
                    "path": "deck.pptx/p3",
                    "page_nums": "3",
                    "connectto": json.dumps([{"target": "asset_a", "relation": "embeds"}]),
                    "extra_metadata": {"assets": [_AUDIO]},
                },
                {
                    "know_id": "asset_a",
                    "type": "audio",
                    "content": "[audio: 1:02:05 · asset:asset_a]",
                    "path": "embedded_media/asset_a",
                    "page_nums": "3",
                    "connectto": "",
                    "extra_metadata": {"asset": _AUDIO},
                },
            ]
        )
    )
    page, audio = chunks
    page_metadata, audio_metadata = page["metadata"], audio["metadata"]
    assert isinstance(page_metadata, dict) and isinstance(audio_metadata, dict)
    assert page_metadata["connect_to"] == [{"target": "asset_a", "relation": "embeds"}]
    assert audio["type"] == "audio"
    assert audio_metadata["asset"] == _AUDIO
    assert split_document_path("embedded_media/asset_a", source_file_name="deck.pptx") == (
        ["embedded_media"],
        [],
    )


def test_document_media_chunks_are_signed_from_their_descriptor() -> None:
    from app.services.documents.lifecycle_service import _document_media_chunk_asset

    storage = _SigningStorage()
    signed = _document_media_chunk_asset(
        chunk_type="audio",
        metadata={"asset": _AUDIO},
        job_id="job_1",
        include_asset_urls=True,
        result_storage=storage,
    )
    assert signed is not None
    assert signed["url"] == "https://signed.test/results/job_1/embedded_media/abc-click.wav"
    assert _document_media_chunk_asset(
        chunk_type="image",
        metadata={"asset": _AUDIO},
        job_id="job_1",
        include_asset_urls=True,
        result_storage=storage,
    ) is None
