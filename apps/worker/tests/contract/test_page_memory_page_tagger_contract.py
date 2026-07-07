from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import app.services.page_memory.page_tagger as page_tagger
from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import PageTagResult, tag_page_titles


def _render_result(page_index: int, image_path: str) -> PageRenderResult:
    return PageRenderResult(
        page_index=page_index,
        image_path=image_path,
        raw_text="",
        width=612.0,
        height=792.0,
        is_landscape=False,
    )


def test_tag_page_titles_preserves_order_under_concurrency(monkeypatch, tmp_path) -> None:
    """Title detection runs concurrently but results land on the right page's tag."""
    page_count = 8
    pages: list[PageRenderResult] = []
    tag_results: list[PageTagResult] = []
    for page in range(1, page_count + 1):
        img = tmp_path / f"page-{page}.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        pages.append(_render_result(page, str(img)))
        tag_results.append(PageTagResult(page_index=page))

    def _fake_tag_vlm_titles(page: PageRenderResult, *, model: str) -> list[dict]:
        return [{"text": f"Title {page.page_index}", "prominence": 0.9}]

    monkeypatch.setattr(page_tagger, "_tag_vlm_titles", _fake_tag_vlm_titles)

    result = tag_page_titles(
        pages=pages,
        tag_results=tag_results,
        fat_leaf_pages=set(range(1, page_count + 1)),
        budget=None,
        vlm_model="fake-vlm",
        max_concurrent=4,
    )

    for page in range(1, page_count + 1):
        tag = next(t for t in result if t.page_index == page)
        assert tag.observed_titles == [{"text": f"Title {page}", "prominence": 0.9}]


def test_tag_page_titles_skips_pages_without_image(monkeypatch, tmp_path) -> None:
    img = tmp_path / "page-1.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    pages = [
        _render_result(1, str(img)),
        _render_result(2, str(tmp_path / "missing.png")),
    ]
    tag_results = [
        PageTagResult(page_index=1),
        PageTagResult(page_index=2),
    ]

    calls: list[int] = []

    def _fake_tag_vlm_titles(page: PageRenderResult, *, model: str) -> list[dict]:
        calls.append(page.page_index)
        return [{"text": "Title", "prominence": 0.9}]

    monkeypatch.setattr(page_tagger, "_tag_vlm_titles", _fake_tag_vlm_titles)

    result = tag_page_titles(
        pages=pages,
        tag_results=tag_results,
        fat_leaf_pages={1, 2},
        budget=None,
        vlm_model="fake-vlm",
        max_concurrent=4,
    )

    assert calls == [1]
    tag_1 = next(t for t in result if t.page_index == 1)
    tag_2 = next(t for t in result if t.page_index == 2)
    assert tag_1.observed_titles == [{"text": "Title", "prominence": 0.9}]
    assert tag_2.observed_titles == []


def test_tag_page_titles_returns_unchanged_when_no_fat_leaf_pages() -> None:
    tag_results = [PageTagResult(page_index=1)]
    result = tag_page_titles(
        pages=[],
        tag_results=tag_results,
        fat_leaf_pages=set(),
        budget=None,
        vlm_model="fake-vlm",
    )
    assert result is tag_results
