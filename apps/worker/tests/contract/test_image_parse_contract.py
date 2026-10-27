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

from PIL import Image
from pytest import MonkeyPatch

from app.services.document_parser.formats.image.parser import parse_image
from app.services.document_parser.formats.image.vlm_input import MAX_VLM_FRAMES
from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess
from shared.core.exceptions.domain_exceptions import ImageParsingException
from shared.services.ai.summary.model import AssetSummary


def _write_large_gif(path: Path, frame_count: int) -> bytes:
    frames = [
        Image.new("RGB", (320, 320), color=(index * 9 % 256, 70, 110))
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


def test_image_parse_persists_original_gif_and_sends_sampled_frames(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "clip.gif"
    original = _write_large_gif(source, 25)
    output_dir = tmp_path / "parser-output"
    captured: dict[str, Any] = {}

    def fake_ask_image(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured["ask_paths"] = list(kwargs.get("paths_") or args[2])
        captured["ask_paras"] = kwargs.get("prompt_paras")
        captured["ask_existing"] = [os.path.isfile(path) for path in captured["ask_paths"]]
        return {"answer": "image"}

    def fake_summarize(**kwargs: Any) -> AssetSummary:
        captured["summary_paths"] = list(kwargs.get("image_paths") or [])
        captured["summary_paras"] = kwargs.get("prompt_paras")
        return AssetSummary(title="clip title", summary="clip summary")

    monkeypatch.setattr(
        "app.services.document_parser.formats.image.parser.ask_image",
        fake_ask_image,
    )
    monkeypatch.setattr(
        "app.services.document_parser.formats.image.parser.summarize",
        fake_summarize,
    )

    parsed_df = parse_image(
        str(source),
        filename="clip.gif",
        output_dir=str(output_dir),
        base_llm_paras={"summary_image": True, "frag_desc": ""},
        relative_root="clip.gif",
    )
    parsed_df = apply_parse_postprocess(str(output_dir), parsed_df)

    assert parsed_df is not None
    image_rows = parsed_df[parsed_df["type"] == "image"]
    assert len(image_rows) == 1

    stored = list((output_dir / "images").glob("*.gif"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == original
    assert not list((output_dir / "images").glob("*.png"))

    assert captured["ask_existing"]
    assert all(captured["ask_existing"])
    assert len(captured["ask_paths"]) == MAX_VLM_FRAMES
    assert captured["ask_paras"] == {
        "sequence_kind": "gif",
        "sequence_frames": MAX_VLM_FRAMES,
    }
    assert captured["summary_paths"] == captured["ask_paths"]
    assert captured["summary_paras"] == captured["ask_paras"]
    assert all(not os.path.exists(path) for path in captured["ask_paths"])
    assert str(image_rows.iloc[0]["path"]).endswith(".gif")


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
