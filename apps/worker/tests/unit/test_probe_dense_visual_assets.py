"""Dense PDF pages must skip table/figure extraction instead of hanging."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.document_agent.tools.probe_page_features import (
    _DENSE_DRAWING_LIMIT,
    _DENSE_IMAGE_LIMIT,
    _probe_visual_assets,
    _rect_area,
)


class _FakeRect:
    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.width = x1 - x0
        self.height = y1 - y0


class _FakePage:
    def __init__(
        self,
        *,
        image_count: int = 0,
        drawings_count: int = 0,
    ) -> None:
        self.rect = _FakeRect(0, 0, 600, 800)
        self._images = [(index, 0, 0, 0, 0, 0, 0, 0) for index in range(image_count)]
        self._drawings = [
            {"rect": _FakeRect(10, 10, 20, 20)} for _ in range(drawings_count)
        ]
        self.find_tables_calls = 0
        self.get_drawings_calls = 0

    def get_images(self, full: bool = True) -> list[tuple[int, ...]]:
        return self._images

    def get_image_rects(self, _xref: int) -> list[_FakeRect]:
        return []

    def find_tables(self) -> SimpleNamespace:
        self.find_tables_calls += 1
        return SimpleNamespace(tables=[])

    def get_drawings(self) -> list[dict[str, _FakeRect]]:
        self.get_drawings_calls += 1
        return self._drawings


def test_dense_drawings_skip_tables_and_still_flag_asset() -> None:
    page = _FakePage(drawings_count=_DENSE_DRAWING_LIMIT + 1)
    result = _probe_visual_assets(
        page, _rect_area(page.rect), header_y=None, footer_y=None
    )

    assert page.find_tables_calls == 0
    assert result["drawings_count"] == _DENSE_DRAWING_LIMIT + 1
    assert result["table_count"] == 0
    assert result["has_asset"] is True


def test_dense_images_skip_tables() -> None:
    page = _FakePage(image_count=_DENSE_IMAGE_LIMIT + 1)
    result = _probe_visual_assets(
        page, _rect_area(page.rect), header_y=None, footer_y=None
    )

    assert page.find_tables_calls == 0
    assert result["image_count"] == _DENSE_IMAGE_LIMIT + 1
    assert result["table_count"] == 0
    assert result["has_asset"] is True


def test_normal_page_still_runs_table_finder() -> None:
    page = _FakePage(image_count=2, drawings_count=8)
    result = _probe_visual_assets(
        page, _rect_area(page.rect), header_y=None, footer_y=None
    )

    assert page.find_tables_calls == 1
    assert result["image_count"] == 2
    assert result["drawings_count"] == 8
    assert result["has_asset"] is False
