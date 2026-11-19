"""Contract tests for the page-image screen that runs before TOC profiling."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pymupdf
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import PageFeature, PageScreen, ToolContext
from app.services.document_agent.page_screen import screen as screen_module
from app.services.document_agent.page_screen.config import GridLayout, PageScreenConfig
from app.services.document_agent.state import ProfileBlackboard

_CONFIG = PageScreenConfig(
    sheet=GridLayout(cols=3, rows=3, thumb_width=40),
    vlm_concurrency=2,
    vlm_max_tokens=100,
)


class _Grids:
    """One fake image per grid, named by the pages it shows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[int]]] = []

    def grids(self, pages: list[int], layout: GridLayout, *, name: str) -> list[str]:
        self.calls.append((name, list(pages)))
        return [
            f"{name}:{pages[i : i + layout.cells]}"
            for i in range(0, len(pages), layout.cells)
        ]


def _ctx(tmp_path: Path, page_count: int) -> ToolContext:
    return ToolContext(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-screen",
        blackboard=ProfileBlackboard(page_count=page_count),
        trace=None,
        output_dir=str(tmp_path),
    )


def test_screen_keeps_typed_pages_shown_on_each_sheet() -> None:
    answers_by_sheet = {
        # 12 is on another sheet and 30 is past the document: both are ignored.
        "screen:[1,": {"toc_pages": [2, 12], "divider_pages": [3, 30]},
        # A page that opens a section and lists its sub-sections is in both lists.
        "screen:[10,": {"toc_pages": [11], "divider_pages": [11, 16, True]},
        "screen:[19,": {"toc_pages": [], "divider_pages": []},
    }
    tasks: list[str] = []

    def vlm(_prompt: str, images: list[str], task: str) -> dict[str, Any]:
        tasks.append(task)
        return next(
            answer
            for key, answer in answers_by_sheet.items()
            if images[0].startswith(key)
        )

    grids = _Grids()

    screen = screen_module.screen_pages(
        page_count=20, grids=grids, config=_CONFIG, vlm=vlm
    )

    assert screen == PageScreen(toc_pages=[2, 11], divider_pages=[3, 11, 16])
    assert grids.calls == [("screen", list(range(1, 21)))]
    assert tasks == ["screen", "screen", "screen"]


def test_run_page_screen_writes_the_screen_to_the_blackboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = PageScreen(toc_pages=[2], divider_pages=[4])
    monkeypatch.setattr(screen_module, "ContactSheets", lambda ctx, **kwargs: None)
    monkeypatch.setattr(screen_module, "vision_call", lambda ctx, **kwargs: None)
    monkeypatch.setattr(screen_module, "screen_pages", lambda **kwargs: screen)
    ctx = _ctx(tmp_path, page_count=5)

    result = screen_module.run_page_screen(ctx)

    assert result.status == "ok"
    assert ctx.blackboard.page_screen == screen
    assert result.payload == {"toc_pages": [2], "divider_pages": [4]}


def test_failed_page_screen_leaves_no_screen_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_screen(**_kwargs: Any) -> PageScreen:
        raise TimeoutError("vlm timed out")

    monkeypatch.setattr(screen_module, "ContactSheets", lambda ctx, **kwargs: None)
    monkeypatch.setattr(screen_module, "vision_call", lambda ctx, **kwargs: None)
    monkeypatch.setattr(screen_module, "screen_pages", failing_screen)
    ctx = _ctx(tmp_path, page_count=5)

    result = screen_module.run_page_screen(ctx)

    assert result.status == "ok"
    assert result.warnings
    assert ctx.blackboard.page_screen is None


def test_contact_sheets_tile_pages_into_numbered_grids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.document_agent.page_screen import contact_sheet as sheet_module

    source = tmp_path / "deck.pdf"
    with pymupdf.open() as document:
        for number in range(1, 6):
            page = document.new_page(width=720, height=405)
            page.insert_text((60, 200), f"Slide {number}", fontsize=40)
        document.save(source)
    rendered: list[tuple[list[int], int, str]] = []

    def render_in_process(
        ctx: ToolContext, pages: list[int], *, folder_name: str, prefix: str, dpi: int
    ) -> list[dict[str, Any]]:
        # Same contract as render_pages, without its child process.
        rendered.append((list(pages), dpi, folder_name))
        out_dir = tmp_path / folder_name
        out_dir.mkdir(exist_ok=True)
        items = []
        with pymupdf.open(ctx.pdf_path) as document:
            for page in pages:
                path = out_dir / f"{prefix}_{page}.png"
                document[page - 1].get_pixmap(dpi=dpi).save(path)
                items.append({"page": page, "png_path": str(path)})
        return items

    monkeypatch.setattr(sheet_module, "render_pages", render_in_process)
    blackboard = ProfileBlackboard(page_count=5)
    # The sheets only read each page's number and width.
    blackboard.page_features = cast(
        list[PageFeature],
        [SimpleNamespace(page=page, width=720.0) for page in range(1, 6)],
    )
    ctx = ToolContext(
        pdf_path=str(source),
        job_id="job-sheets",
        blackboard=blackboard,
        trace=None,
        output_dir=str(tmp_path),
    )

    sheets = sheet_module.ContactSheets(ctx, folder_name="sheets_dir")
    layout = GridLayout(cols=2, rows=2, thumb_width=120)

    images = sheets.grids([1, 2, 3, 4, 5], layout, name="screen")
    sheets.grids([2, 3], layout, name="again")

    assert len(images) == 2
    # Rendered once per width, at the dpi that makes the widest page 120 px,
    # into the folder the caller named.
    assert rendered == [([1, 2, 3, 4, 5], 12, "sheets_dir")]
    saved = sorted(path.name for path in (tmp_path / "sheets_dir").glob("screen_*.png"))
    assert saved == ["screen_01.png", "screen_02.png"]
