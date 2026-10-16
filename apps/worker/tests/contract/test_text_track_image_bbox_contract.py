from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from PIL import Image  # noqa: E402

from app.services.document_parser.formats.markdown.layout_bbox_index import (  # noqa: E402
    load_layout_image_regions,
    load_shard_image_regions,
)
from app.services.document_parser.formats.markdown.parser import parse_md  # noqa: E402
from app.services.document_parser.formats.pdf.shard_splitter import (  # noqa: E402
    MergedShard,
    shard_page_numbers,
)
from shared.core.constants.processing import ProcessingConstants  # noqa: E402
from shared.services.chunks.dataframe_chunk_converter import (  # noqa: E402
    dataframe_to_chunks,
)
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder  # noqa: E402

_PAGE_SIZE = [960, 540]


def _figure_block(block_type: str, image_path: str, bbox: list[int]) -> dict[str, Any]:
    body_type = f"{block_type}_body"
    span = {"bbox": bbox, "type": block_type, "image_path": image_path}
    return {
        "type": block_type,
        "bbox": bbox,
        "blocks": [
            {
                "type": body_type,
                "bbox": bbox,
                "lines": [{"bbox": bbox, "spans": [span]}],
            }
        ],
    }


def _text_block(bbox: list[int]) -> dict[str, Any]:
    span = {"bbox": bbox, "type": "text", "content": "body"}
    return {"type": "text", "bbox": bbox, "lines": [{"bbox": bbox, "spans": [span]}]}


def _write_layout(path: Path, pages: list[list[dict[str, Any]]]) -> None:
    layout = {
        "pdf_info": [
            {"page_idx": index, "page_size": _PAGE_SIZE, "para_blocks": blocks}
            for index, blocks in enumerate(pages)
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout), encoding="utf-8")


def _region(page_num: int, bbox: list[int]) -> dict[str, Any]:
    return {
        "page_num": page_num,
        "bbox": bbox,
        "coord_origin": "top_left",
        "coord_system": "mineru_layout",
        "page_width": 960,
        "page_height": 540,
        "crop_margin_px": 0,
    }


def test_layout_index_collects_image_and_chart_but_not_table_or_text(
    tmp_path: Path,
) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        [
            [
                _figure_block("image", "aaa.jpg", [1, 2, 30, 40]),
                _figure_block("chart", "bbb.jpg", [5, 6, 70, 80]),
                _figure_block("table", "ccc.jpg", [9, 9, 99, 99]),
                _text_block([0, 0, 10, 10]),
            ]
        ],
    )

    regions = load_layout_image_regions(str(layout_path))

    assert regions == {
        "aaa.jpg": _region(1, [1, 2, 30, 40]),
        "bbb.jpg": _region(1, [5, 6, 70, 80]),
    }


def test_layout_index_maps_shard_pages_through_the_page_map(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        [
            [_figure_block("image", "p0.jpg", [1, 1, 2, 2])],
            [_figure_block("image", "p1.jpg", [3, 3, 4, 4])],
            [_figure_block("image", "p2.jpg", [5, 5, 6, 6])],
        ],
    )

    # Original page 11 (a TOC page) was removed from this shard PDF.
    regions = load_layout_image_regions(str(layout_path), [10, 12])

    assert regions["p0.jpg"]["page_num"] == 10
    assert regions["p1.jpg"]["page_num"] == 12
    assert "p2.jpg" not in regions


def test_layout_index_is_empty_without_layout_json(tmp_path: Path) -> None:
    assert load_layout_image_regions(str(tmp_path / "layout.json")) == {}


def test_layout_index_keeps_first_region_of_a_repeated_image(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        [
            [_figure_block("image", "same.jpg", [1, 1, 2, 2])],
            [_figure_block("image", "same.jpg", [7, 7, 8, 8])],
        ],
    )

    regions = load_layout_image_regions(str(layout_path))

    assert regions["same.jpg"] == _region(1, [1, 1, 2, 2])


def test_shard_regions_merge_in_shard_order_and_skip_missing_dirs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "shard_0"
    second = tmp_path / "shard_1"
    _write_layout(
        first / "layout.json",
        [[_figure_block("image", "dup.jpg", [1, 1, 2, 2])]],
    )
    _write_layout(
        second / "layout.json",
        [
            [
                _figure_block("image", "dup.jpg", [9, 9, 9, 9]),
                _figure_block("image", "only_second.jpg", [3, 3, 4, 4]),
            ]
        ],
    )

    regions = load_shard_image_regions(
        [str(first), None, str(second)],
        [[1], [2], [20]],
    )

    assert regions["dup.jpg"] == _region(1, [1, 1, 2, 2])
    assert regions["only_second.jpg"] == _region(20, [3, 3, 4, 4])


def test_shard_page_numbers_skip_excluded_pages() -> None:
    shard = MergedShard(shard_index=0, page_start=5, page_end=9)

    assert shard_page_numbers(shard, set()) == [5, 6, 7, 8, 9]
    assert shard_page_numbers(shard, {7, 8, 100}) == [5, 6, 9]


def _write_jpeg(path: Path, *, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width = height = 640
    pixels = bytearray(width * height * 3)
    for index in range(len(pixels)):
        pixels[index] = (index * seed) % 256
    Image.frombytes("RGB", (width, height), bytes(pixels)).save(
        path, format="JPEG", quality=95
    )
    missing = ProcessingConstants.IMG_MIN_SIZE - path.stat().st_size
    if missing > 0:
        with path.open("ab") as handle:
            handle.write(b"\x00" * missing)


def _parse_phase_b(
    output_dir: Path,
    lines: list[str],
    image_regions: dict[str, dict[str, Any]] | None,
):
    return parse_md(
        str(output_dir),
        source_type="md",
        base_llm_paras={
            "summary_image": False,
            "summary_table": False,
            "summary_txt": False,
            "stopwords": [],
        },
        relative_root="deck.pdf",
        lines_with_heading=lines,
        image_regions=image_regions,
    )


def test_parse_md_attaches_asset_region_to_image_chunks_only(tmp_path: Path) -> None:
    hash_name = "a" * 64 + ".jpg"
    _write_jpeg(tmp_path / "images" / hash_name, seed=37)
    lines = [
        "# Results",
        "Revenue grew this quarter.",
        f"![](images/{hash_name})",
        "<table><tr><td>Metric</td><td>Value</td></tr></table>",
    ]
    region = _region(7, [10, 20, 300, 400])

    df = _parse_phase_b(tmp_path, lines, {hash_name: region})
    chunks = dataframe_to_chunks(df)

    by_type = {str(chunk["type"]): chunk for chunk in chunks}
    assert by_type["image"]["metadata"]["asset_region"] == region
    assert "asset_region" not in by_type["table"]["metadata"]
    assert "asset_region" not in by_type["text"]["metadata"]

    image_chunk = by_type["image"]
    zip_chunks = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={
            str(image_chunk["chunk_id"]): {
                "file_path": image_chunk["metadata"]["file_path"]
            }
        },
        table_files_map={
            str(by_type["table"]["chunk_id"]): {
                "file_path": by_type["table"]["metadata"]["file_path"]
            }
        },
    )
    zip_by_type = {str(chunk["type"]): chunk for chunk in zip_chunks}
    assert zip_by_type["image"]["metadata"]["asset_region"] == region
    assert "asset_region" not in zip_by_type["table"]["metadata"]


def test_parse_md_without_image_regions_writes_no_asset_region(tmp_path: Path) -> None:
    hash_name = "b" * 64 + ".jpg"
    _write_jpeg(tmp_path / "images" / hash_name, seed=41)
    lines = ["# Results", f"![](images/{hash_name})"]

    df = _parse_phase_b(tmp_path, lines, None)

    assert list(df["extra_metadata"]) == [""] * len(df)
    assert all("asset_region" not in chunk["metadata"] for chunk in dataframe_to_chunks(df))


def test_parse_md_looks_up_each_duplicate_image_by_its_own_file_name(
    tmp_path: Path,
) -> None:
    first_name = "c" * 64 + ".jpg"
    second_name = "d" * 64 + ".jpg"
    _write_jpeg(tmp_path / "images" / first_name, seed=53)
    # Identical pixels, different file name: the second one takes the duplicate path.
    (tmp_path / "images" / second_name).write_bytes(
        (tmp_path / "images" / first_name).read_bytes()
    )
    lines = [
        "# Results",
        f"![](images/{first_name})",
        f"![](images/{second_name})",
    ]
    first_region = _region(3, [1, 1, 100, 100])
    second_region = _region(4, [2, 2, 200, 200])

    df = _parse_phase_b(
        tmp_path,
        lines,
        {first_name: first_region, second_name: second_region},
    )

    image_rows = df[df["type"] == "image"]
    assert [json.loads(value)["asset_region"] for value in image_rows["extra_metadata"]] == [
        first_region,
        second_region,
    ]
