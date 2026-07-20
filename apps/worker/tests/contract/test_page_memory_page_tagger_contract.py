from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import PageTagResult, tag_page_titles
from shared.core.exceptions.domain_exceptions import UnavailableException


def _write_page_image(tmp_path, page_index: int) -> str:
    image_path = tmp_path / f"page-{page_index}.png"
    image_path.write_bytes(b"png")
    return str(image_path)


def test_title_detection_preserves_page_index_assignment_under_concurrency(
    monkeypatch,
    tmp_path,
) -> None:
    import gevent

    def _fake_tag_vlm_titles(
        page: PageRenderResult,
        *,
        model: str,
        scan_direction: str = "top_to_bottom_left_to_right",
    ) -> list[dict[str, object]]:
        gevent.sleep(0.01 * (4 - page.page_index))
        return [{"text": f"title-{page.page_index}", "prominence": 0.8}]

    monkeypatch.setitem(
        tag_page_titles.__globals__,
        "_tag_vlm_titles",
        _fake_tag_vlm_titles,
    )
    pages = [
        PageRenderResult(
            page_index=page_index,
            image_path=_write_page_image(tmp_path, page_index),
            raw_text="",
            width=100,
            height=200,
            is_landscape=False,
        )
        for page_index in [1, 2, 3]
    ]
    tag_results = [
        PageTagResult(page_index=3),
        PageTagResult(page_index=1),
        PageTagResult(page_index=2),
    ]

    results = tag_page_titles(
        pages=pages,
        tag_results=tag_results,
        fat_leaf_pages={1, 2, 3},
        vlm_model="fake-vlm",
        max_concurrent=2,
    )

    observed_by_page = {
        tag.page_index: tag.observed_titles[0]["text"]
        for tag in results
    }
    assert observed_by_page == {
        1: "title-1",
        2: "title-2",
        3: "title-3",
    }


def test_title_detection_failed_greenlet_fails_stage(monkeypatch, tmp_path) -> None:
    def _fake_tag_vlm_titles(
        page: PageRenderResult,
        *,
        model: str,
        scan_direction: str = "top_to_bottom_left_to_right",
    ) -> list[dict[str, object]]:
        if page.page_index == 2:
            raise RuntimeError("title detection failed")
        return [{"text": f"title-{page.page_index}"}]

    monkeypatch.setitem(
        tag_page_titles.__globals__,
        "_tag_vlm_titles",
        _fake_tag_vlm_titles,
    )
    pages = [
        PageRenderResult(
            page_index=page_index,
            image_path=_write_page_image(tmp_path, page_index),
            raw_text="",
            width=100,
            height=200,
            is_landscape=False,
        )
        for page_index in [1, 2]
    ]
    tag_results = [PageTagResult(page_index=1), PageTagResult(page_index=2)]

    with pytest.raises(RuntimeError):
        tag_page_titles(
            pages=pages,
            tag_results=tag_results,
            fat_leaf_pages={1, 2},
            vlm_model="fake-vlm",
            max_concurrent=2,
        )


def test_title_detection_unavailable_exception_propagates(
    monkeypatch,
    tmp_path,
) -> None:
    def _fake_tag_vlm_titles(
        page: PageRenderResult,
        *,
        model: str,
        scan_direction: str = "top_to_bottom_left_to_right",
    ) -> list[dict[str, object]]:
        raise UnavailableException(
            internal_message="capacity busy",
            retry_after=5,
        )

    monkeypatch.setitem(
        tag_page_titles.__globals__,
        "_tag_vlm_titles",
        _fake_tag_vlm_titles,
    )
    page = PageRenderResult(
        page_index=1,
        image_path=_write_page_image(tmp_path, 1),
        raw_text="",
        width=100,
        height=200,
        is_landscape=False,
    )

    with pytest.raises(UnavailableException):
        tag_page_titles(
            pages=[page],
            tag_results=[PageTagResult(page_index=1)],
            fat_leaf_pages={1},
            vlm_model="fake-vlm",
            max_concurrent=1,
        )
