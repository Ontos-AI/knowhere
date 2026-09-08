"""ocr.pages writes RapidOCR page texts onto the profile blackboard."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import PageFeature, ToolContext
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.tools.ocr_pages import ocr_pages


def _ctx() -> ToolContext:
    blackboard = ProfileBlackboard(page_count=2)
    blackboard.page_features = [
        PageFeature(
            page=1,
            raw_text_length=0,
            text_density=0.0,
            image_coverage=1.0,
            image_count=1,
            table_count=0,
            drawings_count=0,
            orientation="portrait",
            width=72.0,
            height=72.0,
            has_asset=True,
            is_blank_like=True,
        )
    ]
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-ocr",
        blackboard=blackboard,
        trace=None,
        settings={},
    )


def test_ocr_pages_requires_pages() -> None:
    result = ocr_pages(_ctx(), {})
    assert result.status == "error"
    assert "requires pages" in (result.error or "")


def test_ocr_pages_writes_joined_text_to_blackboard() -> None:
    ctx = _ctx()

    def fake_render(*_args, **_kwargs):
        return [{"page": 1, "png_path": "/tmp/ocr_page_1.png"}]

    calls: list[tuple[int, str, int]] = []

    def fake_child(worker_fn, page, image_path, *, timeout):
        assert worker_fn.__name__ == "_run_ocr_worker"
        calls.append((page, image_path, timeout))
        assert page == 1
        assert image_path == "/tmp/ocr_page_1.png"
        assert timeout == 300
        return {
            "ok": True,
            "page": page,
            "lines": [
                {
                    "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "text": "Hello",
                    "score": 0.9,
                }
            ],
        }

    with (
        patch.dict(ocr_pages.__globals__, {"render_pages": fake_render}),
        patch.dict(ocr_pages.__globals__, {"run_in_child_process": fake_child}),
    ):
        result = ocr_pages(ctx, {"pages": [1]})

    assert result.status == "ok"
    bands = ctx.blackboard.page_full_text_cache[1]
    assert getattr(bands, "content", None) == "Hello"
    assert getattr(bands, "header", None) == ""
    assert getattr(bands, "footer", None) == ""
    assert result.payload["page_lines"][1][0]["text"] == "Hello"
    assert calls == [(1, "/tmp/ocr_page_1.png", 300)]


def test_ocr_pages_runs_one_child_per_page() -> None:
    ctx = _ctx()
    calls: list[tuple[int, str]] = []

    def fake_render(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {"page": 1, "png_path": "/tmp/ocr_page_1.png"},
            {"page": 2, "png_path": "/tmp/ocr_page_2.png"},
        ]

    def fake_child(
        worker_fn: object,
        page: int,
        image_path: str,
        *,
        timeout: int,
    ) -> dict[str, object]:
        assert getattr(worker_fn, "__name__", None) == "_run_ocr_worker"
        assert timeout == 300
        calls.append((page, image_path))
        return {"ok": True, "page": page, "lines": []}

    with (
        patch.dict(ocr_pages.__globals__, {"render_pages": fake_render}),
        patch.dict(ocr_pages.__globals__, {"run_in_child_process": fake_child}),
    ):
        result = ocr_pages(ctx, {"pages": [1, 2]})

    assert result.status == "ok"
    assert calls == [
        (1, "/tmp/ocr_page_1.png"),
        (2, "/tmp/ocr_page_2.png"),
    ]
