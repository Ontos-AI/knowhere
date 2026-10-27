from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from PIL import Image
from pytest import MonkeyPatch

from app.services.document_parser.formats.image.parser import parse_image
from app.services.document_parser.formats.image.vlm_input import MAX_VLM_FRAMES
from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess
from shared.core.exceptions.domain_exceptions import ImageParsingException
from shared.services.ai.summary.model import AssetSummary
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks


def _noise_frame(index: int, size: tuple[int, int] = (360, 360)) -> Image.Image:
    raw = bytes((index * 17 + offset) % 256 for offset in range(size[0] * size[1] * 3))
    return Image.frombytes("RGB", size, raw)


def _random_frame(size: tuple[int, int]) -> Image.Image:
    return Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))


def _write_large_gif(path: Path, frame_count: int) -> bytes:
    frames = [_noise_frame(index) for index in range(frame_count)]
    frames[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=40,
        loop=0,
    )
    return path.read_bytes()


def _write_image(path: Path, *, kind: str, frame_count: int = 1) -> bytes:
    frames = [_noise_frame(index) for index in range(frame_count)]
    suffix = path.suffix.lower()
    if kind == "gif":
        frames[0].save(
            path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=40,
            loop=0,
        )
    elif kind == "png":
        _random_frame((420, 420)).save(path, format="PNG")
    elif kind == "jpeg":
        _random_frame((420, 420)).save(path, format="JPEG", quality=90)
    elif kind == "webp":
        kwargs: dict[str, Any] = {"format": "WEBP"}
        if frame_count > 1:
            kwargs.update(save_all=True, append_images=frames[1:], duration=40, loop=0)
        frames[0].save(path, **kwargs)
    elif kind == "bmp":
        frames[0].save(path, format="BMP")
    elif kind == "tiff":
        frames[0].save(
            path,
            format="TIFF",
            save_all=True,
            append_images=frames[1:],
        )
    elif kind == "heic":
        from pillow_heif import register_heif_opener

        register_heif_opener()
        frames[0].save(path, format="HEIF")
    else:
        raise AssertionError(f"unsupported fixture kind: {kind}")
    assert suffix
    return path.read_bytes()


def _mock_image_parse(monkeypatch: MonkeyPatch, image_parser: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_ask_image(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured["ask_paths"] = list(kwargs.get("paths_") or args[2])
        captured["ask_paras"] = kwargs.get("prompt_paras")
        captured["ask_existing"] = [os.path.isfile(path) for path in captured["ask_paths"]]
        return {"answer": "image"}

    def fake_summarize(**kwargs: Any) -> AssetSummary:
        captured["summary_paths"] = list(kwargs.get("image_paths") or [])
        captured["summary_paras"] = kwargs.get("prompt_paras")
        return AssetSummary(title="asset title", summary="asset summary")

    # Patch the live module object that parse_image will resolve against.
    # String-path patches can miss after contract conftest rebinds ``app.*``.
    monkeypatch.setattr(image_parser, "ask_image", fake_ask_image)
    monkeypatch.setattr(image_parser, "summarize", fake_summarize)
    monkeypatch.setattr(
        image_parser,
        "_get_vision_client",
        lambda: object(),
    )
    return captured


def _persist_image(
    *,
    source: Path,
    filename: str,
    output_dir: Path,
    monkeypatch: MonkeyPatch,
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    import app.services.document_parser.formats.image.parser as image_parser

    captured = _mock_image_parse(monkeypatch, image_parser)
    parsed_df = image_parser.parse_image(
        str(source),
        filename=filename,
        output_dir=str(output_dir),
        base_llm_paras={"summary_image": True, "frag_desc": ""},
        relative_root=filename,
    )
    parsed_df = apply_parse_postprocess(str(output_dir), parsed_df)
    chunks = dataframe_to_chunks(parsed_df)
    return captured, parsed_df, chunks


def test_image_parse_persists_original_gif_and_sends_sampled_frames(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "clip.gif"
    original = _write_large_gif(source, 25)
    output_dir = tmp_path / "parser-output"
    captured, parsed_df, chunks = _persist_image(
        source=source,
        filename="clip.gif",
        output_dir=output_dir,
        monkeypatch=monkeypatch,
    )

    assert parsed_df is not None
    image_rows = parsed_df[parsed_df["type"] == "image"]
    assert len(image_rows) == 1
    assert len(chunks) == 1
    assert chunks[0]["type"] == "image"
    assert chunks[0]["metadata"]["file_path"].endswith(".gif")

    stored = list((output_dir / "images").glob("*.gif"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == original
    assert stored[0].name == Path(str(chunks[0]["metadata"]["file_path"])).name
    assert not list((output_dir / "images").glob("*.png"))

    assert captured["ask_existing"]
    assert all(captured["ask_existing"])
    assert len(captured["ask_paths"]) == MAX_VLM_FRAMES
    assert all(path.endswith(".png") for path in captured["ask_paths"])
    assert captured["ask_paras"] == {
        "sequence_kind": "gif",
        "sequence_frames": MAX_VLM_FRAMES,
    }
    assert captured["summary_paths"] == captured["ask_paths"]
    assert captured["summary_paras"] == captured["ask_paras"]
    assert all(not os.path.exists(path) for path in captured["ask_paths"])
    assert str(image_rows.iloc[0]["path"]).endswith(".gif")


@pytest.mark.parametrize(
    ("filename", "kind", "frame_count", "expected_vlm_frames", "expected_paras"),
    [
        ("clip.gif", "gif", 25, MAX_VLM_FRAMES, {"sequence_kind": "gif", "sequence_frames": MAX_VLM_FRAMES}),
        ("short.gif", "gif", 3, 3, {"sequence_kind": "gif", "sequence_frames": 3}),
        ("still.png", "png", 1, 1, {}),
        ("photo.jpg", "jpeg", 1, 1, {}),
        ("still.webp", "webp", 1, 1, {}),
        (
            "anim.webp",
            "webp",
            8,
            8,
            {"sequence_kind": "animated_webp", "sequence_frames": 8},
        ),
        ("scan.bmp", "bmp", 1, 1, {}),
        (
            "pages.tiff",
            "tiff",
            4,
            4,
            {"sequence_kind": "tiff_pages", "sequence_frames": 4},
        ),
        ("phone.heic", "heic", 1, 1, {}),
    ],
)
def test_image_persist_keeps_original_bytes_and_one_chunk(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    filename: str,
    kind: str,
    frame_count: int,
    expected_vlm_frames: int,
    expected_paras: dict[str, object],
) -> None:
    source = tmp_path / filename
    original = _write_image(source, kind=kind, frame_count=frame_count)
    output_dir = tmp_path / "parser-output"
    captured, parsed_df, chunks = _persist_image(
        source=source,
        filename=filename,
        output_dir=output_dir,
        monkeypatch=monkeypatch,
    )

    assert parsed_df is not None
    assert len(parsed_df[parsed_df["type"] == "image"]) == 1
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["type"] == "image"
    stored_name = Path(str(chunk["metadata"]["file_path"])).name
    stored = output_dir / "images" / stored_name
    assert stored.is_file()
    assert stored.read_bytes() == original
    assert stored.suffix.lower() == Path(filename).suffix.lower()
    assert not list((output_dir / "images").glob("*.png")) or Path(filename).suffix.lower() == ".png"

    assert captured["ask_existing"]
    assert all(captured["ask_existing"])
    assert len(captured["ask_paths"]) == expected_vlm_frames
    assert all(path.endswith(".png") for path in captured["ask_paths"])
    assert captured["ask_paras"] == expected_paras
    assert captured["summary_paths"] == captured["ask_paths"]
    assert all(not os.path.exists(path) for path in captured["ask_paths"])


def test_oversized_png_original_is_resized_by_compressor(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "huge.png"
    _random_frame((2501, 220)).save(source, format="PNG")
    original = source.read_bytes()
    output_dir = tmp_path / "parser-output"
    _captured, _parsed_df, chunks = _persist_image(
        source=source,
        filename="huge.png",
        output_dir=output_dir,
        monkeypatch=monkeypatch,
    )
    stored = output_dir / "images" / Path(str(chunks[0]["metadata"]["file_path"])).name
    assert stored.suffix.lower() == ".png"
    assert stored.read_bytes() != original


def test_standalone_tiny_png_still_persists_one_chunk(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "tiny.png"
    Image.new("RGB", (32, 32), color=(10, 20, 30)).save(source, format="PNG")
    original = source.read_bytes()
    output_dir = tmp_path / "parser-output"
    captured, parsed_df, chunks = _persist_image(
        source=source,
        filename="tiny.png",
        output_dir=output_dir,
        monkeypatch=monkeypatch,
    )
    assert parsed_df is not None
    assert len(parsed_df[parsed_df["type"] == "image"]) == 1
    assert len(chunks) == 1
    stored = output_dir / "images" / Path(str(chunks[0]["metadata"]["file_path"])).name
    assert stored.is_file()
    assert stored.read_bytes() == original
    assert len(captured["ask_paths"]) == 1


def test_image_parse_rejects_undecodable_source(tmp_path: Path) -> None:
    source = tmp_path / "broken.heic"
    source.write_bytes(b"x" * 12_000)

    try:
        parse_image(
            str(source),
            filename="broken.heic",
            output_dir=str(tmp_path / "parser-output"),
            base_llm_paras={"summary_image": False, "frag_desc": ""},
            relative_root="broken.heic",
        )
    except ImageParsingException as exc:
        assert exc.details["reason"] == "IMAGE_DECODE_FAILED"
    else:
        raise AssertionError("expected ImageParsingException")
