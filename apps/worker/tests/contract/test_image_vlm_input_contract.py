from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from PIL import Image

from app.services.document_parser.formats.image.vlm_input import (
    MAX_VLM_FRAMES,
    prepare_vlm_image_input,
    sample_frame_indices,
)
from shared.core.exceptions.domain_exceptions import ImageParsingException


def _write_gif(path: Path, frame_count: int, *, size: tuple[int, int] = (64, 64)) -> bytes:
    frames = [
        Image.new("RGB", size, color=(index * 8 % 256, 40, 80))
        for index in range(frame_count)
    ]
    frames[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=40,
        loop=0,
    )
    return path.read_bytes()


def test_sample_frame_indices_keeps_all_frames_at_or_below_cap() -> None:
    assert sample_frame_indices(1) == [0]
    assert sample_frame_indices(MAX_VLM_FRAMES) == list(range(MAX_VLM_FRAMES))


def test_sample_frame_indices_includes_first_and_last_when_over_cap() -> None:
    indexes = sample_frame_indices(25)
    assert len(indexes) == MAX_VLM_FRAMES
    assert indexes[0] == 0
    assert indexes[-1] == 24


def test_prepare_vlm_image_input_samples_gif_frames(tmp_path: Path) -> None:
    source = tmp_path / "anim.gif"
    original = _write_gif(source, 25)
    prepared = prepare_vlm_image_input(source_path=str(source), filename="anim.gif")
    try:
        assert prepared.frame_count == MAX_VLM_FRAMES
        assert prepared.source_kind == "gif"
        assert prepared.prompt_paras() == {
            "sequence_kind": "gif",
            "sequence_frames": MAX_VLM_FRAMES,
        }
        assert len(prepared.working_paths) == MAX_VLM_FRAMES
        assert all(path.endswith(".png") for path in prepared.working_paths)
        assert all(os.path.isfile(path) for path in prepared.working_paths)
        assert source.read_bytes() == original
        work_dir = prepared.work_dir
    finally:
        prepared.cleanup()
    assert not os.path.exists(work_dir)


def test_prepare_vlm_image_input_keeps_static_png_as_one_frame(tmp_path: Path) -> None:
    source = tmp_path / "still.png"
    Image.new("RGB", (32, 32), color=(10, 20, 30)).save(source, "PNG")
    prepared = prepare_vlm_image_input(source_path=str(source), filename="still.png")
    try:
        assert prepared.frame_count == 1
        assert prepared.source_kind == "static"
        assert prepared.prompt_paras() == {}
    finally:
        prepared.cleanup()


def test_prepare_vlm_image_input_rejects_undecodable_file(tmp_path: Path) -> None:
    source = tmp_path / "broken.heic"
    source.write_bytes(b"not-an-image")
    try:
        prepare_vlm_image_input(source_path=str(source), filename="broken.heic")
    except ImageParsingException as exc:
        assert exc.details["reason"] == "IMAGE_DECODE_FAILED"
        assert exc.user_message == "Failed to process the image file"
    else:
        raise AssertionError("expected ImageParsingException")
