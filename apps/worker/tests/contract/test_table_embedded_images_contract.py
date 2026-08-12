from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from PIL import Image  # noqa: E402

from app.services.document_parser.formats.markdown.parse_state import (  # noqa: E402
    MarkdownParseState,
)
from app.services.document_parser.formats.markdown.parser import (  # noqa: E402
    update_df_list,
)
from app.services.document_parser.formats.markdown.table_asset import (  # noqa: E402
    MarkdownTableAssetRequest,
    build_markdown_table_asset,
)
from app.services.document_parser.formats.markdown.table_embedded_images import (  # noqa: E402
    extract_table_embedded_images,
)
from app.services.document_parser.orchestration.postprocess import (  # noqa: E402
    cleanup_unreferenced_images,
)
from shared.core.constants.processing import ProcessingConstants  # noqa: E402
from shared.services.chunks.dataframe_chunk_converter import (  # noqa: E402
    dataframe_to_chunks,
)
def _write_jpeg(
    path: Path,
    *,
    size: tuple[int, int] = (640, 640),
    min_bytes: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Solid colors compress too well; patterned noise keeps file size realistic.
    width, height = size
    pixels = bytearray(width * height * 3)
    for index in range(len(pixels)):
        pixels[index] = (index * 37) % 256
    Image.frombytes("RGB", size, bytes(pixels)).save(path, format="JPEG", quality=95)
    if min_bytes is not None:
        current_size = path.stat().st_size
        if current_size < min_bytes:
            with path.open("ab") as handle:
                handle.write(b"\x00" * (min_bytes - current_size))


def _make_parser_state() -> MarkdownParseState:
    return MarkdownParseState(
        relative_root="doc.pdf",
        split_char="/",
        llm_parameters={
            "summary_image": False,
            "summary_table": False,
            "summary_txt": False,
            "stopwords": set(),
        },
        timestamp="2026-01-01 00:00:00",
        row_updater=update_df_list,
    )


def test_table_embedded_images_are_extracted_rewritten_and_linked(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "doc"
    images_dir = output_dir / "images"
    tables_dir = output_dir / "tables"
    images_dir.mkdir(parents=True)
    tables_dir.mkdir(parents=True)

    hash_name = "a" * 64 + ".jpg"
    image_path = images_dir / hash_name
    _write_jpeg(image_path, min_bytes=ProcessingConstants.IMG_MIN_SIZE)
    assert image_path.stat().st_size >= ProcessingConstants.IMG_MIN_SIZE

    table_html = (
        "<table><tr><td>流程名称</td><td>评审流程图</td></tr>"
        f'<tr><td colspan="2"><img src="images/{hash_name}"/></td></tr>'
        "<tr><td>说明</td><td>审批节点</td></tr></table>"
    )

    parser_state = _make_parser_state()

    embedded = extract_table_embedded_images(
        table_html=table_html,
        parser_state=parser_state,
        output_dir=str(output_dir),
        image_dir=str(images_dir),
        summary_image=False,
    )

    assert len(embedded.image_assets) == 1
    assert len(embedded.image_refs) == 1
    assert hash_name not in embedded.rewritten_html
    assert "<img src=" in embedded.rewritten_html
    assert embedded.image_refs[0].startswith("[images/image-1-")

    image_asset = embedded.image_assets[0]
    assert image_asset.relative_path is not None
    assert (output_dir / image_asset.relative_path).exists()
    assert not (images_dir / hash_name).exists()

    for asset in embedded.image_assets:
        assert asset.row_values is not None
        parser_state.append_row(asset.row_values)
    for image_ref in embedded.image_refs:
        parser_state.append_content_item(f"\n{image_ref}\n")

    table_asset = build_markdown_table_asset(
        MarkdownTableAssetRequest(
            table_html=embedded.rewritten_html,
            table_dir=str(tables_dir),
            table_count=1,
            timestamp=parser_state.timestamp,
            summary_table=False,
            row_index=len(parser_state.rows),
            image_refs=embedded.image_refs,
        )
    )
    parser_state.append_content_item(table_asset.content_item)
    parser_state.append_row(table_asset.row_values)
    parser_state.flush_current_content()

    table_row = table_asset.row_values
    assert str(table_row[2]).startswith("table\n[images/")

    saved_html = (tables_dir / Path(table_asset.relative_path).name).read_text(
        encoding="utf-8"
    )
    assert image_asset.relative_path in saved_html
    assert hash_name not in saved_html

    # Extracted image-N files are kept; leftover hash names referenced by HTML
    # would also be protected, but this case has none left.
    leftover_hash = "b" * 64 + ".jpg"
    _write_jpeg(
        images_dir / leftover_hash,
        min_bytes=ProcessingConstants.IMG_MIN_SIZE,
    )
    removed = cleanup_unreferenced_images(str(output_dir))
    assert removed == 1
    assert not (images_dir / leftover_hash).exists()
    assert (output_dir / image_asset.relative_path).exists()

    df = parser_state.to_dataframe()
    chunks = dataframe_to_chunks(df)
    by_type = {str(chunk["type"]): chunk for chunk in chunks}
    assert "image" in by_type
    assert "table" in by_type
    assert "text" in by_type

    image_id = str(by_type["image"]["chunk_id"])
    table_id = str(by_type["table"]["chunk_id"])

    text_connect = by_type["text"]["metadata"].get("connect_to") or []
    text_targets = {item["target"] for item in text_connect if isinstance(item, dict)}
    assert image_id in text_targets
    assert table_id in text_targets
    assert all(
        item.get("relation") == "embeds"
        for item in text_connect
        if isinstance(item, dict) and item.get("target") in {image_id, table_id}
    )

    table_connect = by_type["table"]["metadata"].get("connect_to") or []
    assert any(
        isinstance(item, dict)
        and item.get("relation") == "embeds"
        and item.get("target") == image_id
        for item in table_connect
    )

def test_table_embedded_images_skip_below_img_min_size(tmp_path: Path) -> None:
    output_dir = tmp_path / "doc"
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True)

    hash_name = "c" * 64 + ".jpg"
    image_path = images_dir / hash_name
    _write_jpeg(image_path, size=(16, 16))
    assert image_path.stat().st_size < ProcessingConstants.IMG_MIN_SIZE

    table_html = (
        "<table><tr><td>Logo</td></tr>"
        f'<tr><td><img src="images/{hash_name}"/></td></tr></table>'
    )

    embedded = extract_table_embedded_images(
        table_html=table_html,
        parser_state=_make_parser_state(),
        output_dir=str(output_dir),
        image_dir=str(images_dir),
        summary_image=False,
    )

    assert embedded.image_assets == []
    assert embedded.image_refs == []
    assert "<img" not in embedded.rewritten_html.lower()
    assert not image_path.exists()
