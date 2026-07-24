from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.assets.image_size_filter import (  # noqa: E402
    discard_undersized_image_file,
    is_below_img_min_size,
)
from shared.core.constants.processing import ProcessingConstants  # noqa: E402


def test_is_below_img_min_size_boundary() -> None:
    assert is_below_img_min_size(ProcessingConstants.IMG_MIN_SIZE - 1)
    assert not is_below_img_min_size(ProcessingConstants.IMG_MIN_SIZE)
    assert not is_below_img_min_size(ProcessingConstants.IMG_MIN_SIZE + 1)


def test_discard_undersized_image_file(tmp_path: Path) -> None:
    small = tmp_path / "small.jpg"
    small.write_bytes(b"x" * (ProcessingConstants.IMG_MIN_SIZE - 1))
    assert discard_undersized_image_file(small, label="unit") is True
    assert not small.exists()

    large = tmp_path / "large.jpg"
    large.write_bytes(b"y" * ProcessingConstants.IMG_MIN_SIZE)
    assert discard_undersized_image_file(large, label="unit") is False
    assert large.exists()
