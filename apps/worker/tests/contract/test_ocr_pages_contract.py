"""ocr.pages writes RapidOCR page texts onto the profile blackboard."""

from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import PageFeature, ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_agent.tools.ocr_pages import ocr_pages


def _ctx() -> ToolContext:
    blackboard = AgentBlackboard(page_count=2)
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
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={},
    )


def test_ocr_pages_requires_pages() -> None:
    result = ocr_pages(_ctx(), {})
    assert result.status == "error"
    assert "requires pages" in (result.error or "")


def test_ocr_pages_writes_joined_text_to_blackboard() -> None:
    ctx = _ctx()

    class FakeEngine:
        def __call__(self, _image_path: str):
            return [[[[0, 0], [1, 0], [1, 1], [0, 1]], "Hello", 0.9]], 0.01

    fake_mod = ModuleType("rapidocr_onnxruntime")
    fake_mod.RapidOCR = lambda: FakeEngine()  # type: ignore[attr-defined]

    with (
        patch(
            "app.services.document_agent.tools.ocr_pages.render_pages",
            return_value=[{"page": 1, "png_path": "/tmp/ocr_page_1.png"}],
        ),
        patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_mod}),
    ):
        result = ocr_pages(ctx, {"pages": [1]})

    assert result.status == "ok"
    assert ctx.blackboard.page_full_text_cache[1] == "Hello"
    assert result.payload["page_lines"][1][0]["text"] == "Hello"
