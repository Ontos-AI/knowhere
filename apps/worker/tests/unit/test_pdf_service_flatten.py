"""Unit tests for the local MinerU ZIP flattener.

The cloud MinerU flow returns a flat ZIP layout (``full.md`` + ``images/*``
at the root). Local MinerU returns a nested layout
(``{stem}/auto/{stem}.md`` + ``{stem}/auto/images/*``), which downstream
code cannot consume. ``_flatten_extracted_zip`` rewrites the extracted
tree into the expected flat shape and hard-fails on ambiguous markdown
counts so we never silently pick the wrong file.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.providers.mineru.pdf_service import (  # noqa: E402
    _flatten_extracted_zip,
)
from shared.core.exceptions.domain_exceptions import MinerUServiceException  # noqa: E402


def _write_zip(tree: dict[str, bytes], output_dir: Path) -> None:
    """Extract a synthetic local-MinerU ZIP tree into ``output_dir``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path, content in tree.items():
            archive.writestr(relative_path, content)
    buffer.seek(0)
    with zipfile.ZipFile(buffer, "r") as archive:
        archive.extractall(output_dir)


def test_flatten_single_markdown_and_images(tmp_path: Path) -> None:
    _write_zip(
        {
            "report/auto/report.md": b"# Heading\n\nBody text",
            "report/auto/images/page-1-fig-1.jpg": b"\x00img1",
            "report/auto/images/page-2-fig-2.png": b"\x00img2",
            "report/auto/content_list.json": b"[]",
            "report/auto/middle.json": b"{}",
        },
        tmp_path,
    )

    _flatten_extracted_zip(str(tmp_path))

    assert (tmp_path / "full.md").read_text() == "# Heading\n\nBody text"
    image_names = sorted(p.name for p in (tmp_path / "images").iterdir())
    assert image_names == ["page-1-fig-1.jpg", "page-2-fig-2.png"]
    assert not (tmp_path / "report").exists()
    assert not (tmp_path / "content_list.json").exists()
    assert not (tmp_path / "middle.json").exists()

def test_flatten_raises_when_no_markdown(tmp_path: Path) -> None:
    _write_zip(
        {
            "report/auto/images/page-1-fig-1.jpg": b"\x00img1",
            "report/auto/content_list.json": b"[]",
        },
        tmp_path,
    )

    with pytest.raises(MinerUServiceException, match="no markdown"):
        _flatten_extracted_zip(str(tmp_path))


def test_flatten_raises_when_no_auto_dir(tmp_path: Path) -> None:
    _write_zip(
        {
            "report/report.md": b"# Heading",
            "report/images/page-1-fig-1.jpg": b"\x00img1",
        },
        tmp_path,
    )

    with pytest.raises(MinerUServiceException, match=r"\{stem\}/auto/"):
        _flatten_extracted_zip(str(tmp_path))


def test_flatten_raises_when_multiple_markdown(tmp_path: Path) -> None:
    _write_zip(
        {
            "report/auto/report.md": b"# First",
            "report/auto/second.md": b"# Second",
            "report/auto/images/page-1-fig-1.jpg": b"\x00img1",
        },
        tmp_path,
    )

    with pytest.raises(MinerUServiceException, match="2 markdown"):
        _flatten_extracted_zip(str(tmp_path))


def test_flatten_raises_when_multiple_auto_dirs(tmp_path: Path) -> None:
    _write_zip(
        {
            "report1/auto/report1.md": b"# First",
            "report2/auto/report2.md": b"# Second",
        },
        tmp_path,
    )

    with pytest.raises(MinerUServiceException, match=r"\*/auto directories"):
        _flatten_extracted_zip(str(tmp_path))


def test_flatten_preserves_images_when_only_images_present(tmp_path: Path) -> None:
    _write_zip(
        {
            "report/auto/images/page-1-fig-1.jpg": b"\x00img1",
            "report/auto/images/page-2-fig-2.png": b"\x00img2",
        },
        tmp_path,
    )

    with pytest.raises(MinerUServiceException, match="no markdown"):
        _flatten_extracted_zip(str(tmp_path))

    image_names = sorted(p.name for p in (tmp_path / "images").iterdir())
    assert image_names == ["page-1-fig-1.jpg", "page-2-fig-2.png"]


def test_flatten_excludes_metadata_json(tmp_path: Path) -> None:
    _write_zip(
        {
            "report/auto/report.md": b"# Heading",
            "report/auto/images/page-1-fig-1.jpg": b"\x00img1",
            "report/auto/content_list.json": b"[]",
            "report/auto/middle.json": b"{}",
            "report/auto/model.json": b"{}",
            "report/auto/keep.json": b"{}",
        },
        tmp_path,
    )

    _flatten_extracted_zip(str(tmp_path))

    assert (tmp_path / "full.md").read_text() == "# Heading"
    assert (tmp_path / "keep.json").exists()
    assert not (tmp_path / "content_list.json").exists()
    assert not (tmp_path / "middle.json").exists()
    assert not (tmp_path / "model.json").exists()
    assert not (tmp_path / "report").exists()
