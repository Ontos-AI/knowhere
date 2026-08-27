"""Contract: page_memory renders into per-scope paths with page-{n}.png names."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from pathlib import Path

import pytest

import app.services.page_memory.page_renderer as page_renderer


def test_render_document_pages_requires_scope_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scope_id"):
        page_renderer.render_document_pages(
            pdf_path=str(tmp_path / "missing.pdf"),
            page_count=1,
            output_dir=str(tmp_path),
            scope_id="  ",
            pages=[1],
        )


def test_render_document_pages_writes_under_scope_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def _fake_render_pages(ctx, pages, **kwargs):
        captured["folder_name"] = kwargs.get("folder_name")
        captured["prefix"] = kwargs.get("prefix")
        captured["output_dir"] = ctx.output_dir
        folder = Path(ctx.output_dir) / str(kwargs["folder_name"])
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        for page in pages:
            png_path = folder / f"page-{page}.png"
            png_path.write_bytes(b"png")
            results.append({"page": page, "png_path": str(png_path)})
        return results

    monkeypatch.setattr(page_renderer, "render_pages", _fake_render_pages)

    results = page_renderer.render_document_pages(
        pdf_path=str(tmp_path / "doc.pdf"),
        page_count=78,
        output_dir=str(tmp_path),
        scope_id="p58-60",
        pages=[60],
    )

    assert captured["folder_name"] == "pages/p58-60"
    assert captured["prefix"] == ""
    assert len(results) == 1
    assert results[0].image_path.endswith("/pages/p58-60/page-60.png")
    assert Path(results[0].image_path).is_file()
